# Request-owned execution and cancellation

Each admitted request owns its approval authority and (after approval) its
execution until actual cleanup completes. HTTP disconnect, VSOCK EOF/loss,
transport shutdown, and handler cancellation retire the request. Pending approval
is cancelled and joined; even an approval channel returning `True` while handling
cancellation cannot execute the retired operation. Active request IDs remain
reserved through cleanup. Approval slots remain reserved while approval I/O is
still being cleaned up; they are not an execution-concurrency limit.

## Linux descendant boundary

Approved argv runs in a new session/process group. A separate, private Linux
child-subreaper process owns each request and adopts/reaps its orphaned children.
The daemon itself is **not** made a global subreaper. No service manager,
privileged cgroup setup, runtime ceiling, or background-job handle is introduced.
Linux `prctl(PR_SET_CHILD_SUBREAPER)` and readable procfs child inventory are
required; a setup failure prevents command startup.

The supported tree consists of the command and ordinary fork/exec descendants
that retain its process group, including grandchildren, orphaned children,
TERM-resistant children, and inherited stdout/stderr writers. Cancellation sends
SIGTERM to this group, allows 200 ms of cooperative cleanup, then sends SIGKILL.
The short grace permits basic exit handlers without making disconnect cleanup
wait seconds for unresponsive commands. The supervisor pins the unreaped group
leader PID until owned descendants are gone, preventing reuse of that group ID
for unrelated processes. The execution thread joins the supervisor before the
handler releases ownership, including repeated cancellation. A task/future merely
becoming cancelled is not considered process cleanup.

**Remaining gap against a literal “nothing belonging to this request survives”
guarantee:** deliberately detached descendants using `setsid()`/`setpgid()`, work
delegated to another service/container/remote host, or hostile same-user programs
attacking the supervisor are not contained. Group membership is the supported
ownership boundary, not a sandbox. Enforcing a stronger arbitrary-process job
boundary needs separately agreed cgroup/deployment privileges or a service
manager contract; this implementation does not invent one. It never searches
process names or kills an unrelated group. If an escaped writer retains output
pipes after the supervisor completes, capture stops after a 200 ms drain window
and reports incomplete output rather than waiting forever or claiming full
capture. Kernel-uninterruptible tasks may delay SIGKILL/reaping; ownership is
retained instead of falsely reporting completed cleanup. Abrupt daemon/SIGKILL,
OS crash and privileged supervisor interference are not graceful shutdown.

## Deadlines and results

A positive command timeout is an execution deadline, starting at command spawn,
not an approval deadline. It uses the same group termination/reaping path and
returns `approved=true`, no exit code, and an explicit timeout error. Already
completed side effects cannot be undone. Disconnect normally has no response;
it is not an operator denial or evidence that no side effects happened. Never
retry state-changing operations blindly after losing the connection.

`timeout=0` remains unlimited while the caller remains connected. The
`persistent_intent` marker is approval-bound descriptive metadata only and grants
**no exemption** from disconnect/deadline/shutdown cleanup. Normal completion
waits for in-bound descendants, even when the direct command has already exited
or closed its pipes; intentionally detached background supervision is not offered.
Unrestricted-command and allowlist policy are unchanged. Existing bounded disk
logs, capture failures and approval operation binding remain in effect.

Tests use bounded synthetic children and disposable loopback HTTP and TCP socket
fixtures running the VSOCK protocol. They do not establish live AF_VSOCK,
production deployment, broker/phone authentication, or hostile-process isolation.
