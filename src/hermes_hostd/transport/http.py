"""HTTP transport server for hermes-hostd.

Exposes a single ``POST /execute`` endpoint.  The request body must be a
JSON-encoded :class:`~models.CommandRequest`; the response body is a
JSON-encoded :class:`~models.CommandResult`.

aiohttp is required; the weaker stdlib fallback is disabled.

Bearer-token authentication is required unless explicitly disabled with
``insecure_no_auth=True``. Credentials are checked before command allocation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# Path fixup
# ---------------------------------------------------------------------------

from hermes_hostd.models import CommandRequest, CommandResult
from hermes_hostd.transport.base import Handler, TransportServer
from hermes_hostd.transport.limits import Admission, ReceiveLimits
from hermes_hostd.transport.http_gate import HttpGate
from hermes_hostd.transport.auth import RequesterAuth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Attempt aiohttp import
# ---------------------------------------------------------------------------
try:
    import aiohttp
    from aiohttp import web as aiohttp_web

    HANDLER = aiohttp_web.AppKey('handler', object)
    AUTH = aiohttp_web.AppKey('auth', RequesterAuth)
    _AIOHTTP_AVAILABLE = True
    logger.debug("aiohttp available — using async HTTP backend")
except ImportError:
    _AIOHTTP_AVAILABLE = False
    logger.debug("aiohttp missing; HTTP startup will fail closed")


class HttpServer(TransportServer):
    """HTTP transport server with a required-by-default bearer-token guard.

    Args:
        host:       IP address to bind (default ``"127.0.0.1"``).
        port:       TCP port to bind (default ``9998``).
        auth_token: If set, every request must supply
                    ``Authorization: Bearer <auth_token>`` or receive 401.

    Endpoint::

        POST /execute
        Content-Type: application/json
        Body: { CommandRequest fields … }

        → 200 application/json { CommandResult fields … }
        → 400 on parse errors
        → 401 if the required token is missing/wrong
        → 405 on non-POST methods
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9998,
        auth_token: Optional[str] = None,
        limits: ReceiveLimits | None = None,
        insecure_no_auth: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.auth = RequesterAuth(auth_token, insecure_no_auth)
        self.limits = limits or ReceiveLimits()
        self.admission = Admission(self.limits.max_receiving)
        self._listener = None
        self._connections = set()
        self._handlers = set()
        self._runner: Optional["aiohttp_web.AppRunner"] = None  # type: ignore[name-defined]

    # ------------------------------------------------------------------
    # TransportServer interface
    # ------------------------------------------------------------------

    async def start(self, handler: Handler) -> None:
        """Start the HTTP server and serve requests until :meth:`stop`."""
        if _AIOHTTP_AVAILABLE:
            await self._start_aiohttp(handler)
        else:
            raise RuntimeError('HTTP requires aiohttp; weaker stdlib fallback is disabled')

    async def stop(self) -> None:
        """Gracefully shut down the server."""
        if self._listener is not None:
            self._listener.close()
        for connection in tuple(self._connections):
            connection.close()
        handlers = tuple(self._handlers)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        if self._listener is not None:
            await self._listener.wait_closed()
        if self._runner is not None:
            await self._runner.cleanup()
            logger.info("HttpServer (aiohttp) stopped")

    # ------------------------------------------------------------------
    # aiohttp backend
    # ------------------------------------------------------------------

    async def _start_aiohttp(self, handler: Handler) -> None:
        """Run the server using aiohttp."""
        @aiohttp_web.middleware
        async def close_response(request, handler):
            task = asyncio.current_task()
            self._handlers.add(task)
            try:
                try:
                    response = await handler(request)
                except aiohttp_web.HTTPException as response_error:
                    response = response_error
                response.force_close()
                return response
            finally:
                self._handlers.discard(task)

        app = aiohttp_web.Application(client_max_size=self.limits.max_request_bytes,
                                      middlewares=[close_response])
        app[HANDLER] = handler
        app[AUTH] = self.auth
        app.router.add_post("/execute", _aiohttp_execute_view)

        self._runner = aiohttp_web.AppRunner(app, handler_cancellation=True,
                                             auto_decompress=False, lingering_time=0)
        await self._runner.setup()
        self._listener = await asyncio.get_running_loop().create_server(
            lambda: HttpGate(self, self._runner.server), self.host, self.port)

        logger.info(
            "HttpServer (aiohttp) listening on http://%s:%d/execute",
            self.host,
            self.port,
        )

        # Keep running until stop() is called.
        try:
            await asyncio.Event().wait()  # block forever
        except asyncio.CancelledError:
            pass



# ---------------------------------------------------------------------------
# aiohttp view
# ---------------------------------------------------------------------------


async def _aiohttp_execute_view(request: "aiohttp_web.Request") -> "aiohttp_web.Response":
    """Handle ``POST /execute`` requests (aiohttp)."""
    value = request.headers.get("Authorization")
    header = value.encode('utf-8') if value is not None else None
    if not request.app[AUTH].accepts_header(header):
        return _response(status=401, text="Unauthorized")

    try:
        body = json.loads(await request.content.read())
    except (ValueError, TypeError) as exc:
        return _response(
            status=400, text=f"Invalid JSON: {exc}", content_type="text/plain"
        )

    try:
        cmd_request = CommandRequest.from_dict(body)
    except (KeyError, TypeError, ValueError) as exc:
        return _response(
            status=400,
            text=f"Invalid request payload: {exc}",
            content_type="text/plain",
        )

    handler: Handler = request.app[HANDLER]
    try:
        result = await handler(cmd_request)
    except Exception as exc:  # noqa: BLE001
        logger.exception("HttpServer: handler raised: %s", exc)
        result = CommandResult(
            id=cmd_request.id,
            approved=False,
            error=f"internal error: {exc}",
        )

    response = aiohttp_web.Response(
        status=200,
        text=json.dumps(result.to_dict()),
        content_type="application/json",
    )
    response.force_close()
    return response




def _response(**kwargs):
    response = aiohttp_web.Response(**kwargs)
    response.force_close()
    return response
