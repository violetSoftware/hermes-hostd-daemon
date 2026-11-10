"""Cancellable DNS without lingering getaddrinfo executor threads.

Each resolver owns at most two short-lived, killable helper processes (one per
connector host resolution). Admission remains held until children are reaped.
The helper receives only a broker hostname/port, never a token or topic.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
import sys

import aiohttp.abc


class ProcessResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.workers = set()
        self._slots = asyncio.Semaphore(2)

    def _argv(self, host, port):
        return [sys.executable, '-I', str(Path(__file__).resolve()), host, str(port)]

    async def resolve(self, host, port=0, family=socket.AF_INET):
        async with self._slots:
            # Shield process creation: cancellation between fork and the returned
            # Process must still recover and reap the child before releasing.
            creation = asyncio.create_task(asyncio.create_subprocess_exec(
                *self._argv(host, port), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=16384))
            worker = None
            try:
                worker = await asyncio.shield(creation)
                self.workers.add(worker)
                output_stream = worker.stdout
                assert output_stream is not None
                async def read_bounded():
                    output = bytearray()
                    while True:
                        chunk = await output_stream.read(1024)
                        if not chunk:
                            return output
                        output.extend(chunk)
                        if len(output) > 16384:
                            raise OSError('DNS response exceeds limit')
                output = await asyncio.wait_for(read_bounded(), 5)
                await asyncio.wait_for(worker.wait(), 1)
                if worker.returncode:
                    raise OSError('DNS resolution failed')
                return [record for record in json.loads(output)
                        if family in (socket.AF_UNSPEC, record['family'])]
            finally:
                async def cleanup():
                    child = worker if worker is not None else await creation
                    if child.returncode is None:
                        child.kill()
                    await child.wait()
                    self.workers.discard(child)
                cleanup_task = asyncio.create_task(cleanup())
                # Repeated cancellation must not sever ownership during cleanup.
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        continue
                cleanup_task.result()

    async def close(self):
        pass  # resolve owns/reaps its children before returning


if __name__ == '__main__':
    records = socket.getaddrinfo(sys.argv[1], int(sys.argv[2]), type=socket.SOCK_STREAM)
    result = []
    for family, _, proto, _, address in records[:32]:
        if family in (socket.AF_INET, socket.AF_INET6):
            result.append({'hostname': sys.argv[1], 'host': address[0],
                           'port': address[1], 'family': family, 'proto': proto,
                           'flags': socket.AI_NUMERICHOST})
    sys.stdout.write(json.dumps(result))
