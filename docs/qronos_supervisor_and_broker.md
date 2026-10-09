# Supervisor, user runtime and Elevated Action Broker (D8)

Status: **the supervisor is wired into the desktop's runtime start, stop and
shutdown (see [Product wiring](#product-wiring-supervised-desktop-runtime)),
and the broker holds one production action, the print-spooler restart,
reached from the person's own desktop chat turn.** Nothing here is installed, registered as a
service, started at sign-in, or exercised through a real UAC prompt. Live
acceptance on a real desktop is deferred. See
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
| `core/supervisor_relay.py` | Desktop mode: relays the desktop's pipes to the bridge, Job Object, lifecycle events | signed-in user |
| `core/action_broker.py` | The broker: verify one request, perform it, exit | elevated, via UAC |
| `core/action_broker_client.py` | `BrokerSession`: the runtime's side, from a gate grant to a result | user runtime |
| `core/uac_launch.py` | Starts the bundled broker with `ShellExecuteExW` `runas` | user runtime |
| `core/elevated_actions.py` | The spooler-restart request, typed-secret prompt, cancellation and outcomes | user runtime |
| `core/spooler_service.py` | Stops and starts the one service `Spooler` through the Service Control Manager | elevated, via UAC |

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
   can decline). The broker's command line carries only public identifiers:
   `action-broker --pipe NAME --runtime-pid PID`. The session key never goes
   on a command line, in the environment or in a log. The session accepts
   only the launched process id and releases the key to it on the pipe. It
   then runs the mutual handshake and sends one request: the typed action,
   the envelope and the person's typed secret. It reads one result and waits
   for the broker to exit.
4. **Cancellation.** Stop, Pause and shutdown are checked at every handoff
   before the request leaves: before launching, after the UAC prompt
   returns, and immediately before sending (`BrokerCancelled`). A broker
   started after a cancellation gets no key and exits on its own. Once the
   request has left it is not interrupted. The broker performs one action or
   none, and the result, or "unknown", is reported. The broker's process
   handle is released on every path.

The broker (`serve_once`) does the following, in order:

1. **Peer.** It refuses unless the named launching process runs as the same
   user and runs the broker's own installed executable. Then it connects,
   requires the pipe server to be exactly that process, receives the session
   key on the pipe, and runs the mutual handshake.
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
7a. **Typed secret, checked here.** The person's typed approval secret is
   checked in the broker itself, against the same per-profile store and
   shared lockout, whatever the permission settings say. The pipe, the key
   and the envelope are all chosen by whoever launched the broker, so a
   local program that is not Qronos could forge every one of them. It cannot
   type the person's secret. A refusal does not use up the approval.
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

**`PRODUCTION_ACTIONS` holds exactly one action: `printer.spooler_restart`.**

- **Need.** The print workflow stops with "The Windows print spooler is not
  responding". Restarting that one service is the standard recovery, and
  Windows lets only administrators do it.
- **Spec.** It has no parameters. The service is the constant `Spooler`,
  never chosen by a caller. The category is `device_control`, and the
  target is "Windows Print Spooler service (Spooler)".
- **Adapter.** `core/spooler_service.py` uses the Service Control Manager
  with query, stop and start rights only. It never runs a shell, `sc.exe`,
  `net.exe` or PowerShell.
- **Behaviour.** It stops the service, waits up to 30 s, starts it, and
  waits up to 30 s. Each failure says how far it got. Dependent services
  are never stopped, a paused service is not touched, and the service's
  configuration is never read or changed.
- **Everything else** is refused with `unknown_action`.
- **Not done.** The real restart, UAC prompt and elevated run have not been
  exercised. That is deferred live acceptance.

Adding any further action needs a reviewed `ActionSpec`, a permission
policy for its category, and live UAC acceptance.

## Product wiring (supervised desktop runtime)

Implemented in round 1 of the production wave: the desktop now reaches the
supervisor.

```text
desktop (Tauri start_runtime)
  -> qronos-runtime supervisor --desktop          core/supervisor_relay.py
       -> qronos-runtime bridge --supervised      core/runtime_bridge.py
```

1. **Startup.** `start_runtime` (`desktop/src-tauri/src/runtime/mod.rs`)
   launches the closed `Service::Supervisor` command (`backend.rs`: bundled
   executable, fixed words `supervisor --desktop`). It runs as the signed-in
   user and is never elevated. Stop and app exit send `shutdown`, wait at
   most 5 s, then kill the supervisor. Its kill-on-close Job Object then
   ends the bridge as well.
2. **Supervisor.** The supervisor owns the bridge's pipes. The first stdin
   line is the bootstrap, and then the desktop's lines are relayed. Relayed
   content is not parsed, logged or echoed in any event, apart from
   recognising `{"command":"shutdown"}`. Pairing secrets are therefore still
   routed only by the desktop's existing targeted pairing delivery.
2a. **Bounds and back-pressure.** The control loop never waits on a pipe.
   - **Output.** One writer thread writes everything the desktop reads,
     strictly in arrival order, from one queue with three kinds of entry:
     - *Lifecycle* (`supervisor_state`, `runtime_stopping`, the final
       `runtime_error`, the stall warning): never waited for and never
       dropped for space. An unread `supervisor_state` is replaced by a
       newer one. Only beyond 256 unread lifecycle entries are the oldest
       restart notices merged, into a counted notice.
     - *Notices*: one counted entry per kind, e.g. "N command(s) were not
       delivered ...". Thousands of refusals therefore cost one entry and
       can never crowd out a lifecycle event. No notice carries a command's
       or a line's content.
     - *Relayed bridge lines*: whole, valid UTF-8, at most 4 MiB, tagged
       with the bridge's generation, within a 16 MiB budget. When the budget
       is full, the relaying thread waits, which pushes back on that bridge
       and not on supervision.
   - **Generation fence.** When a bridge generation ends (restarting,
     stopped or degraded), its output gets at most 0.5 s to drain. It is
     then fenced *before* the events that announce the end. A line of an
     ended generation, including one already waiting for room, is dropped
     and counted. So nothing a dead bridge wrote can follow its restart
     notice or the next bridge's `runtime_ready`.
   - **Cut-off, invalid and oversized output.** A final line with no
     newline, a line that is not valid UTF-8, and a line over 4 MiB are
     each dropped and counted. A newline is never added to a fragment.
   - **Exit.** The output gets a 2 s drain and is then abandoned. The process
     ends with `os._exit`, never with a flush that could wait.
   - **Commands.** A command is at most 1 MiB. At most 256 commands and
     4 MiB may wait for the bridge. A bridge that stops reading fills that,
     and further commands are refused with a counted `busy` notice. Only the
     thread writing to that bridge can block, and only until that exact
     bridge is stopped.
   - **Command fate.**
     - not delivered / busy / too large / incomplete: refused before any
       byte was written, so nothing ran.
     - unconfirmed: the write failed after it may have started.
     - A command written whole may still be lost if the bridge dies; the
       restart notice says so.
     - No command is ever replayed.
   - **Stop while a bridge is still connecting.** Stop or the desktop's
     end-of-input interrupts the accept and the hello wait at once
     (`ChannelServer.interrupt`, plus the hello wait polling the stop
     event). Neither waits for the connect timeout.
   - **stderr.** It is copied through as it comes. A line holding a pairing
     URI is dropped and counted, and invalid UTF-8 is replaced.
   - **Desktop readers** (`mod.rs`): read bounded byte lines. On stdout, an
     invalid, oversized or cut-off line is skipped, and reading and routing
     go on. On stderr, text is decoded lossily and the pairing Drop rule
     applies.
3. **Health.** The bridge's peer pid is checked first, then the HMAC
   handshake, then heartbeats (every 5 s, timeout 45 s). A bridge that
   exits, closes its channel or stops beating is stopped (exact child only)
   and restarted with backoff. A 4th failure within 10 minutes leads to
   `degraded`: the desktop receives `runtime_stopping` and `runtime_error`,
   and the supervisor exits.
4. **Command-loop health.** Each heartbeat also carries `busy`: the seconds
   the bridge's command loop has spent inside the current command. At 120 s
   or more the supervisor emits one `runtime_warning` per stall. It does not
   kill the bridge for this, because the command may be part-way through a
   change. Stop still works, because the supervisor handles it without the
   bridge's command loop.
5. **Events to the desktop.** `supervisor_state` reports `starting`,
   `running`, `restarting`, `stopping`, `stopped` or `degraded`.
   `runtime_stopping` is sent when a bridge ended without being asked; the
   desktop then clears pending pairing requests and screen prompts.
   `runtime_error` is sent for commands that were not delivered (while
   restarting or degraded, or when the queue is full) and on giving up.
   Commands are never held for a later bridge, and nothing is retried.
6. **Runtime side.** `bridge --supervised` reads the bootstrap before its
   command loop and joins the supervisor; if that fails it exits (code 3)
   and never runs unsupervised. Without the flag it behaves as before.
7. **Development builds.** From source, the supervisor starts the base
   interpreter with this environment's import path. A venv `python.exe`
   launcher's pid is not the process that connects, so the peer check
   would refuse it.

**Limits.**
- Beats prove the process and its interpreter are alive.
- Command-loop progress is reported, not enforced. Stop does not depend on
  the bridge's command loop. The supervisor handles it and ends the exact
  bridge after the grace (tested with a loop stuck inside one command).
- Stop and end-of-input are also prompt while a new bridge is still
  connecting (tested with a bridge that never joins).
- Work handed to other threads, such as conversation turns and queued work,
  is not watched by the supervisor.
- The bridge is bound to the Job Object a moment after it starts, before it
  has started any sidecar.
- The bridge's standard error is copied through as it comes. If the
  desktop stops reading it, that copying thread waits, and the bridge's own
  error writes push back.
- Sign-in start, a persistent supervisor that outlives the desktop, and any
  Windows service remain installer/owner decisions. None of them exist.
- The desktop UI has no case yet for `supervisor_state` or `runtime_warning`
  (Control). `runtime_error` and `runtime_stopping` already reach existing
  handling.

**Elevated action (`core/elevated_actions.py`).** The person's own desktop
chat turn reaches it, for example "سرویس چاپ را ری‌استارت کن" or "restart
the print spooler". A strict deterministic parser is used; negations and
other text are ignored. Device Link text goes to the tool-free chat path and
never reaches it. The layers, all required, in order:

1. The permission gate must not refuse `device_control`.
2. The typed approval secret, always. This is a floor even if settings
   lowered device control to "allow" or "ask". A UAC dialog is Windows'
   consent to elevation, not Qronos's approval of the exact action.
3. A gate grant for this exact request (170 s). `BrokerSession` turns it into
   a single-use HMAC envelope.
4. The UAC prompt, which the person can decline.
5. The broker's own verification and gate re-check.

The prompt uses the existing app-workflow protocol:
- `app_workflow_approval_required` with phase `printer_spooler_restart` and
  permission `require_typed_secret`.
- The decision comes back through `qronos.app_workflow_decision`, routed by
  token.
- Prompts expire after 120 s and are revoked by Stop, Pause and shutdown.

Outcomes are reported once and never retried:
- `succeeded`.
- `failed`: the adapter's reviewed message.
- `refused`: declined, UAC declined, cancelled by Stop/Pause/shutdown
  before the request left, unavailable, forbidden, or not the installed
  build.
- `uncertain`: `BrokerOutcomeUnknown`. The request left but the answer was
  lost, so the person is told to check the service. An unexpected error
  also ends here, so every path gives exactly one visible result.

Only one elevated action runs at a time. From source, elevation is refused
before any prompt.

**Desktop contract (Control).** `desktop/src/appWorkflowApproval.ts` accepts
four phases today, so the frontend drops this prompt until Control adds
`printer_spooler_restart` with permission `require_typed_secret`. The secret
should be sent exactly as for `print`. Until then the action is reachable
in the runtime but not approvable from the UI. It fails closed: the prompt
simply expires.

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
- `tests/test_supervisor_relay.py`: relay units (framing, bounds, refused
  and dropped commands, state events, fixed bridge command). It also runs the
  supervisor as its own process (`tests/fixtures/relay_supervisor_driver.py`)
  over pipes, as the desktop does, with a disposable bridge stand-in
  (`tests/fixtures/relay_bridge_child.py`). These cover pass-through and
  orderly shutdown; the desktop closing its input; a crash loop going
  degraded; a hung bridge replaced; a command loop stuck in one command
  reported and not killed; an oversized output line; a bridge ignoring
  shutdown being terminated after the grace; and killing the supervisor
  ending its bridge through the Job Object. It also covers real full pipes:
  - a desktop that stops reading while the bridge floods. Supervision and
    Stop still finish within bounds, and the supervisor's memory stays
    bounded;
  - a bridge that stops reading its input, which gets `BUSY` refusals
    rather than a backlog, and is ended after the grace;
  - a command cut off by the end of input, which is never delivered;
  - a bridge that acts and dies before replying. Its command is never sent
    to the restarted bridge.
  Unit tests cover the byte and count budgets, events never waiting, and a
  part-way write being reported as unconfirmed. One end-to-end test runs
  `python -m core.supervisor --desktop` with the real bridge on throwaway
  state, through ping, pong and shutdown.
- `tests/test_elevated_actions.py`: the request parser; the exact production
  spec (no parameters, so a caller cannot choose the service); reviewed
  failure messages only; the SCM adapter with a fake advapi32, covering each
  stop/start/timeout/dependent/paused path and only `Spooler` opened; the
  coordinator, covering the typed-secret floor whatever the settings,
  single use, refusals before any prompt, every outcome reported once and
  never retried, UAC declined, revocation and pause; bridge routing; and,
  with real non-elevated broker children, the production spec end to end
  (only `perform` replaced by a marker write) and a lost answer after acting,
  which is reported as `BrokerOutcomeUnknown`.
- Rust (`backend.rs`, `mod.rs`): the closed `supervisor --desktop` command,
  and `runtime_stopping` ending pending pairing requests.
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

- The broker is not wired into the runtime yet. The supervisor is wired into
  the desktop's start, stop and shutdown. The installer starts nothing at
  sign-in.
- No live UAC prompt, elevated run, Windows service, scheduled task or
  sign-in entry has been created or exercised.
- `printer.spooler_restart` has never been run for real: no UAC prompt,
  elevated broker or Service Control Manager call has been exercised. The
  desktop does not yet display its prompt (Control contract above).
- An over-the-shoulder elevation, where a standard user types an
  administrator's credentials, runs the broker as another user. The broker's
  same-user check then refuses it. This fails closed, but it means such users
  cannot use elevated actions.
- **Trust boundary.** Another program running as the same user cannot get
  an elevated action without the person's typed secret, and without the
  UAC prompt. The test for this is a hostile local "runtime" with its own
  pipe, key and correctly MAC'd envelope. Nothing here defends against a
  same-user account that is already fully compromised: malware that reads
  what the person types, or injects into the Qronos runtime.
- **Profile binding.** The elevated broker takes its authority from the
  installed profile, `<Local AppData>\Qronos`. It finds that folder through
  Windows' known-folder API for its own token
  (`SHGetKnownFolderPath(FOLDERID_LocalAppData)`), never from environment
  values a launcher may have chosen, such as `QRONOS_APPROVAL_SECRET_PATH`,
  `QRONOS_DATA_ROOT` or `QRONOS_PERMISSION_AUDIT_PATH`. Its authority there
  is the approval-secret store with its lockout and audit, the single-use
  journal, and its own audit.
  - Tested: a launcher pointing those variables at its own store with
    secret B is refused, and only the profile's secret A works.
  - If the runtime was configured to keep its secret elsewhere, the broker
    refuses and fails closed.
  - Not checked: how Windows passes, or does not pass, a caller's
    environment to an elevated process. The design does not depend on it.
  - The broker's gate re-check still reads the permission policy through
    the ordinary configuration. A launcher could only make that check
    stricter or looser; the typed-secret floor binds either way.
- Off Windows, sessions are refused by design.
