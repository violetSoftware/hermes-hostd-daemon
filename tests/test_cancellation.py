"""Bounded local process ownership checks; no host/broker/runtime access."""
import asyncio
import json
from unittest.mock import patch

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hermes_hostd.disk_logs import LogStore
from hermes_hostd.executor import CommandExecutor
from hermes_hostd.hostd import _make_handler
from hermes_hostd.models import CommandRequest
from hermes_hostd.approval.base import ApprovalChannel
from hermes_hostd.transport.http import HttpServer
from hermes_hostd.transport.vsock import VsockServer, _VsockProtocol
from hermes_hostd.transport.limits import Admission, ReceiveLimits

FIXTURE = Path(__file__).parent / 'fixtures/job_tree.py'


async def until(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('bounded condition did not become true')
        await asyncio.sleep(.01)


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.executor = CommandExecutor(log_store=LogStore(self.root / 'logs', 32768))
        class Approval:
            async def request_approval(self, request):
                return True
        self.handler = _make_handler(Approval(), self.executor)

    def request(self, mode='ignore', timeout=0):
        return CommandRequest('tree', 'synthetic',
                              [sys.executable, str(FIXTURE), str(self.root), mode], timeout)

    def gone(self):
        return all(not Path('/proc', (self.root / role).read_text()).exists()
                   for role in ('parent', 'child', 'grandchild'))

    async def ready(self):
        await until(lambda: all((self.root / role).exists()
                               for role in ('parent', 'child', 'grandchild')))

    async def test_cancel_reaps_term_resistant_grandchildren(self):
        task = asyncio.ensure_future(self.handler(self.request()))
        await self.ready()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(self.gone(), 'handler retired while owned descendants still exist')
        self.assertFalse(list(self.root.glob('*-orphan')))

    async def test_repeated_cancel_keeps_id_and_does_not_kill_unrelated_job(self):
        import subprocess
        unrelated = subprocess.Popen(['/usr/bin/sleep', '5'])
        try:
            task = asyncio.ensure_future(self.handler(self.request()))
            await self.ready()
            task.cancel()
            await asyncio.sleep(.03)
            task.cancel()
            self.assertFalse(task.done())
            self.assertEqual((await self.handler(self.request())).error, 'duplicate active request id')
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(self.gone())
            self.assertIsNone(unrelated.poll())
            result = await self.handler(CommandRequest('tree', 'reuse', ['/usr/bin/true'], 0))
            self.assertEqual(result.exit_code, 0)
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=2)

    async def test_leader_exit_still_owns_descendants(self):
        task = asyncio.ensure_future(self.handler(self.request(mode='leader-exit')))
        await self.ready()
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(self.gone())

    async def test_startup_cancel_never_leaves_a_late_spawn(self):
        import threading
        from hermes_hostd import executor as module
        entered, release = threading.Event(), threading.Event()
        real = module.subprocess.Popen
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(2)
            return real(*args, **kwargs)
        with patch.object(module.subprocess, 'Popen', side_effect=delayed):
            task = asyncio.ensure_future(self.handler(self.request()))
            await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(.03)
            self.assertFalse(task.done())
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        if (self.root / 'parent').exists():
            for path in self.root.glob('*'):
                if path.name in ('parent', 'child', 'grandchild'):
                    self.assertFalse(Path('/proc', path.read_text()).exists())
        self.assertFalse(list(self.root.glob('*-orphan')))

    async def test_capture_error_retires_owned_tree_before_return(self):
        import selectors
        real = selectors.DefaultSelector.select
        raised = False
        def fail_after_start(selector, timeout=None):
            nonlocal raised
            if not raised and (self.root / 'grandchild').exists():
                raised = True
                raise OSError('synthetic selector failure')
            return real(selector, timeout)
        # Patch only the worker's selector object; event loop selector stays real.
        class Selector(selectors.DefaultSelector):
            def select(self, timeout=None):
                return fail_after_start(self, timeout)
        with patch('hermes_hostd.executor.selectors.DefaultSelector', Selector):
            result = await self.handler(self.request())
        self.assertIn('synthetic selector failure', result.error)
        self.assertTrue(self.gone())
        self.assertFalse(list(self.root.glob('*-orphan')))

    async def test_pre_cancelled_executor_does_not_spawn(self):
        from hermes_hostd.executor import ExecutionCancellation
        cancellation = ExecutionCancellation()
        cancellation.cancel()
        result = await asyncio.to_thread(self.executor.execute, self.request(), cancellation)
        self.assertIn('cancelled before startup', result.error)
        self.assertFalse((self.root / 'parent').exists())

    async def test_deadline_reaps_pipe_inheriting_tree(self):
        started = time.monotonic()
        result = await self.handler(self.request(timeout=1))
        self.assertIn('timed out', result.error)
        self.assertIsNone(result.exit_code)
        self.assertTrue(result.approved)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertTrue(self.gone())
        self.assertFalse(list(self.root.glob('*-orphan')))


class WireTests(CancellationTests):
    TOKEN = 'synthetic-integration-requester'

    async def test_wire_deadline_returns_truthful_timeout_after_reap(self):
        for kind in ('http', 'vsock'):
            for path in self.root.glob('*'):
                if path.is_file():
                    path.unlink()
            server, reader, writer = await self.open_wire(kind, self.handler)
            writer.write(self.frame(kind, self.request(timeout=1)))
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), 2.5)
            if kind == 'http':
                raw = raw.split(b'\r\n\r\n', 1)[1]
            result = json.loads(raw)
            self.assertTrue(result['approved'])
            self.assertIsNone(result['exit_code'])
            self.assertEqual(result['error'], 'command timed out after 1s')
            self.assertTrue(self.gone())
            self.assertFalse(list(self.root.glob('*-orphan')))
            await server.stop()

    async def test_disconnect_running_tree_both_transports_and_intent(self):
        for kind in ('http', 'vsock'):
            with self.subTest(kind=kind):
                for path in self.root.glob('*'):
                    if path.is_file():
                        path.unlink()
                request = self.request(mode='closed')
                request.persistent_intent = True
                server, reader, writer = await self.open_wire(kind, self.handler)
                writer.write(self.frame(kind, request))
                await writer.drain()
                await self.ready()
                writer.close()
                await writer.wait_closed()
                await until(self.gone)
                self.assertFalse(list(self.root.glob('*-orphan')))
                await server.stop()

    async def test_disconnect_approval_ignoring_cancel_never_starts_late_work(self):
        for kind in ('http', 'vsock'):
            entered, retired, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
            class Approval(ApprovalChannel):
                async def request_approval(self, request):
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        retired.set()
                        await release.wait()
                        return True
            handler = _make_handler(Approval(), self.executor, max_pending=1)
            server, reader, writer = await self.open_wire(kind, handler)
            writer.write(self.frame(kind, self.request()))
            await writer.drain()
            await asyncio.wait_for(entered.wait(), 1)
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(retired.wait(), 1)
            duplicate = await handler(self.request())
            self.assertEqual(duplicate.error, 'duplicate active request id')
            other = self.request()
            other.id = 'other'
            self.assertEqual((await handler(other)).error, 'pending approval limit reached')
            release.set()
            await asyncio.sleep(.1)
            self.assertFalse((self.root / 'parent').exists())
            await server.stop()

    async def test_disconnect_incomplete_frame_has_no_approval(self):
        for kind in ('http', 'vsock'):
            entered = asyncio.Event()
            class Approval(ApprovalChannel):
                async def request_approval(self, request):
                    entered.set()
                    return True
            server, reader, writer = await self.open_wire(kind, _make_handler(Approval(), self.executor))
            writer.write(self.frame(kind, self.request())[:10])
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(.05)
            self.assertFalse(entered.is_set())
            self.assertEqual(server.admission.used, 0)
            await server.stop()

    async def open_wire(self, kind, handler):
        if kind == 'http':
            server = HttpServer(port=0, auth_token=self.TOKEN)
            task = asyncio.create_task(server.start(handler))
            await until(lambda: server._listener is not None)
            listener = server._listener
        else:
            server = VsockServer(auth_token=self.TOKEN)
            listener = await asyncio.get_running_loop().create_server(
                lambda: server._protocol(handler), '127.0.0.1', 0)
            server._server = listener
            task = None
        self.addAsyncCleanup(server.stop)
        if task:
            async def close_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.addAsyncCleanup(close_task)
        reader, writer = await asyncio.open_connection('127.0.0.1', listener.sockets[0].getsockname()[1])
        self.addCleanup(writer.close)
        return server, reader, writer

    def frame(self, kind, request):
        raw = request.to_dict()
        if kind == 'vsock':
            raw['auth'] = {'token': self.TOKEN}
        body = json.dumps(raw).encode()
        if kind == 'vsock':
            return body + b'\n'
        return (b'POST /execute HTTP/1.1\r\nHost: local\r\nAuthorization: Bearer ' + self.TOKEN.encode() + b'\r\nContent-Length: ' +
                str(len(body)).encode() + b'\r\n\r\n' + body)

    async def test_vsock_shutdown_joins_running_tree(self):
        server, reader, writer = await self.open_wire('vsock', self.handler)
        writer.write(self.frame('vsock', self.request()))
        await writer.drain()
        await self.ready()
        started = time.monotonic()
        await server.stop()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(self.gone())
        self.assertFalse(list(self.root.glob('*-orphan')))

    async def test_http_shutdown_joins_running_tree(self):
        server, reader, writer = await self.open_wire('http', self.handler)
        writer.write(self.frame('http', self.request()))
        await writer.drain()
        await self.ready()
        started = time.monotonic()
        await server.stop()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(self.gone())
        self.assertFalse(list(self.root.glob('*-orphan')))
