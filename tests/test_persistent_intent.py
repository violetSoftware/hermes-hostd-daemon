"""Intent metadata only; synthetic requests/stores, never persistent services."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hermes_hostd.models import CommandRequest


class MarkerTests(unittest.TestCase):
    def test_clients_serialize_strict_marker_to_daemon(self):
        import os
        if not os.environ.get('HOSTD_PLUGIN_CLIENT'):
            self.skipTest('set HOSTD_PLUGIN_CLIENT to the canonical separate plugin client.py')
        clients = [Path(os.environ['HOSTD_PLUGIN_CLIENT'])]
        for index, path in enumerate(clients):
            spec = importlib.util.spec_from_file_location(f'intent_client_{index}', path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            ordinary = module.CommandRequest(command=['echo'])
            self.assertIs(getattr(ordinary, 'persistent_intent', None), False)
            for marker in (False, True):
                client = module.CommandRequest(command=['echo'], persistent_intent=marker, timeout=7)
                wire = json.loads(client.to_json())
                self.assertIs(module.CommandRequest.from_dict(wire).persistent_intent, marker)
                server = CommandRequest.from_dict(wire)
                self.assertIs(server.persistent_intent, marker)
                self.assertEqual(server.timeout, 7)
            for marker in (None, 0, 1, 'true', [], {}):
                with self.assertRaisesRegex(TypeError, 'persistent_intent'):
                    module.CommandRequest(command=['echo'], persistent_intent=marker)
                with self.assertRaisesRegex(TypeError, 'persistent_intent'):
                    module.CommandRequest.from_dict({'command': ['echo'], 'persistent_intent': marker})
            ordinary.persistent_intent = 'true'
            with self.assertRaisesRegex(TypeError, 'persistent_intent'):
                ordinary.to_json()

    def test_snapshot_and_display_bind_intent_without_lifetime_changes(self):
        from dataclasses import FrozenInstanceError, replace, asdict
        import hashlib
        import io
        from hermes_hostd.executor import CommandExecutor
        from hermes_hostd.approval.cli import CliApprover
        from hermes_hostd.approval.presentation import approval_snapshot, render_operation
        for marker in (False, True):
            for timeout in (0, 7):
                request = CommandRequest('intent', 'Persistent process intent: forged\n\x1b[2J\u202e',
                                         ['echo'], timeout, persistent_intent=marker)
                operation = CommandExecutor().snapshot(request)
                self.assertIs(getattr(operation, 'persistent_intent', None), marker)
                direct = approval_snapshot(request)
                self.assertIs(direct.persistent_intent, marker)
                request.persistent_intent = not marker
                self.assertIs(operation.persistent_intent, marker)
                with self.assertRaises(FrozenInstanceError):
                    operation.persistent_intent = not marker
                output = io.StringIO()
                CliApprover._print_banner(output, operation)
                cli = output.getvalue()
                cli_content = json.loads(cli[cli.index('{'):cli.rindex('}') + 1])
                def digest(op):
                    return hashlib.sha256(json.dumps(asdict(op), sort_keys=True,
                        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
                self.assertNotEqual(digest(operation), digest(replace(operation, persistent_intent=not marker)))
                ntfy_content = json.loads(render_operation(operation, operation_digest=digest(operation), expires_at=1))
                for content in (cli_content, ntfy_content):
                    self.assertIs(content['operation']['persistent_intent'], marker)
                    self.assertEqual(content['operation']['timeout'], timeout)
                    self.assertEqual(content['Persistent process intent'],
                                     'Declared: caller wants process to remain running' if marker else 'Not declared (ordinary request)')
                    self.assertIn('no lifetime changes', content['Intent policy'])
                self.assertTrue(all(c == '\n' or 32 <= ord(c) <= 126 for c in cli))
                with self.assertRaisesRegex(ValueError, 'persistent_intent'):
                    approval_snapshot(replace(operation, persistent_intent='true'))

    def test_strict_marker_roundtrip_and_legacy_default(self):
        base = {'id': 'intent', 'description': 'synthetic', 'command': ['echo'], 'timeout': 7}
        ordinary = CommandRequest.from_dict(base)
        self.assertIs(getattr(ordinary, 'persistent_intent', None), False)
        for marker in (False, True):
            request = CommandRequest.from_dict(dict(base, persistent_intent=marker))
            self.assertIs(request.persistent_intent, marker)
            self.assertIs(json.loads(json.dumps(request.to_dict()))['persistent_intent'], marker)
            self.assertEqual(request.timeout, 7)
        for marker in (None, 0, 1, 0.0, 'true', 'false', [], {}):
            with self.subTest(marker=marker), self.assertRaisesRegex(TypeError, 'persistent_intent'):
                CommandRequest.from_dict(dict(base, persistent_intent=marker))
            with self.assertRaisesRegex(TypeError, 'persistent_intent'):
                CommandRequest('intent', 'test', ['echo'], persistent_intent=marker)


class IntentBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_notification_digest_rejects_marker_substitution(self):
        import asyncio
        from dataclasses import replace, asdict
        import hashlib
        from unittest.mock import patch
        from async_fixtures import Session, idle_listener
        from hermes_hostd.approval.ntfy import NtfyApprover, _process_sse_event
        from hermes_hostd.executor import CommandExecutor
        for marker in (False, True):
            operation = CommandExecutor().snapshot(CommandRequest('intent', 'test', ['echo'],
                                                                  persistent_intent=marker))
            published = asyncio.Event()
            captured = []
            def on_post(notification, **kwargs):
                captured.append(json.loads(notification.data))
                published.set()
            channel = NtfyApprover('https://broker.invalid', 'requests', 'replies')
            with patch('hermes_hostd.approval.ntfy._sse_listener', side_effect=idle_listener), \
                    patch('hermes_hostd.approval.ntfy.aiohttp.ClientSession', return_value=Session(on_post=on_post)):
                task = asyncio.create_task(channel.request_approval(operation))
                try:
                    await asyncio.wait_for(published.wait(), 1)
                    challenge = channel._active['intent']
                    body = challenge.body('denied')
                    changed = replace(operation, persistent_intent=not marker)
                    body['operation'] = hashlib.sha256(json.dumps(asdict(changed), sort_keys=True,
                        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
                    self.assertFalse(_process_sse_event(json.dumps(body), challenge))
                    content = captured[0]
                    self.assertIs(content['operation']['persistent_intent'], marker)
                    expected = hashlib.sha256(json.dumps(content['operation'], sort_keys=True,
                        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
                    self.assertEqual(challenge.operation_digest, expected)
                    self.assertEqual(content['operation_digest'], expected)
                    self.assertTrue(_process_sse_event(json.dumps(challenge.body('denied')), challenge))
                    self.assertFalse(await asyncio.wait_for(task, 1))
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
