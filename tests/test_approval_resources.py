"""Local-only approval resource tests, never a real broker or terminal."""
import asyncio
import json
import os
from pathlib import Path
import pty
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hermes_hostd.approval.cli import CliApprover
from hermes_hostd.approval.ntfy import NtfyApprover
from hermes_hostd.models import CommandRequest


class ApprovalResources(unittest.IsolatedAsyncioTestCase):
    async def test_cli_cancel_and_queue_leave_no_executor_workers(self):
        master, slave = pty.openpty()
        original = os.open
        def opening(path, flags, *args):
            return os.dup(slave) if path == '/dev/tty' else original(path, flags, *args)
        channel = CliApprover(timeout=0)
        tasks = []
        try:
            with patch('hermes_hostd.approval.cli.os.open', side_effect=opening), \
                    patch.object(asyncio.get_running_loop(), 'run_in_executor', side_effect=AssertionError('approval thread')):
                for index in range(3):
                    tasks.append(asyncio.create_task(channel.request_approval(CommandRequest(str(index), 'inert', ['echo']))))
                await asyncio.sleep(.03)
                self.assertFalse(any(task.done() for task in tasks))
                for task in tasks:
                    task.cancel()
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), .3)
                self.assertFalse(channel._lock.locked())
        finally:
            for task in tasks:
                task.cancel()
            os.close(master)
            os.close(slave)

    async def test_dns_resolution_has_no_executor_and_reaps_on_cancel(self):
        from hermes_hostd.approval.resolver import ProcessResolver
        resolver = ProcessResolver()
        with patch.object(asyncio.get_running_loop(), 'run_in_executor', side_effect=AssertionError('DNS thread')):
            records = await asyncio.wait_for(resolver.resolve('localhost', 80), 1)
            self.assertTrue(records)
            self.assertFalse(resolver.workers)
            # Substitute a local inert child that never produces an answer.
            with patch.object(resolver, '_argv', return_value=[sys.executable, '-c', 'import time; time.sleep(2)']):
                task = asyncio.create_task(resolver.resolve('localhost', 80))
                for _ in range(100):
                    if resolver.workers:
                        break
                    await asyncio.sleep(.005)
                workers = list(resolver.workers)
                self.assertTrue(workers)
                task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), .3)
                self.assertFalse(resolver.workers)
                self.assertTrue(all(worker.returncode is not None for worker in workers))

    async def test_real_sse_denial_error_and_cancel_cleanup(self):
        from hermes_hostd.approval.ntfy import _sse_listener, _Challenge
        import time
        chunks = [b'data: ' + json.dumps({'decision':'denied'}).encode() + b'\n\n']
        tasks, peers = set(), set()
        ready = asyncio.Event()
        async def broker(reader, writer):
            peers.add(writer)
            tasks.add(asyncio.current_task())
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                if head.startswith(b'POST'):
                    length = int(next(line.split(b':',1)[1] for line in head.split(b'\r\n') if line.lower().startswith(b'content-length:')))
                    await reader.readexactly(length)
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')
                    await writer.drain()
                else:
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n')
                    await writer.drain()
                    ready.set()
                    if chunks:
                        writer.write(chunks[0]); await writer.drain()
                    await reader.read()
            finally:
                writer.close()
                peers.discard(writer)
                tasks.discard(asyncio.current_task())
        server = await asyncio.start_server(broker, '127.0.0.1', 0)
        url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        try:
            for mode in ('denial', 'oversize', 'cancel', 'timeout'):
                ready.clear()
                channel = NtfyApprover(url, 'requests', 'replies', timeout=.15)
                chunks[:] = [b'x'*8193] if mode == 'oversize' else []
                task = asyncio.create_task(channel.request_approval(CommandRequest('one', 'inert', ['echo'])))
                await asyncio.wait_for(ready.wait(), .3)
                if mode == 'denial':
                    challenge = channel._active['one']
                    for writer in tuple(peers):
                        writer.write(b'data: '+json.dumps(challenge.body('denied')).encode()+b'\n\n')
                        await writer.drain()
                elif mode == 'cancel':
                    task.cancel()
                result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), .5)
                if mode != 'cancel':
                    self.assertEqual(result, [False])
                for _ in range(50):
                    if not peers:
                        break
                    await asyncio.sleep(.005)
                self.assertFalse(peers)
                self.assertFalse(channel._active)
        finally:
            server.close(); await server.wait_closed()
            for writer in peers:
                writer.close()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_ntfy_slow_publish_timeout_and_cancel_close_connections(self):
        opened, closed = asyncio.Event(), asyncio.Event()
        peers = set()
        async def broker(reader, writer):
            peers.add(writer)
            try:
                await reader.readuntil(b'\r\n\r\n')
                opened.set()
                await reader.read()
            finally:
                peers.discard(writer)
                writer.close()
                closed.set()
        server = await asyncio.start_server(broker, '127.0.0.1', 0)
        url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        try:
            for cancel in (False, True):
                opened.clear(); closed.clear()
                channel = NtfyApprover(url, 'requests', 'replies', timeout=.08)
                task = asyncio.create_task(channel.request_approval(CommandRequest('one', 'inert', ['echo'])))
                await asyncio.wait_for(opened.wait(), .3)
                if cancel:
                    task.cancel()
                result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), .5)
                if not cancel:
                    self.assertEqual(result, [False])
                await asyncio.wait_for(closed.wait(), .3)
                await asyncio.sleep(.02)
                self.assertFalse(peers)
                self.assertFalse(channel._active)
        finally:
            server.close()
            await server.wait_closed()
            for writer in peers:
                writer.close()


if __name__ == '__main__':
    unittest.main()
