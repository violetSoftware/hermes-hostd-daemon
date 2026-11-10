# Declared persistent process intent

`persistent_intent` is an optional JSON boolean in command requests. Omitted or
`false` means ordinary/unmarked. `true` explicitly declares that the caller
intentionally wants a long-running process to remain running. Null, numbers,
strings, arrays and objects are rejected, not converted to booleans. The separate
Hermes plugin exposes this optional boolean through `run_on_host` and serializes
it in the command request. This requires a matching daemon: older daemons may
ignore unknown fields and cannot promise to show or bind this marker. Upgrade
source/client and daemon together under separately authorized deployment.

Example request shape (illustrative only, not a launch instruction):

```json
{"id":"intent-example","description":"Explain intended long-running work","command":["/path/to/program"],"timeout":120,"persistent_intent":true}
```

## Intent is not execution policy

The marker is **metadata only**. It does not change the requested timeout, grant
new authority, bypass approval/allowlists, detach or reparent a process, create a
service-manager job, or provide check/stop/reconnect/background-job management.
Calls still wait for execution. A finite `timeout: 120` remains 120 seconds even
when intent is true. The existing explicit `timeout: 0` unlimited option remains
independent and does not require the marker. Ordinary requests, including old
clients omitting the field, retain their existing behavior. Timeout validation
and plugin timeout conversion rules are unchanged by this feature.

Declared intent grants no exception to cancellation/cleanup. Disconnect, command
deadline and graceful shutdown terminate/reap the supported command process group
using a private Linux subreaper; see [cancellation boundary](cancellation.md),
including deliberate group-escape and delegated-work limits. Intent does not make
an unlimited request safe, supervised or durable. No mandatory runtime ceilings
or job/process quotas are supplied; broader resource/lifetime concerns remain.

## Approval binding and display

The host freezes `persistent_intent` together with argv, description, timeout,
allowlist and known/unknown requester identity in its immutable operation
snapshot. Both CLI and ntfy display that snapshot's boolean and an explicit
"Declared" or "Not declared (ordinary request)" label beside the unchanged
execution-timeout label and a metadata-only warning. Requester description text
cannot substitute a structural label: all requester strings remain ASCII-escaped
quoted JSON data. Complete-display limits still deny rather than truncate;
the added labels consume some of that same fixed display budget.

The ntfy v2 canonical operation digest includes the boolean, including false for
old requests. Actions bind the digest to the existing host-generated single-use
challenge and expiry. This changes operation digests, not the v2 decision shape.
It does **not** sign replies or authenticate the human operator. A notification
reader with reply-write access can still forge a matching approval; see
[ntfy binding and authentication limits](ntfy-approval.md).

Tests use bounded foreground commands, inert notification fixtures and temporary
stores only. They are not real persistent-service or phone/deployment acceptance.
