"""Approved argv execution with bounded disk capture, never communicate()."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import threading
import time
from typing import Collection

from hermes_hostd.disk_logs import LogStore, DEFAULT_TAIL, MAX_CHUNK
from hermes_hostd.models import CommandRequest, CommandResult, OperationSnapshot

logger = logging.getLogger(__name__)


class ExecutionCancellation:
    """Irreversible per-request latch; no PID is exposed to the event loop."""
    def __init__(self):
        self.event = threading.Event()

    def cancel(self):
        self.event.set()


def _run_streamed(command, *, timeout, store, job, cancellation=None):
    """Drain bounded capture while a private supervisor owns/reaps the job."""
    cancellation = cancellation or ExecutionCancellation()
    totals = {'stdout': 0, 'stderr': 0}
    error = None
    failed = False
    if cancellation.event.is_set():
        return None, totals, 'command cancelled before startup', failed
    parent, child = socket.socketpair()
    proc = None
    outcome = bytearray()
    try:
        proc = subprocess.Popen(
            [sys.executable, '-B', str(Path(__file__).with_name('job.py')), str(child.fileno())],
            pass_fds=(child.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child.close()
        parent.sendall(json.dumps({'command': list(command), 'timeout': timeout}).encode() + b'\n')
        cancelled = False
        completed = None
        with selectors.DefaultSelector() as selector:
            assert proc.stdout is not None and proc.stderr is not None
            selector.register(proc.stdout.fileno(), selectors.EVENT_READ, 'stdout')
            selector.register(proc.stderr.fileno(), selectors.EVENT_READ, 'stderr')
            selector.register(parent.fileno(), selectors.EVENT_READ, 'control')
            while selector.get_map():
                if cancellation.event.is_set() and not cancelled:
                    try:
                        parent.sendall(b'CANCEL\n')
                    except OSError:
                        pass
                    cancelled = True
                for key, _ in selector.select(.02):
                    data = os.read(key.fd, MAX_CHUNK)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    stream = key.data
                    if stream == 'control':
                        outcome.extend(data)
                        if len(outcome) > 65536:
                            raise ValueError('oversized supervisor outcome')
                        completed = time.monotonic()
                        continue
                    if not failed:
                        try:
                            store.append(job, stream, totals[stream], data)
                        except (OSError, ValueError, OverflowError) as exc:
                            failed = True
                            error = f'output logging failed: {exc}'
                    totals[stream] += len(data)
                # A deliberately escaped pipe writer is not owned. Once the
                # supervisor proves owned cleanup, do not await its pipe forever.
                if completed is not None and time.monotonic() - completed > .2:
                    if selector.get_map():
                        failed = True
                        error = (f'{error}; ' if error else '') + 'output pipes remained open outside owned job'
                    break
        proc.wait()
        report = json.loads(outcome)
        if report['error']:
            error = report['error'] + (f'; {error}' if error else '')
        return report['code'], totals, error, failed
    finally:
        child.close()
        # EOF requests cleanup even after capture/parser/OS failures. Never
        # abandon the supervisor while its owned job could still be running.
        try:
            parent.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        if proc is not None:
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
        parent.close()


class CommandExecutor:
    def __init__(self, allowlist: Collection[str] | None = None, *,
                 log_store: LogStore | None = None, tail_bytes=DEFAULT_TAIL):
        self._allowlist = frozenset(allowlist) if allowlist is not None else None
        if type(tail_bytes) is not int or not 1 <= tail_bytes <= DEFAULT_TAIL:
            raise ValueError('tail_bytes must be 1..16384')
        # Lazy creation permits denied operations/tests to avoid any disk access.
        self._log_store = log_store
        self.tail_bytes = tail_bytes

    def snapshot(self, request: CommandRequest) -> OperationSnapshot:
        return OperationSnapshot(
            request.id, request.description, tuple(request.command), request.timeout,
            None if self._allowlist is None else tuple(sorted(self._allowlist)),
            persistent_intent=request.persistent_intent,
        )

    def execute(self, request: CommandRequest | OperationSnapshot, cancellation=None) -> CommandResult:
        executable = request.command[0]
        allowlist = request.allowlist if isinstance(request, OperationSnapshot) else self._allowlist
        if allowlist is not None and executable not in allowlist:
            return CommandResult(request.id, False, error='command not in allowlist')
        logger.info('Executing command id=%a: %a (timeout=%ds)',
                    request.id, request.command, request.timeout)
        result = CommandResult(request.id, True)
        try:
            store = self._log_store if self._log_store is not None else LogStore()
            job = store.new_id()
            result.log_id = job
        except (OSError, ValueError) as exc:
            result.error = f'output log setup failed; command not started: {exc}'
            return result
        try:
            code, totals, error, failed = _run_streamed(
                request.command, timeout=request.timeout if request.timeout > 0 else None,
                store=store, job=job, cancellation=cancellation)
            result.exit_code = None if error and error.startswith(('command timed out', 'command cancelled')) else code
            result.error = error
            for stream in ('stdout', 'stderr'):
                tail = store.tail(job, stream, totals[stream], self.tail_bytes)
                text = tail['data'].decode('utf-8', errors='replace')
                # Preserve universal-newline behavior of the old text=True path.
                text = text.replace('\r\n', '\n').replace('\r', '\n')
                if tail['truncated'] or failed:
                    result.output_truncated = True
                    text = f'[hostd: output truncated/rotated; log_id={job}]\n' + text
                setattr(result, stream, text)
        except FileNotFoundError:
            result.error = f'executable not found: {executable!r}'
        except (OSError, ValueError, OverflowError) as exc:
            result.output_truncated = True
            result.error = f'output capture/OS error: {exc}'
        return result
