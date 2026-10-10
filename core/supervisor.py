"""
The Qronos Supervisor: keeps the user runtime alive, and nothing else.

``docs/qronos_product_architecture.md`` asks for "a persistent supervisor
process and a separate user-session runtime", joined by "authenticated local
IPC", where the supervisor "must not directly own the interactive desktop UI,
microphone, or user confirmation dialogs". This module is that supervisor:

    It launches exactly one child — the bundled Qronos runtime — and hands it a
    fresh per-session channel key on its standard input, never on the command
    line or in the environment.

    It accepts one authenticated connection on a fresh named pipe, and only
    from that exact child process (checked by process id on Windows).

    It expects a heartbeat. A child that exits, or stops beating for
    ``heartbeat_timeout_seconds``, is stopped and restarted after a bounded
    exponential backoff. Too many restarts within a window and it stops
    restarting and reports ``degraded`` instead of spinning.

    Stopping is orderly: ask the child to shut down, wait a grace period,
    terminate that exact child, then kill it. Nothing else is ever stopped.

It imports no audio, screen, UI or dialog module (a test enforces it), runs as
the signed-in user, and installs nothing: registering it as a Windows service
or a scheduled task is an installer decision made later and elsewhere.

``SupervisedRuntimeClient`` is the runtime's half: connect with the bootstrap
from standard input, say hello, beat, and obey "shut down".
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Callable, Optional, Protocol, Sequence

from core.startup_admission import STARTUP_REFUSED_EXIT
from core.supervisor_ipc import (
    PROTOCOL_VERSION,
    Channel,
    ChannelCancelled,
    ChannelError,
    ChannelServer,
    ChannelTimeout,
    MessageSchema,
    connect,
    new_key,
)

#: What the supervisor may receive from the runtime, and send to it.
FROM_RUNTIME = MessageSchema({
    "hello": {"pid": "int", "protocol": "int"},
    # busy: whole seconds the runtime's command loop has spent inside the
    # command it is handling now; 0 while it waits for the next one.
    "heartbeat": {"seq": "int", "busy": "int"},
    "goodbye": {"reason": "str"},
})
TO_RUNTIME = MessageSchema({
    "welcome": {"protocol": "int", "supervisor_pid": "int"},
    "shutdown": {"reason": "str"},
})

MAX_BOOTSTRAP_BYTES = 4096


class SupervisorState(Enum):
    STARTING = "starting"
    RUNNING = "running"
    BACKING_OFF = "backing_off"
    STOPPING = "stopping"
    STOPPED = "stopped"
    #: Restarted too often within the window; no further restarts until the
    #: user restarts Qronos. A crash loop that keeps relaunching a model
    #: runtime would be worse than stopping.
    DEGRADED = "degraded"


@dataclass(frozen=True)
class SupervisorPolicy:
    heartbeat_interval_seconds: float = 5.0
    heartbeat_timeout_seconds: float = 20.0
    connect_timeout_seconds: float = 30.0
    restart_backoff_initial_seconds: float = 1.0
    restart_backoff_max_seconds: float = 60.0
    max_restarts: int = 5
    restart_window_seconds: float = 600.0
    #: A run at least this long resets the backoff: it was not a crash loop.
    stable_after_seconds: float = 120.0
    shutdown_grace_seconds: float = 10.0
    terminate_grace_seconds: float = 5.0
    #: A command loop busy this long with one command is reported, never
    #: killed for it: the command may be in the middle of changing something.
    command_loop_warn_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not 0 < self.heartbeat_interval_seconds < self.heartbeat_timeout_seconds:
            raise ValueError("Need 0 < heartbeat interval < heartbeat timeout.")
        if not 0 < self.restart_backoff_initial_seconds <= self.restart_backoff_max_seconds:
            raise ValueError("Need 0 < initial backoff <= maximum backoff.")
        if self.max_restarts < 0 or self.restart_window_seconds <= 0:
            raise ValueError("Restart limits must be non-negative with a positive window.")
        for value in (self.connect_timeout_seconds, self.shutdown_grace_seconds,
                      self.terminate_grace_seconds, self.stable_after_seconds,
                      self.command_loop_warn_seconds):
            if value <= 0:
                raise ValueError("Timeouts must be positive.")


class RestartBudget:
    """
    When to restart a child that stopped, and when to stop trying.

    Pure: the clock is passed in, so every rule here is tested without a
    process or a wait.
    """

    def __init__(self, policy: SupervisorPolicy) -> None:
        self._policy = policy
        self._restarts: list[float] = []
        self._consecutive = 0

    def after_failure(self, now: float, ran_for_seconds: float) -> Optional[float]:
        """Seconds to wait before the next start, or None to give up."""
        policy = self._policy

        if ran_for_seconds >= policy.stable_after_seconds:
            self._consecutive = 0

        self._restarts = [at for at in self._restarts if now - at < policy.restart_window_seconds]

        if len(self._restarts) >= policy.max_restarts:
            return None

        self._restarts.append(now)
        delay = min(
            policy.restart_backoff_max_seconds,
            policy.restart_backoff_initial_seconds * (2 ** self._consecutive),
        )
        self._consecutive += 1
        return delay


class ChildProcess(Protocol):
    pid: int

    def poll(self) -> Optional[int]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: Optional[float] = None) -> int: ...


class RuntimeLauncher(Protocol):
    def launch(self, bootstrap: bytes) -> ChildProcess:
        """Start the runtime child and give it ``bootstrap`` on standard input."""


class BundledRuntimeLauncher:
    """
    Starts only the bundled runtime executable with fixed arguments.

    The arguments are part of the production wiring contract with the runtime
    owner; nothing here accepts a command from a model, the UI or a message.
    """

    def __init__(self, executable: Path, arguments: Sequence[str]) -> None:
        executable = Path(executable)
        if not executable.is_absolute():
            raise ValueError("The runtime executable must be an absolute path.")
        for argument in arguments:
            if not isinstance(argument, str) or not argument or any(c in argument for c in "\"&|<>^%\r\n"):
                raise ValueError("Runtime arguments must be plain fixed words.")
        self._command = [str(executable), *arguments]

    def launch(self, bootstrap: bytes) -> ChildProcess:
        child = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        try:
            assert child.stdin is not None
            child.stdin.write(bootstrap)
            child.stdin.close()
        except Exception:
            child.kill()
            child.wait(5.0)
            raise
        return child


StateListener = Callable[[SupervisorState, str], None]


class Supervisor:
    """Keeps one runtime child alive under a bounded restart budget."""

    def __init__(
        self,
        launcher: RuntimeLauncher,
        policy: SupervisorPolicy = SupervisorPolicy(),
        notify: Optional[StateListener] = None,
        clock: Callable[[], float] = time.monotonic,
        peer_pid=None,
        warn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._launcher = launcher
        self._warn = warn
        self._policy = policy
        self._notify = notify
        self._clock = clock
        self._peer_pid = peer_pid
        self._budget = RestartBudget(policy)
        self._stop = threading.Event()
        self._state = SupervisorState.STOPPED
        self._lock = threading.Lock()
        self._connecting: Optional[ChannelServer] = None
        self.starts = 0

    @property
    def state(self) -> SupervisorState:
        return self._state

    def stop(self) -> None:
        """
        Ask the run loop to shut the child down and return.

        Prompt even while a new runtime is still connecting: the server it is
        being accepted on is interrupted, and the hello wait polls this.
        """
        self._stop.set()
        with self._lock:
            server = self._connecting
        if server is not None:
            server.interrupt()

    def run(self) -> SupervisorState:
        """Run until stopped or degraded. Returns the final state."""
        while not self._stop.is_set():
            self._set(SupervisorState.STARTING, "starting the runtime")
            started_at = self._clock()
            server, child, channel, reason = self._start()

            if reason is None:
                self._set(SupervisorState.RUNNING, f"runtime pid {child.pid}")
                reason = self._watch(child, channel)

            self._set(SupervisorState.STOPPING, reason)
            self._shutdown(child, channel, reason)
            server.close()

            if self._stop.is_set():
                break

            if child.poll() == STARTUP_REFUSED_EXIT:
                # The runtime refused to start before opening any store (an update in progress,
                # unresolved update recovery, an unknown version...). It said why on its own
                # output; restarting would only repeat the refusal, so this is terminal.
                self._set(SupervisorState.DEGRADED, "the runtime refused to start; it reported why")
                return self._state

            delay = self._budget.after_failure(self._clock(), self._clock() - started_at)

            if delay is None:
                self._set(SupervisorState.DEGRADED, f"stopped restarting after: {reason}")
                return self._state

            self._set(SupervisorState.BACKING_OFF, f"{reason}; restarting in {delay:.0f}s")

            if self._stop.wait(delay):
                break

        self._set(SupervisorState.STOPPED, "stopped")
        return self._state

    # ------------------------------------------------------------ internals

    def _set(self, state: SupervisorState, detail: str) -> None:
        with self._lock:
            self._state = state
        if self._notify is not None:
            try:
                self._notify(state, detail)
            except Exception:
                pass

    def _start(self):
        key = new_key()
        server = ChannelServer("supervisor", key, receive=FROM_RUNTIME, send=TO_RUNTIME)
        bootstrap = json.dumps({
            "address": server.address,
            "key": key.hex(),
            "supervisorPid": os.getpid(),
            "protocol": PROTOCOL_VERSION,
        }).encode("utf-8") + b"\n"
        self.starts += 1

        try:
            child = self._launcher.launch(bootstrap)
        except Exception:
            return server, _NoChild(), None, "the runtime could not be launched"

        with self._lock:
            self._connecting = server
        if self._stop.is_set():
            server.interrupt()  # stop() came between launching and here

        # A child that exits before it connects (for example a start-up refusal) ends the
        # wait at once instead of after the whole connect timeout.
        connected = threading.Event()
        exited: list[int] = []

        def watch_child() -> None:
            while not connected.wait(0.1):
                code = child.poll()
                if code is not None:
                    exited.append(code)
                    server.interrupt()
                    return

        threading.Thread(target=watch_child, name="qronos-supervisor-child-watch", daemon=True).start()

        channel = None
        try:
            channel = server.accept(
                self._policy.connect_timeout_seconds,
                expected_pid=child.pid,
                peer_pid=self._peer_pid,
            )
            hello = self._receive_hello(channel)

            if hello is None:
                channel.close()
                return server, child, None, "supervisor stopping"

            if hello["kind"] != "hello" or hello["pid"] != child.pid or hello["protocol"] != PROTOCOL_VERSION:
                channel.close()
                return server, child, None, "the runtime introduced itself wrongly"

            channel.send("welcome", protocol=PROTOCOL_VERSION, supervisor_pid=os.getpid())
        except ChannelCancelled:
            if exited and not self._stop.is_set():
                return server, child, None, f"the runtime exited before connecting (code {exited[0]})"
            return server, child, None, "supervisor stopping"
        except ChannelError as error:
            if channel is not None:
                channel.close()
            return server, child, None, f"the runtime did not connect ({type(error).__name__})"
        finally:
            connected.set()
            with self._lock:
                self._connecting = None

        return server, child, channel, None

    def _receive_hello(self, channel: Channel) -> Optional[dict]:
        """The runtime's hello within the connect timeout; None if Stop came first."""
        deadline = self._clock() + self._policy.connect_timeout_seconds
        while True:
            if self._stop.is_set():
                return None
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ChannelTimeout("The runtime did not say hello in time.")
            try:
                return channel.receive(min(0.25, remaining))
            except ChannelTimeout:
                continue

    def _watch(self, child: ChildProcess, channel: Channel) -> str:
        last_beat = self._clock()
        interval = self._policy.heartbeat_interval_seconds
        stall_reported = False

        while True:
            if self._stop.is_set():
                return "supervisor stopping"

            if child.poll() is not None:
                return f"the runtime exited with code {child.poll()}"

            try:
                message = channel.receive(min(interval, 0.5))
            except ChannelTimeout:
                message = None
            except ChannelError:
                return "the runtime closed its channel"

            now = self._clock()

            if message is not None:
                if message["kind"] == "heartbeat":
                    last_beat = now
                    busy = message["busy"]
                    if busy >= self._policy.command_loop_warn_seconds and not stall_reported:
                        stall_reported = True
                        self._report(busy)
                    elif busy == 0:
                        stall_reported = False
                elif message["kind"] == "goodbye":
                    return "the runtime said goodbye"
                else:
                    return "the runtime sent an unexpected message"

            if now - last_beat > self._policy.heartbeat_timeout_seconds:
                return "the runtime stopped responding"

    def _report(self, busy: int) -> None:
        if self._warn is not None:
            try:
                self._warn(f"The runtime's command loop has been busy with one command for {busy} s.")
            except Exception:
                pass

    def _shutdown(self, child: ChildProcess, channel: Optional[Channel], reason: str) -> None:
        """Ask, wait, terminate, kill — only ever the exact child launched."""
        if child.poll() is None and channel is not None:
            # A child whose input this supervisor relays is also asked there,
            # where its command loop reads; the request never waits on a pipe.
            request_stop = getattr(child, "request_stop", None)
            if callable(request_stop):
                request_stop()
            try:
                channel.send("shutdown", reason=reason[:200])
            except ChannelError:
                pass

        if channel is not None:
            deadline = self._clock() + self._policy.shutdown_grace_seconds
            while child.poll() is None and self._clock() < deadline:
                time.sleep(0.05)
            channel.close()

        if child.poll() is None:
            child.terminate()
            try:
                child.wait(self._policy.terminate_grace_seconds)
            except subprocess.TimeoutExpired:
                pass

        if child.poll() is None:
            child.kill()
            try:
                child.wait(self._policy.terminate_grace_seconds)
            except subprocess.TimeoutExpired:
                pass


class _NoChild:
    """Stands in when launching failed, so shutdown has nothing to stop."""

    pid = 0

    def poll(self) -> Optional[int]:
        return 1

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self, timeout: Optional[float] = None) -> int:
        return 1


# ------------------------------------------------------------- one instance


class SupervisorAlreadyRunning(RuntimeError):
    pass


class SupervisorInstanceGuard:
    """
    One supervisor per signed-in Windows session, by OWNING ``Local\\QronosSupervisor``.

    A named mutex in the ``Local\\`` namespace is per session by definition,
    so a second sign-in on the same machine gets its own supervisor while a
    second supervisor in the same session is refused. Ownership is a
    wait-acquisition by a holder thread kept for the supervisor's life
    (``core.instance_ownership``); creating the mutex, or finding that it
    exists, proves nothing. After acquiring, the update fence is checked: while
    an update or its installer is alive the supervisor releases the mutex and
    refuses (``UpdateInProgress``), after retrying for a bounded time. The
    updater's quiescence takes this same name, so a live supervisor makes it
    refuse. Off Windows it is a no-op; the supervisor is a Windows component.
    """

    NAME = "Local\\QronosSupervisor"

    def __init__(self, owned) -> None:
        self._owned = owned

    @classmethod
    def acquire(cls, *, api=None, fence_exists=None, retry_seconds: Optional[float] = None,
                name: Optional[str] = None) -> "SupervisorInstanceGuard":
        if sys.platform != "win32" and api is None:
            return cls(None)

        from core.instance_ownership import ENTRY_RETRY_SECONDS, AlreadyOwned, acquire_unless_updating

        options = {"retry_seconds": ENTRY_RETRY_SECONDS if retry_seconds is None else retry_seconds, "api": api}
        if fence_exists is not None:
            options["fence_exists"] = fence_exists
        try:
            return cls(acquire_unless_updating(name or cls.NAME, **options))
        except AlreadyOwned as error:
            raise SupervisorAlreadyRunning("A Qronos supervisor is already running in this session.") from error

    @property
    def owned(self) -> bool:
        return self._owned is not None and self._owned.owned

    def release(self) -> bool:
        """Release the singleton on its holder thread, once; True only if Windows confirmed it."""
        owned, self._owned = self._owned, None
        return owned.release() if owned is not None else True


# ------------------------------------------------------- the runtime's half


def read_bootstrap(stream: BinaryIO) -> dict:
    """The one line the supervisor wrote on standard input, validated."""
    line = stream.readline(MAX_BOOTSTRAP_BYTES + 1)

    if not line or len(line) > MAX_BOOTSTRAP_BYTES or not line.endswith(b"\n"):
        raise ChannelError("No valid supervisor bootstrap on standard input.")

    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ChannelError("The supervisor bootstrap is not valid JSON.") from error

    if (
        not isinstance(data, dict)
        or set(data) != {"address", "key", "supervisorPid", "protocol"}
        or not isinstance(data["address"], str)
        or not isinstance(data["key"], str)
        or type(data["supervisorPid"]) is not int
        or data["protocol"] != PROTOCOL_VERSION
    ):
        raise ChannelError("The supervisor bootstrap has the wrong shape.")

    try:
        key = bytes.fromhex(data["key"])
    except ValueError as error:
        raise ChannelError("The supervisor bootstrap key is not hex.") from error

    return {"address": data["address"], "key": key, "supervisor_pid": data["supervisorPid"]}


class SupervisedRuntimeClient:
    """
    The runtime side: connect, introduce itself, beat, obey "shut down".

    ``on_shutdown`` is called once, from the channel thread, when the
    supervisor asks; the runtime decides how to wind down. The runtime keeps
    every user-facing duty — audio, UI, dialogs — to itself.
    """

    def __init__(
        self,
        bootstrap: dict,
        on_shutdown: Callable[[str], None],
        heartbeat_interval_seconds: float = 5.0,
        peer_pid=None,
        busy_seconds: Callable[[], float] = lambda: 0,
    ) -> None:
        self._bootstrap = bootstrap
        self._busy_seconds = busy_seconds
        self._on_shutdown = on_shutdown
        self._interval = heartbeat_interval_seconds
        self._peer_pid = peer_pid
        self._channel: Optional[Channel] = None
        self._stopping = threading.Event()
        self._shutdown_once = threading.Lock()
        self._shutdown_called = False
        self._threads: list[threading.Thread] = []

    def start(self, timeout: float = 30.0) -> None:
        channel = connect(
            self._bootstrap["address"],
            self._bootstrap["key"],
            receive=TO_RUNTIME,
            send=FROM_RUNTIME,
            expected_server_pid=self._bootstrap["supervisor_pid"],
            peer_pid=self._peer_pid,
            timeout=timeout,
        )
        channel.send("hello", pid=os.getpid(), protocol=PROTOCOL_VERSION)
        welcome = channel.receive(timeout)

        if welcome["kind"] != "welcome" or welcome["supervisor_pid"] != self._bootstrap["supervisor_pid"]:
            channel.close()
            raise ChannelError("The supervisor answered wrongly.")

        self._channel = channel
        for target, name in ((self._beat, "qronos-heartbeat"), (self._listen, "qronos-supervisor-listen")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, reason: str = "runtime exiting") -> None:
        self._stopping.set()
        if self._channel is not None:
            try:
                self._channel.send("goodbye", reason=reason[:200])
            except ChannelError:
                pass
            self._channel.close()

    def _beat(self) -> None:
        seq = 0
        while not self._stopping.wait(self._interval):
            seq += 1
            try:
                busy = max(0, min(int(self._busy_seconds()), 10**9))
            except Exception:
                busy = 0
            try:
                self._channel.send("heartbeat", seq=seq, busy=busy)  # type: ignore[union-attr]
            except ChannelError:
                return

    def _listen(self) -> None:
        while not self._stopping.is_set():
            try:
                message = self._channel.receive(0.5)  # type: ignore[union-attr]
            except ChannelTimeout:
                continue
            except ChannelError:
                self._request_shutdown("the supervisor is gone")
                return

            if message["kind"] == "shutdown":
                self._request_shutdown(message["reason"])
                return

    def _request_shutdown(self, reason: str) -> None:
        with self._shutdown_once:
            if self._shutdown_called:
                return
            self._shutdown_called = True
        self._stopping.set()
        self._on_shutdown(reason)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    ``qronos-runtime supervisor --desktop``: supervise the desktop's runtime
    bridge, relaying the desktop's standard input and output to it, until
    the desktop stops it (see ``core/supervisor_relay.py``).

    That is the only mode: the bridge's command and arguments are fixed by
    this module, and nothing else is accepted.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)

    if arguments != ["--desktop"]:
        print(json.dumps({"ok": False, "error": "Usage: supervisor --desktop"}))
        return 2

    from core.supervisor_relay import exit_process, run_desktop

    exit_process(run_desktop())
    return 1  # not reached


if __name__ == "__main__":
    raise SystemExit(main())
