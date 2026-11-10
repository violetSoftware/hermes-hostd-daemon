"""Finite framing BEFORE aiohttp parsing. One fixed-length request per socket."""
import asyncio

HEADER_BYTES = 16384


class HttpGate(asyncio.Protocol):
    def __init__(self, server, factory):
        self.server, self.factory = server, factory
        self.transport = self.delegate = self.timer = None
        self.buffer = bytearray()
        self.header = b''
        self.length = None
        self.admitted = self.closed = False

    def connection_made(self, transport):
        self.transport = transport
        if not self.server.admission.acquire():
            self.closed = True
            transport.close()
            return
        self.admitted = True
        self.server._connections.add(self)
        self.timer = asyncio.get_running_loop().call_later(
            self.server.limits.receive_timeout, self.close)

    def release(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if self.admitted:
            self.admitted = False
            self.server.admission.release()

    def close(self):
        self.closed = True
        self.buffer.clear()
        self.header = b''
        self.release()
        if self.transport:
            self.transport.close()

    def reject(self, status):
        self.transport.write(f'HTTP/1.1 {status}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'.encode())
        self.close()

    def data_received(self, data):
        if self.closed or self.delegate is not None:
            return
        if self.length is None:
            previous = len(self.buffer)
            self.buffer.extend(data[:HEADER_BYTES - previous])
            marker = self.buffer.find(b'\r\n\r\n')
            if marker < 0:
                if len(self.buffer) >= HEADER_BYTES:
                    self.reject('431 Request Header Fields Too Large')
                return
            self.header = bytes(self.buffer[:marker + 4])
            self.buffer.clear()
            data = data[marker + 4 - previous:]
            try:
                lines = self.header[:-4].split(b'\r\n')
                if len(lines) > 101 or len(lines[0]) > 4096:
                    raise ValueError()
                headers = {}
                for line in lines[1:]:
                    key, value = line.split(b':', 1)
                    if not key or key.strip() != key or key.lower() in headers:
                        raise ValueError()
                    headers[key.lower()] = value.strip()
                if any(key in headers for key in (b'transfer-encoding', b'content-encoding', b'expect')):
                    raise ValueError()
                length = headers.get(b'content-length', b'0')
                if not length.isdigit() or len(length) > 10:
                    raise ValueError()
                self.length = int(length)
                if self.length > self.server.limits.max_request_bytes:
                    self.reject('413 Content Too Large')
                    return
                if not self.server.auth.accepts_header(headers.get(b'authorization')):
                    self.reject('401 Unauthorized')
                    return
            except ValueError:
                self.reject('400 Bad Request')
                return
        self.buffer.extend(data[:self.length - len(self.buffer)])
        if len(self.buffer) == self.length:
            payload = self.header + self.buffer
            self.buffer.clear()
            self.header = b''
            self.release()
            try:
                self.delegate = self.factory()
                self.delegate.connection_made(self.transport)
                self.delegate.data_received(payload)
            except Exception:
                self.close()
                if self.delegate:
                    self.delegate.connection_lost(None)
                    self.delegate = None

    def connection_lost(self, exc):
        self.close()
        self.server._connections.discard(self)
        if self.delegate:
            self.delegate.connection_lost(exc)

    def eof_received(self):
        if self.delegate:
            return self.delegate.eof_received()
        self.close()
        return False

    def pause_writing(self):
        if self.delegate:
            self.delegate.pause_writing()

    def resume_writing(self):
        if self.delegate:
            self.delegate.resume_writing()
