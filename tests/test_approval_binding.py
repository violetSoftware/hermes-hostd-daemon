"""Offline operation-binding tests. No broker, config discovery or host requests."""
import asyncio
import concurrent.futures
import json
import time
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from dataclasses import FrozenInstanceError

from async_fixtures import Session, idle_listener
from hermes_hostd.approval.ntfy import NtfyApprover, _Challenge, _process_sse_event
from hermes_hostd.approval.base import ApprovalChannel
from hermes_hostd.executor import CommandExecutor
from hermes_hostd.hostd import _make_handler
from hermes_hostd.models import CommandRequest, CommandResult


class HandlerBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_execution_uses_immutable_snapshot_including_policy(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = CommandRequest('immutable', 'original', ['original', 'a b'], 7)
        seen = []

        class Approval(ApprovalChannel):
            async def request_approval(self, request):
                seen.append(request)
                entered.set()
                await release.wait()
                return True

        executor = CommandExecutor(['original'])
        handler = _make_handler(Approval(), executor)
        with patch('hermes_hostd.executor._run_streamed') as run:
            run.return_value = (0, {'stdout': 0, 'stderr': 0}, None, False)
            task = asyncio.ensure_future(handler(original))
            await entered.wait()
            original.command[:] = ['mutated']
            original.timeout = 0
            original.id = 'mutated'
            executor._allowlist = frozenset(['mutated'])
            release.set()
            result = await task
        self.assertEqual(result.id, 'immutable')
        self.assertTrue(result.approved)
        self.assertEqual(run.call_args.args[0], ('original', 'a b'))
        self.assertEqual(run.call_args.kwargs['timeout'], 7)
        self.assertEqual(seen[0].allowlist, ('original',))
        self.assertIsNone(seen[0].requester_identity)
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            seen[0].timeout = 0

    async def test_cancelled_execution_keeps_id_active_until_worker_finishes(self):
        import threading
        started, finish = threading.Event(), threading.Event()

        class Approval(ApprovalChannel):
            async def request_approval(self, request):
                return True

        executor = CommandExecutor()
        handler = _make_handler(Approval(), executor)

        def execute(request, cancellation):
            started.set()
            finish.wait(2)
            return CommandResult(request.id, True, 0)

        with patch.object(executor, 'execute', side_effect=execute):
            task = asyncio.ensure_future(handler(CommandRequest('running', 'first', ['first'])))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                await asyncio.sleep(.02)
                self.assertFalse(task.done(), 'handler abandoned worker cleanup')
                duplicate = await handler(CommandRequest('running', 'second', ['second']))
                self.assertFalse(duplicate.approved)
                self.assertEqual(duplicate.error, 'duplicate active request id')
            finally:
                finish.set()
                await asyncio.sleep(.05)

    async def test_duplicate_active_id_is_denied_before_approval(self):
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []

        class Approval(ApprovalChannel):
            async def request_approval(self, request):
                seen.append(request)
                entered.set()
                await release.wait()
                return True

        executor = CommandExecutor()
        handler = _make_handler(Approval(), executor)
        with patch.object(executor, 'execute', side_effect=lambda r, cancellation: CommandResult(r.id, True, 0)) as run:
            first = asyncio.ensure_future(handler(CommandRequest('same', 'first', ['first'])))
            await entered.wait()
            second = asyncio.ensure_future(handler(CommandRequest('same', 'second', ['second'])))
            await asyncio.sleep(0)
            rejected_before_release = second.done()
            release.set()
            results = await asyncio.gather(first, second)
        self.assertTrue(rejected_before_release, 'duplicate was admitted to approval')
        self.assertEqual(len(seen), 1)
        self.assertTrue(results[0].approved)
        self.assertFalse(results[1].approved)
        self.assertEqual(results[1].error, 'duplicate active request id')
        self.assertEqual(run.call_count, 1)


class NtfyBindingTests(unittest.IsolatedAsyncioTestCase):
    def challenge(self, lifetime=60):
        loop = asyncio.get_running_loop()
        return _Challenge('a' * 64, 'b' * 64, 1234567890,
                          time.monotonic() + lifetime, loop, loop.create_future())

    async def test_wrong_malformed_legacy_and_expired_fail_closed(self):
        challenge = self.challenge()
        valid = challenge.body('approved')
        wrong = []
        for field, value in (('version', True), ('challenge', 'wrong'),
                             ('operation', 'wrong'), ('expires_at', True),
                             ('decision', 'yes')):
            wrong.append(json.dumps(dict(valid, **{field: value})))
        wrong.extend(['{', 'null', '[]', '1', '"text"',
                      json.dumps({'id': 'caller', 'decision': 'approved'}),
                      json.dumps(dict(valid, extra='no')),
                      json.dumps({'message': []}),
                      json.dumps({'message': '[]'}),
                      json.dumps({'event': 'keepalive', 'message': json.dumps(valid)})])
        for payload in wrong:
            with self.subTest(payload=payload):
                self.assertFalse(_process_sse_event(payload, challenge))
        expired = self.challenge(-1)
        self.assertFalse(_process_sse_event(json.dumps(expired.body('approved')), expired))
        self.assertFalse(challenge.future.done())
        self.assertTrue(_process_sse_event(json.dumps(valid), challenge))
        self.assertFalse(_process_sse_event(json.dumps(valid), challenge))
        await asyncio.sleep(0)
        self.assertEqual(challenge.future.result(), 'approved')

    async def test_atomic_consumption_under_concurrent_delivery(self):
        challenge = self.challenge()
        payload = json.dumps(challenge.body('approved'))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: _process_sse_event(payload, challenge), range(32)))
        self.assertEqual(results.count(True), 1)
        await asyncio.sleep(0)
        self.assertEqual(challenge.future.result(), 'approved')

    async def test_decision_cannot_release_another_challenge(self):
        first, second = self.challenge(), self.challenge()
        # Distinct actual operation authorities, even if caller IDs are reused.
        from dataclasses import replace
        second = replace(second, nonce='c' * 64, operation_digest='d' * 64)
        payload = json.dumps(first.body('approved'))
        self.assertFalse(_process_sse_event(payload, second))
        self.assertTrue(_process_sse_event(payload, first))
        await asyncio.sleep(0)
        self.assertFalse(second.future.done())
        first.revoke()
        self.assertFalse(_process_sse_event(payload, first))

    async def test_duplicate_json_keys_are_rejected(self):
        challenge = self.challenge()
        payload = json.dumps(challenge.body('approved'))
        duplicate = payload[:-1] + ', "decision":"approved"}'
        self.assertFalse(_process_sse_event(duplicate, challenge))
        self.assertFalse(challenge.future.done())

    async def test_lifecycle_retires_authority_before_id_reuse(self):
        for outcome in ('denied', 'timeout', 'failure', 'cancel'):
            with self.subTest(outcome=outcome):
                approver = NtfyApprover('https://broker.invalid', 'requests', 'responses', timeout=.15)
                published = []
                ready = asyncio.Event()
                loop = asyncio.get_running_loop()

                async def publish(operation, challenge, session):
                    published.append(challenge)
                    loop.call_soon_threadsafe(ready.set)
                    if outcome == 'failure' and len(published) == 1:
                        raise OSError('synthetic publish failure')

                with patch('hermes_hostd.approval.ntfy._sse_listener', side_effect=idle_listener), patch.object(approver, '_publish_notification', side_effect=publish):
                    request = CommandRequest('reused', 'first', ['first'])
                    task = asyncio.create_task(approver.request_approval(request))
                    await asyncio.wait_for(ready.wait(), .5)
                    old = published[0]
                    if outcome != 'failure':
                        self.assertFalse(await approver.request_approval(CommandRequest('reused', 'collision', ['collision'])))
                    if outcome == 'denied':
                        self.assertTrue(_process_sse_event(json.dumps(old.body('denied')), old))
                    elif outcome == 'cancel':
                        task.cancel()
                    result = await asyncio.gather(task, return_exceptions=True)
                    if outcome != 'cancel':
                        self.assertEqual(result, [False])
                    self.assertEqual(approver._active, {})
                    self.assertFalse(_process_sse_event(json.dumps(old.body('approved')), old))
                    ready.clear()
                    next_task = asyncio.create_task(approver.request_approval(CommandRequest('reused', 'next', ['next'])))
                    try:
                        await asyncio.wait_for(ready.wait(), .5)
                        new = published[-1]
                        self.assertNotEqual(old.nonce, new.nonce)
                        self.assertFalse(_process_sse_event(json.dumps(old.body('approved')), new))
                        self.assertTrue(_process_sse_event(json.dumps(new.body('approved')), new))
                        self.assertTrue(await asyncio.wait_for(next_task, .5))
                    finally:
                        next_task.cancel()
                        await asyncio.gather(next_task, return_exceptions=True)

    async def test_one_decision_executes_only_its_operation_end_to_end(self):
        approver = NtfyApprover('https://broker.invalid', 'requests', 'responses')
        executor = CommandExecutor()
        handler = _make_handler(approver, executor)
        published = []
        ready = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def publish(operation, challenge, session):
            published.append((operation, challenge))
            if len(published) == 2:
                loop.call_soon_threadsafe(ready.set)

        def execute(operation, cancellation):
            return CommandResult(operation.id, True, 0, stdout=operation.command[0])

        with patch('hermes_hostd.approval.ntfy._sse_listener', side_effect=idle_listener), patch.object(approver, '_publish_notification', side_effect=publish), patch.object(executor, 'execute', side_effect=execute) as run:
            first = asyncio.ensure_future(handler(CommandRequest('one', 'first', ['first'])))
            second = asyncio.ensure_future(handler(CommandRequest('two', 'second', ['second'])))
            try:
                await asyncio.wait_for(ready.wait(), 1)
                by_id = {op.id: challenge for op, challenge in published}
                payload = json.dumps(by_id['one'].body('approved'))
                self.assertFalse(_process_sse_event(payload, by_id['two']))
                self.assertTrue(_process_sse_event(payload, by_id['one']))
                self.assertEqual((await asyncio.wait_for(first, 1)).stdout, 'first')
                self.assertFalse(second.done())
                self.assertTrue(_process_sse_event(json.dumps(by_id['two'].body('denied')), by_id['two']))
                self.assertFalse((await asyncio.wait_for(second, 1)).approved)
                self.assertEqual(run.call_count, 1)
            finally:
                first.cancel()
                second.cancel()
                await asyncio.gather(first, second, return_exceptions=True)

    async def test_nonpositive_or_nonfinite_timeout_rejected(self):
        for timeout in (0, -1, float('inf'), float('nan')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                NtfyApprover('https://broker.invalid', 'requests', 'responses', timeout)

    async def test_publish_window_expiration_revokes_late_response(self):
        started, finish = asyncio.Event(), asyncio.Event()
        published = []
        approver = NtfyApprover('https://broker.invalid', 'requests', 'responses', timeout=.15)

        async def publish(operation, challenge, session):
            published.append(challenge)
            started.set()
            await finish.wait()

        with patch('hermes_hostd.approval.ntfy._sse_listener', side_effect=idle_listener), patch.object(approver, '_publish_notification', side_effect=publish):
            task = asyncio.create_task(approver.request_approval(CommandRequest('expired', 'late', ['echo'])))
            try:
                self.assertTrue(await asyncio.wait_for(started.wait(), 1))
                self.assertFalse(await asyncio.wait_for(task, 1))
                challenge = published[0]
                self.assertEqual(approver._active, {})
                self.assertFalse(_process_sse_event(json.dumps(challenge.body('approved')), challenge))
            finally:
                finish.set()

    async def test_publish_cancellation_revokes_late_notification(self):
        started, finish = asyncio.Event(), asyncio.Event()
        published = []
        approver = NtfyApprover('https://broker.invalid', 'requests', 'responses')

        async def publish(operation, challenge, session):
            published.append(challenge)
            started.set()
            await finish.wait()

        with patch('hermes_hostd.approval.ntfy._sse_listener', side_effect=idle_listener), patch.object(approver, '_publish_notification', side_effect=publish):
            task = asyncio.create_task(approver.request_approval(CommandRequest('late', 'late', ['echo'])))
            try:
                self.assertTrue(await asyncio.wait_for(started.wait(), 1))
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(approver._active, {})
                challenge = published[0]
                self.assertFalse(_process_sse_event(json.dumps(challenge.body('approved')), challenge))
            finally:
                finish.set()

    async def test_sse_mock_stream_delivers_only_bound_decision(self):
        from hermes_hostd.approval.ntfy import _sse_listener
        from async_fixtures import Session
        challenge = self.challenge()
        wrong = json.dumps({'id': 'caller', 'decision': 'approved'})
        valid = json.dumps({'event': 'message', 'message': json.dumps(challenge.body('denied'))})
        session = Session([f'data: {wrong}\n\ndata: {valid}\n\n'.encode()])
        await _sse_listener('https://broker.invalid', 'responses', challenge, 'synthetic-daemon', session)
        await asyncio.sleep(0)
        self.assertEqual(challenge.future.result(), 'denied')
        self.assertEqual(session.calls[0][2]['headers']['Authorization'], 'Bearer synthetic-daemon')

    async def test_notification_exposes_binding_not_operator_credential(self):
        from hermes_hostd.models import OperationSnapshot
        operation = OperationSnapshot('synthetic', 'line\n\x1b', ('echo', 'a b'), 7, ('echo',))
        challenge = self.challenge()
        approver = NtfyApprover('https://broker.invalid', 'requests', 'responses', auth_token='daemon-only-synthetic')
        session = Session()
        await approver._publish_notification(operation, challenge, session)
        req = session.notification
        content = json.loads(req.data)
        self.assertEqual(content['operation']['command'], ['echo', 'a b'])
        self.assertEqual(content['operation']['timeout'], 7)
        self.assertEqual(content['operation']['allowlist'], ['echo'])
        self.assertIsNone(content['operation']['requester_identity'])
        self.assertNotIn(b'\x1b', req.data)
        actions = req.get_header('Actions')
        self.assertIn(challenge.nonce, actions)
        self.assertIn(challenge.operation_digest, actions)
        self.assertNotIn('daemon-only-synthetic', actions)
        self.assertEqual(req.get_header('Authorization'), 'Bearer daemon-only-synthetic')

    async def test_host_challenge_not_caller_id_authorizes_exact_operation(self):
        import json
        published = []
        ready = asyncio.Event()
        approver = NtfyApprover('https://broker.invalid', 'requests', 'responses')
        loop = asyncio.get_running_loop()

        async def publish(request, challenge, session):
            published.append((request, challenge))
            loop.call_soon_threadsafe(ready.set)

        with patch('hermes_hostd.approval.ntfy._sse_listener', side_effect=idle_listener), patch.object(approver, '_publish_notification', side_effect=publish):
            task = asyncio.create_task(approver.request_approval(CommandRequest('caller', 'first', ['first'])))
            try:
                await asyncio.wait_for(ready.wait(), .5)
                operation, challenge = published[0]
                self.assertEqual(len(challenge.nonce), 64)
                self.assertNotEqual(challenge.nonce, 'caller')
                decision = {'version': 2, 'challenge': challenge.nonce,
                            'operation': challenge.operation_digest,
                            'expires_at': challenge.expires_at, 'decision': 'approved'}
                _process_sse_event(json.dumps({'message': json.dumps(decision)}), challenge)
                self.assertTrue(await asyncio.wait_for(task, .5))
                self.assertEqual(operation.command, ('first',))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
