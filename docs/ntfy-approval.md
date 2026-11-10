# ntfy v2 operation binding and authentication limits

Operation association and replay/collision handling are enforced. Operator
authentication is not implemented.
Binding does **not** identify a human operator or finish the independent
operator-approval security boundary. Source acceptance is not production or
handset integration acceptance and does not authorize deployment. No broker ACL
or phone-local credential behavior is verified here. Do not deploy it as a
complete security fix.

## Intentional protocol migration

HTTP/AF_VSOCK command request/result JSON and CLI approval remain compatible.
Caller `id` is result correlation only. A second active ID is denied before
approval with `error="duplicate active request id"`. IDs may be reused after
retirement, but each ntfy approval gets a new host-generated 256-bit random
challenge. An executing worker keeps its ID reserved even if its handler is
cancelled. Owned-process cleanup is described in [cancellation](cancellation.md).

The handler creates an immutable snapshot before awaiting approval: ID,
description, exact argv tuple, effective execution timeout, allowlist policy,
and trusted requester identity if available. Current transports pass **no
trusted principal** to the handler; identity is explicitly `null`, never inferred
from ID, description or additional caller JSON. The approved snapshot is also
what the executor uses. Runtime PATH/environment/filesystem state are not frozen
or authenticated; an executable-name allowlist is not a sandbox.

Notifications use the same ASCII-only JSON presentation as CLI approval. Every
argument is a separate array element: `["echo", "a b"]` differs from
`["echo", "a", "b"]`, and `""` is an empty argument. Quotes, backslashes, controls,
non-ASCII characters (including bidi/invisible characters) are JSON-escaped in
**all** snapshot fields. The description stays quoted data, never a trusted
command/timeout label. This text is a display representation, not a shell command;
execution still uses the original immutable argv without decoding the display.

Both channels explicitly label the effective execution timeout in seconds;
zero reads `unlimited (0 seconds)`. It is requester-selected, with no host maximum,
and is not a guarantee of descendant cleanup. Approval expiry is separate.
The JSON ends with `"Display complete": true`; review the complete operation,
not a collapsed notification preview, before deciding. Missing/cut-off content
must not be treated as an informed approval. Actual phone/OS rendering and
broker-specific limits still need separately authorized acceptance testing;
a source payload cannot force a handset to show all of its text.

Display admission fails closed, without a CLI decision prompt or ntfy listener/
notification, if any of these fixed limits is exceeded: 1024 code points per
string field/argument/policy entry, 64 argv elements, 64 allowlist entries, 2048
code points across all snapshot strings, or 3500 ASCII bytes for the complete
JSON display (including conservative space for ntfy binding metadata in both
channels). Timeout must be a nonnegative integer representable in 64 bits; this
is a presentation bound, not a new job-runtime ceiling. Escaping can hit the
byte limit before raw-string limits. Nothing is silently truncated or executed
from an excerpt. These deliberately conservative presentation limits may deny
long administrative requests or large allowlists. They do not bound ingress
buffering, connection counts or other pre-approval resources.

The notification also includes policy, unknown/trusted identity as available,
operation digest and expiration. The digest is SHA-256 over UTF-8 compact
sorted-key ASCII-escaped JSON of the **snapshot only**, unchanged by display
labels. It is an association value, **not a signature**.

Both action buttons POST the following shape to the response topic, with the
appropriate `decision`. Values below are generic placeholders, not credentials:

```json
{"version":2,"challenge":"<64 lowercase hex characters>","operation":"<snapshot SHA-256>","expires_at":1900000060,"decision":"approved"}
```

The daemon requires an exact matching version, challenge, digest, expiration and
one of `approved`/`denied`. Unknown fields, duplicate JSON keys, malformed values,
wrong bindings and legacy `{"id":"example","decision":"approved"}` replies are
rejected. There is no legacy fallback or selectable ID-only mode. Decisions may
arrive as direct JSON or inside an ntfy SSE `message` JSON string.

The approval timeout must be finite and positive (default 60 seconds). The window
starts before publication; a host monotonic deadline governs validity, independent
of caller time or wall-clock adjustments. Wall-clock `expires_at` is included in
the bound reply for presentation/correlation. A lock permits exactly one
consumption of each challenge. Denial, expiration, publish failure, cancellation
and final cleanup revoke it. An old listener/notification cannot authorize a
later operation even if the caller ID is reused. Publication and SSE use bounded
async I/O, cancellable DNS subprocesses and joined listener cleanup. Subscription
readiness is not a guaranteed phone-delivery acknowledgment; failed/closed streams
deny and retire authority. See [admission limits](admission-limits.md).

Old notifications/custom response senders are deliberately incompatible; discard
old pending notifications and migrate all response producers together during a
separately approved deployment. Phone rendering and the real ntfy Actions parser
have not been exercised for v2; source mocks are not handset acceptance.

## Remaining authentication and trust limitations

Anyone who can read an active notification and publish to the response topic can
copy the complete v2 payload and approve it. The challenge is public in that
notification. Random topic names and per-action tokens exposed there cannot
prove a human tap. The optional `auth_token` authenticates **daemon-to-broker**
requests, not the operator behind received decisions. No operator credential is
placed in action bodies or headers.

There is no authenticated operator verifier or phone-side signer in this source.
Broker permissions and phone-local HTTP action credentials are not established
by these tests. Deployment must isolate notification/reply authority or provide
a separately verified operator-authentication mechanism. Requester-endpoint
isolation alone does not secure reply publication.

Do not embed an operator secret in buttons or label an unverified broker trust
assumption secure. Operator authentication is not provided;
it must be independently established before claiming authenticated approval.

## Validation scope

Synthetic regression tests cover operation binding, single-use/expiry/cancellation,
malformed nonobject and wrapped JSON, duplicate keys, bounded SSE reads, closed
subscriptions and resource cleanup. They do not establish phone action behavior,
broker ACLs or human identity. See [release notes](release.md) for reproduction.
