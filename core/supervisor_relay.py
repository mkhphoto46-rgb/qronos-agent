"""
The supervised desktop runtime: the Supervisor standing between the desktop
and the runtime bridge.

The desktop speaks newline JSON with the runtime bridge over standard input
and output. With supervision, the desktop starts

    qronos-runtime supervisor --desktop

and the supervisor starts the bridge as its own child:

    qronos-runtime bridge --supervised

The supervisor:

    Owns the bridge's pipes. The first line on the bridge's standard input is
    the supervisor bootstrap (channel address, key, supervisor pid); after it,
    the desktop's lines are relayed to the bridge and the bridge's lines back
    to the desktop. Relayed content is never parsed — except to recognise the
    desktop's own ``shutdown`` command — and never logged or echoed in any
    event. Lines are framed, so the supervisor's own events are never spliced
    into the middle of a relayed line.

    Binds the bridge to a kill-on-close Job Object. If the supervisor dies,
    Windows ends the bridge and everything the bridge started.

    Watches the bridge over the authenticated channel (``core.supervisor``):
    peer process id first, then a mutual HMAC handshake, then heartbeats. A
    bridge that exits or stops beating is stopped — that exact child only —
    and restarted after a bounded backoff; too many restarts and the state
    becomes ``degraded``, the desktop is told, and the supervisor exits.
    Each heartbeat also says how long the bridge's command loop has been
    inside one command; a loop stuck past ``command_loop_warn_seconds`` is
    reported (``runtime_warning``) but not killed for it, because that
    command may be in the middle of changing something.

    Stops when the desktop says ``shutdown`` or closes its input: the bridge
    is asked to shut down (on its input and on the channel), given a bounded
    grace, then terminated, then killed.

Nothing the supervisor's control loop does waits on the desktop or on the
bridge's pipes:

    Everything the desktop reads is written by ONE writer thread, in arrival
    order, from one queue with two byte budgets. Lifecycle events are never
    waited for: if the desktop stops reading and their small budget is full,
    they are counted, and the count is reported once it reads again. Relayed
    bridge output has its own larger budget; when that is full, the thread
    relaying it waits, which pushes back on the bridge's own writes, not on
    supervision. On exit the queue gets a short bounded drain and is then
    abandoned.

    The desktop's commands are read by their own thread and queued under an
    item count AND a byte budget; each is at most ``MAX_COMMAND_BYTES``. A
    bridge that stops reading its input fills the queue, and further commands
    are refused out loud; the thread writing to the bridge may block, but
    only it, and only until that exact bridge is stopped.

What a command's fate is, as reported (never with its content):

    ``NOT_DELIVERED``: refused before any byte was written — no bridge could
    take it (restarting, degraded, stopping), the queue was full, it was too
    large, or the desktop's input ended in the middle of it. Nothing ran.

    ``DELIVERY_UNCONFIRMED``: writing it failed after it may have started to
    leave, so the bridge may or may not have received it.

    A command written whole may still be lost if the bridge stops before
    acting on it; the restart notice says so. No command is ever replayed,
    to a restarted bridge or otherwise.

Limits, stated plainly: the heartbeat runs on its own thread, so beats prove
the process and its interpreter are alive; the command loop's progress is
only reported, as above, and work the loop handed to other threads (a
conversation turn, queued work) is not watched by the supervisor. Stop never
depends on the bridge's command loop: the supervisor handles it and ends the
exact bridge after the grace. A bridge is bound to the Job Object a moment
after it starts, before it can have started a sidecar of its own. The bridge's
standard error is copied through as it comes; if the desktop stops reading
it, the copying thread waits and the bridge's own error writes push back.
"""

from __future__ import annotations

import enum
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import BinaryIO, Callable, Optional, Sequence

from core.supervisor import (
    Supervisor,
    SupervisorAlreadyRunning,
    SupervisorInstanceGuard,
    SupervisorPolicy,
    SupervisorState,
)

#: One bridge output line; a larger one is dropped whole and counted.
MAX_LINE_BYTES = 4 * 1024 * 1024
#: Bridge output waiting for the desktop to read it.
OUTPUT_BUDGET_BYTES = 16 * 1024 * 1024
#: Lifecycle entries waiting for the desktop. Superseded states are coalesced
#: first; only beyond this many (hours of restarts nobody read) are the oldest
#: restart notices merged into a counted notice.
MAX_LIFECYCLE_ENTRIES = 256
#: One desktop command (the largest, a 16 000-character text message, is far smaller).
MAX_COMMAND_BYTES = 1024 * 1024
#: Desktop commands waiting for the bridge to read them: both limits apply.
MAX_PENDING_COMMANDS = 256
PENDING_COMMAND_BUDGET_BYTES = 4 * 1024 * 1024
#: How long queued output may take to reach the desktop when the supervisor exits.
EXIT_DRAIN_SECONDS = 2.0
#: How long an ended generation's output may still drain before it is fenced.
GENERATION_DRAIN_SECONDS = 0.5
_READ_CHUNK = 64 * 1024
PAIRING_URI_PREFIX = b"qronos://pair?"

SHUTDOWN_LINE = b'{"command":"shutdown"}\n'

#: The desktop's supervision policy. The heartbeat timeout is generous
#: because a cold bridge start imports the whole runtime; a real hang is
#: still caught within it. Shutdown grace stays below the desktop's own wait
#: so the supervisor, not the desktop's kill, normally ends the bridge.
DESKTOP_POLICY = SupervisorPolicy(
    heartbeat_interval_seconds=5.0,
    heartbeat_timeout_seconds=45.0,
    connect_timeout_seconds=90.0,
    restart_backoff_initial_seconds=1.0,
    restart_backoff_max_seconds=30.0,
    max_restarts=3,
    restart_window_seconds=600.0,
    stable_after_seconds=120.0,
    shutdown_grace_seconds=3.0,
    terminate_grace_seconds=1.0,
    command_loop_warn_seconds=120.0,
)

JOB_MUTEX_NAME = "Local\\QronosSupervisedRuntime"

RESTARTING = (
    "The Qronos runtime stopped unexpectedly and is restarting. Commands it had already received may or may "
    "not have been carried out; none are sent again."
)
STILL_STOPPABLE = " It is not restarted for this; Stop still works."
GAVE_UP = "The Qronos runtime stopped after repeated failures. Start Qronos again to retry."

#: Counted, coalesced reports: one pending entry per kind, written with its count.
#: None of them ever carries a command's or an output line's content.
NOTICES = {
    "not_delivered": ("runtime_error", "error",
                      "{count} command(s) were not delivered because the Qronos runtime was not available; "
                      "nothing ran."),
    "busy": ("runtime_error", "error",
             "{count} command(s) were refused because the Qronos runtime is not reading commands; nothing ran."),
    "too_large": ("runtime_error", "error", "{count} command(s) were too large; they were not delivered and nothing ran."),
    "incomplete": ("runtime_error", "error",
                   "{count} command(s) were cut off when the desktop's input ended; not delivered, nothing ran."),
    "unconfirmed": ("runtime_error", "error",
                    "{count} command(s) failed part-way while being sent; each may or may not have been received. "
                    "None is sent again."),
    "output_too_large": ("runtime_warning", "warning",
                         "{count} output line(s) from the Qronos runtime were too large and were dropped."),
    "output_incomplete": ("runtime_warning", "warning",
                          "{count} output line(s) cut off when the Qronos runtime stopped were dropped."),
    "output_invalid": ("runtime_warning", "warning",
                       "{count} output line(s) from the Qronos runtime were not valid text and were dropped."),
    "old_output": ("runtime_warning", "warning",
                   "{count} output line(s) from a Qronos runtime that had already stopped were dropped."),
    "stderr_secret": ("runtime_warning", "warning",
                      "{count} diagnostic line(s) that contained a pairing secret were dropped."),
    "lifecycle_merged": ("runtime_warning", "warning",
                         "{count} older restart notice(s) were merged while the desktop was not reading."),
}

#: The pre-repair single-command messages, kept for callers that test them.
NOT_DELIVERED = NOTICES["not_delivered"][2]
BUSY = NOTICES["busy"][2]
TOO_LARGE = NOTICES["too_large"][2]
INCOMPLETE = NOTICES["incomplete"][2]
DELIVERY_UNCONFIRMED = NOTICES["unconfirmed"][2]
OUTPUT_TOO_LARGE = NOTICES["output_too_large"][2]

_STATUS = {
    SupervisorState.STARTING: "starting",
    SupervisorState.RUNNING: "running",
    SupervisorState.BACKING_OFF: "restarting",
    SupervisorState.STOPPING: "stopping",
    SupervisorState.STOPPED: "stopped",
    SupervisorState.DEGRADED: "degraded",
}


def _event_line(event_type: str, status: str, message: str) -> bytes:
    return json.dumps(
        {"eventType": event_type, "status": status, "message": message}, ensure_ascii=True,
    ).encode("ascii") + b"\n"


class Relayed(enum.Enum):
    QUEUED = "queued"
    #: The line's generation has ended: dropped and counted, never queued.
    FENCED = "fenced"
    #: The desktop side is gone.
    CLOSED = "closed"


class Outbound:
    """
    Everything the desktop reads, written by ONE thread, in arrival order.

    Three kinds of entry share one ordered queue, so their relative order is
    exactly the order they happened in:

        lifecycle  supervisor_state, runtime_stopping, the final runtime_error,
                   the stall warning. Never waited for and never dropped for
                   space; a superseded supervisor_state still unread is replaced
                   by the newer one, so the lane stays small.

        notice     a counted report ("N commands were not delivered ..."). At
                   most ONE pending entry per kind; more occurrences only raise
                   its count. Thousands of refusals therefore cost one entry and
                   cannot crowd out a lifecycle event.

        relayed    one bridge output line, tagged with its generation, within a
                   byte budget. The relaying thread waits for room (back-pressure
                   on that bridge, not on supervision). Once a generation has
                   ended (``end_generation``), any later line of it — including
                   one already waiting for room — is dropped and counted, so no
                   output of an ended generation can follow its end.
    """

    def __init__(self, stream: BinaryIO, on_broken: Callable[[], None] = lambda: None) -> None:
        self._stream = stream
        self._on_broken = on_broken
        self._cond = threading.Condition()
        self._queue: deque[list] = deque()  # [kind, payload, tag]
        self._notices: dict[str, list] = {}
        self._lifecycle = 0
        self._lifecycle_cap = MAX_LIFECYCLE_ENTRIES
        self._relayed_bytes = 0
        self._relayed_budget = OUTPUT_BUDGET_BYTES
        self._ended = 0
        self._writing = False
        self._closed = False
        threading.Thread(target=self._write_loop, name="qronos-relay-stdout", daemon=True).start()

    # ------------------------------------------------------------ entries

    def lifecycle(self, event_type: str, status: str, message: str, *, state: bool = False) -> None:
        """Queue a lifecycle event. Never waits; never dropped for space."""
        line = _event_line(event_type, status, message)
        with self._cond:
            if self._closed:
                return
            if state:
                stale = [entry for entry in self._queue if entry[0] == "lifecycle" and entry[2] == "state"]
                for entry in stale:
                    self._queue.remove(entry)
                    self._lifecycle -= 1
            self._queue.append(["lifecycle", line, "state" if state else "event"])
            self._lifecycle += 1
            while self._lifecycle > self._lifecycle_cap:
                oldest = next(entry for entry in self._queue if entry[0] == "lifecycle" and entry[2] == "event")
                self._queue.remove(oldest)
                self._lifecycle -= 1
                self._count_locked("lifecycle_merged")
            self._cond.notify_all()

    def notice(self, kind: str) -> None:
        """Count one occurrence of ``kind``. Never waits."""
        if kind not in NOTICES:
            raise ValueError(f"Unknown notice {kind!r}.")
        with self._cond:
            if self._closed:
                return
            self._count_locked(kind)
            self._cond.notify_all()

    def _count_locked(self, kind: str) -> None:
        entry = self._notices.get(kind)
        if entry is None:
            entry = ["notice", kind, 0]
            self._notices[kind] = entry
            self._queue.append(entry)
        entry[2] += 1

    def relayed(self, line: bytes, generation: int) -> Relayed:
        """Queue one complete bridge output line of ``generation``."""
        with self._cond:
            while (not self._closed and generation > self._ended and self._relayed_bytes
                   and self._relayed_bytes + len(line) > self._relayed_budget):
                self._cond.wait()
            if self._closed:
                return Relayed.CLOSED
            if generation <= self._ended:
                self._count_locked("old_output")
                self._cond.notify_all()
                return Relayed.FENCED
            self._queue.append(["relayed", line, generation])
            self._relayed_bytes += len(line)
            self._cond.notify_all()
            return Relayed.QUEUED

    def end_generation(self, generation: int) -> None:
        """No line of ``generation`` (or an earlier one) is queued after this returns."""
        with self._cond:
            self._ended = max(self._ended, generation)
            self._cond.notify_all()

    # ------------------------------------------------------------ writing

    def _take(self) -> bytes:
        kind, payload, tag = self._queue.popleft()
        if kind == "lifecycle":
            self._lifecycle -= 1
            return payload
        if kind == "notice":
            del self._notices[payload]
            event_type, status, template = NOTICES[payload]
            return _event_line(event_type, status, template.format(count=tag))
        self._relayed_bytes -= len(payload)
        return payload

    def _write_loop(self) -> None:
        while True:
            with self._cond:
                while not self._closed and not self._queue:
                    self._cond.wait()
                if self._closed:
                    return
                line = self._take()
                self._writing = True
                self._cond.notify_all()  # room for a waiting relayed() call
            broken = False
            try:
                self._stream.write(line)
                self._stream.flush()
            except (OSError, ValueError):
                broken = True
            with self._cond:
                self._writing = False
                if broken:
                    self._close_locked()
                self._cond.notify_all()
            if broken:
                self._on_broken()
                return

    def _close_locked(self) -> None:
        self._closed = True
        self._queue.clear()
        self._notices.clear()
        self._lifecycle = self._relayed_bytes = 0

    def drain(self, timeout: float) -> bool:
        """Wait at most ``timeout`` for everything queued to be written."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._closed and (self._queue or self._writing):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return not self._queue

    def close(self) -> None:
        with self._cond:
            self._close_locked()
            self._cond.notify_all()


class Delivery(enum.Enum):
    WRITTEN = "written"
    #: Nothing was written: the input was already closed.
    REFUSED = "refused"
    #: Writing failed after it may have started.
    UNCONFIRMED = "unconfirmed"


class ChildInput:
    """One bridge's standard input. Each line goes out in one write, under a lock."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._closed = False

    def write(self, line: bytes) -> Delivery:
        with self._lock:
            if self._closed:
                return Delivery.REFUSED
            try:
                self._stream.write(line)
                self._stream.flush()
                return Delivery.WRITTEN
            except (OSError, ValueError):
                self._closed = True
                return Delivery.UNCONFIRMED

    def finish(self, last_line: bytes) -> None:
        """Write a last line, then close. Later writes are refused."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._stream.write(last_line)
                self._stream.flush()
            except (OSError, ValueError):
                pass
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass


def is_shutdown_command(line: bytes) -> bool:
    """The desktop's own ``{"command": "shutdown"}``, and nothing else."""
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("command") == "shutdown"


def _is_text(line: bytes) -> bool:
    try:
        line.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


class DesktopRelay:
    """Carries the desktop's lines to the current bridge, and the bridge's back."""

    def __init__(self, desktop_in: BinaryIO, desktop_out: BinaryIO, desktop_err: Optional[BinaryIO] = None) -> None:
        self._in = desktop_in
        self.out = Outbound(desktop_out, on_broken=lambda: self._request_stop("the desktop stopped reading"))
        self._err = desktop_err
        self._err_lock = threading.Lock()
        self._cond = threading.Condition()
        self._pending: deque[bytes] = deque()
        self._pending_bytes = 0
        self._max_pending = MAX_PENDING_COMMANDS
        self._pending_budget = PENDING_COMMAND_BUDGET_BYTES
        self._input: Optional[ChildInput] = None
        self._accepting = True
        self._generation = 0
        self._pump: Optional[threading.Thread] = None
        self._stop_requested: Optional[Callable[[str], None]] = None
        self._stop_reason: Optional[str] = None

    # ----------------------------------------------------------- wiring

    def start(self, on_stop: Callable[[str], None]) -> None:
        with self._cond:
            self._stop_requested = on_stop
            early = self._stop_reason
        for target, name in ((self._read_desktop, "qronos-relay-desktop"), (self._deliver, "qronos-relay-deliver")):
            threading.Thread(target=target, name=name, daemon=True).start()
        if early is not None:
            on_stop(early)

    def _request_stop(self, reason: str) -> None:
        with self._cond:
            if self._stop_reason is not None:
                return
            self._stop_reason = reason
            callback = self._stop_requested
        if callback is not None:
            try:
                callback(reason)
            except Exception:  # noqa: BLE001 - the reader must keep reading whatever stop does
                pass

    def attach(self, child_input: ChildInput, generation: int = 0, pump: Optional[threading.Thread] = None) -> None:
        with self._cond:
            self._input = child_input
            self._generation = generation
            self._pump = pump
            self._cond.notify_all()

    def set_accepting(self, accepting: bool) -> None:
        """While not accepting, waiting and new commands are refused as not delivered."""
        dropped = 0
        with self._cond:
            self._accepting = accepting
            if not accepting:
                self._input = None
                dropped = len(self._pending)
                self._pending.clear()
                self._pending_bytes = 0
            self._cond.notify_all()
        for _ in range(dropped):
            self.out.notice("not_delivered")

    def end_generation(self) -> None:
        """
        Fence the current bridge generation's output: a bounded drain, then no
        line of it is ever queued again.
        """
        with self._cond:
            generation, pump = self._generation, self._pump
        if pump is not None:
            pump.join(GENERATION_DRAIN_SECONDS)
        self.out.end_generation(generation)

    # ------------------------------------------------------ desktop -> bridge

    def _read_desktop(self) -> None:
        oversized = False
        while True:
            try:
                line = self._in.readline(MAX_COMMAND_BYTES + 1)
            except (OSError, ValueError):
                line = b""

            if not line:
                self._request_stop("the desktop closed the runtime's input")
                return

            if oversized:
                oversized = not line.endswith(b"\n")
                continue

            if not line.endswith(b"\n"):
                if len(line) > MAX_COMMAND_BYTES:
                    self.out.notice("too_large")
                    oversized = True
                else:
                    # Only the end of input ends a line without a newline: a
                    # command cut off there is never delivered.
                    self.out.notice("incomplete")
                continue

            if not line.strip():
                continue

            if is_shutdown_command(line):
                self._request_stop("the desktop asked the runtime to stop")
                continue

            self._submit(line)

    def _submit(self, line: bytes) -> None:
        with self._cond:
            accepting = self._accepting
            if accepting and len(self._pending) < self._max_pending \
                    and self._pending_bytes + len(line) <= self._pending_budget:
                self._pending.append(line)
                self._pending_bytes += len(line)
                self._cond.notify_all()
                return
        self.out.notice("busy" if accepting else "not_delivered")

    def _deliver(self) -> None:
        while True:
            with self._cond:
                while not self._pending or (self._accepting and self._input is None):
                    self._cond.wait()
                line = self._pending.popleft()
                self._pending_bytes -= len(line)
                target = self._input if self._accepting else None
            outcome = target.write(line) if target is not None else Delivery.REFUSED
            if outcome is Delivery.REFUSED:
                self.out.notice("not_delivered")
            elif outcome is Delivery.UNCONFIRMED:
                self.out.notice("unconfirmed")

    # ------------------------------------------------------ bridge -> desktop

    def pump_output(self, stream: BinaryIO, generation: int = 0) -> None:
        """
        Forward one bridge generation's standard output: whole, bounded, valid
        UTF-8 lines only. An oversized, invalid or unterminated final line is
        dropped and counted — a newline is never added to make one whole.
        """
        partial = bytearray()
        oversized = False
        while True:
            try:
                chunk = stream.readline(_READ_CHUNK)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            if oversized:
                oversized = not chunk.endswith(b"\n")
                continue
            partial += chunk
            if len(partial) > MAX_LINE_BYTES:
                partial.clear()
                oversized = not chunk.endswith(b"\n")
                self.out.notice("output_too_large")
                continue
            if not chunk.endswith(b"\n"):
                continue
            line = bytes(partial)
            partial.clear()
            if not _is_text(line):
                self.out.notice("output_invalid")
                continue
            if self.out.relayed(line, generation) is Relayed.CLOSED:
                return
        if partial and not oversized:
            self.out.notice("output_incomplete")

    def pump_errors(self, stream: BinaryIO) -> None:
        """
        Forward the bridge's standard error as it comes. A line holding a
        pairing URI is dropped and counted, as the desktop drops it on stdout;
        invalid UTF-8 is replaced, so no reader stops on it.
        """
        while True:
            try:
                chunk = stream.readline(_READ_CHUNK)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            if PAIRING_URI_PREFIX in chunk:
                self.out.notice("stderr_secret")
                continue
            if self._err is None:
                continue
            chunk = chunk.decode("utf-8", "replace").encode("utf-8")
            with self._err_lock:
                try:
                    self._err.write(chunk)
                    self._err.flush()
                except (OSError, ValueError):
                    self._err = None

    # ----------------------------------------------------------- lifecycle

    def on_warning(self, detail: str) -> None:
        self.out.lifecycle("runtime_warning", "warning", detail[:300] + STILL_STOPPABLE)

    def on_state(self, state: SupervisorState, detail: str) -> None:
        """
        Called from the supervisor's control loop. Queues events and never
        waits on the desktop; when a generation has ended, its output is fenced
        (after at most GENERATION_DRAIN_SECONDS) BEFORE the events that say so.
        """
        if state in (SupervisorState.BACKING_OFF, SupervisorState.STOPPED, SupervisorState.DEGRADED):
            self.end_generation()
        self.set_accepting(state in (SupervisorState.STARTING, SupervisorState.RUNNING))
        self.out.lifecycle("supervisor_state", _STATUS[state], detail[:300], state=True)
        if state is SupervisorState.BACKING_OFF:
            self.out.lifecycle("runtime_stopping", "stopping", RESTARTING)
        elif state is SupervisorState.DEGRADED:
            self.out.lifecycle("runtime_stopping", "stopping", GAVE_UP)
            self.out.lifecycle("runtime_error", "error", GAVE_UP)


class RelayedChild:
    """One launched bridge: the exact process, and its relayed pipes."""

    def __init__(self, process: subprocess.Popen, child_input: ChildInput) -> None:
        self._process = process
        self._input = child_input
        self.pid = process.pid

    def poll(self) -> Optional[int]:
        return self._process.poll()

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    def wait(self, timeout: Optional[float] = None) -> int:
        return self._process.wait(timeout)

    def request_stop(self) -> None:
        """Ask on the bridge's input, without ever waiting on a full pipe."""
        threading.Thread(
            target=self._input.finish, args=(SHUTDOWN_LINE,), name="qronos-relay-stop", daemon=True,
        ).start()


class DesktopRuntimeLauncher:
    """Starts the bridge with fixed arguments and relays its pipes."""

    def __init__(self, relay: DesktopRelay, command: Sequence[str], environment: Optional[dict] = None, job=None) -> None:
        command = [str(part) for part in command]
        if not command or not Path(command[0]).is_absolute():
            raise ValueError("The runtime executable must be an absolute path.")
        self._relay = relay
        self._command = command
        self._environment = environment
        self._job = job
        self._generation = 0

    def launch(self, bootstrap: bytes) -> RelayedChild:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            env=self._environment,
            creationflags=flags,
        )
        try:
            if self._job is not None:
                self._job.bind_process(process.pid, process_handle=getattr(process, "_handle", None))
            assert process.stdin is not None
            # The bootstrap (under 4 KiB) goes into the new, empty pipe.
            process.stdin.write(bootstrap)
            process.stdin.flush()
        except Exception:
            process.kill()
            process.wait(5.0)
            raise

        self._generation += 1
        generation = self._generation
        child_input = ChildInput(process.stdin)
        pump = threading.Thread(target=self._relay.pump_output, args=(process.stdout, generation),
                                name=f"qronos-relay-out-{generation}", daemon=True)
        pump.start()
        threading.Thread(target=self._relay.pump_errors, args=(process.stderr,),
                         name=f"qronos-relay-err-{generation}", daemon=True).start()
        self._relay.attach(child_input, generation, pump)
        return RelayedChild(process, child_input)


def bridge_command() -> tuple[list[str], dict]:
    """
    The bridge this supervisor starts: the bundled executable when frozen.

    From source, the base interpreter is started directly with this
    environment's import path, because a virtual environment's
    ``python.exe`` is a launcher whose process id is not the process that
    connects — exactly what the channel's peer check refuses.
    """
    environment = dict(os.environ)

    if getattr(sys, "frozen", False):
        return [sys.executable, "bridge", "--supervised"], environment

    root = str(Path(__file__).resolve().parents[1])
    paths = [root, *(path for path in sys.path if path and os.path.isdir(path) and path != root)]
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    environment.pop("PYTHONHOME", None)
    executable = getattr(sys, "_base_executable", None) or sys.executable
    return [executable, "-u", "-m", "core.runtime_bridge", "--supervised"], environment


def _default_job():
    if sys.platform != "win32":
        return None
    from core.windows_sidecar_guard import WindowsSidecarGuard

    # Breakaway is allowed only for an application the person asked to open
    # (core.computer_executor launches it with CREATE_BREAKAWAY_FROM_JOB);
    # everything else the bridge starts still ends with this Job.
    return WindowsSidecarGuard.acquire(JOB_MUTEX_NAME, allow_breakaway=True)


def run_desktop(
    desktop_in: Optional[BinaryIO] = None,
    desktop_out: Optional[BinaryIO] = None,
    desktop_err: Optional[BinaryIO] = None,
    command: Optional[Sequence[str]] = None,
    environment: Optional[dict] = None,
    policy: SupervisorPolicy = DESKTOP_POLICY,
    job_factory: Callable[[], object] = _default_job,
    instance_guard: Callable[[], SupervisorInstanceGuard] = SupervisorInstanceGuard.acquire,
    peer_pid=None,
) -> int:
    """Supervise the desktop's bridge until the desktop stops it. 0 when stopped cleanly."""
    relay = DesktopRelay(
        desktop_in if desktop_in is not None else sys.stdin.buffer,
        desktop_out if desktop_out is not None else sys.stdout.buffer,
        desktop_err if desktop_err is not None else sys.stderr.buffer,
    )

    from core.instance_ownership import OwnershipError

    try:
        guard = instance_guard()
    except SupervisorAlreadyRunning:
        relay.out.lifecycle("runtime_error", "error", "Another Qronos runtime is already running in this Windows session.")
        relay.out.drain(EXIT_DRAIN_SECONDS)
        return 2
    except (OwnershipError, OSError):
        relay.out.lifecycle("runtime_error", "error",
                            "Windows could not establish the supervisor's ownership; nothing was started.")
        relay.out.drain(EXIT_DRAIN_SECONDS)
        return 2

    job = None
    code = 1
    try:
        job = job_factory()
        if command is None:
            command, environment = bridge_command()
        launcher = DesktopRuntimeLauncher(relay, command, environment, job)
        supervisor = Supervisor(launcher, policy, notify=relay.on_state, peer_pid=peer_pid, warn=relay.on_warning)
        relay.start(lambda reason: supervisor.stop())
        state = supervisor.run()
        code = 0 if state is SupervisorState.STOPPED else 1
    except Exception as error:  # noqa: BLE001 - reported to the desktop, then exit
        relay.out.lifecycle("supervisor_state", "degraded", f"The supervisor could not run ({type(error).__name__}).",
                            state=True)
        relay.out.lifecycle("runtime_error", "error", GAVE_UP)
    finally:
        if job is not None:
            job.close()  # the final fail-safe: nothing the bridge started outlives this, except an app the person opened
        guard.release()
        # Bounded: a desktop that stopped reading cannot hold the exit.
        relay.out.drain(EXIT_DRAIN_SECONDS)
        relay.out.close()

    return code


def exit_process(code: int) -> None:
    """
    End the supervisor process once ``run_desktop`` has cleaned up.

    The thread reading the desktop's input stays blocked in that read, and
    Python's own shutdown would then fail fatally on the input's lock. The
    output has had its bounded drain, the Job Object is closed and the
    instance released, so the process ends here directly — without a flush
    that could wait on a desktop that stopped reading.
    """
    os._exit(code)
