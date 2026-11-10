#!/usr/bin/env python3
"""hermes-hostd — host-side command execution daemon.

Listens for :class:`~models.CommandRequest` objects from a VM over a
pluggable transport (vsock or HTTP), presents each request to a
configurable approval channel (ntfy push notification or CLI prompt),
and executes approved commands via :class:`~executor.CommandExecutor`.

Usage::

    python hostd.py [--config /path/to/config.yaml]

Config selection:
  An explicit ``--config`` path is the only candidate; any failure stops startup.
  Without it, search ``./config.yaml`` then ``/etc/hermes-hostd/config.yaml``.
  If neither exists, built-in defaults still require requester authentication.

Example ``config.yaml``::

    log_level: INFO

    transport:
      type: vsock       # or "http"
      port: 9999        # vsock port  (vsock transport)
      # host: 127.0.0.1  # bind address (http transport)
      # port: 9998        # bind port    (http transport)
      # auth_token_file: /private/requester.token  # required for both transports

    approval:
      type: cli         # or "ntfy"
      timeout: 30

    executor:
      allowlist:        # omit or leave empty to allow any command
        - ls
        - echo
        - cat
        - systemctl
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path for sibling imports.
# ---------------------------------------------------------------------------

from hermes_hostd.models import CommandRequest, CommandResult, OperationSnapshot
from hermes_hostd.executor import CommandExecutor
from hermes_hostd.transport.base import TransportServer, Handler
from hermes_hostd.approval.base import ApprovalChannel

logger = logging.getLogger("hermes-hostd")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATHS = [
    os.path.join(os.getcwd(), "config.yaml"),
    "/etc/hermes-hostd/config.yaml",
]


class ConfigurationError(ValueError):
    """Safe startup diagnostic; never carries configuration values."""


def _config_error(path, reason):
    # Escape controls, retain the literal token (no resolve/expand/strip), bound display.
    label = ascii(path) if path is not None else 'discovered/default configuration'
    if len(label) > 400:
        label = label[:397] + '...'
    print(f'hostd: configuration {label}: {reason}', file=sys.stderr)


def _load_config(path: Optional[str]) -> dict[str, Any]:
    """Load exactly *path* when supplied; discover only when it is None."""
    config_path = path
    if path is None:
        config_path = next((p for p in _DEFAULT_CONFIG_PATHS if os.path.exists(p)), None)
        if config_path is None:
            logger.info("No config file found — using defaults (requester authentication still required)")
            return {}
    assert config_path is not None
    try:
        # Nonblocking open plus fstat prevents named pipes/devices from hanging startup.
        import stat
        fd = os.open(config_path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('not a regular file')
            content = stream.read()
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError('cannot read configuration (expected a readable regular UTF-8 file)') from None
    cfg = _parse_config(content)
    _validate_config(cfg)
    return cfg


def _parse_config(content):
    """Never include parser excerpts, configuration values or credentials in errors."""
    try:
        import yaml
    except ImportError:
        import json
        try:
            return json.loads(content)
        except (ValueError, RecursionError):
            raise ValueError('cannot parse configuration (use JSON or install the yaml extra for YAML)') from None
    try:
        return yaml.safe_load(content)
    except (yaml.YAMLError, ValueError, RecursionError):
        raise ValueError('cannot parse configuration (invalid YAML document)') from None


def _validate_config(cfg):
    """Check supported settings without creating stores/listeners or invoking plugins.

    Unknown extension fields remain allowed. Credentials are resolved by the
    existing transport factory at startup, not by log-management commands.
    """
    if not isinstance(cfg, dict):
        raise ValueError('configuration must be a mapping')
    for section in ('transport', 'approval', 'executor'):
        if not isinstance(cfg.get(section, {}), dict):
            raise ValueError(f'{section} must be a mapping')
    level = cfg.get('log_level', 'INFO')
    if not isinstance(level, str) or level not in ('CRITICAL', 'FATAL', 'ERROR', 'WARNING', 'WARN', 'INFO', 'DEBUG', 'NOTSET'):
        raise ValueError('invalid log_level')
    transport, approval, executor = (cfg.get(s, {}) for s in ('transport', 'approval', 'executor'))
    for section, block, default, registry in (
            ('transport', transport, 'vsock', _transport_factories),
            ('approval', approval, 'cli', _approval_factories)):
        name = block.get('type', default)
        if not isinstance(name, str) or name not in registry:
            raise ValueError(f'invalid {section} type (use an exact registered name)')
    # Custom factories own their additional settings; validate common built-ins only.
    if transport.get('type', 'vsock') in ('http', 'vsock'):
        from hermes_hostd.transport.limits import ReceiveLimits
        ReceiveLimits(**{k: transport[k] for k in ('max_request_bytes', 'receive_timeout', 'max_receiving') if k in transport})
        port = transport.get('port', 9999 if transport.get('type', 'vsock') == 'vsock' else 9998)
        maximum = 2**32 - 1 if transport.get('type', 'vsock') == 'vsock' else 65535
        if type(port) is not int or not 0 <= port <= maximum:
            raise ValueError('invalid transport port')
        if 'host' in transport and (not isinstance(transport['host'], str) or not transport['host'] or '\x00' in transport['host']):
            raise ValueError('invalid transport host')
        if type(transport.get('insecure_no_auth', False)) is not bool:
            raise ValueError('insecure_no_auth must be an explicit boolean')
        for field in ('auth_token_file', 'auth_token_env'):
            if field in transport and (not isinstance(transport[field], str) or not transport[field] or '\x00' in transport[field]):
                raise ValueError(f'invalid {field} setting')
        if 'auth_token_file' in transport and 'auth_token' in transport:
            raise ValueError('choose one requester token source')
        if 'auth_token' in transport:
            from hermes_hostd.transport.auth import validate_token
            validate_token(transport['auth_token'])
    from hermes_hostd.transport.limits import Admission
    Admission(approval.get('max_pending', 4))
    if approval.get('type', 'cli') in ('cli', 'ntfy'):
        timeout = approval.get('timeout', 30)
        if type(timeout) is not int or timeout < 0:
            raise ValueError('approval timeout must be a non-negative integer')
        if approval.get('type', 'cli') == 'ntfy':
            for field in ('base_url', 'request_topic', 'response_topic'):
                value = approval.get(field, 'https://ntfy.sh' if field == 'base_url' else None)
                if not isinstance(value, str) or not value or any(ord(c) < 33 for c in value):
                    raise ValueError(f'invalid approval {field}')
            # Validate the literal endpoint before constructing any serving components.
            # urlsplit alone accepts relative URLs and strips some control characters.
            from urllib.parse import urlsplit
            import re
            endpoint = approval.get('base_url', 'https://ntfy.sh')
            try:
                url = urlsplit(endpoint)
                valid = (endpoint.startswith(('http://', 'https://'))
                         and bool(url.hostname) and bool(url.netloc)
                         and url.username is None and url.password is None
                         and '?' not in endpoint and '#' not in endpoint
                         and '\\' not in endpoint
                         and not any(ord(c) <= 32 or ord(c) == 127 for c in endpoint)
                         and '%' not in url.netloc and not url.netloc.endswith(':')
                         and (url.port is None or 1 <= url.port <= 65535)
                         and not re.search(r'%(?![0-9a-fA-F]{2})', endpoint))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('invalid approval base_url (expected an absolute HTTP(S) endpoint)') from None
            if approval.get('auth_token') is not None and not isinstance(approval['auth_token'], str):
                raise ValueError('invalid approval auth_token')
    allowlist = executor.get('allowlist')
    if allowlist is not None and (not isinstance(allowlist, list) or
            any(not isinstance(item, str) or not item or '\x00' in item for item in allowlist)):
        raise ValueError('executor allowlist must be a list of non-empty strings or null')
    logs = executor.get('logs', {})
    if not isinstance(logs, dict):
        raise ValueError('executor logs must be a mapping')
    from hermes_hostd.disk_logs import PAGE, DEFAULT_BUDGET, DEFAULT_TAIL
    budget, tail = logs.get('budget_bytes', DEFAULT_BUDGET), logs.get('tail_bytes', DEFAULT_TAIL)
    if type(budget) is not int or budget < PAGE * 3 or budget % PAGE:
        raise ValueError('invalid log budget (multiple of 4096, minimum 12288)')
    if type(tail) is not int or not 1 <= tail <= DEFAULT_TAIL:
        raise ValueError('invalid log tail (1..16384 bytes)')
    directory = logs.get('directory')
    if directory is not None and (not isinstance(directory, str) or not directory or '\x00' in directory):
        raise ValueError('invalid log directory')


# ---------------------------------------------------------------------------
# Component factories
# ---------------------------------------------------------------------------

def _builtin_transport(cfg: dict[str, Any]) -> TransportServer:
    """Instantiate the transport server from the ``transport:`` config block."""
    tcfg: dict[str, Any] = cfg.get("transport", {})
    transport_type = tcfg.get("type", "vsock").lower()
    from hermes_hostd.transport.limits import ReceiveLimits
    limits = ReceiveLimits(**{name: tcfg[name] for name in
                             ('max_request_bytes', 'receive_timeout', 'max_receiving')
                             if name in tcfg})
    from hermes_hostd.transport.auth import token_from_config
    auth: dict[str, Any] = dict(auth_token=token_from_config(tcfg),
                insecure_no_auth=tcfg.get('insecure_no_auth', False))

    if transport_type == "vsock":
        from hermes_hostd.transport.vsock import VsockServer
        port = int(tcfg.get("port", 9999))
        logger.info("Transport: vsock port=%d", port)
        return VsockServer(port=port, limits=limits, **auth)

    elif transport_type == "http":
        from hermes_hostd.transport.http import HttpServer
        host = tcfg.get("host", "127.0.0.1")
        port = int(tcfg.get("port", 9998))
        logger.info("Transport: http %s:%d", host, port)
        return HttpServer(host=host, port=port, limits=limits, **auth)

    else:
        raise ValueError(f"Unknown transport type: {transport_type!r} (expected 'vsock' or 'http')")


def _builtin_approval(cfg: dict[str, Any]) -> ApprovalChannel:
    """Instantiate the approval channel from the ``approval:`` config block."""
    acfg: dict[str, Any] = cfg.get("approval", {})
    approval_type = acfg.get("type", "cli").lower()

    if approval_type == "cli":
        from hermes_hostd.approval.cli import CliApprover
        timeout = int(acfg.get("timeout", 30))
        logger.info("Approval: cli timeout=%ds", timeout)
        return CliApprover(timeout=timeout)

    elif approval_type == "ntfy":
        from hermes_hostd.approval.ntfy import NtfyApprover
        base_url = acfg.get("base_url", "https://ntfy.sh")
        request_topic = acfg["request_topic"]
        response_topic = acfg["response_topic"]
        timeout = int(acfg.get("timeout", 60))
        auth_token = acfg.get("auth_token") or None
        logger.info("Approval: ntfy timeout=%ds", timeout)
        return NtfyApprover(
            base_url=base_url,
            request_topic=request_topic,
            response_topic=response_topic,
            timeout=timeout,
            auth_token=auth_token,
        )

    else:
        raise ValueError(f"Unknown approval type: {approval_type!r} (expected 'cli' or 'ntfy')")


_transport_factories = {
    name: (lambda block: _builtin_transport({'transport': block}))
    for name in ('vsock', 'http')
}
_approval_factories = {
    name: (lambda block: _builtin_approval({'approval': block}))
    for name in ('cli', 'ntfy')
}


def _register(registry, name, factory):
    if not isinstance(name, str) or not name or name != name.lower():
        raise ValueError('factory name must be a non-empty lowercase string')
    if name in registry:
        raise ValueError(f'Factory already registered: {name!r}')
    if not callable(factory):
        raise TypeError('factory must be callable')
    registry[name] = factory


def register_transport(name, factory):
    """Register a trusted factory receiving the transport config block."""
    _register(_transport_factories, name, factory)


def register_approval(name, factory):
    """Register a trusted factory receiving the approval config block."""
    _register(_approval_factories, name, factory)


def _build_component(cfg, section, default, registry):
    block = dict(cfg.get(section, {}))
    name = block.get('type', default)
    if not isinstance(name, str) or name not in registry:
        raise ValueError(f'Invalid {section} type (use an exact registered name)')
    block['type'] = name
    return registry[name](block)


def _build_transport(cfg: dict[str, Any]) -> TransportServer:
    return _build_component(cfg, 'transport', 'vsock', _transport_factories)


def _build_approval_channel(cfg: dict[str, Any]) -> ApprovalChannel:
    return _build_component(cfg, 'approval', 'cli', _approval_factories)


def _build_executor(cfg: dict[str, Any]) -> CommandExecutor:
    """Instantiate the executor from the ``executor:`` config block."""
    ecfg: dict[str, Any] = cfg.get("executor", {})
    raw_allowlist = ecfg.get("allowlist")
    allowlist: Optional[list[str]] = None

    if raw_allowlist:
        allowlist = [str(x) for x in raw_allowlist]
        logger.info("Executor: allowlist=%r", allowlist)
    else:
        logger.info("Executor: no allowlist — all commands permitted")

    from hermes_hostd.disk_logs import LogStore, DEFAULT_BUDGET, DEFAULT_TAIL
    logs = ecfg.get('logs', {})
    return CommandExecutor(allowlist=allowlist,
                           log_store=LogStore(logs.get('directory'), logs.get('budget_bytes', DEFAULT_BUDGET)),
                           tail_bytes=logs.get('tail_bytes', DEFAULT_TAIL))


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _make_handler(
    approval_channel: ApprovalChannel,
    executor: CommandExecutor,
    max_pending: int = 4,
) -> Handler:
    """Return the async handler coroutine used by the transport server."""

    active_ids: dict[str, object] = {}
    executing: set[str] = set()
    from hermes_hostd.transport.limits import Admission
    pending = Admission(max_pending)

    async def process(request: OperationSnapshot) -> CommandResult:
        """Process a single :class:`CommandRequest`.

        1. Asks the approval channel for a decision.
        2. If approved, runs the command via the executor.
        3. Returns a :class:`CommandResult` in all cases.
        """
        logger.info(
            "Received request id=%a description=%a command=%a",
            request.id,
            request.description,
            request.command,
        )

        if not pending.acquire():
            return CommandResult(request.id, False, error='pending approval limit reached')
        approval = asyncio.create_task(approval_channel.request_approval(request))
        try:
            approved = await asyncio.shield(approval)
        except asyncio.CancelledError:
            approval.cancel()
            # Keep ownership/accounting during real I/O cleanup. Repeated
            # transport cancellation cannot detach an approval cleanup worker.
            while not approval.done():
                try:
                    await asyncio.shield(approval)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not approval.cancelled():
                approval.exception()
            raise
        finally:
            pending.release()

        if not approved:
            logger.info("Request %a was denied by approval channel", request.id)
            return CommandResult(
                id=request.id,
                approved=False,
                exit_code=None,
                stdout="",
                stderr="",
                error="request denied by operator",
            )

        logger.info("Request %a approved — executing", request.id)
        # Execute in the default executor (thread pool) so it doesn't
        # block the event loop.
        loop = asyncio.get_event_loop()
        from hermes_hostd.executor import ExecutionCancellation
        cancellation = ExecutionCancellation()
        worker = loop.run_in_executor(None, executor.execute, request, cancellation)
        executing.add(request.id)
        try:
            return await asyncio.shield(worker)
        except BaseException:
            cancellation.cancel()
            # Cancellation retires approval authority immediately but cannot
            # retire ownership until the actual supervisor/thread has reaped.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()
            raise
        finally:
            executing.remove(request.id)

    async def handler(request: CommandRequest) -> CommandResult:
        # Admission is atomic on the transport event loop (no intervening await).
        request_id = request.id
        if request_id in active_ids:
            return CommandResult(request_id, False, error="duplicate active request id")
        admission = object()
        active_ids[request_id] = admission
        try:
            return await process(executor.snapshot(request))
        finally:
            # A cancelled handler cannot release an ID while its thread still
            # executes, nor can old cleanup erase a newer admission of that ID.
            if active_ids.get(request_id) is admission and request_id not in executing:
                del active_ids[request_id]

    return handler


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(level_name: str) -> None:
    """Configure root logger to write to stdout."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        stream=sys.stdout,
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def _install_signal_handlers(
    server: TransportServer,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Install SIGINT / SIGTERM handlers that call :meth:`server.stop`."""

    def _handle_signal(sig_num: int) -> None:
        sig_name = signal.Signals(sig_num).name
        logger.info("Received %s — shutting down…", sig_name)
        loop.create_task(server.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            # Windows — not targeted but be defensive.
            signal.signal(sig, lambda s, f: loop.call_soon_threadsafe(_handle_signal, s))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hostd",
        description="hermes-hostd — host-side command execution daemon",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Select exactly this config file; failure is fatal (absent option: ./config.yaml then /etc/hermes-hostd/config.yaml)",
    )
    commands = parser.add_subparsers(dest='subcommand')
    token = commands.add_parser('create-token', help='Explicitly create a new private requester-token file; never print or overwrite')
    token.add_argument('path')
    logs = commands.add_parser('logs', help='Read/erase this user’s managed job logs only')
    operations = logs.add_subparsers(dest='log_operation', required=True)
    read = operations.add_parser('read')
    read.add_argument('log_id')
    read.add_argument('--stream', choices=('stdout', 'stderr'), default='stdout')
    read.add_argument('--offset', type=int, default=0)
    read.add_argument('--limit', type=int, default=8192)
    cleanup = operations.add_parser('cleanup')
    target = cleanup.add_mutually_exclusive_group(required=True)
    target.add_argument('log_id', nargs='?')
    target.add_argument('--all', action='store_true')
    return parser.parse_args()


async def _async_main(cfg: dict[str, Any]) -> None:
    """Async entry point: build components, install shutdown hooks, serve."""
    try:
        server = _build_transport(cfg)
        approval_channel = _build_approval_channel(cfg)
        executor = _build_executor(cfg)
        handler = _make_handler(approval_channel, executor,
                                max_pending=cfg.get('approval', {}).get('max_pending', 4))
    except Exception:
        # Factories may echo credential paths/values. Do not forward exceptions.
        raise ConfigurationError('cannot initialize configured components; check requester token source/insecure opt-out, approval and executor settings') from None

    loop = asyncio.get_event_loop()
    _install_signal_handlers(server, loop)

    logger.info("hermes-hostd starting up…")
    try:
        await server.start(handler)
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()
        logger.info("hermes-hostd shut down cleanly")


def main() -> None:
    """CLI entry point."""
    args = _parse_args()
    if args.subcommand == 'create-token':
        import secrets
        try:
            fd = os.open(args.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w', encoding='ascii') as stream:
                stream.write(secrets.token_urlsafe(32) + '\n')
        except (OSError, ValueError):
            print('hostd: cannot create new private requester-token file (existing files are not overwritten)', file=sys.stderr)
            sys.exit(1)
        return
    if args.subcommand == 'logs':
        import base64
        import json
        from hermes_hostd.disk_logs import LogStore, DEFAULT_BUDGET
        # Keep machine-readable stdout free of bootstrap/config diagnostics.
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
        try:
            try:
                cfg = _load_config(args.config)
            except Exception:
                _config_error(args.config, 'cannot read, parse or validate configuration; check file format and supported settings')
                sys.exit(1)
            logs = cfg.get('executor', {}).get('logs', {})
            store = LogStore(logs.get('directory'), logs.get('budget_bytes', DEFAULT_BUDGET))
            if args.log_operation == 'read':
                result = store.read(args.log_id, args.stream, args.offset, args.limit)
                result['data_base64'] = base64.b64encode(result.pop('data')).decode('ascii')
            else:
                store.cleanup(None if args.all else args.log_id)
                result = {'cleaned': 'all' if args.all else args.log_id,
                          'notice': 'active jobs may append more data after cleanup'}
            print(json.dumps(result))
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            print(f'hostd logs: {exc}', file=sys.stderr)
            sys.exit(1)
        return

    # Bootstrap logging early with a sensible default so config-load
    # messages appear.
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    try:
        cfg = _load_config(args.config)
    except Exception:
        _config_error(args.config, 'cannot read, parse or validate configuration; check file format and supported settings')
        sys.exit(1)

    # Reconfigure logging with level from config.
    log_level = cfg.get("log_level", "INFO")
    _setup_logging(log_level)

    try:
        asyncio.run(_async_main(cfg))
    except KeyboardInterrupt:
        pass  # already handled by SIGINT handler; just exit cleanly
    except ConfigurationError as exc:
        _config_error(args.config, str(exc))
        sys.exit(1)
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
