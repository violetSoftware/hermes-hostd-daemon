"""Real local brokers, synthetic secrets, no command execution."""
import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hermes_hostd.approval.ntfy import NtfyApprover
from hermes_hostd.hostd import _make_handler
from hermes_hostd.executor import CommandExecutor
from hermes_hostd.models import CommandRequest, CommandResult


class ApprovalRedirects(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []
        self.peers, self.tasks = set(), set()
        self.mode, self.status, self.location = 'direct', 200, None
        self.challenges = []
        self.channel = None
        self.servers = [await asyncio.start_server(self.broker, '127.0.0.1', 0) for _ in range(2)]
        self.urls = [f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}' for server in self.servers]

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        for writer in tuple(self.peers):
            writer.close()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)

    async def broker(self, reader, writer):
        self.peers.add(writer)
        self.tasks.add(asyncio.current_task())
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            lines = head.decode('ascii').split('\r\n')
            method, path, _ = lines[0].split()
            headers = dict(line.split(': ', 1) for line in lines[1:] if ': ' in line)
            body = await reader.readexactly(int(headers.get('Content-Length', 0)))
            self.seen.append((writer.get_extra_info('sockname')[1], method, path, headers, body))
            challenge = next(iter(self.channel._active.values()))
            if challenge not in self.challenges:
                self.challenges.append(challenge)
            redirect = ((self.mode == 'publication' and path == '/requests') or
                        (self.mode == 'subscription' and path == '/replies/sse'))
            if redirect:
                location = b'' if self.location is None else f'Location: {self.location}\r\n'.encode()
                payload = b'' if self.status == 304 else b'untrusted-response-secret'
                writer.write(f'HTTP/1.1 {self.status} Redirect\r\n'.encode() + location +
                             f'Content-Length: {len(payload)}\r\n\r\n'.encode() + payload)
            elif method == 'POST':
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')
            else:
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n')
                if self.mode != 'publication':
                    # Even a redirect destination capable of approving must never be reached.
                    writer.write(b'data: ' + json.dumps(challenge.body('approved')).encode() + b'\n\n')
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            self.peers.discard(writer)
            self.tasks.discard(asyncio.current_task())

    async def settle(self):
        for _ in range(100):
            if not self.peers:
                break
            await asyncio.sleep(.005)
        self.assertFalse(self.peers, 'broker connections leaked')
        self.assertFalse(self.channel._active, 'approval authority leaked')
        for challenge in self.challenges:
            self.assertTrue(challenge._closed.is_set())
            self.assertFalse(challenge.consume(challenge.body('approved')), 'late approval accepted')
        self.assertFalse([task for task in asyncio.all_tasks() if task is not asyncio.current_task()
                          and '_sse_listener' in task.get_coro().__qualname__])

    async def test_redirects_deny_release_and_recover(self):
        executor = CommandExecutor()
        for mode in ('publication', 'subscription'):
            for status in (301, 302, 303, 307, 308):
                for location in ('/destination?location-secret',
                                 self.urls[0] + '/destination?location-secret',
                                 self.urls[1] + '/destination?location-secret'):
                    with self.subTest(mode=mode, status=status, location=location):
                        self.seen.clear()
                        self.challenges.clear()
                        self.mode, self.status, self.location = mode, status, location
                        self.channel = NtfyApprover(self.urls[0], 'requests', 'replies', timeout=1,
                                                    auth_token='synthetic-token')
                        handler = _make_handler(self.channel, executor, max_pending=1)
                        request = CommandRequest('same-id', 'inert-secret', ['inert-never-execute'])
                        with patch.object(executor, 'execute', side_effect=AssertionError('execution after redirect')):
                            with self.assertLogs('hermes_hostd.approval.ntfy', level='ERROR') as logs:
                                result = await asyncio.wait_for(handler(request), .8)
                            self.assertFalse(result.approved)
                            await self.settle()
                            self.assertFalse(any('/destination' in row[2] for row in self.seen), self.seen)
                            for _, _, _, headers, _ in self.seen:
                                self.assertEqual(headers.get('Authorization'), 'Bearer synthetic-token')
                            diagnostic = '\n'.join(logs.output)
                            self.assertIn(f'ntfy {mode} redirect rejected ({status})', diagnostic)
                            self.assertIn('configure the actual endpoint', diagnostic)
                            for secret in ('synthetic-token', 'location-secret', 'untrusted-response-secret', 'inert-secret'):
                                self.assertNotIn(secret, diagnostic)
                        # Reuse the handler, pending slot and request ID on a direct endpoint.
                        self.mode = 'direct'
                        with patch.object(executor, 'execute', return_value=CommandResult('same-id', True, 0)) as run:
                            result = await asyncio.wait_for(handler(request), .8)
                            self.assertTrue(result.approved)
                            run.assert_called_once()
                        await self.settle()

    async def test_nonredirect_success(self):
        self.channel = NtfyApprover(self.urls[0], 'requests', 'replies', timeout=1,
                                    auth_token='synthetic-token')
        self.assertTrue(await self.channel.request_approval(CommandRequest('direct', 'synthetic', ['inert'])))
        await self.settle()
        self.assertEqual({row[2] for row in self.seen}, {'/requests', '/replies/sse'})
        publish = next(row for row in self.seen if row[1] == 'POST')
        self.assertIn(b'inert', publish[4])
        self.assertIn('Actions', publish[3])

    async def test_other_3xx_missing_location_rejected(self):
        for mode in ('publication', 'subscription'):
            for status in (300, 301, 304, 305, 306, 399):
                with self.subTest(mode=mode, status=status):
                    self.mode, self.status, self.location = mode, status, None
                    self.channel = NtfyApprover(self.urls[0], 'requests', 'replies', timeout=1)
                    with self.assertLogs('hermes_hostd.approval.ntfy', level='ERROR') as logs:
                        self.assertFalse(await self.channel.request_approval(CommandRequest('other', 'synthetic', ['inert'])))
                    await self.settle()
                    self.assertIn(f'redirect rejected ({status})', '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
