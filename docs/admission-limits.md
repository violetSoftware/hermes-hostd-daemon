# Incoming-request and approval budgets

These are source defaults, not evidence that an installed daemon uses them.
No deployment or production configuration change is implied.

```yaml
transport:
  type: vsock                 # also applies to the aiohttp transport
  max_request_bytes: 65536     # JSON bytes; VSOCK newline excluded
  receive_timeout: 10         # absolute seconds from connection acceptance
  max_receiving: 16           # reject rather than queue incomplete connections
approval:
  type: cli
  timeout: 30                 # existing CLI 0 = deliberately unlimited
  max_pending: 4             # includes queued prompts and cancellation cleanup
```

All byte/count settings must be positive integers, not booleans. Receive time
must be finite and positive. Limits are host configuration, not request options.
64 KiB leaves generous JSON framing room beyond the stricter complete-display
limit; 10 seconds bounds incomplete/trickling input without requiring idle time.
16 receivers and four approvals give small installations modest parallelism
without creating an unbounded approval queue. Tune deliberately for the intended
latency and operator workload. Receiving admission is released when the one
complete bounded request is handed off; approval admission is separate.

VSOCK accepts exactly one newline-delimited frame per connection. Additional
frames/bytes are discarded, not buffered or scheduled. Oversize or overdue
frames are rejected without prompting. Excess connections close immediately.
Reads stay enabled during approval so a disconnect can cancel it.

## HTTP compatibility and rejection

Only aiohttp is supported; missing aiohttp fails startup closed. A framing gate
runs **before** its HTTP parser, even before bearer authentication. Each TCP
connection carries one request. The gate permits a fixed Content-Length body
(absence means zero), not chunked transfer, compressed bodies, duplicate headers,
Expect/100-continue handshakes, streaming uploads or keep-alive/pipelined requests.
Clients must send the whole fixed-length request without waiting for 100 Continue.
The existing bundled urllib JSON client uses compatible framing. Responses close
the connection, including errors. Extra pipelined input never reaches a second
handler. Existing method/path/authentication behavior is otherwise retained.

Header storage is capped at 16 KiB including the terminator, request line at
4096 bytes and headers at 100. Over-limit headers return 431, body lengths above
the configured maximum return 413, unsupported/ambiguous framing returns 400.
Receive deadline or receiving-cap rejection closes the connection; no approval
resource or notification is created. The absolute deadline covers headers and
body together and never resets on progress. The gate does not promise early
authentication before receiving the bounded body. Missing/incorrect bearer
returns 401 before approval, as before.

## Approval cleanup and dependency

The package now **requires aiohttp >=3.9,<4**, for ntfy even on VSOCK, and for
HTTP. The former `[http]` extra remains a no-op compatibility alias. `[yaml]`
remains optional; JSON configuration needs no PyYAML. CLI terminal I/O itself
continues to use the standard library and preserves canonical editing, fresh
newline input, serialization and intentional `timeout: 0`. Its lock wait is
cancellable and does not occupy an executor worker. Nonblocking descriptor
reads/writes are removed and closed on denial, errors, timeout or cancellation;
finite prompt deadlines also cover terminal output backpressure. Queue wait
remains separate from the per-prompt timer, as before, but is count bounded.

ntfy keeps its existing positive finite approval window (default 60 seconds),
covering publication and subscription/decision waiting. Both HTTP operations are
async, disable redirects/ambient proxies and compression, and close before
pending admission is released. SSE lines are bounded to 8192 bytes and event
data to 16384 bytes; over-limit, malformed UTF-8, subscription failure or EOF
denies. Broker publication response bodies are not accumulated. Client session
socket buffers and HTTP response headers also have aiohttp's finite limits.
A maximum of two broker connections is allowed per pending approval.

DNS uses killable local helper processes instead of uncancellable executor
getaddrinfo workers. At most two helpers per pending approval, 32 returned
addresses and 16 KiB output; a five-second DNS deadline kills/reaps incomplete
lookups. Cancellation also kills/reaps them, including cancellation during
process creation, before releasing admission. This requires local subprocess
creation with the daemon interpreter (already needed for execution). Cleanup
waits for actual OS child reaping, not an abandoned background future. As with
all process cleanup, a wedged kernel cannot be given a userspace hard guarantee;
accounting remains owned rather than falsely reporting freed capacity.

## Deliberate remaining limits

Concurrency caps are **not per-principal prompt-rate limiting**. Many fast small
requests can still generate repeated notifications. Requester identity/prompt
rate policy remains unresolved, as does authenticated operator approval (operator authentication).
CLI unlimited approval can retain a bounded slot indefinitely by design.
Trusted external transport/approval plugins must supply equivalent bounds and
cooperative cleanup; builtin guarantees do not certify arbitrary Python plugins.
These are application ingress/approval budgets, not OS socket/backlog/FD limits,
CPU quotas or execution concurrency limits. Bounded retained output is described
in [disk logs](disk-logs.md); already-running request cancellation and its Linux
descendant boundary in [cancellation](cancellation.md). Binding and revocation
prevent late approval from starting cancelled pre-approval work; they do not undo
executed side effects.
Actual VSOCK, phone, broker and deployment behavior needs separately authorized
acceptance. Offline tests use inert frames, local TCP, disposable PTYs and mocks.
