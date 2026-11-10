"""Abstract base class for transport servers.

A transport server listens for incoming :class:`~models.CommandRequest`
objects (e.g. over AF_VSOCK or HTTP), passes them to the application
handler, and sends back the resulting :class:`~models.CommandResult`.
"""

from __future__ import annotations

import sys
import os

# ---------------------------------------------------------------------------
# Path fixup — allow sibling imports when running from the project root.
# ---------------------------------------------------------------------------

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from hermes_hostd.models import CommandRequest, CommandResult

# Type alias for the handler callable accepted by TransportServer.start().
Handler = Callable[[CommandRequest], Awaitable[CommandResult]]


class TransportServer(ABC):
    """Abstract base for pluggable transport servers.

    Concrete subclasses must implement :meth:`start` and :meth:`stop`.

    The *handler* argument passed to :meth:`start` is an async callable
    with signature ``async (request: CommandRequest) -> CommandResult``.
    The transport is responsible for deserialising incoming data into a
    :class:`~models.CommandRequest`, invoking the handler, and serialising
    the returned :class:`~models.CommandResult` back to the caller.
    """

    @abstractmethod
    async def start(self, handler: Handler) -> None:
        """Start listening and serving requests.

        This coroutine should run until :meth:`stop` is called (or an
        unrecoverable error occurs).  It must call *handler* for every
        received :class:`~models.CommandRequest`.

        Args:
            handler: Async callable that processes a request and returns
                a result.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the server and release resources."""
        ...
