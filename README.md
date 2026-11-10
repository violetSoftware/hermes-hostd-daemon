# hermes-hostd

Host-side approved **argv** execution bridge, with HTTP or AF_VSOCK transport
and CLI or ntfy approval. No implicit shell, typed executor, signed approval,
or SSH lifecycle management is provided.

## Install and run

Python 3.10+ and aiohttp >=3.9,<4 are required. Installing the release wheel
installs aiohttp, including for ntfy over VSOCK. Use a dedicated venv:

```sh
python3 -m venv "$HOME/.local/share/hermes-hostd/venv"
"$HOME/.local/share/hermes-hostd/venv/bin/python" -m pip install /path/to/hermes_hostd-0.1.0rc1-py3-none-any.whl
"$HOME/.local/share/hermes-hostd/venv/bin/hostd" --config /absolute/path/to/your-config.json
```

Create a private JSON configuration using the documented schema in
[release and installation notes](docs/release.md). Optional package extras:
`hermes-hostd[yaml]` supplies PyYAML; JSON needs no PyYAML. The former
`hermes-hostd[http]` extra remains a compatibility alias. An explicit `--config`
path must exist, parse and validate or startup exits 1; it never falls through
to discovery/defaults. Discovery is only for an absent option; see migration
notes in [configuration selection](docs/release.md#configuration). The weaker stdlib HTTP
fallback is disabled; missing aiohttp fails HTTP startup closed.

Canonical installed modules are under `hermes_hostd`; `hostd` and
`python -m hermes_hostd` start the daemon. The root `hostd.py`, `models.py`,
`executor.py`, `approval/`, and `transport/` remain compatibility paths for
source-checkout users. Retained client directories are not shipped in the wheel;
the canonical Hermes plugin is the separate `hermes-hostd-plugin` repository.
Keep `src/` with the root wrappers; copying an old root module alone is no longer
sufficient. Installed imports do not expose generic top-level module names.

## Command transport protocol

HTTP: `POST /execute`, JSON request and JSON result. Required bearer
authentication returns 401 before approval. Both transports fail closed without
credentials unless explicitly configured insecure. See [requester authentication
and migration](docs/requester-auth.md). Invalid JSON/payload returns 400.
AF_VSOCK: one newline-terminated JSON request and response per connection;
requires a supporting Linux hypervisor. It is not an IP transport.

```json
{"id":"example-request","description":"Print a diagnostic","command":["/usr/bin/printf","hello\\n"],"timeout":30}
```

```json
{"id":"example-request","approved":true,"exit_code":0,"stdout":"hello\n","stderr":"","error":null}
```

`timeout` is the command timeout; 0 means unlimited. Approval has its own
configured timeout. Denied/timed-out approval returns `approved=false`,
`exit_code=null`, `error="request denied by operator"`. A failed executable may
have `approved=true` and a nonzero exit code. Approval happens **before** the
executor allowlist check; a later allowlist rejection returns `approved=false`.
The allowlist compares argv[0], not arguments, and is not a sandbox.

Optional `persistent_intent: true` declares that the caller intentionally wants
a long-running process to remain running. Omitted/false is ordinary; other types
are rejected. This is approval-bound **metadata only**, not background execution:
timeouts (including explicit 0), cancellation/cleanup and authority are unchanged.
See [intent versus lifetime behavior](docs/persistent-intent.md) and
[request cancellation and Linux descendant boundary](docs/cancellation.md).
Disconnect/deadline cleanup does not establish hostile-process containment or
background supervision.

See [incoming-request and approval budgets](docs/admission-limits.md) for
finite defaults, configuration, rejection and fixed-length HTTP framing.
See [bounded retained job logs](docs/disk-logs.md) for the aggregate 64 MiB disk
budget, persistent sensitive-output retention, bounded stream tails and host-local
chunk retrieval/cleanup. Results add `log_id` and `output_truncated`; older
clients also see inline truncation notices.

## Approval and service limits

CLI approval uses an operator-controlled `/dev/tty` and must run in the
foreground terminal. Ordinary background services do not provide it and deny
requests. Automated PTY tests verify implementation, not independent human
approval or isolation. Both built-in approvers show lossless ASCII-escaped JSON
argv (including empty arguments), separate quoted descriptions and explicit
finite/unlimited execution timeouts. Oversized complete displays are denied,
never truncated; see [presentation limits and review guidance](docs/ntfy-approval.md#intentional-protocol-migration).

The newly authored portable USER service template is
`packaging/systemd/hermes-hostd.service`, also included in the wheel. Follow
[installation notes](docs/release.md) before using it. A non-terminal approver is
required. **ntfy accepts unsigned decisions and is NOT a strong approval boundary**:
any party able to read an active notification and publish its v2 decision can
forge approval. Host-generated single-use challenges bind each decision to an
immutable operation, but do **not** authenticate the operator. Legacy ID-only
replies are rejected. See [ntfy protocol migration and authentication limits](docs/ntfy-approval.md).
Operation binding does not supply operator authentication.
Source acceptance does not establish production protection.
Do not use random topics or network encryption as a substitute for operator
identity. Keep daemon code/config/approval credentials outside agent-writable
or agent-readable paths when they enforce host approval.

## Extensibility and tests

Trusted external wrappers can register transport and approval factories without
editing hostd wiring; see [factory contract and validation](docs/release.md).
The executor remains argv-based. Tests launch disposable real daemons on
loopback, create synthetic JSON configs in TMPDIR, and exercise approval,
denial, command failure, command timeout, approval timeout/recovery, malformed
payloads and bearer rejection. No production deployment is tested or changed.

## Candidate status and deployment boundary

This is the initial coordinated **0.1.0rc1** public-source candidate. Earlier
private checkpoints used different versions; this is not a published upgrade.
Copyright 2026 Violet Software. Distributed under the [MIT License](LICENSE).
Install from the local source/artifact; no public repository URL is assumed.

Linux is the implementation target (Linux process groups/subreaper, flock and
PTY behavior). Python metadata permits >=3.10; candidate checks use Python 3.14.9
and aiohttp 3.14.5, not a full interpreter/dependency/platform matrix. No native
host/hypervisor AF_VSOCK, phone, broker ACL, SSH or production deployment is
established by synthetic loopback/socket/PTY tests. Separate exact-artifact review
is required before this local checkpoint is accepted.

Keep deployed executable, dependencies, configuration and approval credentials
outside directly requester-writable development shares. Run with only the account
authority you intend to grant through approval; approved arbitrary commands can
change files, mount writable host data, detach work or launch external services.
There is no command sandbox or overall CPU/memory/job quota. Ordinary process-group
cleanup is not deliberate-escape containment and cannot undo completed effects.

Both HTTP and VSOCK require requester credentials unless explicitly configured
`insecure_no_auth: true`. That opt-out warns and removes requester authentication;
missing credentials never select it implicitly. Tokens authenticate possession,
not the human approver. HTTP needs external TLS/reverse proxy or a secure tunnel
when crossing an untrusted network; protect **every hop**, including any
proxy-to-daemon link, and prevent direct bypass. VSOCK is not IP and supplies no
native encryption.

ntfy decisions are unsigned. Reading an active notification plus publishing a
matching reply permits forged approval. Exclusive requester access alone does
not protect that reply path. Broker/notification/reply access isolation is the
operator's responsibility; random topics and daemon broker credentials do not
prove human intent. Concurrency/receive bounds do not rate-limit notification spam.

Command descriptions/argv reach approval readers and possibly a broker. Output
persists in private bounded logs and may reach the requester/model provider.
Escaping is not redaction: avoid secrets in argv/descriptions, keep log directories
private, and review commands for intentional secret output. Requester transport
tokens are not command fields and must not enter approval payloads or diagnostics.
