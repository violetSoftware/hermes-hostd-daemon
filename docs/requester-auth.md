# Requester authentication and migration

HTTP and VSOCK require a requester token by default, before command allocation,
approval or execution. Missing/empty/malformed credentials fail startup; missing
or incorrect wire credentials fail closed. No token is generated automatically.
This intentionally breaks older unauthenticated callers. Upgrade the separate
plugin and daemon together, provision the same secret at both ends, and then
start them; do not rely on a missing configuration selecting insecure defaults.

## Explicit operator provisioning

On the trusted host, create a private parent directory, then explicitly run:

```sh
hostd create-token /absolute/private/directory/requester.token
```

This uses Python `secrets.token_urlsafe(32)` (32 random bytes), creates a new 0600
file, prints no token and refuses to overwrite an existing file or symlink.
It does not start the daemon or modify configuration. Provision a copy to the
client through an operator-controlled secure file transfer outside the agent's
conversation, Git and tool results. The client must be able to read its own
requester credential: it is not an operator-approval secret or protection from a
compromised requester. Never copy ntfy operator/admin credentials to the client.
Keep parent directories trusted/private; same-UID/root processes can read tokens.
No automatic production provisioning, rotation, deployment or phone setup occurs.

Daemon configuration (nonsecret path only, for either transport):

```yaml
transport:
  type: vsock
  port: 9999
  auth_token_file: /absolute/private/directory/requester.token
```

The file must be a regular file owned by the daemon UID, with no group/other
permissions. Leaf symlinks, oversized/non-ASCII contents and failed reads are
rejected. One final LF is permitted. Tokens are 1..256 ASCII bearer characters
(letters/digits and `._~+/-`, with optional trailing `=`); short existing tokens
remain compatible but are not recommended. Use the generator for fresh secrets.
An explicit file takes precedence over the environment. Without a file, use
`HOSTD_AUTH_TOKEN` from the daemon process's secure environment, or select its
name with `transport.auth_token_env`. Existing explicit `transport.auth_token`
is retained for programmatic/private-config compatibility, never a public
example; file and inline sources together are rejected. An invalid selected
source never falls back to another source or insecure mode.

Plugin nonsecret settings are `auth_token_file` and `insecure_no_auth`, alongside
transport/port/URL. Settings come from real `ctx.get_config`, with the existing
flat `$HERMES_HOME/hostd-client.yaml` overriding them. Credential precedence is
`HOSTD_AUTH_TOKEN` (including an explicitly empty value, which fails), then
`HOSTD_AUTH_TOKEN_FILE`, then the selected `auth_token_file` path. An explicitly
empty/invalid selected file source fails closed, even with insecure mode enabled;
remove the setting entirely to select no source. Valid higher-priority overrides
still take precedence. Set secrets in
the launching process's protected environment/profile secret mechanism, not in
public plugin settings or command arguments. The plugin does not load `.env`
itself. The flat legacy parser accepts unquoted scalar paths and exact lowercase
`true`/`false`; it is not general YAML. No auth value is a model-facing tool field.
Library callers pass `auth_token=` to `VsockClient`, `HttpClient` or
`client_from_config`; legacy `http_auth_token=` remains an alias, and conflicting
arguments fail. Direct constructors do not implicitly read environment secrets.

## Explicit insecure acknowledgment

Only a deliberate operator choice may disable requester authentication:

```yaml
transport:
  type: vsock
  insecure_no_auth: true
```

For the plugin set `insecure_no_auth: true` in its nonsecret settings/flat file;
for direct library callers pass `insecure_no_auth=True`. Each endpoint warns
`INSECURE` and still requires the normal approval. Supplying a token and insecure
mode together is an error. This permits legacy ordinary no-auth operations, not
a fallback when a token is absent. Any reachable caller can submit approvals.
Never expose this mode publicly; loopback/VSOCK alone are not authentication.


## Independent security boundaries

Requester authentication proves token possession, not guest identity, honest server
execution or human approval. Strict result validation and redirect rejection do not
supply encryption. Use externally managed TLS or SSH and protect the proxy-to-daemon
hop as well as the public hop. The ntfy reply path remains unsigned: keeping the
request endpoint private does not prevent reply forgery by actors with notification
read and matching reply-write authority.
