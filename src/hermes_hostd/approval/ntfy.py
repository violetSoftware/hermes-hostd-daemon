"""ntfy v2 operation binding (NOT authenticated operator approval).

Action payloads contain a public per-operation challenge, digest and expiry.
They contain no operator credential. Reply publishers are still unauthenticated
by this implementation: broker ACL/phone-local authentication or a signed
protocol is not implemented. Deployment must protect reply authority; hidden
topics do not authenticate a human operator.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
import math
import secrets
import threading
import time
import aiohttp

SSE_LINE_BYTES = 8192
SSE_EVENT_BYTES = 16384
from typing import Optional

from hermes_hostd.approval.resolver import ProcessResolver
from hermes_hostd.approval.base import ApprovalChannel
from hermes_hostd.approval.presentation import approval_snapshot, render_operation
from hermes_hostd.models import CommandRequest, OperationSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Challenge:
    """Immutable binding with lock-protected single-use/revocation state."""

    nonce: str
    operation_digest: str
    expires_at: int
    deadline: float
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False)
    _closed: threading.Event = field(default_factory=threading.Event, compare=False)

    def body(self, decision: str) -> dict:
        return {'version': 2, 'challenge': self.nonce,
                'operation': self.operation_digest, 'expires_at': self.expires_at,
                'decision': decision}

    def consume(self, candidate: dict) -> bool:
        # Strict types prevent bool/int coercion and reject legacy id-only replies.
        expected = self.body(candidate.get('decision'))
        if (set(candidate) != set(expected)
                or type(candidate.get('version')) is not int
                or type(candidate.get('expires_at')) is not int
                or candidate.get('decision') not in ('approved', 'denied')
                or candidate != expected):
            return False
        with self._lock:
            if self._closed.is_set() or time.monotonic() >= self.deadline:
                return False
            self._closed.set()
            self.loop.call_soon_threadsafe(self._deliver, candidate['decision'])
            return True

    def _deliver(self, decision: str) -> None:
        if not self.future.done():
            self.future.set_result(decision)

    def revoke(self) -> None:
        with self._lock:
            self._closed.set()


class NtfyApprover(ApprovalChannel):
    """Publish exact operations and wait for one v2 decision.

    auth_token authenticates this daemon's broker I/O only, NOT the decision
    author. This binding-only checkpoint must not be used as an independent
    operator authentication boundary.
    """

    def __init__(self, base_url: str, request_topic: str, response_topic: str,
                 timeout: float = 60, auth_token: Optional[str] = None) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('ntfy approval timeout must be finite and positive')
        self.base_url = base_url.rstrip('/')
        self.request_topic = request_topic
        self.response_topic = response_topic
        self.timeout = timeout
        self.auth_token = auth_token
        self._active: dict[str, _Challenge] = {}

    async def request_approval(self, request: CommandRequest | OperationSnapshot) -> bool:
        # Direct users get a defensive immutable copy too. Only the handler's
        # host-created snapshot carries the effective executor policy.
        try:
            operation = approval_snapshot(request)
        except ValueError:
            logger.warning('NtfyApprover: denied: complete approval display exceeds limits or is invalid')
            return False
        if operation.id in self._active:
            return False
        loop = asyncio.get_running_loop()
        encoded = json.dumps(asdict(operation), sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True).encode('utf-8')
        challenge = _Challenge(
            secrets.token_hex(32), hashlib.sha256(encoded).hexdigest(),
            math.ceil(time.time() + self.timeout), time.monotonic() + self.timeout,
            loop, loop.create_future())
        self._active[operation.id] = challenge
        listener = None
        try:
            # The outer deadline covers connect, publish, streaming and decision.
            connector = aiohttp.TCPConnector(resolver=ProcessResolver(), use_dns_cache=False, limit=2)
            async with aiohttp.ClientSession(
                    connector=connector, timeout=aiohttp.ClientTimeout(total=self.timeout),
                    read_bufsize=SSE_LINE_BYTES, auto_decompress=False,
                    trust_env=False) as session:
                listener = asyncio.create_task(_sse_listener(
                    self.base_url, self.response_topic, challenge, self.auth_token, session))
                try:
                    await asyncio.wait_for(
                        self._publish_notification(operation, challenge, session),
                        max(0, challenge.deadline - time.monotonic()))
                    # A failed/closed subscription denies immediately, not a
                    # detached task exception or a still-authoritative prompt.
                    done, _ = await asyncio.wait(
                        (challenge.future, listener),
                        timeout=max(0, challenge.deadline - time.monotonic()),
                        return_when=asyncio.FIRST_COMPLETED)
                    if challenge.future in done:
                        return (challenge.future.result() == 'approved'
                                and time.monotonic() < challenge.deadline)
                    if listener in done:
                        listener.result()
                    return False
                finally:
                    challenge.revoke()
                    listener.cancel()
                    await asyncio.gather(listener, return_exceptions=True)
        except asyncio.TimeoutError:
            logger.warning('NtfyApprover: approval expired')
            return False
        except Exception as exc:
            logger.error('NtfyApprover: approval failed: %s', exc)
            return False
        finally:
            challenge.revoke()
            challenge.future.cancel()
            if self._active.get(operation.id) is challenge:
                del self._active[operation.id]

    async def _publish_notification(self, request: OperationSnapshot, challenge: _Challenge,
                                    session: aiohttp.ClientSession) -> None:
        body = render_operation(request, operation_digest=challenge.operation_digest,
                                expires_at=challenge.expires_at).encode('ascii')
        actions = []
        for label, decision in (('Approve', 'approved'), ('Deny', 'denied')):
            payload = json.dumps(challenge.body(decision), separators=(',', ':'))
            escaped = payload.replace('"', '\\"')
            actions.append(
                f'http, {label}, {self.base_url}/{self.response_topic}, '
                f'method=POST, body="{escaped}", '
                'headers.Content-Type="application/json"')
        headers = {'Title': 'Hermes Host Command Request', 'Priority': 'default',
                   'Tags': 'warning,computer', 'Actions': '; '.join(actions),
                   'Content-Type': 'text/plain; charset=utf-8'}
        if self.auth_token:
            headers['Authorization'] = f'Bearer {self.auth_token}'
        async with session.post(f'{self.base_url}/{self.request_topic}', data=body,
                                headers=headers, allow_redirects=False) as response:
            if 300 <= response.status < 400:
                raise OSError(
                    f'ntfy publication redirect rejected ({response.status}); configure the actual endpoint')
            if not 200 <= response.status < 300:
                raise OSError('ntfy publication rejected')
            # Do not buffer/read an untrusted publication response body.


async def _sse_listener(base_url: str, response_topic: str, challenge: _Challenge,
                        auth_token: Optional[str], session: aiohttp.ClientSession) -> None:
    headers = {'Accept': 'text/event-stream'}
    if auth_token:
        headers['Authorization'] = f'Bearer {auth_token}'
    async with session.get(f'{base_url}/{response_topic}/sse', headers=headers,
                           allow_redirects=False) as response:
        if 300 <= response.status < 400:
            raise OSError(
                f'ntfy subscription redirect rejected ({response.status}); configure the actual endpoint')
        if response.status != 200:
            raise OSError('ntfy subscription rejected')
        pending = bytearray()
        event = bytearray()
        # Chunk iteration, not unbounded readline or a list of unbounded lines.
        async for chunk in response.content.iter_chunked(1024):
            for byte in chunk:
                if byte != 10:
                    if len(pending) >= SSE_LINE_BYTES:
                        raise ValueError('ntfy SSE line exceeds limit')
                    pending.append(byte)
                    continue
                line = bytes(pending).rstrip(b'\r')
                pending.clear()
                if line.startswith(b'data:'):
                    value = line[5:].lstrip(b' ')
                    if len(event) + len(value) + 1 > SSE_EVENT_BYTES:
                        raise ValueError('ntfy SSE event exceeds limit')
                    event.extend(value + b'\n')
                elif not line and event:
                    payload = event.decode('utf-8')
                    event.clear()
                    if _process_sse_event(payload, challenge):
                        return


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _process_sse_event(payload: str, challenge: _Challenge) -> bool:
    """Validate a direct v2 reply or ntfy message envelope, then consume once."""
    try:
        data = json.loads(payload, object_pairs_hook=_unique_object)
        if not isinstance(data, dict):
            return False
        if 'message' in data:
            if data.get('event') not in (None, 'message') or not isinstance(data['message'], str):
                return False
            data = json.loads(data['message'], object_pairs_hook=_unique_object)
        return isinstance(data, dict) and challenge.consume(data)
    except (ValueError, TypeError):
        return False
