# Bounded retained command logs

## Storage and retention contract

The executor drains stdout and stderr separately in reads of at most 64 KiB,
without `communicate()`, `capture_output`, an output-sized queue, or a full-log
completion read. One private **fixed-size ring file** serves every active and
completed job in that configured store. Defaults:

```json
{"executor":{"logs":{"budget_bytes":67108864,"tail_bytes":16384}}}
```

`directory` optionally selects a host-controlled dedicated directory; otherwise
it is `$XDG_STATE_HOME/hermes-hostd/job-logs`, falling back to
`~/.local/state/hermes-hostd/job-logs`. Do not put this directory in a share,
source checkout, public artifact or an agent-readable location. All daemon
instances that are intended to share a budget must use the **same directory and
budget**, on a local filesystem with working Linux advisory `flock` and 4 KiB
allocation. Separate configured directories are separate budgets. Same-user
arbitrary commands can access/modify these files: this is not a sandbox against
the host account or independently approved destructive commands.

The budget includes the control page, all record headers, unused/padded record
space and output bytes, not just payload. It must be a multiple of 4096 and at
least 12288 bytes. `ring-v1` has exactly `budget_bytes` apparent bytes; reported
allocated data blocks are checked against the budget. It is initially sparse;
allocation/write failure is reported, not memory-buffered. Filesystem directory,
inode, journal, snapshots/CoW copies, encryption and allocator metadata are
outside application accounting; a filesystem quota is needed to constrain those
physical costs. Diagnostic/service journal logs and files created by commands
are not managed job logs. There are no per-job files or unbounded job indexes.
The store creates only this one file, never temporary rotation copies.

Each record occupies 4096 bytes, with up to 4055 payload bytes. Every append
reserves a monotonically increasing sequence and overwrites the oldest slot.
Eviction order is **capture/append order**, not wall-clock job start/completion,
with active and completed output treated equally. One busy job can evict all
other logs. Cross-stream scheduling is not an authoritative chronological merge.
Small reads waste padded space intentionally; retained payload may be much less
than 64 MiB. No minimum retained tail/window is guaranteed. Memory is bounded
per executing job/read operation, not across unlimited concurrent jobs.

Completed data survives normal daemon restarts under the same budget. Startup
checks the existing file; it never resets it just because the daemon restarted.
Records carry job ID, stream, absolute byte offset, sequence and a CRC over header
and payload. Invalid/torn records are skipped; sequence reservation prevents an
interrupted overwrite exposing an old record as new. This is best-effort
operational logging, **not an fsync-per-record, power-loss-proof audit trail**.
A damaged control page or a budget mismatch fails closed, not automatic deletion.
To change budget, stop writers, explicitly clean the old store, then remove its
empty ring and change configuration; do not resize a running store. Keep private
ancestors under host control. The final directory/file must be owned by the daemon
user and have no group/other permission; symlinks, nonregular and hard-linked
ring files are rejected. Creation uses exclusive/no-follow opens, directory FDs,
0700 directory and 0600 file permissions. Parent symlinks are not a confinement
boundary: use trusted host-local ancestors. Reads/writes/cleanup share the same
exclusive inode lock across threads and processes.

## Completion compatibility and failure

Existing `stdout`, `stderr`, `approved`, `exit_code`, `error` fields remain.
Completion includes at most `tail_bytes` raw retained bytes per stream (maximum
16 KiB), decoded UTF-8 with replacement and universal newlines. Invalid UTF-8
and a tail starting inside a multibyte character can produce replacement
characters; raw retrieval is lossless. JSON escaping/base64 can expand encoded
size but remain bounded. A short unrotated output remains unchanged.

Additive `log_id` and `output_truncated` fields identify retained data and output
loss. `log_id` is a host-generated opaque 32-character lowercase hex UUID, never
a caller path or request ID. When output was omitted, a bounded
`[hostd: output truncated/rotated; log_id=...]` line is prefixed **to that stream**,
so older clients ignoring new fields still see the notice. Tails are read at
completion; other jobs can evict them even immediately afterwards.

Setup failure prevents command start and reports that fact. Mid-command disk
full/write failure stops logging and continues draining/discarding pipes with no
fallback buffering; the result has an explicit `error` even if the command exits
0, and truncation is indicated. The actual command exit status is retained when
known. Do not equate `approved && exit_code == 0` with complete log capture; check
`error` and truncation. Some older clients' `succeeded` helper ignores `error`.
Commands may already have side effects when capture fails. Capture/read failures
must not be retried as if the operation was denied.

Timeout 0 still means unlimited while connected. Finite deadlines and request
cancellation terminate/reap the supported command process group through a private
Linux subreaper; see [cancellation and descendant boundary](cancellation.md) for
grace, disconnect semantics and deliberate escape limits. No background process
manager, automatic detachment, mandatory runtime ceiling or model-memory cap is
added. Storage pressure can backpressure command pipes; it does not limit
generation rate or solve broader command/host quotas.

## Host-local bounded access and explicit cleanup

Use the installed `hostd` as the same host user, with the same host-local config:

```sh
hostd --config /host/private/config.json logs read LOG_ID --stream stdout --offset 0 --limit 8192
hostd --config /host/private/config.json logs read LOG_ID --stream stderr --offset 0 --limit 8192
hostd --config /host/private/config.json logs cleanup LOG_ID
hostd --config /host/private/config.json logs cleanup --all
```

`LOG_ID` is replaced by the returned opaque ID; arbitrary paths are rejected.
`read` emits bounded JSON with `data_base64`, `next_offset`, `gap`, and a retention
notice. Decode base64 to recover exact bytes. Follow `next_offset` for the next
chunk. `limit` is 1..65536 raw bytes, default 8192. An offset is an absolute stream
byte offset, including evicted data, **not** a retained-file offset. `gap=true`
means data ahead of your cursor was missing. Empty data cannot distinguish an
unknown/fully-evicted ID from a currently quiet/completed job. No job-status or
listing API is implied. Concurrent writes/eviction between reads can create
gaps. IDs are locators, not authentication tokens or per-principal ACLs.

Remote retrieval/cleanup is just another command through the existing human-
approved execute interface: no new unauthenticated route exists. Use **8192-byte
or smaller chunks remotely** so base64 JSON fits within the 16 KiB execution tail;
64 KiB is for direct host-local CLI use (or a suitably smaller raw chunk).
The retrieval command's own stdout is itself logged and may evict source data;
this is not a snapshot/export guarantee. Config paths are deployment inputs,
not caller-supplied log paths. Operators must approve the exact command and
config choice; existing executable allowlists may need an explicit hostd entry.

Cleanup overwrites matching current slots (or all slots) under lock without
unlinking/replacing the live inode or resetting sequence numbers. Active jobs
can append after cleanup. For an empty store, complete/stop relevant writers
first. It does not erase the caller's returned tails, retrieval-command copies,
backups, snapshots, swap or filesystem remnants; it is **not secure erase**.
Sensitive command output persists across restart until evicted or explicitly
cleaned. No age-based deletion or guaranteed retention period is provided.
