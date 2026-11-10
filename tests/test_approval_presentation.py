"""Offline display tests: captured strings and inert notification requests only."""
import io
import json
from pathlib import Path
import sys
import unittest
import asyncio
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from async_fixtures import Session, idle_listener
from hermes_hostd.approval.cli import CliApprover
from hermes_hostd.models import CommandRequest, CommandResult


class PresentationTests(unittest.TestCase):
    def banner(self, argv):
        output = io.StringIO()
        CliApprover._print_banner(output, CommandRequest('example', 'example', argv, 7))
        return output.getvalue()

    def test_cli_argv_has_explicit_lossless_json_boundaries(self):
        self.assertNotEqual(self.banner(['echo', 'a b']), self.banner(['echo', 'a', 'b']))
        argv = ['echo', '', 'a b', '"quoted"', '\\path', '\t', '\n']
        banner = self.banner(argv)
        content = json.loads(banner[banner.index('{'):banner.rindex('}') + 1])
        self.assertEqual(content['operation']['command'], argv)

    def test_display_boundaries_reject_without_truncating(self):
        from dataclasses import replace
        from hermes_hostd.approval.presentation import approval_snapshot, render_operation
        from hermes_hostd.models import OperationSnapshot
        base = OperationSnapshot('id', '', ('echo',), 0, None)
        for field in ('id', 'description', 'requester_identity'):
            self.assertIsNotNone(approval_snapshot(replace(base, **{field: 'x' * 1024})))
            with self.assertRaises(ValueError):
                approval_snapshot(replace(base, **{field: 'x' * 1025}))
        approval_snapshot(replace(base, command=('echo',) + ('',) * 63))
        with self.assertRaises(ValueError):
            approval_snapshot(replace(base, command=('echo',) + ('',) * 64))
        with self.assertRaises(ValueError):
            approval_snapshot(replace(base, allowlist=('x',) * 65))
        approval_snapshot(replace(base, description='x' * 1024, command=('e', 'x' * 1021)))
        with self.assertRaises(ValueError):
            approval_snapshot(replace(base, description='x' * 1024, command=('e', 'x' * 1022)))
        # Escaped-byte cap, not raw code-point count, is authoritative.
        last = None
        for length in range(400, 600):
            operation = replace(base, command=('echo', '\u202e' * length))
            try:
                last = approval_snapshot(operation)
            except ValueError:
                self.assertIsNotNone(last)
                assert last is not None
                self.assertEqual(json.loads(render_operation(last))['operation']['command'], list(last.command))
                self.assertEqual(length, len(last.command[1]) + 1)
                break
        else:
            self.fail('escaped byte limit was not enforced')


class ChannelSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_renderers_display_the_snapshot_that_executes_despite_mutation(self):
        from hermes_hostd.approval.ntfy import NtfyApprover, _process_sse_event
        from hermes_hostd.executor import CommandExecutor
        from hermes_hostd.hostd import _make_handler
        from dataclasses import asdict
        import threading
        for channel in (CliApprover(), NtfyApprover('https://broker.invalid', 'requests', 'replies')):
            request = CommandRequest('original', 'description', ['echo', '', 'a b', '\\x', '\"'], 7,
                                     persistent_intent=True)
            executor = CommandExecutor(['echo'])
            handler = _make_handler(channel, executor)
            ready, release = threading.Event(), threading.Event()
            captured = []

            async def output_prompt(operation):
                output = io.StringIO()
                CliApprover._print_banner(output, operation)
                captured.append((operation, output.getvalue()))
                ready.set()
                return await asyncio.to_thread(release.wait, 2)

            def capture_notification(notification, **kwargs):
                captured.append((None, notification.data.decode('ascii')))
                ready.set()
                from unittest.mock import MagicMock
                return MagicMock()

            with patch('hermes_hostd.approval.ntfy._sse_listener', side_effect=idle_listener), \
                    patch.object(CliApprover, '_blocking_prompt', side_effect=output_prompt), \
                    patch('hermes_hostd.approval.ntfy.aiohttp.ClientSession', return_value=Session(on_post=capture_notification)), \
                    patch('hermes_hostd.executor._run_streamed') as run:
                run.return_value = (0, {'stdout': 0, 'stderr': 0}, None, False)
                task = asyncio.ensure_future(handler(request))
                try:
                    self.assertTrue(await asyncio.to_thread(ready.wait, 1))
                    request.id, request.description, request.timeout = 'changed', 'changed', 0
                    request.command[:] = ['changed']
                    request.persistent_intent = False
                    executor._allowlist = frozenset(['changed'])
                    if isinstance(channel, NtfyApprover):
                        challenge = channel._active['original']
                        self.assertTrue(_process_sse_event(json.dumps(challenge.body('approved')), challenge))
                    else:
                        release.set()
                    result = await asyncio.wait_for(task, 1)
                    self.assertTrue(result.approved)
                    self.assertEqual(result.id, 'original')
                    text = captured[0][1]
                    content = json.loads(text[text.index('{'):text.rindex('}') + 1])
                    self.assertEqual(content['operation']['id'], 'original')
                    self.assertEqual(content['operation']['description'], 'description')
                    self.assertEqual(content['operation']['allowlist'], ['echo'])
                    self.assertIs(content['operation']['persistent_intent'], True)
                    self.assertIn('Declared:', content['Persistent process intent'])
                    self.assertEqual(list(run.call_args.args[0]), content['operation']['command'])
                    self.assertEqual(run.call_args.kwargs['timeout'], content['operation']['timeout'])
                    if isinstance(channel, NtfyApprover):
                        import hashlib
                        encoded = json.dumps(content['operation'], sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
                        self.assertEqual(hashlib.sha256(encoded).hexdigest(), content['operation_digest'])
                    else:
                        self.assertEqual(asdict(captured[0][0])['command'], run.call_args.args[0])
                finally:
                    release.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_request_logging_cannot_spoof_a_shared_operator_terminal(self):
        from hermes_hostd.approval.base import ApprovalChannel
        from hermes_hostd.executor import CommandExecutor
        from hermes_hostd.hostd import _make_handler

        class Approval(ApprovalChannel):
            async def request_approval(self, request):
                return True

        hostile = '\x1b[2J\r\nDecision: approved\u202e'
        with self.assertLogs('hermes_hostd', level='INFO') as logs, \
                patch('hermes_hostd.executor._run_streamed') as run:
            run.return_value = (0, {'stdout': 0, 'stderr': 0}, None, False)
            result = await _make_handler(Approval(), CommandExecutor())(
                CommandRequest(hostile, hostile, ['echo', hostile], 7))
        self.assertTrue(result.approved)
        for entry in logs.output:
            self.assertNotIn('\x1b', entry)
            self.assertNotIn('\r', entry)
            self.assertNotIn('\n', entry)
            self.assertNotIn('\u202e', entry)

    async def test_oversized_display_denies_before_any_prompt_notification_or_execution(self):
        from hermes_hostd.approval.ntfy import NtfyApprover
        from hermes_hostd.executor import CommandExecutor
        from hermes_hostd.hostd import _make_handler
        cases = [CommandRequest('x' * 5000, 'test', ['echo']),
                 CommandRequest('test', 'x' * 5000, ['echo']),
                 CommandRequest('test', 'test', ['echo', 'x' * 5000]),
                 CommandRequest('test', 'test', ['echo'] + [''] * 1000),
                 CommandRequest('test', 'test', ['echo', '\u202e' * 1000]),
                 CommandRequest('test', 'test', ['echo'], 10 ** 200)]
        for channel in (CliApprover(), NtfyApprover('https://broker.invalid', 'requests', 'replies', timeout=.01)):
            executor = CommandExecutor()
            handler = _make_handler(channel, executor)
            with patch.object(CliApprover, '_blocking_prompt', return_value=True) as prompt, \
                    patch('hermes_hostd.approval.ntfy._sse_listener', side_effect=idle_listener) as listener, \
                    patch('hermes_hostd.approval.ntfy.aiohttp.ClientSession') as opening, \
                    patch.object(executor, 'execute', return_value=CommandResult('test', True, 0)) as execute:
                for request in cases:
                    with self.subTest(channel=type(channel).__name__, field=len(request.description)):
                        result = await handler(request)
                        self.assertFalse(result.approved)
                prompt.assert_not_called()
                listener.assert_not_called()
                opening.assert_not_called()
                execute.assert_not_called()

    async def test_both_channels_escape_all_fields_and_label_effective_timeout(self):
        from hermes_hostd.approval.ntfy import NtfyApprover, _Challenge
        from hermes_hostd.models import OperationSnapshot
        hostile = ('\x1b[2J\x1b[H\r\nCommand: benign\t\x7f\x85\u202e\u2066\u200b\u00ad\ufeff'
                   + '\u2028\u2029\u034f\ufe0f\U000e0001\ud800'
                   + ''.join(chr(code) for code in range(32)))
        for timeout, label in ((7, '7 seconds'), (0, 'unlimited (0 seconds)')):
            operation = OperationSnapshot(hostile, hostile, ('echo', hostile, ''), timeout,
                                          (hostile,), hostile)
            output = io.StringIO()
            CliApprover._print_banner(output, operation)
            challenge = _Challenge('a' * 64, 'b' * 64, 1900000060,
                                   time.monotonic() + 60, asyncio.get_running_loop(),
                                   asyncio.get_running_loop().create_future())
            session = Session()
            await NtfyApprover('https://broker.invalid', 'requests', 'replies')._publish_notification(operation, challenge, session)
            notification = session.notification
            for display in (output.getvalue(), notification.data.decode('ascii')):
                with self.subTest(timeout=timeout, display=display):
                    self.assertIn('Effective execution timeout', display)
                    self.assertIn('Complete operation - review every argument before approving', display)
                    self.assertIn(label, display)
                    self.assertNotIn('\x7f', display)
                    self.assertTrue(all(c == '\n' or 32 <= ord(c) <= 126 for c in display))
                    content = json.loads(display[display.index('{'):display.rindex('}') + 1])
                    self.assertEqual(content['operation']['id'], hostile)
                    self.assertEqual(content['operation']['description'], hostile)
                    self.assertEqual(content['operation']['command'], ['echo', hostile, ''])
                    self.assertEqual(content['operation']['allowlist'], [hostile])
                    self.assertEqual(content['operation']['requester_identity'], hostile)
                    # No requester string can introduce a second structural label/line.
                    self.assertEqual(display.count('\nCommand: benign'), 0)
            self.assertTrue(notification.data.decode('ascii').endswith('"Display complete": true\n}'))
            self.assertIsNone(notification.get_header('Markdown'))


if __name__ == '__main__':
    unittest.main()
