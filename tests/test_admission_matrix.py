"""Bounded local admission acceptance matrix. No real approval or command."""
import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hermes_hostd.transport.http import HttpServer
from hermes_hostd.transport.limits import ReceiveLimits, Admission
from hermes_hostd.transport.vsock import _VsockProtocol
from hermes_hostd.hostd import _build_transport, _make_handler
from hermes_hostd.executor import CommandExecutor
from hermes_hostd.models import CommandRequest, CommandResult
from hermes_hostd.approval.ntfy import _sse_listener
from async_fixtures import Session
from test_admission import Transport


class Matrix(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []
        self.entered, self.cancelled = asyncio.Event(), asyncio.Event()
        self.block = False
        self.server = HttpServer(port=0, auth_token='synthetic', limits=ReceiveLimits(128, .12, 1))
        async def handler(request):
            self.seen.append(request.id)
            self.entered.set()
            if self.block:
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled.set()
            return CommandResult(request.id, False)
        self.task = asyncio.create_task(self.server.start(handler))
        for _ in range(100):
            if self.server._listener:
                break
            if self.task.done():
                await self.task
            await asyncio.sleep(.005)
        self.port = self.server._listener.sockets[0].getsockname()[1]
        self.writers = []

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
        await self.server.stop()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def connect(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        self.writers.append(writer)
        return reader, writer

    def frame(self, body=None, headers=b'Authorization: Bearer synthetic\r\n'):
        body = body or json.dumps({'id':'one', 'description':'inert', 'command':['echo']}).encode()
        return b'POST /execute HTTP/1.1\r\nHost: local\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n' + headers + b'\r\n' + body

    async def request(self, frame):
        reader, writer = await self.connect()
        writer.write(frame)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), .5)

    async def test_http_disconnect_cancels_pending_approval(self):
        self.block = True
        reader, writer = await self.connect()
        writer.write(self.frame())
        await writer.drain()
        await asyncio.wait_for(self.entered.wait(), .3)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(self.cancelled.wait(), .3)
        self.assertFalse(self.server._connections)

    async def test_http_body_trickle_and_disconnect_recover(self):
        reader, writer = await self.connect()
        writer.write(b'POST /execute HTTP/1.1\r\nAuthorization: Bearer synthetic\r\nContent-Length: 128\r\n\r\n')
        for _ in range(4):
            writer.write(b'x'); await writer.drain(); await asyncio.sleep(.035)
        self.assertEqual(await asyncio.wait_for(reader.read(), .3), b'')
        self.assertEqual(self.server.admission.used, 0)
        reader, writer = await self.connect()
        writer.write(b'POST /'); await writer.drain()
        writer.close(); await writer.wait_closed(); await asyncio.sleep(.02)
        self.assertEqual(self.server.admission.used, 0)
        self.assertIn(b'200 OK', await self.request(self.frame()))
        self.assertEqual(self.seen, ['one'])

    async def test_http_early_rejections_have_no_approval_and_recover(self):
        cases = [(self.frame(headers=b''), b'401'),
                 (self.frame(body=b'{'), b'400'),
                 (self.frame(body=b'[]'), b'400'),
                 (self.frame(headers=b'Content-Length: 1\r\n'), b'400'),
                 (self.frame(headers=b'Transfer-Encoding: chunked\r\n'), b'400'),
                 (self.frame(headers=b'Expect: 100-continue\r\n'), b'400'),
                 (self.frame(headers=b'Content-Encoding: gzip\r\n'), b'400'),
                 (b'POST / HTTP/1.1\r\nX: '+ b'x'*16384, b'431'),
                 (b'POST / HTTP/1.1\r\nContent-Length: 129\r\n\r\n', b'413')]
        for frame, status in cases:
            with self.subTest(status=status):
                self.assertIn(status, await self.request(frame))
                self.assertEqual(self.seen, [])
                self.assertEqual(self.server.admission.used, 0)
        self.assertIn(b'200 OK', await self.request(self.frame()))

    async def test_http_fragmented_boundary_and_pipeline_only_one(self):
        body = json.dumps({'id':'one', 'description':'inert', 'command':['echo']}).encode().ljust(128)
        reader, writer = await self.connect()
        frame = self.frame(body)
        for part in (frame[:5], frame[5:43], frame[43:-7], frame[-7:] + self.frame()):
            writer.write(part); await writer.drain(); await asyncio.sleep(.005)
        self.assertIn(b'200 OK', await asyncio.wait_for(reader.read(), .5))
        self.assertEqual(self.seen, ['one'])


class OtherBounds(unittest.IsolatedAsyncioTestCase):
    async def test_vsock_boundary_malformed_error_and_disconnect_recovery(self):
        seen = []
        async def handler(request):
            seen.append(request.id)
            raise OSError('synthetic')
        body = b'{"id":"one","description":"inert","command":["echo"]}'
        admission = Admission(1)
        for payload in (body, b'[]', b'\xff', b'{'):
            wire = Transport()
            protocol = _VsockProtocol(handler, ReceiveLimits(len(body), .1, 1), admission, insecure_no_auth=True)
            protocol.connection_made(wire)
            protocol.data_received(payload[:3]); protocol.data_received(payload[3:] + b'\n' + body + b'\n')
            await asyncio.sleep(.01)
            self.assertTrue(wire.closed)
            self.assertEqual(admission.used, 0)
            self.assertFalse(json.loads(wire.output)['approved'])
            protocol.connection_lost(None)
        self.assertEqual(seen, ['one'])
        protocol = _VsockProtocol(handler, admission=admission, insecure_no_auth=True)
        protocol.connection_made(Transport()); protocol.data_received(b'x'); protocol.connection_lost(OSError('inert'))
        self.assertEqual(admission.used, 0)
        self.assertEqual(protocol._buffer, b'')

    async def test_pending_slot_held_through_repeated_cancellation_cleanup(self):
        started, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Approval:
            async def request_approval(self, request):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup.set()
                    await release.wait()
        handler = _make_handler(Approval(), CommandExecutor(), max_pending=1)
        task = asyncio.create_task(handler(CommandRequest('one','inert',['echo'])))
        await started.wait(); task.cancel(); await cleanup.wait(); task.cancel(); await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual((await handler(CommandRequest('two','inert',['echo']))).error, 'pending approval limit reached')
        release.set(); await asyncio.gather(task, return_exceptions=True)
        next_task = asyncio.create_task(handler(CommandRequest('next','inert',['echo'])))
        await asyncio.sleep(.01); next_task.cancel(); await asyncio.gather(next_task, return_exceptions=True)

    async def test_sse_line_event_bounds_and_invalid_utf8_fail_closed(self):
        from hermes_hostd.approval.ntfy import _Challenge
        import time
        for chunks in ([b'x'*8193], [b'data: '+b'x'*8000+b'\n']*3, [b'data: \xff\n\n']):
            challenge = _Challenge('a'*64, 'b'*64, 1900000060, time.monotonic()+1,
                                   asyncio.get_running_loop(), asyncio.get_running_loop().create_future())
            with self.assertRaises((ValueError, UnicodeError)):
                await _sse_listener('http://inert.invalid', 'replies', challenge, None, Session(chunks))
            self.assertFalse(challenge.future.done())

    def test_config_limits_and_invalid_values(self):
        for transport in ('http', 'vsock'):
            server = _build_transport({'transport':dict(type=transport, insecure_no_auth=True, max_request_bytes=123,
                                                       receive_timeout=.2, max_receiving=2)})
            self.assertEqual(server.limits, ReceiveLimits(123,.2,2))
        for values in ({'max_request_bytes':0}, {'max_receiving':True}, {'receive_timeout':float('inf')}):
            with self.assertRaises(ValueError):
                _build_transport({'transport':values})
        with self.assertRaises(ValueError):
            _make_handler(None, None, max_pending=0)
