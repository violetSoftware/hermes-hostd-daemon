# Release, installation and extension contract

## Configuration

Use an explicit absolute `--config` path. When supplied, it is the **only**
candidate: missing/unreadable/non-regular files, invalid UTF-8, parse errors and
invalid supported settings stop startup with exit 1 and a bounded diagnostic on
stderr. Empty paths also fail; paths are used literally, without trimming,
expansion or normalization. No discovery/default policy is substituted. Parser
excerpts, setting values and credential-source errors are not printed.

Only when `--config` is absent does the installed launcher search cwd/config.yaml
then /etc/hermes-hostd/config.yaml; the legacy root launcher retains its
checkout-root/config.yaml first candidate. The first existing candidate must
parse and validate; it is not skipped on failure. If none exists, built-in
defaults apply, including the required requester token. There is no implicit
insecure mode. Tests inject disposable discovery locations, not system files.

Migration: fix scripts that relied on a missing/empty `--config` falling through;
omit the option only when discovery is genuinely intended. Configurations must
be mappings with mapping-valued component sections. Built-in component names are
exact lowercase registered names, numeric ports/timeouts are numbers (not
numeric strings or booleans), and allowlists are lists of nonempty strings or
null. Omitted/null/empty allowlists retain intentional unrestricted approved
command execution. Empty/null/scalar documents are invalid; `{}` is a valid
settings mapping but startup still needs a requester token or explicit insecure
opt-out. Unknown extension settings remain allowed, and trusted custom factories
validate their own additional settings; this is not a closed-schema rewrite.
Log-management commands validate the selected file but do not require a live
transport credential; `create-token` operates on its own positional path without
loading daemon configuration.

JSON works without PyYAML. This newly reviewed nonsecret foreground example is
not a production config (provision the private token file separately):

```json
{
  "transport": {"type": "http", "host": "127.0.0.1", "port": 9998, "auth_token_file": "/absolute/private/directory/requester.token"},
  "approval": {"type": "cli", "timeout": 30},
  "executor": {"allowlist": ["/usr/bin/printf", "/usr/bin/false"]}
}
```

`transport.type`: http or vsock (default); vsock uses `port` (default 9999).
HTTP uses host (default 127.0.0.1), port (9998). Both transports require a
requester token by default; see [private provisioning and explicit insecure
migration](requester-auth.md). Bind
loopback for a separately managed SSH forward or TLS reverse proxy; hostd does
not create tunnels. Authentication, routing and preventing proxy bypass remain
operator responsibilities. An unrestricted agent SSH shell bypasses hostd.

`approval.type`: cli (default) or ntfy. CLI timeout defaults to 30 seconds;
0 waits indefinitely. Keep the operator terminal open. CLI decisions require
Enter (a newline); Ctrl-D after partial input is not an approval. Canonical
editing and echo are preserved. Pending input is discarded when a prompt
finishes and before the next request is displayed: type each answer only after
its prompt appears. Timeout, empty input/EOF and invalid input deny by default.
ntfy needs request_topic and response_topic; base_url defaults to `https://ntfy.sh`
when omitted. An explicit base_url must be an absolute lowercase `http://` or
`https://` endpoint with a host and valid optional port (1–65535). Path prefixes
are supported. Relative URLs, unsupported schemes, whitespace/control characters,
backslashes, malformed escapes, URL userinfo, queries and fragments fail startup
rather than being normalized or deferred until approval. Put broker credentials
in auth_token, not the URL. This is a local syntax check, not a network reachability
or TLS/credential verification. ntfy timeout defaults to 60
seconds and must be finite and positive. auth_token authenticates daemon broker
I/O only. **ntfy decisions are unsigned, not a strong approval boundary**.
The v2 protocol rejects legacy ID-only replies and binds a one-use challenge to
an immutable operation; it does not prove operator identity. Read the
[protocol migration and authentication limits](ntfy-approval.md) before considering
handset integration. Signed decisions, verified broker ACLs and phone-local
credential behavior are not implemented or established by this source change.
Collision/binding source acceptance is separate from authentication remediation
and production/handset acceptance; it does not authorize deployment.

`executor.allowlist`: omitted or empty allows any argv. Matching is exact
argv[0] and occurs after approval; it does not restrict arguments. Command
execution uses subprocess argv unchanged, never an implicit shell. Command
and approval timeouts are separate. The weaker stdlib HTTP backend has been
removed; aiohttp is required. Choose client deadlines including approval and
command time. Disconnect cancels pending approval and terminates/reaps the
supported owned command tree; see [Linux cancellation boundary](cancellation.md).
See [admission limits](admission-limits.md)
for receive budgets, pending caps and supported fixed-length HTTP framing.

## USER service (operator opt-in; never automatic)

The template is newly authored, not a copy of any deployment unit. It uses
only systemd `%h` paths, no particular username, mount, network or topics.
Install the wheel into `~/.local/share/hermes-hostd/venv` as shown in README.
Create private `~/.config/hermes-hostd/config.json` on the destination host,
then configure and test a **non-terminal** approval channel before activation.
Do not use cli with this template: background user units have no controlling
operator tty and fail closed. ntfy is an available non-terminal choice only
with the unsigned-approval weakness above accepted; the template is not an
endorsement of ntfy isolation.

After separately approving activation on the destination, the operator can:

```sh
mkdir -p "$HOME/.config/systemd/user"
cp "$HOME/.local/share/hermes-hostd/venv/share/hermes-hostd/systemd/hermes-hostd.service" "$HOME/.config/systemd/user/hermes-hostd.service"
systemd-analyze --user verify "$HOME/.config/systemd/user/hermes-hostd.service"
systemctl --user daemon-reload
systemctl --user enable --now hermes-hostd.service
```

If paths differ, edit the copied unit explicitly. No service is installed,
activated or restarted by packaging/testing. Stop/remove using
`systemctl --user disable --now hermes-hostd.service`, remove the copied unit,
then `systemctl --user daemon-reload`. Venv and private configuration are
separate operator-managed files; removing the unit does not delete them.
User services normally depend on a user manager/session; boot-time lingering
is an administrator/operator decision, not configured by this release.

`systemd-analyze verify` checks syntax and executable presence, not approval,
network availability or safe deployment. Repeat validation on the destination
and record its results and environment limitations; no deployment validation is
implied by the template or the commands below.

## Externally registerable factories

Registrations are explicit **trusted Python startup code**, not auto-discovered
plugins and not config-provided import strings. A factory takes one shallow
copy of its component config dict (including the exact registered `type`) and
returns an instance implementing the corresponding ABC. Configuration cannot
register code. Names must be nonempty lowercase strings and unique; duplicates
including builtin names raise ValueError, noncallables raise TypeError.
Register once before main() starts serving; registration is not a hot-reload API.

```python
from hermes_hostd.hostd import main, register_transport, register_approval
from hermes_hostd.transport.http import HttpServer
from hermes_hostd.approval.cli import CliApprover

register_transport("local-http", lambda block: HttpServer(
    host="127.0.0.1", port=int(block["port"]), auth_token=block.get("auth_token")))
register_approval("terminal", lambda block: CliApprover(timeout=int(block.get("timeout", 30))))
main()  # use --config with transport.type=local-http, approval.type=terminal
```

External transports subclass `hermes_hostd.transport.base.TransportServer`:
`async start(handler)` serves until stopped, deserializes CommandRequest and
calls `await handler(request)`, then serializes CommandResult;
`async stop()` releases resources (must tolerate repeated shutdown).
External approvers subclass `hermes_hostd.approval.base.ApprovalChannel`:
`async request_approval(request) -> bool` presents the complete request to an
operator and returns True only for approval, False for denial/timeout/failure.
The handler supplies an immutable `OperationSnapshot` (tuple argv, timeout,
allowlist and currently unknown requester identity), rather than the mutable
wire `CommandRequest`; trusted custom approvers must consume those attributes
without mutating them. Direct built-in approver callers may still supply the
wire model. This internal snapshot change does not alter CLI interaction or
transport/client JSON.
Factories/approvers execute trusted host code and must own their timeout and
cancellation behavior. No executor registration was introduced.

## Reproducible validation

Set TMPDIR to a task-owned scratch directory. Use an isolated environment with the required aiohttp dependency; unittest
and Linux PTYs are used. Never test with a
production config or agent-controlled production approval terminal.

```sh
TMPDIR=/absolute/task-scratch python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Optional separate-plugin compatibility test (read-only): set
`HOSTD_PLUGIN_CLIENT=/absolute/hermes-hostd-plugin/client/client.py`; absent
this variable that single test is explicitly skipped. Tests never load plugin
runtime config. `HOSTD_TEST_PYTHON` can select a disposable Python for root
launcher tests. `HOSTD_TEST_COMMAND=/absolute/venv/bin/hostd` exercises a clean
installed wheel with fresh HOME/cwd and no PYTHONPATH.

For an offline build with locally available setuptools>=77 in a dedicated
build venv (no unrelated environment borrowing):

```sh
/path/build-venv/bin/python -m pip wheel --no-index --no-build-isolation --no-deps --wheel-dir /absolute/task-scratch/wheels .
python3 -m venv /absolute/task-scratch/installed
/absolute/task-scratch/installed/bin/python -m pip install --no-index --find-links /absolute/task-scratch/dependency-wheels /absolute/task-scratch/wheels/hermes_hostd-0.1.0rc1-py3-none-any.whl
HOSTD_TEST_COMMAND=/absolute/task-scratch/installed/bin/hostd TMPDIR=/absolute/task-scratch python3 -m unittest discover -s tests -p test_daemon_integration.py -v
```

Prepare the required aiohttp dependency wheels in the task-owned directory
under an explicitly authorized dependency-download workflow before offline
installation. No publishing, production service test or SSH round trip is
implied. The integration suite exercises aiohttp with synthetic configs and
automated PTY answers, not independent human approval. Real AF_VSOCK and broker/
phone acceptance remain separate from loopback protocol fixtures.
