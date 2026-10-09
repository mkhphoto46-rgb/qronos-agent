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
    desktop's own ``shutdown`` command — and never logged. Lines are framed and
    bounded, so the supervisor's lifecycle events are never spliced into the
    middle of a relayed line.

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

    Reports its state to the desktop as ordinary runtime events (type
    ``supervisor_state``), plus ``runtime_stopping`` when a bridge ended
    without being asked and ``runtime_error`` when it gives up.

    Stops when the desktop says ``shutdown`` or closes its input: the bridge
    is asked to shut down (on its input and on the channel), given a bounded
    grace, then terminated, then killed.

Commands that arrive while no bridge can take them (restarting, degraded,
stopping) are not held for a later bridge: each is answered with a
``runtime_error`` event saying it was not delivered. Nothing is retried.

Limits, stated plainly: the heartbeat runs on its own thread, so beats prove
the process and its interpreter are alive; the command loop's progress is
only reported, as above, and work the loop handed to other threads (a
conversation turn, queued work) is not watched by the supervisor. A bridge is bound to the Job Object a
moment after it starts, before it can have started a sidecar of its own.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import BinaryIO, Callable, Optional, Sequence

from core.supervisor import (
    Supervisor,
    SupervisorAlreadyRunning,
    SupervisorInstanceGuard,
    SupervisorPolicy,
    SupervisorState,
)

#: A relayed line larger than this is dropped whole, and the desktop is told.
MAX_LINE_BYTES = 16 * 1024 * 1024
#: Desktop commands waiting for the bridge to read them.
MAX_PENDING_COMMANDS = 256
_READ_CHUNK = 64 * 1024

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

NOT_DELIVERED = "The Qronos runtime is not available right now; the command was not delivered."
BUSY = "The Qronos runtime is busy; the command was not delivered."
TOO_LARGE = "A command was too large and was not delivered."
OUTPUT_TOO_LARGE = "The Qronos runtime produced an output line that was too large; it was dropped."
RESTARTING = "The Qronos runtime stopped unexpectedly and is restarting."
STILL_STOPPABLE = " It is not restarted for this; Stop still works."
GAVE_UP = "The Qronos runtime stopped after repeated failures. Start Qronos again to retry."

_STATUS = {
    SupervisorState.STARTING: "starting",
    SupervisorState.RUNNING: "running",
    SupervisorState.BACKING_OFF: "restarting",
    SupervisorState.STOPPING: "stopping",
    SupervisorState.STOPPED: "stopped",
    SupervisorState.DEGRADED: "degraded",
}


class EventWriter:
    """The supervisor's standard output: whole lines only, one writer at a time."""

    def __init__(self, stream: BinaryIO, on_broken: Callable[[], None] = lambda: None) -> None:
        self._stream = stream
        self._on_broken = on_broken
        self._lock = threading.Lock()
        self._broken = False

    def line(self, data: bytes) -> None:
        if not data.endswith(b"\n"):
            data += b"\n"

        with self._lock:
            if self._broken:
                return
            try:
                self._stream.write(data)
                self._stream.flush()
                return
            except (OSError, ValueError):
                self._broken = True

        self._on_broken()

    def event(self, event_type: str, status: str, message: str) -> None:
        self.line(json.dumps(
            {"eventType": event_type, "status": status, "message": message}, ensure_ascii=True,
        ).encode("ascii"))


class ChildInput:
    """One bridge's standard input. Each line goes out in one write, under a lock."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._closed = False

    def write(self, line: bytes) -> bool:
        with self._lock:
            if self._closed:
                return False
            try:
                self._stream.write(line)
                self._stream.flush()
                return True
            except (OSError, ValueError):
                self._closed = True
                return False

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


class DesktopRelay:
    """Carries the desktop's lines to the current bridge, and the bridge's back."""

    def __init__(self, desktop_in: BinaryIO, desktop_out: BinaryIO, desktop_err: Optional[BinaryIO] = None) -> None:
        self._in = desktop_in
        self.out = EventWriter(desktop_out, on_broken=lambda: self._request_stop("the desktop stopped reading"))
        self._err = desktop_err
        self._err_lock = threading.Lock()
        self._pending: "queue.Queue[bytes]" = queue.Queue(MAX_PENDING_COMMANDS)
        self._cond = threading.Condition()
        self._input: Optional[ChildInput] = None
        self._accepting = True
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
            callback(reason)

    def attach(self, child_input: ChildInput) -> None:
        with self._cond:
            self._input = child_input
            self._cond.notify_all()

    def set_accepting(self, accepting: bool) -> None:
        """While not accepting, waiting and new commands are answered as not delivered."""
        dropped = 0
        with self._cond:
            self._accepting = accepting
            if not accepting:
                self._input = None
                while True:
                    try:
                        self._pending.get_nowait()
                    except queue.Empty:
                        break
                    dropped += 1
            self._cond.notify_all()
        for _ in range(dropped):
            self.out.event("runtime_error", "error", NOT_DELIVERED)

    # ------------------------------------------------------ desktop -> bridge

    def _read_desktop(self) -> None:
        oversized = False
        while True:
            try:
                line = self._in.readline(MAX_LINE_BYTES + 1)
            except (OSError, ValueError):
                line = b""

            if not line:
                self._request_stop("the desktop closed the runtime's input")
                return

            if oversized or (len(line) > MAX_LINE_BYTES and not line.endswith(b"\n")):
                if not oversized:
                    self.out.event("runtime_error", "error", TOO_LARGE)
                oversized = not line.endswith(b"\n")
                continue

            if not line.strip():
                continue

            if is_shutdown_command(line):
                self._request_stop("the desktop asked the runtime to stop")
                continue

            self._submit(line if line.endswith(b"\n") else line + b"\n")

    def _submit(self, line: bytes) -> None:
        with self._cond:
            accepting = self._accepting
            if accepting:
                try:
                    self._pending.put_nowait(line)
                    return
                except queue.Full:
                    pass
        self.out.event("runtime_error", "error", BUSY if accepting else NOT_DELIVERED)

    def _deliver(self) -> None:
        while True:
            line = self._pending.get()
            with self._cond:
                while self._accepting and self._input is None:
                    self._cond.wait()
                target = self._input if self._accepting else None
            if target is None or not target.write(line):
                self.out.event("runtime_error", "error", NOT_DELIVERED)

    # ------------------------------------------------------ bridge -> desktop

    def pump_output(self, stream: BinaryIO) -> None:
        """Forward the bridge's standard output, one whole bounded line at a time."""
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
                self.out.event("runtime_error", "error", OUTPUT_TOO_LARGE)
                continue
            if chunk.endswith(b"\n"):
                self.out.line(bytes(partial))
                partial.clear()
        if partial and not oversized:
            self.out.line(bytes(partial))

    def pump_errors(self, stream: BinaryIO) -> None:
        """Forward the bridge's standard error as it comes; nothing else writes there."""
        while True:
            try:
                chunk = stream.readline(_READ_CHUNK)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            if self._err is None:
                continue
            with self._err_lock:
                try:
                    self._err.write(chunk)
                    self._err.flush()
                except (OSError, ValueError):
                    self._err = None

    # ----------------------------------------------------------- lifecycle

    def on_warning(self, detail: str) -> None:
        self.out.event("runtime_warning", "warning", detail[:300] + STILL_STOPPABLE)

    def on_state(self, state: SupervisorState, detail: str) -> None:
        self.set_accepting(state in (SupervisorState.STARTING, SupervisorState.RUNNING))
        self.out.event("supervisor_state", _STATUS[state], detail[:300])
        if state is SupervisorState.BACKING_OFF:
            self.out.event("runtime_stopping", "stopping", RESTARTING)
        elif state is SupervisorState.DEGRADED:
            self.out.event("runtime_stopping", "stopping", GAVE_UP)
            self.out.event("runtime_error", "error", GAVE_UP)


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
            process.stdin.write(bootstrap)
            process.stdin.flush()
        except Exception:
            process.kill()
            process.wait(5.0)
            raise

        child_input = ChildInput(process.stdin)
        for target, stream, name in (
            (self._relay.pump_output, process.stdout, "qronos-relay-out"),
            (self._relay.pump_errors, process.stderr, "qronos-relay-err"),
        ):
            threading.Thread(target=target, args=(stream,), name=name, daemon=True).start()
        self._relay.attach(child_input)
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

    return WindowsSidecarGuard.acquire(JOB_MUTEX_NAME)


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

    try:
        guard = instance_guard()
    except SupervisorAlreadyRunning:
        relay.out.event("runtime_error", "error", "Another Qronos runtime is already running in this Windows session.")
        return 2

    job = None
    try:
        job = job_factory()
        if command is None:
            command, environment = bridge_command()
        launcher = DesktopRuntimeLauncher(relay, command, environment, job)
        supervisor = Supervisor(launcher, policy, notify=relay.on_state, peer_pid=peer_pid, warn=relay.on_warning)
        relay.start(lambda reason: supervisor.stop())
        state = supervisor.run()
    except Exception as error:  # noqa: BLE001 - reported to the desktop, then exit
        relay.out.event("supervisor_state", "degraded", f"The supervisor could not run ({type(error).__name__}).")
        relay.out.event("runtime_error", "error", GAVE_UP)
        return 1
    finally:
        if job is not None:
            job.close()  # the final fail-safe: nothing the bridge started outlives this
        guard.release()

    return 0 if state is SupervisorState.STOPPED else 1


def exit_process(code: int) -> None:
    """
    End the supervisor process once ``run_desktop`` has cleaned up.

    The thread reading the desktop's input stays blocked in that read, and
    Python's own shutdown would then fail fatally on the input's lock. Every
    event has already been flushed, the Job Object closed and the instance
    released, so the process ends here directly.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError, AttributeError):
            pass
    os._exit(code)
