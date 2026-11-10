"""Data models for hermes-hostd.

Uses dataclasses with __post_init__ validation instead of pydantic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_str(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str, got {type(value).__name__}")
    return value


def _ensure_list_of_str(name: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TypeError(f"{name} must be a list[str]")
    if not value:
        raise ValueError(f"{name} must be non-empty")
    return value


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class CommandRequest:
    """A request to execute a command on the host.

    Attributes:
        id:          Caller-supplied result-correlation identifier (not authority).
        description: Human-readable description shown in approval notifications.
        command:     Argv list; command[0] is the executable.
        timeout:     Maximum seconds to wait for the command to finish.
        persistent_intent: Declared wish for a process to remain running; metadata
                     only, never a timeout, cancellation or authority override.
    """

    id: str
    description: str
    command: list[str]
    timeout: int = 30
    persistent_intent: bool = False

    def __post_init__(self) -> None:
        if type(self.persistent_intent) is not bool:
            raise TypeError("persistent_intent must be a bool")
        self.id = _ensure_str("id", self.id)
        self.description = _ensure_str("description", self.description)
        self.command = _ensure_list_of_str("command", self.command)
        if not isinstance(self.timeout, int) or self.timeout < 0:
            raise ValueError("timeout must be a non-negative integer (0 = no timeout)")

    # ------------------------------------------------------------------
    # (De)serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON encoding."""
        return {
            "id": self.id,
            "description": self.description,
            "command": self.command,
            "timeout": self.timeout,
            "persistent_intent": self.persistent_intent,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CommandRequest":
        """Deserialise from a plain dict (e.g. parsed JSON)."""
        if not isinstance(data, dict):
            raise TypeError('request payload must be an object')
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            description=data["description"],
            command=data["command"],
            timeout=int(data.get("timeout", 30)),
            persistent_intent=data.get("persistent_intent", False),
        )


@dataclass(frozen=True)
class OperationSnapshot:
    """Host-owned approval/execution values; never deserialised from callers.

    Current transports supply no authenticated principal to the handler, so
    requester_identity is explicitly unknown, not a caller's ID/description.
    PATH, environment and filesystem state are not frozen by this snapshot.
    """

    id: str
    description: str
    command: tuple[str, ...]
    timeout: int
    allowlist: tuple[str, ...] | None
    requester_identity: str | None = None
    persistent_intent: bool = False


@dataclass
class CommandResult:
    """The result of a command execution attempt.

    Attributes:
        id:        Echoes the corresponding :class:`CommandRequest` id.
        approved:  Whether the command was approved for execution.
        exit_code: Process exit code, or ``None`` if the command was not run
                   or timed out.
        stdout:    Captured standard output (may be empty).
        stderr:    Captured standard error (may be empty).
        error:     Human-readable error message when something went wrong,
                   otherwise ``None``.
    """

    id: str
    approved: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    log_id: str | None = None
    output_truncated: bool = False

    metadata: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for name in ('id', 'stdout', 'stderr'):
            if type(getattr(self, name)) is not str:
                raise TypeError('invalid result string field')
        for name in ('error', 'log_id'):
            value = getattr(self, name)
            if value is not None and type(value) is not str:
                raise TypeError('invalid result nullable string field')
        if type(self.approved) is not bool or type(self.output_truncated) is not bool:
            raise TypeError('invalid result boolean field')
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise TypeError('invalid result exit code')
        if 'persistent_intent' in self.metadata and type(self.metadata['persistent_intent']) is not bool:
            raise TypeError('invalid result persistent intent')

    # ------------------------------------------------------------------
    # (De)serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON encoding."""
        self.__post_init__()
        return {
            **self.metadata,
            "id": self.id,
            "approved": self.approved,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "log_id": self.log_id,
            "output_truncated": self.output_truncated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CommandResult":
        """Deserialise from a plain dict (e.g. parsed JSON)."""
        if type(data) is not dict or 'id' not in data or 'approved' not in data:
            raise ValueError('result must be an object with id and approved')
        names = ('id', 'approved', 'exit_code', 'stdout', 'stderr', 'error', 'log_id', 'output_truncated')
        return cls(
            id=data["id"],
            approved=data["approved"],
            exit_code=data.get("exit_code"),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            error=data.get("error"),
            log_id=data.get("log_id"),
            output_truncated=data.get("output_truncated", False),
            metadata={k: v for k, v in data.items() if k not in names},
        )
