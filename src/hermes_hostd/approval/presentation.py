"""Lossless approval presentation; display text never becomes executable argv."""
from dataclasses import asdict
import json

from hermes_hostd.models import CommandRequest, OperationSnapshot


# A conservative text budget; broker/client-specific rendering still needs acceptance.
MAX_DISPLAY_BYTES = 3500
MAX_FIELD_CHARS = 1024
MAX_ITEMS = 64
MAX_TOTAL_CHARS = 2048


def _validate_display(operation: OperationSnapshot) -> None:
    """Bound work before JSON expansion; these are display limits, not ingress caps."""
    if type(operation.persistent_intent) is not bool:
        raise ValueError('persistent_intent must be a bool')
    if (type(operation.timeout) is not int or operation.timeout < 0
            or operation.timeout.bit_length() > 64):
        raise ValueError('approval display timeout is not representable')
    if (not operation.command or len(operation.command) > MAX_ITEMS
            or (operation.allowlist is not None and len(operation.allowlist) > MAX_ITEMS)):
        raise ValueError('approval display has too many items')
    values = (operation.id, operation.description, *operation.command,
              *(operation.allowlist or ()), operation.requester_identity or '')
    if (any(not isinstance(value, str) or len(value) > MAX_FIELD_CHARS for value in values)
            or sum(map(len, values)) > MAX_TOTAL_CHARS):
        raise ValueError('approval display input exceeds limits')


def approval_snapshot(request: CommandRequest | OperationSnapshot) -> OperationSnapshot:
    """Reuse the handler's immutable operation; defensively freeze direct callers."""
    operation = request if isinstance(request, OperationSnapshot) else OperationSnapshot(
        request.id, request.description, tuple(request.command), request.timeout, None,
        persistent_intent=request.persistent_intent)
    # Use the larger ntfy form for admission in BOTH channels.
    render_operation(operation, operation_digest='0' * 64, expires_at=99999999999999999999)
    return operation


def render_operation(operation: OperationSnapshot, *, operation_digest: str | None = None,
                     expires_at: int | None = None) -> str:
    """ASCII-only JSON keeps every requester value inside a quoted data field."""
    _validate_display(operation)
    timeout = ('unlimited (0 seconds)' if operation.timeout == 0
               else f'{operation.timeout} seconds')
    content = {'Review': 'Complete operation - review every argument before approving',
               'Effective execution timeout': timeout,
               'Timeout policy': 'Requester-selected; no host maximum. Not a descendant cleanup guarantee.',
               'Persistent process intent': ('Declared: caller wants process to remain running'
                    if operation.persistent_intent else 'Not declared (ordinary request)'),
               'Intent policy': 'Metadata only; no lifetime changes, detachment, new authority or background management.',
               'operation': asdict(operation)}
    if operation_digest is not None:
        content['operation_digest'] = operation_digest
        content['expires_at'] = expires_at
    content['Display complete'] = True
    rendered = json.dumps(content, ensure_ascii=True, indent=2)
    if len(rendered) > MAX_DISPLAY_BYTES:
        raise ValueError('complete escaped approval display exceeds byte limit')
    return rendered
