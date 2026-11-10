"""AF_VSOCK transport server for hermes-hostd.

Listens on an AF_VSOCK socket so that commands can be sent from a KVM/QEMU
guest VM to this host daemon without requiring network configuration.

Wire protocol (per connection):
  →  one line of newline-terminated JSON encoding a :class:`~models.CommandRequest`
  ←  one line of newline-terminated JSON encoding a :class:`~models.CommandResult`

The connection is closed by the server after the response is sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# Path fixup
# ---------------------------------------------------------------------------

from hermes_hostd.models import CommandRequest, CommandResult
from hermes_hostd.transport.base import Handler, TransportServer
from hermes_hostd.transport.limits import Admission, ReceiveLimits
from hermes_hostd.transport.auth import RequesterAuth

logger = logging.getLogger(__name__)

# AF_VSOCK constants (Linux kernel headers).
AF_VSOCK: int = 40
VMADDR_CID_ANY: int = 0xFFFFFFFF  # UINT32_MAX — accept from any CID


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


class VsockServer(TransportServer):
    """Transport server that listens on an AF_VSOCK socket.

    Args:
        port: The vsock port to listen on (default 9999).
        backlog: Listen backlog passed to :func:`socket.socket.listen`.

    Note:
        AF_VSOCK requires a hypervisor that exposes the vsock device
        (e.g. ``virtio-vsock`` under QEMU/KVM).  If the kernel does not
        support AF_VSOCK, :meth:`start` will raise an :exc:`OSError`.
    """

    def __init__(self, port: int = 9999, backlog: int = 5,
                 limits: ReceiveLimits | None = None, auth_token: str | None = None,
                 insecure_no_auth: bool = False) -> None:
        self.auth = RequesterAuth(auth_token, insecure_no_auth)
        self.port = port
        self.backlog = backlog
        self.limits = limits or ReceiveLimits()
        self.admission = Admission(self.limits.max_receiving)
        self._server: Optional[asyncio.AbstractServer] = None
        self._connections = set()
        self._tasks = set()

    def _protocol(self, handler):
        return _VsockProtocol(handler, self.limits, self.admission,
                              self._connections, self._tasks, auth=self.auth)

    # ------------------------------------------------------------------
    # TransportServer interface
    # ------------------------------------------------------------------

    async def start(self, handler: Handler) -> None:
        """Bind the vsock socket and start accepting connections.

        Args:
            handler: Async callable ``(CommandRequest) -> CommandResult``.
        """
        # asyncio does not natively support AF_VSOCK; we create the raw
        # socket ourselves and hand it to asyncio's low-level server API.
        sock = _create_vsock_socket(self.port, self.backlog)

        logger.info("VsockServer listening on vsock port %d", self.port)

        loop = asyncio.get_event_loop()
        self._server = await loop.create_server(
            lambda: self._protocol(handler),
            sock=sock,
        )

        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop the server gracefully."""
        if self._server is not None:
            self._server.close()
        for connection in tuple(self._connections):
            connection._transport.close()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
        logger.info("VsockServer stopped")


# ---------------------------------------------------------------------------
# asyncio Protocol
# ---------------------------------------------------------------------------


class _VsockProtocol(asyncio.Protocol):
    """Per-connection protocol that handles a single request/response."""

    def __init__(self, handler: Handler, limits: ReceiveLimits | None = None,
                 admission: Admission | None = None, connections=None, tasks=None, *,
                 auth: RequesterAuth | None = None, insecure_no_auth: bool = False) -> None:
        self._connections = connections if connections is not None else set()
        self._tasks = tasks if tasks is not None else set()
        self._auth = auth if auth is not None else RequesterAuth(insecure_no_auth=insecure_no_auth)
        self._handler = handler
        self._limits = limits or ReceiveLimits()
        self._admission = admission or Admission(self._limits.max_receiving)
        self._receiving = False
        self._timer = None
        self._task = None
        self._transport: Optional[asyncio.Transport] = None
        self._buffer = b""
        self._started = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        self._connections.add(self)
        if not self._admission.acquire():
            self._started = True
            transport.close()
            return
        self._receiving = True
        self._timer = asyncio.get_running_loop().call_later(
            self._limits.receive_timeout, self._reject, 'receive deadline exceeded')
        peer = getattr(transport, "get_extra_info", lambda _: None)("peername")
        logger.debug("VsockServer: new connection from %s", peer)

    def data_received(self, data: bytes) -> None:
        if self._started:
            return
        end = data.find(b'\n')
        incoming = len(data) if end < 0 else end
        if len(self._buffer) + incoming > self._limits.max_request_bytes:
            self._reject('request too large')
            return
        self._buffer += data[:incoming]
        if end >= 0:
            self._started = True
            line = self._buffer
            self._buffer = b""
            self._release_receive()
            # Keep reads enabled to observe EOF; later frames are discarded.
            self._task = asyncio.ensure_future(self._handle_line(line))
            self._tasks.add(self._task)
            self._task.add_done_callback(self._tasks.discard)

    def connection_lost(self, exc: Exception | None) -> None:
        self._connections.discard(self)
        self._started = True
        self._buffer = b''
        self._release_receive()
        if self._task is not None:
            self._task.cancel()
        if exc:
            logger.warning("VsockServer: connection lost with error: %s", exc)

    def _release_receive(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._receiving:
            self._receiving = False
            self._admission.release()

    def _reject(self, message):
        self._started = True
        self._buffer = b''
        self._release_receive()
        self._send_error('invalid-id', message)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _handle_line(self, line: bytes) -> None:
        """Parse *line* as a :class:`CommandRequest`, call the handler."""
        try:
            raw = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(raw, dict):
                raise ValueError('request must be an object')
            has_auth = 'auth' in raw
            metadata = raw.pop('auth', None)
            if not has_auth:
                token = None
            elif isinstance(metadata, dict) and set(metadata) == {'token'}:
                token = metadata['token']
                if token is None:
                    self._send_error('invalid-id', 'Unauthorized')
                    return
            else:
                self._send_error('invalid-id', 'Unauthorized')
                return
            if not self._auth.accepts(token):
                self._send_error('invalid-id', 'Unauthorized')
                return
            request = CommandRequest.from_dict(raw)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.error("VsockServer: invalid request payload")
            self._send_error("invalid-id", "invalid request payload")
            return

        try:
            result = await self._handler(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception("VsockServer: handler raised exception: %s", exc)
            result = CommandResult(
                id=request.id,
                approved=False,
                error=f"internal error: {exc}",
            )

        self._send_result(result)

    def _send_result(self, result: CommandResult) -> None:
        if self._transport and not self._transport.is_closing():
            payload = json.dumps(result.to_dict()).encode("utf-8") + b"\n"
            self._transport.write(payload)
            self._transport.close()

    def _send_error(self, req_id: str, message: str) -> None:
        result = CommandResult(
            id=req_id,
            approved=False,
            error=message,
        )
        self._send_result(result)


# ---------------------------------------------------------------------------
# Socket helpers
# ---------------------------------------------------------------------------


def _create_vsock_socket(port: int, backlog: int) -> socket.socket:
    """Create and bind a listening AF_VSOCK socket."""
    try:
        sock = socket.socket(AF_VSOCK, socket.SOCK_STREAM, 0)
    except OSError as exc:
        raise OSError(
            f"Failed to create AF_VSOCK socket — ensure the kernel "
            f"supports vsock (AF_VSOCK={AF_VSOCK}): {exc}"
        ) from exc

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setblocking(False)

    # (CID, port) — CID=VMADDR_CID_ANY means listen on all CIDs.
    sock.bind((VMADDR_CID_ANY, port))
    sock.listen(backlog)
    return sock
