# Desktop service IPC

Library, Memory, Settings, Permissions and the local setup Doctor use one-shot,
asynchronous subprocess IPC. Installed builds still select only the bundled
`qronos-runtime` executable and a closed `Service` enum. Frontend command names,
arguments and successful JSON results are unchanged. Unsupported operations are
rejected before spawning a child; no executable, module, shell command or pipe
path can be supplied through this interface.

The request is compact JSON on stdin, followed by EOF. Each backend emits exactly
one compact JSON line on stdout and exits. Doctor uses `--models` with no stdin
request; its versioned snapshot does not have an `ok` field. The other services
must return an object with boolean `ok`. Nonzero exit, `ok=false`, malformed or
truncated frames, additional non-whitespace frames and oversized output all fail.
Errors identify the service and failure phase without echoing input, backend
stdout/stderr, exception details, private pipe names or credentials.

| Service/operation | Input limit | Stdout limit | Absolute deadline |
| --- | ---: | ---: | ---: |
| Doctor | 0 | 1 MiB | 30 seconds |
| Settings / Permissions | 1 MiB | 4 MiB | 30 seconds |
| Library reads | 8 MiB | 32 MiB | 120 seconds |
| Library mutations | 8 MiB | 64 MiB | 120 seconds |
| Library prepare / commit | 8 MiB | 64 MiB | 30 minutes |
| Library folder picker | 8 MiB | 32 MiB | 5 minutes |
| Memory | 8 MiB | 32 MiB | 6 minutes |
| Memory retry_pending / reindex | 8 MiB | 32 MiB | 30 minutes |

Every service also has a cumulative 64 KiB stderr limit. Limits apply while pipes
are drained, before unbounded collection. Large Library preparation and commit
budgets accommodate the existing recoverable file workflow; the deadline is
still finite. The single deadline starts before acquiring the Settings gate and
covers command preparation, process startup, writing, reading and child exit.
There is no automatic retry. An operation that may have mutated state and fails
after spawning reports **outcome unknown**. Inspect current state and the existing
Library recovery records before taking further action; a timeout does not prove
that files or settings were unchanged.

Input and both output streams run concurrently. After exact-child exit, output
draining has at most 50 milliseconds of grace, within the original deadline, to
consume buffered output. It does not wait for inherited EOF. A valid framed reply
and successful child exit therefore work when Library `open` leaves a user
document application holding inherited handles. Such applications are never
killed as a cleanup shortcut. Extra frames observed during bounded collection
are rejected; descendants cannot provide a subsequent accepted reply.

Windows uses private asynchronous named pipes because Tokio's ordinary Windows
child stdio uses blocking file adapters. Each launch uses system-random private
names, first-instance creation, one local instance/connection and remote-client
refusal. Parent-owned client handles are established before the child starts.
Cleanup cancels pending overlapped I/O, closes all parent handles, kills only the
exact spawned service child on failure, and reaps that child. Normal stdin
completion waits for its final overlapped write before closing. Unix uses native
asynchronous child pipes. No blocking reader/writer joins wait for descendants.

Dropping the awaiting caller signals a separate owned supervisor. It retains the
owned asynchronous Settings guard until its child has been reaped, so cancellation
cannot let another settings mutation race the first process. Preparation timeout
or cancellation discards a late prepared command without starting a service.
Debug-only Python version probes have their own two-second exact-child cleanup.
Uninterruptible operating-system filesystem calls, process creation or kernel
reaping cannot be guaranteed by a user-space timer; the protocol does not place
blocking pipe work behind that limitation. Closing a request process does not
prove that a local model server immediately released GPU resources.

The Rust tests launch owned synthetic Python children for full-duplex pipe-sized
traffic, blocked stdin/body, floods, malformed and multiple frames, cancellation,
Settings serialization and surviving descendants. They import no Qronos services
and open no real user applications. An ignored integration test can additionally
exercise an explicitly trusted frozen bundle against an internally isolated
profile:

```powershell
$env:QRONOS_TEST_BUNDLED_RUNTIME_DIRECTORY = '<trusted directory containing qronos-runtime.exe>'
cargo test --manifest-path desktop/src-tauri/Cargo.toml --offline --locked --lib runtime::service_ipc::tests::frozen_closed_services_use_private_transport_with_isolated_profile -- --ignored --exact
```

This selector exists only in test code. The test invokes Settings, Permissions,
Library and Memory snapshots plus Doctor, without native actions or inference.
