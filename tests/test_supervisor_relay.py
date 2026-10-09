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
import threading
import time
import uuid
from pathlib import Path

import pytest

from core import supervisor_relay as relay_module
from core.supervisor import SupervisorState
from core.supervisor_relay import (
    GAVE_UP,
    NOTICES,
    RESTARTING,
    ChildInput,
    Delivery,
    DesktopRelay,
    DesktopRuntimeLauncher,
    Outbound,
    Relayed,
    is_shutdown_command,
)
from tests.fixtures.child_python import child_python
from tests.live_device import requires_live_device

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "fixtures" / "relay_supervisor_driver.py"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Supervision runs over Windows named pipes.")


# ------------------------------------------------------------------ units


def events_of(buffer: io.BytesIO, relay=None) -> list[dict]:
    if relay is not None:
        assert relay.out.drain(5)
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def notice(kind: str, count: int) -> str:
    return NOTICES[kind][2].format(count=count)


class Gate(io.BytesIO):
    """A desktop pipe that blocks every write until opened: a desktop that stopped reading."""

    def __init__(self):
        super().__init__()
        self.open = threading.Event()

    def write(self, data):
        self.open.wait(30)
        return super().write(data)


def test_only_the_desktop_s_own_shutdown_command_is_recognised():
    assert is_shutdown_command(b'{"command":"shutdown"}\n')
    assert is_shutdown_command(b'{"command": "shutdown", "extra": 1}')
    for other in (b'{"command":"ping"}', b"shutdown", b'["shutdown"]', b"\xff", b'{"text":"shutdown"}'):
        assert not is_shutdown_command(other)


def test_event_lines_are_whole_and_a_broken_desktop_stops_the_supervisor():
    out = io.BytesIO()
    outbound = Outbound(out)
    outbound.lifecycle("supervisor_state", "running", "ok", state=True)
    assert outbound.drain(5)
    assert events_of(out) == [{"eventType": "supervisor_state", "status": "running", "message": "ok"}]

    class Broken(io.BytesIO):
        def write(self, data):
            raise OSError("pipe closed")

    stopped = []
    broken = Outbound(Broken(), on_broken=lambda: stopped.append(True))
    broken.lifecycle("a", "b", "c")
    broken.drain(5)
    broken.lifecycle("a", "b", "c")
    assert stopped == [True]  # told once; later events are dropped quietly
    assert broken.relayed(b"line\n", 1) is Relayed.CLOSED


def test_refusals_coalesce_and_lifecycle_events_are_never_dropped_for_a_slow_desktop():
    """F2: thousands of refusals cost one counted entry; every lifecycle event survives, in order."""
    desktop = Gate()
    outbound = Outbound(desktop)
    started = time.monotonic()
    outbound.lifecycle("supervisor_state", "running", "gen 1", state=True)
    for _ in range(5000):
        outbound.notice("busy")
    outbound.lifecycle("supervisor_state", "stopping", "gen 1 died", state=True)
    outbound.lifecycle("runtime_stopping", "stopping", RESTARTING)
    for _ in range(5000):
        outbound.notice("not_delivered")
    outbound.lifecycle("supervisor_state", "restarting", "restarting", state=True)
    outbound.lifecycle("supervisor_state", "running", "gen 2", state=True)
    assert time.monotonic() - started < 1.0  # the control loop is never held
    assert not outbound.drain(0.2)  # and an exit drain is bounded
    desktop.open.set()
    assert outbound.drain(5)
    events = [(event["eventType"], event["message"]) for event in events_of(desktop)]
    # The first state may already be in the blocked write; superseded unread states are coalesced.
    assert ("runtime_stopping", RESTARTING) in events
    assert events[-1] == ("supervisor_state", "gen 2")
    assert events.index(("runtime_stopping", RESTARTING)) < events.index(("supervisor_state", "gen 2"))
    assert ("runtime_error", notice("busy", 5000)) in events
    assert ("runtime_error", notice("not_delivered", 5000)) in events
    assert len(events) <= 6


def test_beyond_its_cap_the_lifecycle_lane_merges_old_restart_notices_and_says_so(monkeypatch):
    monkeypatch.setattr(relay_module, "MAX_LIFECYCLE_ENTRIES", 3)
    desktop = Gate()
    outbound = Outbound(desktop)
    outbound.lifecycle("supervisor_state", "running", "blocked write", state=True)
    time.sleep(0.2)  # the writer now holds that one, blocked
    for index in range(5):
        outbound.lifecycle("runtime_stopping", "stopping", f"restart {index}")
    desktop.open.set()
    assert outbound.drain(5)
    events = [(event["eventType"], event["message"]) for event in events_of(desktop)]
    assert [message for kind, message in events if kind == "runtime_stopping"] == [
        "restart 2", "restart 3", "restart 4"]
    assert ("runtime_warning", notice("lifecycle_merged", 2)) in events


def test_relayed_output_pushes_back_within_its_byte_budget_and_never_past_close(monkeypatch):
    monkeypatch.setattr(relay_module, "OUTPUT_BUDGET_BYTES", 1000)
    desktop = Gate()
    outbound = Outbound(desktop)
    accepted = []

    def produce():
        for index in range(100):
            if outbound.relayed(b"x" * 98 + b"\n", 1) is not Relayed.QUEUED:
                break
            accepted.append(index)

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    time.sleep(0.5)
    assert 9 <= len(accepted) <= 12 and producer.is_alive()  # budget reached; the bridge's writer waits
    outbound.close()
    producer.join(5)
    assert not producer.is_alive()  # closing releases it
    desktop.open.set()


def test_an_ended_generation_can_never_put_output_after_its_end(monkeypatch):
    """F1: a line blocked for room when its generation ends is dropped, never queued after the end."""
    monkeypatch.setattr(relay_module, "OUTPUT_BUDGET_BYTES", 200)
    desktop = Gate()
    outbound = Outbound(desktop)
    outbound.relayed(b'{"gen":1,"n":0}' + b"x" * 150 + b"\n", 1)
    time.sleep(0.2)  # that one is in the blocked write
    outbound.relayed(b'{"gen":1,"n":1}' + b"x" * 150 + b"\n", 1)  # fills the budget
    results = []
    blocked = threading.Thread(target=lambda: results.append(
        outbound.relayed(b'{"gen":1,"n":2}' + b"x" * 100 + b"\n", 1)), daemon=True)
    blocked.start()
    time.sleep(0.2)
    assert blocked.is_alive()  # waiting for room
    outbound.end_generation(1)
    blocked.join(5)
    assert results == [Relayed.FENCED]
    outbound.lifecycle("runtime_stopping", "stopping", RESTARTING)
    assert outbound.relayed(b'{"gen":1,"n":3}\n', 1) is Relayed.FENCED
    assert outbound.relayed(b'{"gen":2,"n":0}\n', 2) is Relayed.QUEUED
    desktop.open.set()
    assert outbound.drain(5)
    lines = desktop.getvalue().splitlines()
    order = [line[:15] for line in lines]
    end = next(i for i, line in enumerate(lines) if RESTARTING.encode() in line)
    assert all(b'"gen":1' not in line for line in lines[end:])  # nothing of gen 1 after its end
    assert any(b'"gen":2' in line for line in lines[end:]), order  # the next generation still flows
    assert any(notice("old_output", 2).encode() in line for line in lines)


def test_commands_are_refused_out_loud_when_no_runtime_can_take_them():
    out = io.BytesIO()
    relay = DesktopRelay(io.BytesIO(), out)
    relay.set_accepting(False)
    relay._submit(b'{"command":"ping"}\n')
    assert events_of(out, relay)[-1]["message"] == notice("not_delivered", 1)


@pytest.mark.parametrize("limit, value", [("MAX_PENDING_COMMANDS", 2), ("PENDING_COMMAND_BUDGET_BYTES", 50)])
def test_the_command_queue_is_bounded_by_count_and_bytes(monkeypatch, limit, value):
    monkeypatch.setattr(relay_module, limit, value)
    out = io.BytesIO()
    relay = DesktopRelay(io.BytesIO(), out)
    for _ in range(3):
        relay._submit(b'{"command":"ping","x":1}\n')  # 25 bytes each
    assert len(relay._pending) == 2
    relay.set_accepting(False)  # the bridge went away with two still waiting
    messages = [event["message"] for event in events_of(out, relay)]
    assert messages == [notice("busy", 1), notice("not_delivered", 2)]
    assert relay._pending_bytes == 0


def test_a_write_that_fails_part_way_is_unconfirmed_never_not_delivered():
    class Failing(io.BytesIO):
        def write(self, data):
            raise OSError("broken pipe")

    child = ChildInput(Failing())
    assert child.write(b"x\n") is Delivery.UNCONFIRMED
    assert child.write(b"x\n") is Delivery.REFUSED  # closed: nothing written now
    finished = ChildInput(io.BytesIO())
    finished.finish(b"last\n")
    assert finished.write(b"x\n") is Delivery.REFUSED

    out = io.BytesIO()
    relay = DesktopRelay(io.BytesIO(), out)
    relay.attach(ChildInput(Failing()))
    relay.start(lambda reason: None)
    relay._submit(b'{"command":"act"}\n')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not out.getvalue():
        time.sleep(0.02)
    assert events_of(out, relay)[-1]["message"] == notice("unconfirmed", 1)


def test_bridge_output_is_relayed_only_as_whole_valid_bounded_lines(monkeypatch):
    """F4: an oversized, invalid or cut-off final line is dropped and counted; no newline is ever added."""
    monkeypatch.setattr(relay_module, "MAX_LINE_BYTES", 64)
    monkeypatch.setattr(relay_module, "_READ_CHUNK", 16)
    out = io.BytesIO()
    relay = DesktopRelay(io.BytesIO(), out)
    first = b'{"eventType":"a","status":"s","message":"' + b"y" * 5 + b'"}\n'
    stream = io.BytesIO(first + b"z" * 200 + b"\n" + b'{"eventType":"bad\xff\xfe"}\n'
                        + b'{"eventType":"b"}\n' + b'{"eventType":"cut_off","message":"tail')
    relay.pump_output(stream, 1)
    assert relay.out.drain(5)
    lines = out.getvalue().splitlines()
    assert lines[0] == first.rstrip(b"\n")
    assert json.loads(lines[1])["message"] == notice("output_too_large", 1)
    assert json.loads(lines[2])["message"] == notice("output_invalid", 1)
    assert lines[3] == b'{"eventType":"b"}'
    assert json.loads(lines[4])["message"] == notice("output_incomplete", 1)
    assert b"cut_off" not in out.getvalue() and b"\xff" not in out.getvalue()


def test_bridge_stderr_never_carries_a_pairing_secret_or_invalid_text():
    """F8: the same Drop rule as the desktop's stdout routing, applied to stderr."""
    out, err = io.BytesIO(), io.BytesIO()
    relay = DesktopRelay(io.BytesIO(), out, err)
    relay.pump_errors(io.BytesIO(b"Traceback\nopen qronos://pair?k=SECRET&n=1\nbad \xff byte\n"))
    assert b"SECRET" not in err.getvalue() and b"pair?" not in err.getvalue()
    assert err.getvalue().decode("utf-8") == "Traceback\nbad � byte\n"
    assert events_of(out, relay)[-1]["message"] == notice("stderr_secret", 1)


def test_desktop_input_is_framed_bounded_and_a_cut_off_command_is_never_delivered(monkeypatch):
    monkeypatch.setattr(relay_module, "MAX_COMMAND_BYTES", 32)
    out = io.BytesIO()
    desktop = io.BytesIO(
        b"q" * 100 + b"\n" + b'{"command":"ping"}\n' + b"\n" + b'{"command":"shutdown"}\n' + b'{"command":"ec'
    )
    relay = DesktopRelay(desktop, out)
    stops: list[str] = []
    relay._stop_requested = stops.append
    relay._read_desktop()
    messages = [event["message"] for event in events_of(out, relay)]
    assert messages == [notice("too_large", 1), notice("incomplete", 1)]
    assert list(relay._pending) == [b'{"command":"ping"}\n']  # the blank line, shutdown and cut-off are not relayed
    assert stops == ["the desktop asked the runtime to stop"]  # EOF afterwards does not ask twice


def test_state_changes_reach_the_desktop_as_runtime_events():
    out = io.BytesIO()
    relay = DesktopRelay(io.BytesIO(), out)
    relay.on_state(SupervisorState.BACKING_OFF, "the runtime exited with code 3; restarting in 1s")
    seen = [(event["eventType"], event["status"], event["message"]) for event in events_of(out, relay)]
    relay.on_state(SupervisorState.DEGRADED, "stopped restarting")
    seen += [(event["eventType"], event["status"], event["message"]) for event in events_of(out, relay)][2:]
    assert seen == [
        ("supervisor_state", "restarting", "the runtime exited with code 3; restarting in 1s"),
        ("runtime_stopping", "stopping", RESTARTING),
        ("supervisor_state", "degraded", "stopped restarting"),
        ("runtime_stopping", "stopping", GAVE_UP),
        ("runtime_error", "error", GAVE_UP),
    ]
    assert not relay._accepting
    assert "none are sent again" in RESTARTING


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
        self.bridges: dict[int, object] = {}  # pid -> verified process handle, opened when announced
        self.unverified: list[int] = []
        self.reading = threading.Event()
        self.reading.set()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        while True:
            self.reading.wait()  # cleared: the desktop stops reading and the pipe fills
            raw = self.process.stdout.readline()
            if not raw:
                return
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

    def _drain_one(self, timeout: float) -> bool:
        try:
            event = self.events.get(timeout=timeout)
        except queue.Empty:
            return False
        self.seen.append(event)
        self.pending.append(event)
        self._track(event)
        return True

    def _track(self, event: dict) -> None:
        pid = None
        if event.get("eventType") == "supervisor_state" and event.get("status") == "running":
            pid = int(event["message"].rsplit(" ", 1)[-1])
        if pid and pid not in self.bridges:
            self.bridges[pid] = self._owned_handle(pid)

    def _owned_handle(self, pid: int):
        """
        A handle to ``pid`` only if it is provably our supervisor's child.

        The handle is opened first: while it is held, the process object (and
        so its id) cannot be reused. Then, through that pinned id, the parent
        must be our supervisor and the creation time must not precede it.
        Anything else is never terminated by this fixture.
        """
        import _winapi

        import psutil

        try:
            handle = _winapi.OpenProcess(0x100000 | 0x1000 | 0x0001, False, pid)
        except OSError:
            return None  # already gone and reaped
        try:
            child = psutil.Process(pid)
            ours = psutil.Process(self.process.pid)
            if child.ppid() == ours.pid and child.create_time() >= ours.create_time():
                return handle
        except psutil.Error:
            pass
        if _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_OBJECT_0:
            # Already exited (its parent can no longer be read). Whatever process
            # this handle names is dead, so holding or "terminating" it is harmless.
            return handle
        _winapi.CloseHandle(handle)
        self.unverified.append(pid)
        return None

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

    def start(mode: str, **extra_environment: str) -> SupervisedDesktop:
        executable, environment = child_python()
        environment.update(extra_environment)
        instance = SupervisedDesktop(
            [executable, "-B", str(DRIVER), mode, uuid.uuid4().hex[:12]], environment,
        )
        made.append(instance)
        return instance

    yield start
    for instance in made:
        assert instance.close() == [], "a bridge outlived its test"
        assert instance.unverified == [], "a announced bridge could not be verified as ours"


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
    assert session.wait_for("runtime_warning")["message"] == notice("output_too_large", 1)
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
def test_a_desktop_that_stops_reading_cannot_hold_supervision_or_stop(desktop):
    import psutil

    session = desktop("chatty")
    session.wait_for("supervisor_state", "running")
    session.wait_for("runtime_ready")
    (pid,) = session.bridges
    session.reading.clear()  # the UI stops reading; its pipe, then the queue, then the bridge fill
    time.sleep(3)
    supervisor_memory = psutil.Process(session.process.pid).memory_info().rss
    assert supervisor_memory < 200 * 1024 * 1024, supervisor_memory  # bounded queues, not a backlog
    started = time.monotonic()
    session.send({"command": "shutdown"})  # its input is read by its own thread
    assert session.process.wait(20) == 0  # grace, terminate, a bounded drain, exit
    assert time.monotonic() - started < 15
    assert session.bridge_exited(pid, 5)
    session.reading.set()


@windows_only
def test_a_bridge_that_stops_reading_its_input_gets_refusals_not_a_backlog(desktop):
    session = desktop("deaf")
    session.wait_for("supervisor_state", "running")
    session.wait_for("runtime_ready")
    (pid,) = session.bridges
    for index in range(120):  # ~12 MB against a 4 MiB pending budget
        session.send({"command": "echo", "text": "y" * 100_000})
    refused = session.wait_for("runtime_error", timeout=20)
    assert refused["message"].endswith(NOTICES["busy"][2].split("}", 1)[1])
    assert "y" * 20 not in refused["message"]  # content is never echoed
    started = time.monotonic()
    session.send({"command": "shutdown"})
    session.wait_for("supervisor_state", "stopped", timeout=20)
    assert time.monotonic() - started < 10
    assert session.process.wait(15) == 0
    assert session.bridge_exited(pid, 5)


@windows_only
def test_a_command_cut_off_by_the_end_of_input_is_never_delivered(desktop):
    session = desktop("echo")
    session.wait_for("runtime_ready")
    session.process.stdin.write(b'{"command":"echo","text":"cut')
    session.process.stdin.close()
    assert session.wait_for("runtime_error")["message"] == notice("incomplete", 1)
    session.wait_for("supervisor_state", "stopped")
    assert session.process.wait(10) == 0
    assert not any(event.get("eventType") == "echo" for event in session.seen)


@windows_only
def test_a_bridge_that_acts_then_dies_is_never_sent_the_command_again(desktop, tmp_path):
    marker = tmp_path / "acted.txt"
    session = desktop("act", QRONOS_TEST_RELAY_MARKER=str(marker))
    session.wait_for("supervisor_state", "running")
    session.wait_for("runtime_ready")
    (first,) = session.bridges
    session.send({"command": "act"})
    assert session.wait_for("runtime_stopping")["message"] == RESTARTING  # "none are sent again"
    session.wait_for("supervisor_state", "running", timeout=30)
    session.wait_for("runtime_ready")
    second = next(pid for pid in session.bridges if pid != first)
    session.send({"command": "ping"})
    assert session.wait_for("runtime_pong")["message"] == str(second)
    assert marker.read_text(encoding="utf-8").splitlines() == [str(first)]  # once, by the first bridge only
    session.send({"command": "shutdown"})
    assert session.process.wait(15) == 0


@windows_only
def test_stop_does_not_depend_on_the_bridge_s_command_loop(desktop):
    session = desktop("echo")
    session.wait_for("supervisor_state", "running")
    session.wait_for("runtime_ready")
    (pid,) = session.bridges
    session.send({"command": "block"})  # the loop sleeps 3 s inside one command
    time.sleep(0.3)
    started = time.monotonic()
    session.send({"command": "shutdown"})
    session.wait_for("supervisor_state", "stopped", timeout=15)
    assert time.monotonic() - started < 6  # within the 2 s grace and the terminate step
    assert session.process.wait(10) == 0
    assert session.bridge_exited(pid, 5)


@windows_only
def test_old_generation_output_never_follows_its_restart_notice_or_the_new_ready(desktop, tmp_path):
    """F1: generation 1 floods past the output budget while the desktop is not reading, then dies."""
    session = desktop("flood-then-die", QRONOS_TEST_RELAY_DIR=str(tmp_path))
    session.reading.clear()
    deadline = time.monotonic() + 60
    while not (tmp_path / "gen-2-ready").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (tmp_path / "gen-2-ready").exists()
    time.sleep(1.0)
    session.reading.set()
    session.wait_for("runtime_ready", timeout=60)  # gen 1's
    ready2 = session.wait_for("runtime_ready", timeout=60)
    assert json.loads(ready2["message"])["generation"] == 2
    session.send({"command": "shutdown"})
    assert session.process.wait(20) == 0
    while session._drain_one(0.5):
        pass
    order = [(event.get("eventType"), event.get("message", "")) for event in session.seen]
    notice_at = order.index(("runtime_stopping", RESTARTING))
    ready_at = next(i for i, (kind, message) in enumerate(order)
                    if kind == "runtime_ready" and '"generation": 2' in message)
    late = [i for i, (kind, _) in enumerate(order) if kind == "old_output" and i > notice_at]
    assert late == [] and notice_at < ready_at
    assert any(kind == "runtime_warning" and message.endswith("had already stopped were dropped.")
               for kind, message in order)  # what was fenced is counted, not hidden


@windows_only
def test_the_restart_notice_survives_thousands_of_refusals_while_the_desktop_is_not_reading(desktop, tmp_path):
    """F2: refusals coalesce into one counted notice; the lifecycle lane is never dropped for space."""
    session = desktop("deaf-then-die", QRONOS_TEST_RELAY_DIR=str(tmp_path))
    session.wait_for("runtime_ready")
    session.reading.clear()
    for index in range(3000):
        session.send({"command": "echo", "text": f"c{index}"})
    (tmp_path / "die").write_text("1")
    deadline = time.monotonic() + 60
    while not (tmp_path / "gen-2-ready").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(1.0)
    session.reading.set()
    assert session.wait_for("runtime_stopping", timeout=30)["message"] == RESTARTING
    session.wait_for("supervisor_state", "running", timeout=30)
    errors = [event for event in session.seen if event.get("eventType") == "runtime_error"]
    assert len(errors) < 50  # counted, not one event per refused command
    session.send({"command": "shutdown"})
    assert session.process.wait(20) == 0


@windows_only
def test_stop_while_a_bridge_is_still_connecting_is_prompt(desktop, tmp_path):
    """F3: a bridge that never joins cannot hold Stop for the connect timeout."""
    session = desktop("noconnect", QRONOS_TEST_RELAY_DIR=str(tmp_path))
    deadline = time.monotonic() + 30
    while not (tmp_path / "gen-1").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    started = time.monotonic()
    session.send({"command": "shutdown"})
    assert session.process.wait(15) == 0
    assert time.monotonic() - started < 5.0  # the driver's connect timeout is 20 s


@windows_only
def test_the_desktop_closing_its_input_while_a_bridge_connects_is_prompt(desktop, tmp_path):
    session = desktop("noconnect", QRONOS_TEST_RELAY_DIR=str(tmp_path))
    deadline = time.monotonic() + 30
    while not (tmp_path / "gen-1").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    started = time.monotonic()
    session.process.stdin.close()
    assert session.process.wait(15) == 0
    assert time.monotonic() - started < 5.0


@windows_only
def test_a_bridge_that_dies_mid_line_reaches_the_desktop_as_a_count_not_a_fragment(desktop, tmp_path):
    """F4: never a newline added to a cut-off line; invalid UTF-8 never reaches the desktop."""
    session = desktop("cut-then-die", QRONOS_TEST_RELAY_DIR=str(tmp_path))
    session.wait_for("runtime_ready")
    session.wait_for("runtime_stopping", timeout=30)
    session.wait_for("supervisor_state", "running", timeout=30)
    ready = session.wait_for("runtime_ready", timeout=30)
    session.send({"command": "ping"})
    session.wait_for("runtime_pong")  # routing goes on after the bad output
    session.send({"command": "shutdown"})
    assert session.process.wait(20) == 0
    while session._drain_one(0.5):
        pass
    assert not any(event.get("eventType") in ("unparsed", "cut_off") for event in session.seen)
    messages = [event.get("message", "") for event in session.seen]
    assert any(message.startswith("1 output line(s) cut off") for message in messages)
    assert any(message.startswith("1 output line(s) from the Qronos runtime were not valid text") for message in messages)
    assert json.loads(ready["message"])["generation"] == 2


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
@requires_live_device
def test_the_production_entry_supervises_the_real_bridge_end_to_end(tmp_path):
    """
    LIVE-DEVICE PRODUCTION PROBE, default off (QRONOS_RUN_LIVE_DEVICE_TESTS=1
    only). It is not synthetic fixture acceptance and is not counted as one.

    What it really invokes (source-audited, not assumed):

    - ``python -m core.supervisor --desktop``, the production entry. It
      creates the PRODUCTION session objects ``Local\\QronosSupervisor`` (the
      instance mutex) and ``Local\\QronosSupervisedRuntime`` (the Job Object's
      mutex), which would collide with a real Qronos in the same session.
    - The REAL ``core.runtime_bridge --supervised`` as a child process. That
      child does not inherit pytest's in-process primitive stand-ins
      (tests/conftest.py), so ``QRONOS_RUN_LIVE_DEVICE_TESTS=0`` does not guard
      it.
    - Only the environment is passed down: conftest's isolated
      LOCALAPPDATA/APPDATA/USERPROFILE, plus the settings, history and queue
      paths below, and Ollama auto-start off.

    By source: QronosRuntime() builds settings, history and in-memory objects.
    Audio, wake, screen and the queue's GPU sampler are lazy, and this probe
    sends only ping and shutdown. "Not touched by the code paths read" is
    not a device guard, so the probe stays behind the live-device gate.
    """
    executable, environment = child_python()
    state = tmp_path
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
