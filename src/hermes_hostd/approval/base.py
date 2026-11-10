"""Abstract base class for approval channels.

An approval channel is responsible for presenting a
:class:`~models.CommandRequest` to a human operator and returning their
decision (``True`` = approved, ``False`` = denied / timed out).
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Path fixup
# ---------------------------------------------------------------------------

from abc import ABC, abstractmethod

from hermes_hostd.models import CommandRequest, OperationSnapshot


class ApprovalChannel(ABC):
    """Abstract base for pluggable approval channels.

    Concrete subclasses must implement :meth:`request_approval`.  The
    method should present the request to an operator and return their
    decision within the configured timeout. The daemon handler supplies an
    immutable OperationSnapshot with tuple argv and effective policy; direct
    callers may still supply a CommandRequest. Approvers must not mutate either.

    Example implementations:

    * :class:`~approval.ntfy.NtfyApprover` — push notification via ntfy.
    * :class:`~approval.cli.CliApprover` — interactive terminal prompt.
    """

    @abstractmethod
    async def request_approval(self, request: CommandRequest | OperationSnapshot) -> bool:
        """Ask the operator to approve or deny *request*.

        Args:
            request: The command request awaiting a decision.

        Returns:
            ``True`` if the operator approved the request,
            ``False`` if they denied it or the timeout elapsed.
        """
        ...
