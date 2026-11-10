"""Bounded inert transport fixtures; no real VSOCK, broker or commands."""
import asyncio
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hermes_hostd.models import CommandResult
from hermes_hostd.transport.vsock import _VsockProtocol


class Transport(asyncio.Transport):
    def __init__(self):
        self.closed = False
        self.output = bytearray()

    def get_extra_info(self, name):
        return None

    def is_closing(self):
        return self.closed

    def write(self, data):
        self.output.extend(data)

    def close(self):
        self.closed = True

    def pause_reading(self):
        pass


class VsockAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_disconnect_cancels_pending_handler(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def handler(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        server = await asyncio.get_running_loop().create_server(lambda: _VsockProtocol(handler, insecure_no_auth=True), '127.0.0.1', 0)
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
            writer.write(b'{"id":"one","description":"inert","command":["echo"]}\n')
            await writer.drain()
            await asyncio.wait_for(entered.wait(), .3)
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(cancelled.wait(), .2)
        finally:
            server.close()
            await server.wait_closed()

    async def test_size_deadline_and_receiving_cap_recover(self):
        from hermes_hostd.transport.limits import ReceiveLimits, Admission
        limits = ReceiveLimits(max_request_bytes=32, receive_timeout=.04, max_receiving=1)
        admission = Admission(1)
        seen = []

        async def handler(request):
            seen.append(request)
            return CommandResult(request.id, False)

        first = _VsockProtocol(handler, limits=limits, admission=admission, insecure_no_auth=True)
        wire = Transport()
        first.connection_made(wire)
        other = _VsockProtocol(handler, limits=limits, admission=admission, insecure_no_auth=True)
        excess = Transport()
        other.connection_made(excess)
        self.assertTrue(excess.closed)
        first.data_received(b'x' * 32)
        self.assertFalse(wire.closed)
        first.data_received(b'x')
        self.assertTrue(wire.closed)
        self.assertEqual(first._buffer, b'')
        self.assertEqual(admission.used, 0)
        slow = _VsockProtocol(handler, limits=limits, admission=admission, insecure_no_auth=True)
        stalled = Transport()
        slow.connection_made(stalled)
        for _ in range(3):
            slow.data_received(b'x')
            await asyncio.sleep(.02)
        self.assertTrue(stalled.closed)
        self.assertEqual(admission.used, 0)
        self.assertEqual(slow._buffer, b'')
        self.assertEqual(seen, [])

    async def test_only_one_handler_for_repeated_frames(self):
        seen = []
        release = asyncio.Event()

        async def handler(request):
            seen.append(request.id)
            await release.wait()
            return CommandResult(request.id, False)

        protocol = _VsockProtocol(handler, insecure_no_auth=True)
        transport = Transport()
        protocol.connection_made(transport)
        frame = json.dumps({'id': 'one', 'description': 'inert', 'command': ['echo']}).encode() + b'\n'
        try:
            protocol.data_received(frame)
            await asyncio.sleep(0)
            protocol.data_received(frame)
            await asyncio.sleep(0)
            self.assertEqual(seen, ['one'])
        finally:
            release.set()
            await asyncio.sleep(.01)
            protocol.connection_lost(None)


class HttpAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_header_body_deadline_size_and_capacity(self):
        from hermes_hostd.transport.http import HttpServer
        from hermes_hostd.transport.limits import ReceiveLimits
        seen = []

        async def handler(request):
            seen.append(request.id)
            return CommandResult(request.id, False)

        server = HttpServer(port=0, insecure_no_auth=True, limits=ReceiveLimits(128, .08, 1))
        task = asyncio.create_task(server.start(handler))
        for _ in range(100):
            if getattr(server, '_listener', None) is not None:
                break
            if task.done():
                await task
            await asyncio.sleep(.005)
        port = server._listener.sockets[0].getsockname()[1]
        writers = []

        async def connect():
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writers.append(writer)
            return reader, writer

        try:
            reader, writer = await connect()
            writer.write(b'POST /execute HTTP/1.1\r\n')
            await writer.drain()
            await asyncio.sleep(.01)
            excess, extra = await connect()
            self.assertEqual(await asyncio.wait_for(excess.read(), .3), b'')
            # Progress never renews the absolute connection deadline.
            for _ in range(3):
                writer.write(b'x')
                await writer.drain()
                await asyncio.sleep(.025)
            self.assertEqual(await asyncio.wait_for(reader.read(), .3), b'')
            self.assertEqual(server.admission.used, 0)
            reader, writer = await connect()
            writer.write(b'POST /execute HTTP/1.1\r\nHost: local\r\nContent-Length: 129\r\n\r\n')
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), .3)
            self.assertIn(b'413', response)
            self.assertEqual(seen, [])
            reader, writer = await connect()
            body = json.dumps({'id': 'http', 'description': 'inert', 'command': ['echo']}).encode()
            body += b' ' * (128 - len(body))
            writer.write(b'POST /execute HTTP/1.1\r\nHost: local\r\nContent-Length: 128\r\n\r\n' + body)
            await writer.drain()
            self.assertIn(b'200 OK', await asyncio.wait_for(reader.read(), .3))
            self.assertEqual(seen, ['http'])
        finally:
            for writer in writers:
                writer.close()
            await server.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_missing_aiohttp_fails_closed(self):
        from unittest.mock import patch
        from hermes_hostd.transport.http import HttpServer
        with patch('hermes_hostd.transport.http._AIOHTTP_AVAILABLE', False):
            with self.assertRaisesRegex(RuntimeError, 'aiohttp'):
                await HttpServer(port=0, insecure_no_auth=True).start(lambda request: None)


class HandlerAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_cap_rejects_before_approval_and_recovers(self):
        from hermes_hostd.hostd import _make_handler
        from hermes_hostd.executor import CommandExecutor
        from hermes_hostd.models import CommandRequest
        from hermes_hostd.approval.base import ApprovalChannel
        entered = asyncio.Event()
        seen = []

        class Approval(ApprovalChannel):
            async def request_approval(self, request):
                seen.append(request.id)
                entered.set()
                await asyncio.Event().wait()

        handler = _make_handler(Approval(), CommandExecutor(), max_pending=1)
        first = asyncio.create_task(handler(CommandRequest('one', 'inert', ['echo'])))
        await entered.wait()
        try:
            rejected = await asyncio.wait_for(handler(CommandRequest('two', 'inert', ['echo'])), .2)
            self.assertFalse(rejected.approved)
            self.assertEqual(rejected.error, 'pending approval limit reached')
            self.assertEqual(seen, ['one'])
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        entered.clear()
        recovered = asyncio.create_task(handler(CommandRequest('three', 'inert', ['echo'])))
        try:
            await asyncio.wait_for(entered.wait(), .2)
            self.assertEqual(seen, ['one', 'three'])
        finally:
            recovered.cancel()
            await asyncio.gather(recovered, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
