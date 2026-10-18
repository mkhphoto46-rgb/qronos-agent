# Supervisor and user runtime (D8)

Status: **the supervisor is wired into the desktop's runtime start, stop and
shutdown (see [Product wiring](#product-wiring-supervised-desktop-runtime)).**
Nothing here is installed, registered as a service or started at sign-in. Live
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

`build_support/runtime_entry.py` exposes the supervisor as one more closed
service of the frozen executable: `supervisor`.

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
   user and is never elevated. It is created suspended, made a member of a
   desktop-owned Job Object (no limits: it never kills) and then resumed, so
   every descendant is in that Job. Stop and app exit send `shutdown`, wait at
   most 5 s, then kill the supervisor. Its kill-on-close Job Object then
   ends the bridge as well. A stop counts as done only when the supervisor
   exited AND the desktop's Job has no live process (at most 10 s more);
   otherwise the Job is kept as busy ownership and a new start is refused.
2. **Supervisor.** The supervisor owns the bridge's pipes. The first stdin
   line is the bootstrap, and then the desktop's lines are relayed. Relayed
   content is not parsed, logged or echoed in any event, apart from
   recognising `{"command":"shutdown"}`.
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
     go on. On stderr, text is decoded lossily.
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
   desktop then clears pending screen prompts.
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

## Start-up order and singletons

Every process that opens Qronos's persistent stores runs one order before its
first store opens (`core/startup_admission.py`). The frozen entry point
(`build_support/runtime_entry.py`) runs it BEFORE importing the service module,
so no import-time code can open a store first; each service's `main` calls it
again, a no-op once done.

1. **Singleton, when the service has one (the bridge).** It is OWNED: a holder
   thread wait-acquires the mutex and keeps it for the process's life
   (`core/instance_ownership.py`). Creating the mutex, finding that it exists,
   an initial-owner flag or a caller value never count as ownership. The
   release runs on that same thread, once, and a native release failure is
   recorded, never reported as clean.
2. **The writer admission:** a shared `admit_writer` on the data root, kept for
   the process's life (`core/store_admission.py`). An exclusive admission is
   refused while any writer holds it, and a writer is refused (`busy`) while
   another process holds the exclusive one.

A refusal is terminal and opens nothing:
- the bridge reports `runtime_error` with the typed code and exits with 78;
- the supervisor treats 78 as final (DEGRADED, no restart loop);
- CLI services print `{"ok": false, "code": ...}`.

Which processes run what:
- **The supervisor** opens no store: it owns `Local\QronosSupervisor` only.
- **Store-opening services** (bridge, settings, library, memory, permissions):
  the whole order.
- **Audio and doctor** open no store.

Where it applies: the installed runtime. That is always the frozen executable,
and no environment value can turn it off there.
- A source checkout keeps its data in the checkout, so it takes nothing.
- The exception: a source process whose store-path overrides point INTO an
  installed data root runs the whole order on that root. Overrides and roots are
  compared as resolved paths (junctions, symbolic links, `\\?\` and 8.3 names).
- An override that is an existing FILE with more than one name (a hard link) is
  refused with `store` before any store is opened or changed. A hard link keeps
  its own outside name, so its other names could be in installed data, and
  placing them would mean listing the file's names. The check is one link-count
  read per override; nothing is opened and no folder is listed. Single-name files
  and missing files are unaffected. Use a copy, not a link, for a private store.

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
- Rust (`backend.rs`, `mod.rs`): the closed `supervisor --desktop` command.

Test children start the base interpreter directly. A venv `python.exe` is a
launcher whose child is the real process, and the peer-id check correctly
refuses that indirection.

## What is not done

- The supervisor is wired into the desktop's start, stop and
  shutdown. The installer starts nothing at sign-in.
- No Windows service, scheduled task or sign-in entry has been created or
  exercised.
- Off Windows, sessions are refused by design.
