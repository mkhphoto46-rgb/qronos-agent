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

from core.supervisor_ipc import (
    PROTOCOL_VERSION,
    Channel,
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
    "heartbeat": {"seq": "int"},
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

    def __post_init__(self) -> None:
        if not 0 < self.heartbeat_interval_seconds < self.heartbeat_timeout_seconds:
            raise ValueError("Need 0 < heartbeat interval < heartbeat timeout.")
        if not 0 < self.restart_backoff_initial_seconds <= self.restart_backoff_max_seconds:
            raise ValueError("Need 0 < initial backoff <= maximum backoff.")
        if self.max_restarts < 0 or self.restart_window_seconds <= 0:
            raise ValueError("Restart limits must be non-negative with a positive window.")
        for value in (self.connect_timeout_seconds, self.shutdown_grace_seconds,
                      self.terminate_grace_seconds, self.stable_after_seconds):
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
    ) -> None:
        self._launcher = launcher
        self._policy = policy
        self._notify = notify
        self._clock = clock
        self._peer_pid = peer_pid
        self._budget = RestartBudget(policy)
        self._stop = threading.Event()
        self._state = SupervisorState.STOPPED
        self._lock = threading.Lock()
        self.starts = 0

    @property
    def state(self) -> SupervisorState:
        return self._state

    def stop(self) -> None:
        """Ask the run loop to shut the child down and return."""
        self._stop.set()

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

        try:
            channel = server.accept(
                self._policy.connect_timeout_seconds,
                expected_pid=child.pid,
                peer_pid=self._peer_pid,
            )
            hello = channel.receive(self._policy.connect_timeout_seconds)

            if hello["kind"] != "hello" or hello["pid"] != child.pid or hello["protocol"] != PROTOCOL_VERSION:
                channel.close()
                return server, child, None, "the runtime introduced itself wrongly"

            channel.send("welcome", protocol=PROTOCOL_VERSION, supervisor_pid=os.getpid())
        except ChannelError as error:
            return server, child, None, f"the runtime did not connect ({type(error).__name__})"

        return server, child, channel, None

    def _watch(self, child: ChildProcess, channel: Channel) -> str:
        last_beat = self._clock()
        interval = self._policy.heartbeat_interval_seconds

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
                elif message["kind"] == "goodbye":
                    return "the runtime said goodbye"
                else:
                    return "the runtime sent an unexpected message"

            if now - last_beat > self._policy.heartbeat_timeout_seconds:
                return "the runtime stopped responding"

    def _shutdown(self, child: ChildProcess, channel: Optional[Channel], reason: str) -> None:
        """Ask, wait, terminate, kill — only ever the exact child launched."""
        if child.poll() is None and channel is not None:
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
    One supervisor per signed-in Windows session.

    A named mutex in the ``Local\\`` namespace is per session by definition,
    so a second sign-in on the same machine gets its own supervisor while a
    second supervisor in the same session is refused. Off Windows it is a
    no-op; Qronos's supervisor is a Windows component.
    """

    NAME = "Local\\QronosSupervisor"

    def __init__(self, handle: int, close: Callable[[int], None]) -> None:
        self._handle = handle
        self._close = close

    @classmethod
    def acquire(cls, create_mutex=None, last_error=None, close_handle=None) -> "SupervisorInstanceGuard":
        if sys.platform != "win32" and create_mutex is None:
            return cls(0, lambda handle: None)

        if create_mutex is None:
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            create_mutex = lambda name: kernel32.CreateMutexW(None, False, name)  # noqa: E731
            last_error = ctypes.get_last_error
            close_handle = kernel32.CloseHandle

        handle = create_mutex(cls.NAME)

        if not handle:
            raise OSError("Windows could not create the supervisor mutex.")

        if last_error() == 183:  # ERROR_ALREADY_EXISTS
            close_handle(handle)
            raise SupervisorAlreadyRunning("A Qronos supervisor is already running in this session.")

        return cls(int(handle), close_handle)

    def release(self) -> None:
        if self._handle:
            self._close(self._handle)
            self._handle = 0


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
    ) -> None:
        self._bootstrap = bootstrap
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
                self._channel.send("heartbeat", seq=seq)  # type: ignore[union-attr]
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
    ``qronos-runtime supervisor --runtime-arguments <words...>``: supervise the
    bundled runtime started with those fixed arguments, until stopped.

    The runtime arguments are fixed by the production wiring (see
    docs/qronos_supervisor_and_broker.md); nothing else is accepted.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)

    if not arguments or arguments[0] != "--runtime-arguments" or len(arguments) < 2:
        print(json.dumps({"ok": False, "error": "Usage: supervisor --runtime-arguments <words>"}))
        return 2

    guard = SupervisorInstanceGuard.acquire()
    try:
        supervisor = Supervisor(BundledRuntimeLauncher(Path(sys.executable), arguments[1:]))
        state = supervisor.run()
    finally:
        guard.release()

    print(json.dumps({"ok": state is SupervisorState.STOPPED, "state": state.value}))
    return 0 if state is SupervisorState.STOPPED else 1
