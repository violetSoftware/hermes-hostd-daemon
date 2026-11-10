"""Private Linux job supervisor. One subreaper per request, never daemon-global.

The control socket is host-owned, not part of the command's inherited fds.
Only the command's new process group is signalled. Deliberate group escapes
and work delegated to other services are outside this ownership boundary.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time

POLL = .02
TERM_GRACE = .2


def _subreaper():
    if sys.platform != 'linux':
        raise OSError('request-owned execution requires Linux child subreapers')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'cannot establish request child subreaper')


def _children():
    # Kernel-owned direct-child inventory, including orphaned grandchildren
    # adopted by THIS supervisor. No process-name or global PID-tree matching.
    return [int(pid) for pid in Path(f'/proc/self/task/{os.getpid()}/children').read_text().split()]


def _other_members(leader):
    members = []
    reaped = True
    while reaped:
        reaped = False
        members = []
        for pid in _children():
            if pid == leader:
                continue
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    # Death/reparenting can add adopted grandchildren after
                    # this inventory snapshot. Rescan before declaring empty.
                    reaped = True
                    continue
                if os.getpgid(pid) == leader:
                    members.append(pid)
            except (ChildProcessError, ProcessLookupError):
                pass
    return members


def _signal_group(leader, sig):
    try:
        os.killpg(leader, sig)
    except ProcessLookupError:
        pass


def _run(control, command, timeout):
    stopped = False
    def retire(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, retire)
    signal.signal(signal.SIGINT, retire)
    proc = None
    reason = None
    try:
        # Consume cancellation already queued during startup before spawning.
        if select.select([control], [], [], 0)[0]:
            return {'code': None, 'error': 'command cancelled before startup'}
        proc = subprocess.Popen(command, start_new_session=True, close_fds=True)
        deadline = None if timeout is None else time.monotonic() + timeout
        termination = None
        killed = False
        while True:
            # WNOWAIT pins the group leader PID until owned descendants are gone.
            # Reaping it early would allow signalling a recycled/unrelated PGID.
            status = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            others = _other_members(proc.pid)
            if status is not None and not others:
                break
            if reason is None:
                if stopped:
                    reason = 'command cancelled'
                elif deadline is not None and time.monotonic() >= deadline:
                    reason = f'command timed out after {timeout}s'
                elif select.select([control], [], [], POLL)[0]:
                    # Either explicit cancellation or EOF is irreversible.
                    control.recv(4096)
                    reason = 'command cancelled'
                if reason is not None:
                    _signal_group(proc.pid, signal.SIGTERM)
                    termination = time.monotonic() + TERM_GRACE
            else:
                time.sleep(POLL)
            if termination is not None and not killed and time.monotonic() >= termination:
                _signal_group(proc.pid, signal.SIGKILL)
                killed = True
        code = proc.wait()
        return {'code': code, 'error': reason}
    finally:
        if proc is not None and proc.returncode is None:
            # Startup/monitor errors must not retire a still-running job. Keep
            # the leader unreaped until all in-bound adopted children are reaped.
            _signal_group(proc.pid, signal.SIGKILL)
            while True:
                status = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if status is not None and not _other_members(proc.pid):
                    break
                time.sleep(POLL)
            proc.wait()


def main():
    with socket.socket(fileno=int(sys.argv[1])) as control:
        try:
            _subreaper()  # Fail before any command if containment unavailable.
            _children()  # Verify required procfs access before command startup.
            data = bytearray()
            while b'\n' not in data:
                part = control.recv(65536)
                if not part:
                    return
                data.extend(part)
            line, rest = bytes(data).split(b'\n', 1)
            if rest:  # cancellation arrived along with startup
                result = {'code': None, 'error': 'command cancelled before startup'}
            else:
                spec = json.loads(line)
                result = _run(control, spec['command'], spec['timeout'])
        except Exception as exc:
            result = {'code': None, 'error': f'job supervisor error: {type(exc).__name__}: {exc}'}
        try:
            control.sendall(json.dumps(result).encode() + b'\n')
        except OSError:
            pass  # Caller is gone; cleanup above has already finished.


if __name__ == '__main__':
    main()
