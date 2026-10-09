"""
The supervised desktop runtime (core/supervisor_relay.py).

Unit tests drive the relay with in-memory streams. Process tests start the
supervisor as its own process (tests/fixtures/relay_supervisor_driver.py)
over pipes, exactly as the desktop does, with a disposable bridge stand-in
(tests/fixtures/relay_bridge_child.py) joined over the real authenticated
channel. One end-to-end test runs ``python -m core.supervisor --desktop``
with the real bridge on throwaway state.

Every wait is bounded; every process is owned by the test and is killed and
verified gone at the end. No device, model, UAC, service or user data is
touched. Test runs use their own mutex and Job Object names, except the
end-to-end test, which is the production entry point.
"""

from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from core import supervisor_relay as relay_module
from core.supervisor import SupervisorState
from core.supervisor_relay import (
    BUSY,
    GAVE_UP,
    NOT_DELIVERED,
    OUTPUT_TOO_LARGE,
    RESTARTING,
    TOO_LARGE,
    DesktopRelay,
    DesktopRuntimeLauncher,
    EventWriter,
    is_shutdown_command,
)
from tests.fixtures.child_python import child_python

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "fixtures" / "relay_supervisor_driver.py"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Supervision runs over Windows named pipes.")


# ------------------------------------------------------------------ units


def events_of(buffer: io.BytesIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_only_the_desktop_s_own_shutdown_command_is_recognised():
    assert is_shutdown_command(b'{"command":"shutdown"}\n')
    assert is_shutdown_command(b'{"command": "shutdown", "extra": 1}')
    for other in (b'{"command":"ping"}', b"shutdown", b'["shutdown"]', b"\xff", b'{"text":"shutdown"}'):
        assert not is_shutdown_command(other)


def test_event_lines_are_whole_and_a_broken_desktop_stops_the_supervisor():
    out = io.BytesIO()
    EventWriter(out).event("supervisor_state", "running", "ok")
    assert events_of(out) == [{"eventType": "supervisor_state", "status": "running", "message": "ok"}]

    class Broken(io.BytesIO):
        def write(self, data):
            raise OSError("pipe closed")

    stopped = []
    writer = EventWriter(Broken(), on_broken=lambda: stopped.append(True))
    writer.event("a", "b", "c")
    writer.event("a", "b", "c")
    assert stopped == [True]  # told once; later writes are dropped quietly


def test_commands_are_refused_out_loud_when_no_runtime_can_take_them():
    out = io.BytesIO()
    relay = DesktopRelay(io.BytesIO(), out)
    relay.set_accepting(False)
    relay._submit(b'{"command":"ping"}\n')
    assert events_of(out)[-1]["message"] == NOT_DELIVERED


def test_a_full_command_queue_is_refused_as_busy_and_waiting_commands_are_dropped_on_restart(monkeypatch):
    monkeypatch.setattr(relay_module, "MAX_PENDING_COMMANDS", 2)
    out = io.BytesIO()
    relay = DesktopRelay(io.BytesIO(), out)
    for _ in range(3):
        relay._submit(b'{"command":"ping"}\n')
    assert events_of(out)[-1]["message"] == BUSY
    relay.set_accepting(False)  # the bridge went away with two still waiting
    assert [event["message"] for event in events_of(out)[-2:]] == [NOT_DELIVERED, NOT_DELIVERED]


def test_bridge_output_is_relayed_in_whole_bounded_lines(monkeypatch):
    monkeypatch.setattr(relay_module, "MAX_LINE_BYTES", 64)
    monkeypatch.setattr(relay_module, "_READ_CHUNK", 16)
    out = io.BytesIO()
    relay = DesktopRelay(io.BytesIO(), out)
    first = b'{"eventType":"a","status":"s","message":"' + b"y" * 5 +b'"}\n'
    stream = io.BytesIO(first + b"z" * 200 + b"\n" + b'{"eventType":"b"}\n' + b"tail")
    relay.pump_output(stream)
    lines = out.getvalue().splitlines()
    assert lines[0] == first.rstrip(b"\n")
    assert json.loads(lines[1])["message"] == OUTPUT_TOO_LARGE
    assert lines[2:] == [b'{"eventType":"b"}', b"tail"]


def test_desktop_input_is_framed_bounded_and_shutdown_or_eof_stops(monkeypatch):
    monkeypatch.setattr(relay_module, "MAX_LINE_BYTES", 32)
    out = io.BytesIO()
    desktop = io.BytesIO(b"q" * 100 + b"\n" + b'{"command":"ping"}\n' + b"\n" + b'{"command":"shutdown"}\n')
    relay = DesktopRelay(desktop, out)
    stops: list[str] = []
    relay._stop_requested = stops.append
    relay._read_desktop()
    assert events_of(out)[0]["message"] == TOO_LARGE
    assert relay._pending.get_nowait() == b'{"command":"ping"}\n'
    assert relay._pending.empty()  # the blank line and the shutdown are not relayed
    assert stops == ["the desktop asked the runtime to stop"]  # EOF afterwards does not ask twice


def test_state_changes_reach_the_desktop_as_runtime_events():
    out = io.BytesIO()
    relay = DesktopRelay(io.BytesIO(), out)
    relay.on_state(SupervisorState.BACKING_OFF, "the runtime exited with code 3; restarting in 1s")
    relay.on_state(SupervisorState.DEGRADED, "stopped restarting")
    seen = [(event["eventType"], event["status"], event["message"]) for event in events_of(out)]
    assert seen == [
        ("supervisor_state", "restarting", "the runtime exited with code 3; restarting in 1s"),
        ("runtime_stopping", "stopping", RESTARTING),
        ("supervisor_state", "degraded", "stopped restarting"),
        ("runtime_stopping", "stopping", GAVE_UP),
        ("runtime_error", "error", GAVE_UP),
    ]
    assert not relay._accepting


def test_the_bridge_command_is_fixed(monkeypatch):
    command, environment = relay_module.bridge_command()
    assert command[1:] == ["-u", "-m", "core.runtime_bridge", "--supervised"]
    assert environment["PYTHONPATH"].split(os.pathsep)[0] == str(ROOT)
    monkeypatch.setattr(relay_module.sys, "frozen", True, raising=False)
    command, _ = relay_module.bridge_command()
    assert command == [sys.executable, "bridge", "--supervised"]


def test_the_launcher_needs_an_absolute_executable():
    with pytest.raises(ValueError):
        DesktopRuntimeLauncher(DesktopRelay(io.BytesIO(), io.BytesIO()), ["python", "x.py"])


def test_the_supervisor_entry_accepts_only_desktop_mode(capsys):
    from core import supervisor

    for arguments in ([], ["--runtime-arguments", "bridge"], ["--desktop", "extra"]):
        assert supervisor.main(arguments) == 2
    assert "supervisor --desktop" in capsys.readouterr().out


def test_the_bridge_reports_how_long_its_command_loop_is_inside_one_command():
    import core.runtime_bridge as bridge

    now = [100.0]
    health = bridge.CommandLoopHealth(clock=lambda: now[0])
    assert health.busy_seconds() == 0
    health.enter()
    now[0] = 107.9
    assert health.busy_seconds() == 7
    health.leave()
    assert health.busy_seconds() == 0

    seen = []
    lines = bridge._command_lines(iter(["a", "b"]))
    for _ in lines:
        seen.append(bridge.COMMAND_LOOP._since is not None)  # busy while a line is handled
    assert seen == [True, True] and bridge.COMMAND_LOOP.busy_seconds() == 0


def test_a_supervised_bridge_without_a_valid_bootstrap_exits_instead_of_running_unsupervised(monkeypatch):
    import core.runtime_bridge as bridge

    events = []
    monkeypatch.setattr(bridge, "emit", lambda event, status, message: events.append((event, message)))
    monkeypatch.setattr(bridge, "QronosRuntime", lambda: pytest.fail("the runtime must not start"))
    monkeypatch.setattr(bridge.sys, "argv", ["bridge", "--supervised"])
    monkeypatch.setattr(bridge.sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"address":"x"}\n')))
    assert bridge.main() == 3
    assert events and events[0][0] == "runtime_error" and "supervisor" in events[0][1]


# --------------------------------------------------------------- processes


class SupervisedDesktop:
    """Plays the desktop: owns the supervisor process and reads its events."""

    def __init__(self, arguments: list[str], environment: dict) -> None:
        self.process = subprocess.Popen(
            arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environment, cwd=str(ROOT),
        )
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.seen: list[dict] = []
        self.pending: list[dict] = []
        self.bridges: dict[int, object] = {}  # pid -> process handle, opened when announced
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            if raw.strip():
                try:
                    self.events.put(json.loads(raw))
                except ValueError:
                    self.events.put({"eventType": "unparsed", "status": "", "message": raw[:80].decode("latin-1")})

    def send(self, payload: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def wait_for(self, event_type: str, status: str | None = None, timeout: float = 30.0) -> dict:
        """The first not-yet-claimed matching event, in arrival order."""
        deadline = time.monotonic() + timeout
        while True:
            for index, event in enumerate(self.pending):
                if event.get("eventType") == event_type and (status is None or event.get("status") == status):
                    del self.pending[index]
                    return event
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no {event_type}/{status} within {timeout}s; saw {self.seen[-12:]}")
            try:
                event = self.events.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            self.seen.append(event)
            self.pending.append(event)
            self._track(event)

    def _track(self, event: dict) -> None:
        pid = None
        if event.get("eventType") == "supervisor_state" and event.get("status") == "running":
            pid = int(event["message"].rsplit(" ", 1)[-1])
        if pid and pid not in self.bridges:
            import _winapi

            try:
                self.bridges[pid] = _winapi.OpenProcess(0x100000 | 0x1000 | 0x0001, False, pid)
            except OSError:
                self.bridges[pid] = None  # already gone and reaped

    def bridge_exited(self, pid: int, timeout: float) -> bool:
        import _winapi

        if self.bridges[pid] is None:
            return True
        return _winapi.WaitForSingleObject(self.bridges[pid], int(timeout * 1000)) == _winapi.WAIT_OBJECT_0

    def close(self) -> list[int]:
        """Kill whatever is left; return the bridge pids that had to be killed here."""
        import _winapi

        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(10)
        survivors = []
        for pid, handle in self.bridges.items():
            if handle is None:
                continue
            if _winapi.WaitForSingleObject(handle, 5000) != _winapi.WAIT_OBJECT_0:
                survivors.append(pid)
                _winapi.TerminateProcess(handle, 1)
            _winapi.CloseHandle(handle)
        return survivors


@pytest.fixture
def desktop():
    made: list[SupervisedDesktop] = []

    def start(mode: str) -> SupervisedDesktop:
        executable, environment = child_python()
        instance = SupervisedDesktop(
            [executable, "-B", str(DRIVER), mode, uuid.uuid4().hex[:12]], environment,
        )
        made.append(instance)
        return instance

    yield start
    for instance in made:
        assert instance.close() == [], "a bridge outlived its test"


@windows_only
def test_commands_and_events_pass_through_and_shutdown_is_orderly(desktop):
    session = desktop("echo")
    session.wait_for("supervisor_state", "running")
    session.wait_for("runtime_ready")
    (pid,) = session.bridges
    session.send({"command": "ping"})
    assert session.wait_for("runtime_pong")["message"] == str(pid)
    session.send({"command": "echo", "text": "سلام Qronos"})
    assert session.wait_for("echo")["message"] == "سلام Qronos"
    session.send({"command": "shutdown"})
    assert session.wait_for("runtime_stopping")["message"] == str(pid)  # the bridge itself was asked
    session.wait_for("supervisor_state", "stopped")
    assert session.process.wait(10) == 0
    assert session.bridge_exited(pid, 5)


@windows_only
def test_the_desktop_closing_its_side_stops_the_runtime(desktop):
    session = desktop("echo")
    session.wait_for("runtime_ready")
    (pid,) = session.bridges
    session.process.stdin.close()
    session.wait_for("supervisor_state", "stopped")
    assert session.process.wait(10) == 0
    assert session.bridge_exited(pid, 5)


@windows_only
def test_a_command_loop_stuck_in_one_command_is_reported_not_killed(desktop):
    session = desktop("echo")
    session.wait_for("supervisor_state", "running")
    session.wait_for("runtime_ready")
    session.send({"command": "block"})  # 3 s inside one command; the driver warns at 1 s
    warning = session.wait_for("runtime_warning", timeout=10)
    assert "busy with one command" in warning["message"] and "Stop still works" in warning["message"]
    session.wait_for("unblocked", timeout=10)
    session.send({"command": "ping"})
    session.wait_for("runtime_pong")
    assert len(session.bridges) == 1
    assert not any(event.get("status") == "restarting" for event in session.seen)
    session.send({"command": "shutdown"})
    assert session.process.wait(15) == 0


@windows_only
def test_a_crashing_runtime_is_restarted_then_reported_degraded(desktop):
    session = desktop("crash")
    session.wait_for("runtime_ready")
    assert session.wait_for("runtime_stopping")["message"] == RESTARTING
    session.wait_for("runtime_ready")
    session.wait_for("supervisor_state", "degraded", timeout=60)
    assert session.wait_for("runtime_error")["message"] == GAVE_UP
    assert session.process.wait(10) == 1
    assert len(session.bridges) == 3  # the first start and max_restarts=2
    assert all(session.bridge_exited(pid, 5) for pid in session.bridges)


@windows_only
def test_a_runtime_that_stops_beating_is_replaced(desktop):
    session = desktop("silent")
    session.wait_for("supervisor_state", "running")
    session.wait_for("runtime_ready")
    (first,) = session.bridges
    restarting = session.wait_for("supervisor_state", "restarting", timeout=15)
    assert "stopped responding" in restarting["message"]
    assert session.bridge_exited(first, 5)  # the exact hung child was stopped
    session.wait_for("supervisor_state", "running", timeout=30)
    assert len(session.bridges) == 2
    session.send({"command": "shutdown"})
    session.wait_for("supervisor_state", "stopped")
    assert session.process.wait(10) == 0


@windows_only
def test_an_oversized_output_line_is_dropped_whole_and_the_stream_continues(desktop):
    session = desktop("flood")
    session.wait_for("runtime_ready")
    assert session.wait_for("runtime_error")["message"] == OUTPUT_TOO_LARGE
    assert session.wait_for("after_flood")["message"] == "still here"
    session.send({"command": "shutdown"})
    assert session.process.wait(15) == 0


@windows_only
def test_a_runtime_that_ignores_shutdown_is_terminated_after_the_grace(desktop):
    session = desktop("linger")
    session.wait_for("runtime_ready")
    (pid,) = session.bridges
    started = time.monotonic()
    session.send({"command": "shutdown"})
    session.wait_for("supervisor_state", "stopped", timeout=15)
    assert 1.5 <= time.monotonic() - started < 8  # grace 2 s, then terminate
    assert session.bridge_exited(pid, 5)
    assert session.process.wait(10) == 0


@windows_only
def test_killing_the_supervisor_ends_its_runtime_through_the_job_object(desktop):
    session = desktop("linger")
    session.wait_for("runtime_ready")
    (pid,) = session.bridges
    session.process.kill()  # no shutdown, no cleanup: what a crash leaves
    session.process.wait(10)
    assert session.bridge_exited(pid, 10)


# ------------------------------------------------------------- end to end


@windows_only
def test_the_production_entry_supervises_the_real_bridge_end_to_end():
    """
    ``python -m core.supervisor --desktop`` starts the real bridge with
    ``--supervised`` (base interpreter, real peer check and handshake). The
    bridge runs on throwaway state with Ollama auto-start off; no wake
    bootstrap is requested, so no microphone is opened, and nothing is asked
    of a model.
    """
    scratch = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    executable, environment = child_python()
    state = Path(scratch.name)
    environment.update({
        "QRONOS_CONVERSATION_HISTORY_PATH": str(state / "conversation_history.sqlite3"),
        "QRONOS_QUEUE_STATE_PATH": str(state / "queue_state.json"),
        "QRONOS_SETTINGS_PATH": str(state / "settings.json"),
        "QRONOS_OLLAMA_AUTO_START": "0",
        "QRONOS_RUN_LIVE_DEVICE_TESTS": "0",
    })
    environment.pop("QRONOS_DESKTOP_WAKE_BOOTSTRAP", None)
    session = SupervisedDesktop([executable, "-B", "-m", "core.supervisor", "--desktop"], environment)
    try:
        session.wait_for("supervisor_state", "running", timeout=120)
        session.wait_for("runtime_ready", timeout=120)
        (pid,) = session.bridges
        session.send({"command": "ping"})
        session.wait_for("runtime_pong", timeout=30)
        session.send({"command": "shutdown"})
        session.wait_for("runtime_stopping", timeout=30)  # from the real bridge's command loop
        session.wait_for("supervisor_state", "stopped", timeout=30)
        assert session.process.wait(15) == 0
        assert session.bridge_exited(pid, 10)
    finally:
        assert session.close() == []
        scratch.cleanup()
