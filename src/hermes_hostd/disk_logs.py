"""Private fixed-size disk ring. No output-sized Python collections or reads.

One 4096-byte control page and fixed 4096-byte records, shared by processes
using flock. Records carry opaque job ID, stream, absolute byte offset and CRC.
The control page reserves a sequence before overwriting a slot; interrupted
writes are invalidated by CRC/sequence and never interpreted as valid output.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import stat
import struct
import uuid
import zlib

PAGE = 4096
CONTROL = struct.Struct('!8sQQ')
MAGIC = b'HOSTLOG1'
RECORD = struct.Struct('!Q16scQII')
PAYLOAD = PAGE - RECORD.size
MAX_CHUNK = 65536
DEFAULT_BUDGET = 64 * 1024 * 1024
DEFAULT_TAIL = 16384


def default_directory():
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'hermes-hostd/job-logs'


class LogStore:
    def __init__(self, directory=None, budget=DEFAULT_BUDGET):
        if type(budget) is not int or budget < PAGE * 3 or budget % PAGE:
            raise ValueError('log budget must be a multiple of 4096 and at least 12288 bytes')
        self.directory = Path(directory) if directory is not None else default_directory()
        self.budget = budget
        self.capacity = budget // PAGE - 1
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Reject symlinks, permissive/foreign directories and preexisting files.
        with self._directory() as directory_fd:
            created = False
            try:
                fd = os.open('ring-v1', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
                created = True
            except FileExistsError:
                fd = os.open('ring-v1', os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                self._validate(fd, directory=False)
                fcntl.flock(fd, fcntl.LOCK_EX)
                if created:
                    os.ftruncate(fd, budget)
                    self._write(fd, CONTROL.pack(MAGIC, budget, 0), 0)
                self._control(fd)
            finally:
                os.close(fd)

    @staticmethod
    def new_id():
        return uuid.uuid4().hex

    @staticmethod
    def _id(job):
        if not isinstance(job, str) or re.fullmatch('[0-9a-f]{32}', job) is None:
            raise ValueError('log ID must be 32 lowercase hexadecimal characters')
        return bytes.fromhex(job)

    @staticmethod
    def _validate(fd, directory):
        info = os.fstat(fd)
        correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not correct_type or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError('log store must be private and owned by the current user')
        if not directory and info.st_nlink != 1:
            raise PermissionError('hard-linked log store rejected')

    @contextmanager
    def _directory(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._validate(fd, directory=True)
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _locked(self):
        with self._directory() as directory_fd:
            fd = os.open('ring-v1', os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            self._validate(fd, directory=False)
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._control(fd)
            yield fd
        finally:
            os.close(fd)

    def _control(self, fd):
        raw = os.pread(fd, CONTROL.size, 0)
        if len(raw) != CONTROL.size:
            raise OSError('incomplete log store control page')
        magic, budget, sequence = CONTROL.unpack(raw)
        if magic != MAGIC or budget != self.budget or os.fstat(fd).st_size != budget:
            raise OSError('log store format/budget mismatch; use original budget for cleanup before resizing')
        # Allocation accounting includes all file content/control bytes; reject
        # filesystems allocating more than the configured storage ceiling.
        if os.fstat(fd).st_blocks * 512 > budget:
            raise OSError('filesystem allocation exceeds log budget')
        return sequence

    @staticmethod
    def _write(fd, data, offset):
        while data:
            size = os.pwrite(fd, data, offset)
            if size <= 0:
                raise OSError('short log write')
            offset += size
            data = data[size:]

    def append(self, job, stream, offset, data):
        identity = self._id(job)
        marker = self._stream(stream)
        if type(offset) is not int or offset < 0 or offset > 2**64 - 1:
            raise ValueError('invalid stream offset')
        if len(data) > MAX_CHUNK:
            raise ValueError('append exceeds bounded chunk')
        with self._locked() as fd:
            sequence = self._control(fd)
            for start in range(0, len(data), PAYLOAD):
                chunk = data[start:start + PAYLOAD]
                sequence += 1
                self._write(fd, CONTROL.pack(MAGIC, self.budget, sequence), 0)
                position = PAGE * (1 + (sequence - 1) % self.capacity)
                prefix = RECORD.pack(sequence, identity, marker, offset + start, len(chunk), 0)[:-4]
                header = prefix + struct.pack('!I', zlib.crc32(prefix + chunk))
                self._write(fd, (header + chunk).ljust(PAGE, b'\x00'), position)
            self._control(fd)

    @staticmethod
    def _stream(stream):
        if stream not in ('stdout', 'stderr'):
            raise ValueError('stream must be stdout or stderr')
        return b'o' if stream == 'stdout' else b'e'

    def _records(self, fd):
        last = self._control(fd)
        for sequence in range(max(1, last - self.capacity + 1), last + 1):
            raw = os.pread(fd, PAGE, PAGE * (1 + (sequence - 1) % self.capacity))
            if len(raw) != PAGE:
                raise OSError('short log read')
            seq, job, stream, offset, length, crc = RECORD.unpack(raw[:RECORD.size])
            data = raw[RECORD.size:RECORD.size + length]
            if seq == sequence and 0 < length <= PAYLOAD and zlib.crc32(raw[:RECORD.size - 4] + data) == crc:
                yield job, stream, offset, data

    def read(self, job, stream, offset=0, limit=MAX_CHUNK):
        identity, marker = self._id(job), self._stream(stream)
        if type(offset) is not int or offset < 0:
            raise ValueError('offset must be non-negative')
        if type(limit) is not int or not 1 <= limit <= MAX_CHUNK:
            raise ValueError('limit must be 1..65536 bytes')
        output = bytearray()
        cursor, gap = offset, False
        with self._locked() as fd:
            for record_job, record_stream, start, data in self._records(fd):
                if record_job != identity or record_stream != marker or start + len(data) <= cursor:
                    continue
                if start > cursor:
                    gap = True
                    cursor = start
                chunk = data[max(0, cursor - start):][:limit - len(output)]
                output.extend(chunk)
                cursor += len(chunk)
                if len(output) == limit:
                    break
        return {'data': bytes(output), 'next_offset': cursor, 'gap': gap,
                'notice': 'retention is best-effort; empty data does not prove job completion or existence'}

    def tail(self, job, stream, total, limit=DEFAULT_TAIL):
        result = self.read(job, stream, max(0, total - limit), limit)
        result['truncated'] = total > len(result['data'])
        return result

    def cleanup(self, job=None):
        identity = None if job is None else self._id(job)
        with self._locked() as fd:
            last = self._control(fd)
            for sequence in range(max(1, last - self.capacity + 1), last + 1):
                position = PAGE * (1 + (sequence - 1) % self.capacity)
                raw = os.pread(fd, RECORD.size, position)
                if identity is None or raw[8:24] == identity:
                    self._write(fd, bytes(PAGE), position)
            # Erase content, not unlink: concurrent writers keep the same inode,
            # locking, capacity and monotonically increasing eviction order.
