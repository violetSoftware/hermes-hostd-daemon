"""Cancellation-safe terminal approval; timeout=0 deliberately waits forever.

All terminal descriptors are nonblocking. No executor workers, buffered writes,
thread lock waiters or blocking readline calls survive request cancellation.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import os
import termios
from typing import IO

from hermes_hostd.approval.base import ApprovalChannel
from hermes_hostd.approval.presentation import approval_snapshot, render_operation
from hermes_hostd.models import CommandRequest, OperationSnapshot

logger = logging.getLogger(__name__)


class CliApprover(ApprovalChannel):
    def __init__(self, timeout: int = 30) -> None:
        self.timeout = timeout
        self._lock = asyncio.Lock()

    async def request_approval(self, request: CommandRequest | OperationSnapshot) -> bool:
        try:
            operation = approval_snapshot(request)
        except ValueError:
            logger.warning('CliApprover: denied: complete approval display exceeds limits or is invalid')
            return False
        # Retain timeout=0 and the existing per-prompt (not queue) timeout.
        async with self._lock:
            return await self._blocking_prompt(operation)

    async def _blocking_prompt(self, request: OperationSnapshot) -> bool:
        """Historical name; now nonblocking and cancellable, including output."""
        descriptors = []
        try:
            fd = os.open('/dev/tty', os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
            descriptors.append(fd)
            out = os.open('/dev/tty', os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
            descriptors.append(out)
            termios.tcflush(fd, termios.TCIFLUSH)
            text = ('\nHERMES HOST - COMMAND APPROVAL REQUEST\n'
                    + render_operation(request)
                    + '\n----------------------------------------------------------\nDecision [y/N]: ')
            # Include output backpressure in the same finite prompt window.
            if self.timeout > 0:
                return await asyncio.wait_for(self._prompt(fd, out, text), self.timeout)
            return await self._prompt(fd, out, text)
        except asyncio.TimeoutError:
            logger.warning('CliApprover: timed out for request %a', request.id)
            return False
        except (OSError, termios.error) as exc:
            logger.error('CliApprover: terminal failed: %s', exc)
            return False
        finally:
            if descriptors:
                try:
                    termios.tcflush(descriptors[0], termios.TCIFLUSH)
                except (OSError, termios.error):
                    pass
            for descriptor in descriptors:
                os.close(descriptor)

    async def _ready(self, fd: int, write: bool = False) -> None:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        def ready():
            if not future.done():
                future.set_result(None)
        add = loop.add_writer if write else loop.add_reader
        remove = loop.remove_writer if write else loop.remove_reader
        add(fd, ready)
        try:
            await future
        finally:
            remove(fd)

    async def _write(self, fd: int, data: bytes) -> None:
        while data:
            try:
                count = os.write(fd, data)
                if count <= 0:
                    raise OSError(errno.EIO, 'terminal write failed')
                data = data[count:]
            except BlockingIOError:
                await self._ready(fd, write=True)

    async def _prompt(self, fd: int, out: int, text: str) -> bool:
        await self._write(out, text.encode('ascii'))
        answer = await self._read_answer(fd)
        approved = answer in ('y', 'yes')
        await self._write(out, ('[hermes-hostd] ' + ('APPROVED' if approved else 'DENIED') + '\n').encode())
        return approved

    async def _read_answer(self, fd: int) -> str:
        pending = bytearray()
        while True:
            await self._ready(fd)
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                return ''
            pending.extend(chunk)
            if len(pending) > 4096:
                return ''
            if b'\n' in pending:
                return pending.split(b'\n', 1)[0].decode('ascii', errors='replace').strip().lower()

    @staticmethod
    def _print_banner(tty: IO[str], request: CommandRequest | OperationSnapshot) -> None:
        tty.write('\nHERMES HOST - COMMAND APPROVAL REQUEST\n'
                  + render_operation(approval_snapshot(request))
                  + '\n----------------------------------------------------------\n')
        tty.flush()
