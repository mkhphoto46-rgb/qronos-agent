# Supervisor, user runtime and Elevated Action Broker (D8)

Status: **component contracts implemented and tested with disposable fixtures; not
wired into the product.** Nothing here is installed, registered as a service,
started at sign-in, or exercised through a real UAC prompt yet. Those steps
belong to the product startup owner and to later live acceptance. See
[What is not done](#what-is-not-done).

The architecture asks for a persistent supervisor and a separate user-session
runtime joined by authenticated local IPC. It also asks for an Elevated Action
Broker that starts only when needed, through the normal UAC prompt, and
accepts one exact approved action rather than a shell command.

## Components

| Module | Role | Runs as |
| --- | --- | --- |
| `core/supervisor_ipc.py` | Authenticated, deadline-bounded local channel | both sides |
| `core/pipe_peer.py` | Read-only pipe peer process ids (`GetNamedPipe{Client,Server}ProcessId`) | both sides |
| `core/supervisor.py` | `Supervisor` (keeps the runtime alive) and `SupervisedRuntimeClient` (the runtime's half) | signed-in user |
| `core/action_broker.py` | The broker: verify one request, perform it, exit | elevated, via UAC |
| `core/action_broker_client.py` | `BrokerSession`: the runtime's side, from a gate grant to a result | user runtime |
| `core/uac_launch.py` | Starts the bundled broker with `ShellExecuteExW` `runas` | user runtime |

`build_support/runtime_entry.py` exposes them as two more closed services of
the frozen executable: `supervisor` and `action-broker`.

## Channel

All waits in the channel have deadlines.

- **Local only.** Sessions use Windows named pipes with 128-bit random names.
  The first instance is created before the peer is launched. Host/port
  addresses are refused. Off Windows a session is refused outright, because a
  stream socket's partial reads could not be bounded.
- **Identity first.** On Windows the server reads the client's process id
  from the pipe before reading any byte from it. Any process other than the
  one launched is closed, as is one whose id cannot be read, and waiting
  continues within the same deadline. An expected process id is mandatory,
  and a client checks the server's id the same way.
- **Mutual authentication.** Both directions use an HMAC-SHA256
  challenge-response with a per-session 32-byte key.
  `multiprocessing.connection`'s own authkey handshake is deliberately not
  used: it reads without a deadline. A client that connected and never
  answered kept `accept(timeout=1)` from returning for 8 seconds or more
  (reproduced). Opening a busy pipe also honours the caller's deadline instead
  of multiprocessing's fixed 20 seconds.
- **Bounded reads.** Every read follows `poll(remaining)`, and message-mode
  pipes deliver whole messages. A read that `poll` cleared therefore completes
  at once. An oversized message is refused by its length without being read.
- **Bounded messages.** Each message is a closed-schema JSON object of at most
  4 KiB. This only means one message fits an empty 8 KiB pipe buffer. It does
  **not** bound a send. An earlier version of this document claimed it did;
  that was wrong. Repeated sends to a peer that has stopped reading fill the
  buffer, and multiprocessing's `send_bytes` would then wait forever.
- **Bounded sends.** `Channel.send` takes a deadline
  (`timeout`, default 5 s). It issues an overlapped `WriteFile` and waits on
  it for at most the time left. A write still pending at the deadline is
  cancelled, the cancellation is collected, and `ChannelTimeout` is raised.
  The channel is then closed as broken, because a cancelled write may have
  left a partial message, so further sends are refused at once. Waiting
  for an earlier send on the same channel to finish counts against the same
  deadline. A send that runs out of time there has written nothing and
  leaves the channel open.
- **Finite timeouts.** Every channel timeout (`send`, `receive`, `accept`,
  `connect`) must be a finite number of seconds up to 3600. Infinity, NaN,
  booleans and other types are refused with `ValueError`.
- **Prompt close.** `Channel.close` never waits on a send. If a send is
  pending, close cancels that write. The sending thread wakes at once with
  `ChannelError` and releases the pipe handle as it leaves, so the handle is
  never closed under a write that still uses it.

## Supervisor

`Supervisor(launcher, policy)`:

- **Launch.** It launches exactly one child through a `RuntimeLauncher`. In
  production that is `BundledRuntimeLauncher(executable, fixed words)`. The
  session address and key go on the child's standard input as one bounded
  JSON line, never on the command line or in the environment.
- **Handshake.** It accepts the channel only from that child's process id,
  and requires its `hello` to name the same pid.
- **Health.** It expects heartbeats. If the child exits, closes its channel,
  sends something unexpected, or misses heartbeats for
  `heartbeat_timeout_seconds`, it is stopped and restarted.
- **Restart budget.** The backoff doubles from
  `restart_backoff_initial_seconds` up to `restart_backoff_max_seconds`. A
  run of at least `stable_after_seconds` resets it. After `max_restarts`
  within `restart_window_seconds`, the state becomes `degraded` and
  restarting stops.
- **Shutdown.** It sends `shutdown`, waits `shutdown_grace_seconds`, then
  terminates and finally kills the **exact** child. Nothing else is ever
  stopped.
- **Scope.** It imports no audio, screen, UI or dialog module (a test
  enforces this). `SupervisorInstanceGuard` allows one supervisor per Windows
  session (`Local\QronosSupervisor`).

## Elevated Action Broker

`BrokerSession.perform(action, request, grant)`:

1. **Grant.** It takes a live `security.gate.ActionGrant` for exactly that
   `ActionRequest`. It refuses anything else, and refuses a second use within
   the session before launching.
2. **Envelope.** It builds the approval envelope: action id, category,
   target, the digest of the canonical typed action, wall-clock issue and
   expiry (at most what the grant has left and at most 180 s), a nonce, and an
   HMAC under a fresh session key.
3. **Launch and connect.** It listens on a fresh pipe and launches the broker
   (`UacBrokerLauncher` in production: the normal UAC prompt, which the user
   can decline). It accepts only the launched process id, sends one request,
   reads one result, and waits for the broker to exit.

The broker (`serve_once`) does the following, in order:

1. **Peer.** It refuses unless the launching runtime runs as the same user,
   then connects back and requires the pipe server to be that runtime's pid.
2. **Envelope.** It checks the envelope shape, then the HMAC.
3. **Freshness.** Not issued in the future (5 s skew), not expired, and a
   lifetime of at most 180 s.
4. **Digest.** The approval must cover exactly this kind and these
   parameters. A change to a non-target parameter is refused too.
5. **Registry.** The kind must be in the closed registry, and the parameters
   must be exactly the spec's fields and types and pass its validation.
6. **No code or shell.** No parameter may name code, a script, an executable
   or shell syntax.
7. **Category and target.** Both are re-derived from the parameters and must
   match the approval.
8. **Permission gate.** It re-asks `security.gate`. A category the policy
   forbids is refused even with an approval.
9. **Single use.** The nonce is claimed with an atomic exclusive create in
   `ActionBroker/consumed`, so a replay to this broker or a later one is
   refused.
10. **Perform.** It performs the action and reports the result. It audits the
    kind, the action id and the outcome, never parameters, keys or MACs.

The registry refuses command-like kinds (`shell`, `cmd`, `powershell`,
`script`, `exec`, `run`, `eval`, …) and categories that are never brokered
(script execution, code, registry, boot, raw disk, security settings, backup
destruction, privilege escalation, cyber attack).

**`PRODUCTION_ACTIONS` is empty.** No current Qronos feature needs elevation,
so the shipped broker refuses every request with `unknown_action`. That
proves the mechanics fail closed. **It does not mean any privileged
capability exists or is complete.** Adding an action requires a reviewed
`ActionSpec`, a permission policy for its category, and live UAC acceptance.

## Production wiring contract (for the runtime and desktop startup owner)

These files are owned by Control (`core/runtime_bridge.py`) and the desktop
startup owner. Nothing below has been edited, and nothing calls these modules
in production yet.

1. **Startup.** Start `qronos-runtime.exe supervisor --runtime-arguments
   bridge --supervised` as the signed-in user, from the desktop or a per-user
   sign-in entry the installer owner decides. Do not use an elevated Windows
   service.
2. **Runtime side.** When `bridge` receives `--supervised`, the runtime reads
   `core.supervisor.read_bootstrap(sys.stdin.buffer)` first and starts
   `SupervisedRuntimeClient(bootstrap, on_shutdown=…)`. `on_shutdown` should
   trigger the bridge's existing orderly shutdown. Audio, UI and dialogs stay
   in the runtime and desktop.
3. **Elevated action.** For an approved action that needs elevation, the
   runtime calls
   `BrokerSession(UacBrokerLauncher(Path(sys.executable))).perform(TypedAction(kind, params), request, grant)`.
   `grant` comes from `security.gate.issue_grant` after the user's approval.
   `UacDeclined` and `BrokerUnavailable` must be shown as refusals or as
   "outcome unknown". A lost answer after the request was sent is reported as
   outcome unknown and never retried automatically.
4. **Owner decisions.** The installer owner decides how the supervisor is
   started at sign-in, if at all, and the broker's code signing.

## Tests (fixtures only)

- `tests/test_supervisor_ipc.py`: schema, size, NaN, key and address rules,
  plus adversarial peers on the test process's own pipes. These cover correct
  identity; wrong, missing and unreadable peer identity; an impostor before
  the real peer; a peer that never authenticates; a garbled handshake; a
  server that never challenges; a busy pipe; a silent authenticated peer; an
  oversized payload; a full-size send to a non-reader; repeated sends to a
  peer that stopped reading (they time out within the deadline, and the
  channel is then refused); close cancelling a send that is waiting on a full
  buffer; a short send queued behind a blocked one keeping its own deadline;
  refused non-finite timeouts; and prompt close. Each wait runs on a watchdog thread and must
  finish within its bound. The same scenarios also run across real processes:
  `tests/fixtures/ipc_send_driver.py` serves channels to disposable peer
  children (`tests/fixtures/ipc_peer_child.py`) that authenticate and then
  never read. The driver runs under a hard 60 s outer timeout. The test holds
  a handle to every peer from the moment it is reported, then verifies each
  peer has exited and terminates any that has not.
- `tests/test_supervisor.py`: pure restart-budget rules, plus disposable
  normal-user children (`tests/fixtures/supervised_child.py`). These cover a
  healthy run with clean shutdown, a crash loop going `degraded`, a hung child
  being restarted, a child that never connects, a wrong key or a stalled
  authentication, an impostor grandchild, and a bystander process left
  untouched.
- `tests/test_action_broker.py`: every verification step and refusal, the
  registry rules, single use across instances and races, and audit contents.
- `tests/test_action_broker_session.py`: a real non-elevated broker child
  (`tests/fixtures/broker_child.py`, same code, test-only action table) over a
  real pipe. It also covers the UAC launcher with every Win32 call injected:
  verb `runas`, the exact arguments, a declined prompt, and refused
  executables and arguments.

Test children start the base interpreter directly. A venv `python.exe` is a
launcher whose child is the real process, and the peer-id check correctly
refuses that indirection.

## What is not done

- Not wired into `runtime_bridge`, the desktop, or the installer (contract
  above).
- No live UAC prompt, elevated run, Windows service, scheduled task or
  sign-in entry has been created or exercised.
- `PRODUCTION_ACTIONS` is empty, so there is no elevated product capability.
- The broker's session key is on its command line. Another process of the
  same user could read it. The pid checks in both directions mean such a
  process can neither impersonate the runtime nor receive the action, but the
  key is not secret from the same user.
- Off Windows, sessions are refused by design.
