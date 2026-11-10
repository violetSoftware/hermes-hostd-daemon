"""Inert async broker seam: captures payloads without network or threads."""
from types import SimpleNamespace
import asyncio

async def idle_listener(*args):
    await asyncio.Event().wait()

class Response:
    status = 200
    def __init__(self, chunks=()):
        self.chunks = chunks
        self.content = self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk

class Session:
    def __init__(self, chunks=(), on_post=None):
        self.chunks, self.on_post = chunks, on_post
        self.calls = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    def post(self, url, **kwargs):
        self.calls.append(('POST', url, kwargs))
        self.notification = SimpleNamespace(data=kwargs['data'], get_header=lambda key: kwargs['headers'].get(key))
        if self.on_post:
            self.on_post(self.notification)
        return Response()
    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        return Response(self.chunks)
