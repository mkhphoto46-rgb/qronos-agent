"""
The Supervisor: restart rules as pure functions, and its whole lifecycle with
real, disposable child processes over a real authenticated local channel.

The children are tests/fixtures/supervised_child.py run as normal user
processes; they touch no device, model, application or user data.
"""

from __future__ import annotations

import ast
import io
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from core import supervisor
from core.supervisor import (
    BundledRuntimeLauncher,
    RestartBudget,
    Supervisor,
    SupervisorAlreadyRunning,
    SupervisorInstanceGuard,
    SupervisorPolicy,
    SupervisorState,
    read_bootstrap,
)
from core.supervisor_ipc import ChannelError
from tests.fixtures.child_python import child_python

CHILD = Path(__file__).parent / "fixtures" / "supervised_child.py"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Supervisor sessions are Windows named pipes.")
FAST = SupervisorPolicy(
    heartbeat_interval_seconds=0.1, heartbeat_timeout_seconds=1.0, connect_timeout_seconds=10.0,
    restart_backoff_initial_seconds=0.05, restart_backoff_max_seconds=0.2, max_restarts=2,
    restart_window_seconds=60.0, stable_after_seconds=60.0, shutdown_grace_seconds=3.0,
    terminate_grace_seconds=3.0,
)


# ------------------------------------------------------------ restart budget


def test_backoff_doubles_up_to_the_limit_then_gives_up():
    policy = SupervisorPolicy(restart_backoff_initial_seconds=1, restart_backoff_max_seconds=5,
                              max_restarts=4, restart_window_seconds=100)
    budget = RestartBudget(policy)
    assert [budget.after_failure(t, 1.0) for t in (0, 1, 2, 3)] == [1, 2, 4, 5]
    assert budget.after_failure(4, 1.0) is None


def test_restarts_outside_the_window_are_forgotten():
    policy = SupervisorPolicy(max_restarts=2, restart_window_seconds=10, restart_backoff_max_seconds=60)
    budget = RestartBudget(policy)
    assert budget.after_failure(0, 1.0) is not None
    assert budget.after_failure(1, 1.0) is not None
    assert budget.after_failure(2, 1.0) is None
    assert budget.after_failure(20, 1.0) is not None


def test_a_stable_run_resets_the_backoff():
    policy = SupervisorPolicy(restart_backoff_initial_seconds=1, restart_backoff_max_seconds=60,
                              max_restarts=10, stable_after_seconds=30)
    budget = RestartBudget(policy)
    assert [budget.after_failure(t, 1.0) for t in (0, 1, 2)] == [1, 2, 4]
    assert budget.after_failure(100, 45.0) == 1


@pytest.mark.parametrize("fields", [
    {"heartbeat_interval_seconds": 5, "heartbeat_timeout_seconds": 5},
    {"restart_backoff_initial_seconds": 10, "restart_backoff_max_seconds": 1},
    {"max_restarts": -1},
    {"shutdown_grace_seconds": 0},
])
def test_policy_refuses_inconsistent_limits(fields):
    with pytest.raises(ValueError):
        SupervisorPolicy(**fields)


# ----------------------------------------------------------------- bootstrap


def test_bootstrap_is_read_from_one_bounded_line():
    line = json.dumps({"address": "a", "key": "00" * 32, "supervisorPid": 7, "protocol": 1}).encode() + b"\n"
    assert read_bootstrap(io.BytesIO(line)) == {"address": "a", "key": bytes(32), "supervisor_pid": 7}


@pytest.mark.parametrize("data", [
    b"", b"{}\n", b"not json\n", b"{\"address\": \"a\"}",
    json.dumps({"address": "a", "key": "zz", "supervisorPid": 7, "protocol": 1}).encode() + b"\n",
    json.dumps({"address": "a", "key": "00", "supervisorPid": True, "protocol": 1}).encode() + b"\n",
    json.dumps({"address": "a", "key": "00", "supervisorPid": 7, "protocol": 2}).encode() + b"\n",
    b"x" * 5000 + b"\n",
])
def test_bad_bootstraps_are_refused(data):
    with pytest.raises(ChannelError):
        read_bootstrap(io.BytesIO(data))


# --------------------------------------------------------------- boundaries


def test_supervisor_modules_own_no_audio_ui_or_dialog():
    forbidden = ("audio", "sounddevice", "screen_capture", "tkinter", "PySide6", "dialog", "window",
                 "accessibility", "runtime_bridge", "orchestrator", "wake")
    for name in ("supervisor", "supervisor_ipc", "pipe_peer"):
        tree = ast.parse((Path(supervisor.__file__).parent / f"{name}.py").read_text(encoding="utf-8"))
        imported = {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not [module for module in imported if any(word in module.lower() for word in forbidden)], name


def test_launcher_accepts_only_an_absolute_executable_and_plain_words(tmp_path):
    with pytest.raises(ValueError):
        BundledRuntimeLauncher(Path("qronos-runtime.exe"), ["bridge"])
    for bad in (["bridge && calc"], ["%COMSPEC%"], ["a\"b"], [""]):
        with pytest.raises(ValueError):
            BundledRuntimeLauncher(tmp_path / "qronos-runtime.exe", bad)


def test_one_supervisor_per_session():
    existing = {"value": 0}

    def create(name):
        assert name == "Local\\QronosSupervisor"
        return 42

    closed = []
    first = SupervisorInstanceGuard.acquire(create, lambda: existing["value"], closed.append)
    existing["value"] = 183
    with pytest.raises(SupervisorAlreadyRunning):
        SupervisorInstanceGuard.acquire(create, lambda: existing["value"], closed.append)
    assert closed == [42]
    first.release()
    assert closed == [42, 42]


# ---------------------------------------------------- real child processes


class ChildLauncher:
    """Starts tests/fixtures/supervised_child.py as a normal child process."""

    def __init__(self, modes, record: Path | None = None):
        self.modes = list(modes)
        self.record = record
        self.children: list[subprocess.Popen] = []

    def launch(self, bootstrap: bytes):
        mode = self.modes.pop(0) if len(self.modes) > 1 else self.modes[0]
        executable, environment = child_python()
        command = [executable, str(CHILD), mode] + ([str(self.record)] if self.record else [])
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env=environment)
        child.stdin.write(bootstrap)
        child.stdin.close()
        self.children.append(child)
        return child


def run_in_thread(target):
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("state", target()), daemon=True)
    thread.start()
    return thread, result


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def all_gone(children) -> bool:
    return all(child.poll() is not None for child in children)


@pytest.fixture
def reap():
    launchers = []
    yield launchers.append
    for launcher in launchers:
        for child in launcher.children:
            if child.poll() is None:
                child.kill()
                child.wait(5)


@windows_only
def test_a_healthy_runtime_runs_and_is_shut_down_cleanly(tmp_path, reap):
    record = tmp_path / "record.json"
    launcher = ChildLauncher(["beat"], record)
    reap(launcher)
    states = []
    boss = Supervisor(launcher, FAST, notify=lambda state, detail: states.append(state))
    thread, result = run_in_thread(boss.run)

    assert wait_for(lambda: boss.state is SupervisorState.RUNNING)
    time.sleep(1.5)  # longer than the heartbeat timeout: beats keep it running
    assert boss.state is SupervisorState.RUNNING and boss.starts == 1

    boss.stop()
    thread.join(15)
    assert result["state"] is SupervisorState.STOPPED
    assert launcher.children[0].wait(10) == 0  # exited by itself, not killed
    assert json.loads(record.read_text())["reasons"] == ["supervisor stopping"]


@windows_only
def test_a_crash_loop_backs_off_then_stops_restarting(reap):
    launcher = ChildLauncher(["crash"])
    reap(launcher)
    boss = Supervisor(launcher, FAST)
    thread, result = run_in_thread(boss.run)
    thread.join(30)
    assert result["state"] is SupervisorState.DEGRADED
    assert boss.starts == FAST.max_restarts + 1
    assert wait_for(lambda: all_gone(launcher.children))


@windows_only
def test_a_runtime_that_stops_beating_is_stopped_and_restarted(reap):
    launcher = ChildLauncher(["hang", "beat"])
    reap(launcher)
    boss = Supervisor(launcher, FAST)
    thread, result = run_in_thread(boss.run)

    assert wait_for(lambda: boss.starts == 2 and boss.state is SupervisorState.RUNNING, 20)
    assert launcher.children[0].poll() is not None  # the hung child was stopped
    boss.stop()
    thread.join(15)
    assert result["state"] is SupervisorState.STOPPED


@windows_only
@pytest.mark.parametrize("mode", ["silent", "wrong-key", "stall-auth"])
def test_a_runtime_that_cannot_authenticate_is_never_trusted(mode, reap):
    # stall-auth opens the pipe as the launched process and never answers the
    # challenge: the supervisor must still give up within its connect deadline
    # and stop that exact child.
    policy = SupervisorPolicy(**{**FAST.__dict__, "connect_timeout_seconds": 2.0, "max_restarts": 0})
    launcher = ChildLauncher([mode])
    reap(launcher)
    boss = Supervisor(launcher, policy)
    started = time.monotonic()
    assert boss.run() is SupervisorState.DEGRADED
    assert time.monotonic() - started < 2.0 + 2 * policy.terminate_grace_seconds + 3.0
    assert wait_for(lambda: all_gone(launcher.children))


@pytest.mark.skipif(sys.platform != "win32", reason="Peer process ids are checked on Windows.")
def test_a_different_process_with_the_key_is_refused(reap):
    policy = SupervisorPolicy(**{**FAST.__dict__, "connect_timeout_seconds": 5.0, "max_restarts": 0})
    launcher = ChildLauncher(["imposter"])
    reap(launcher)
    boss = Supervisor(launcher, policy)
    assert boss.run() is SupervisorState.DEGRADED
    assert wait_for(lambda: all_gone(launcher.children))


@windows_only
def test_only_the_launched_child_is_ever_stopped(reap):
    executable, environment = child_python()
    bystander = subprocess.Popen([executable, "-c", "import time; time.sleep(30)"], env=environment)
    try:
        launcher = ChildLauncher(["hang"])
        reap(launcher)
        policy = SupervisorPolicy(**{**FAST.__dict__, "max_restarts": 0})
        assert Supervisor(launcher, policy).run() is SupervisorState.DEGRADED
        assert bystander.poll() is None and psutil.pid_exists(bystander.pid)
    finally:
        bystander.kill()
        bystander.wait(5)
