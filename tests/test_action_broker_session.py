"""
The runtime-to-broker round trip with a real, disposable, NON-elevated broker
child process (tests/fixtures/broker_child.py) over a real authenticated pipe,
and the UAC launcher with every Win32 call injected. Nothing is elevated,
nothing asks for UAC, and the only file written is a marker in tmp_path.
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.action_broker_client import (
    BrokerResult,
    BrokerSession,
    BrokerUnavailable,
    TypedAction,
    broker_arguments,
    build_approval,
)
from core.actions import ActionRequest
from core import uac_launch
from core.uac_launch import SHELLEXECUTEINFOW, UacBrokerLauncher, UacDeclined, Win32, command_parameters
from security import gate
from security.permissions import ActionCategory
from tests.fixtures.child_python import child_python

BROKER_CHILD = Path(__file__).parent / "fixtures" / "broker_child.py"


def grant_for(request: ActionRequest):
    verdict = gate.evaluate(request)
    if verdict.needs_approval:
        return gate.issue_grant(request, verdict, approval=verdict.decision)
    return gate.issue_grant(request, verdict)


def marker_request(name: str):
    action = TypedAction("fixture.write_marker", {"name": name})
    request = ActionRequest(category=ActionCategory.CREATE_OR_EDIT_FILE, target=f"test marker {name}",
                            summary="Write a test marker file.")
    return action, request, grant_for(request)


class ChildBroker:
    """A non-elevated broker child: same arguments as production, test registry."""

    def __init__(self, process: subprocess.Popen):
        self._process = process
        self.pid = process.pid

    def wait(self, timeout: float):
        try:
            return self._process.wait(timeout)
        except subprocess.TimeoutExpired:
            return None


class ChildLauncher:
    def __init__(self, work: Path, extra: tuple[str, ...] = (), imposter: bool = False):
        self.work = work
        self.extra = extra
        self.imposter = imposter
        self.processes: list[subprocess.Popen] = []

    def launch(self, address: str, key: bytes, runtime_pid: int):
        executable, environment = child_python()
        command = [executable, str(BROKER_CHILD), str(self.work), *broker_arguments(address, key, runtime_pid)[1:],
                   *self.extra]
        real = subprocess.Popen(command, env=environment)
        self.processes.append(real)
        if self.imposter:
            # Report a different process than the one that will connect.
            decoy = subprocess.Popen([executable, "-c", "import time; time.sleep(20)"], env=environment)
            self.processes.append(decoy)
            return ChildBroker(decoy)
        return ChildBroker(real)

    def reap(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(10)


@pytest.fixture
def launchers():
    made = []
    yield made.append
    for launcher in made:
        launcher.reap()


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Broker sessions are Windows named pipes.")


@windows_only
def test_an_approved_action_runs_once_in_the_broker_process(tmp_path, launchers):
    launcher = ChildLauncher(tmp_path)
    launchers(launcher)
    session = BrokerSession(launcher, accept_timeout_seconds=20, reply_timeout_seconds=20)
    action, request, grant = marker_request("alpha")

    result = session.perform(action, request, grant)

    assert result == BrokerResult(ok=True, outcome="performed", detail="marker alpha written")
    assert (tmp_path / "markers" / "alpha.done").read_text() == "performed"
    assert launcher.processes[0].wait(10) == 0
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [record["outcome"] for record in audit] == ["performed"]

    with pytest.raises(BrokerUnavailable, match="already been used"):
        session.perform(action, request, grant)
    assert len(launcher.processes) == 1  # refused before anything was launched


@windows_only
def test_a_broker_that_acts_on_other_parameters_is_refused_by_the_digest(tmp_path, launchers):
    launcher = ChildLauncher(tmp_path, extra=("--tamper",))
    launchers(launcher)
    action, request, grant = marker_request("alpha")
    result = BrokerSession(launcher, accept_timeout_seconds=20, reply_timeout_seconds=20).perform(action, request, grant)
    assert result.ok is False and result.outcome == "not_approved"
    assert not (tmp_path / "markers").exists()


@windows_only
def test_a_broker_with_no_such_action_refuses_and_writes_nothing(tmp_path, launchers):
    launcher = ChildLauncher(tmp_path)
    launchers(launcher)
    action = TypedAction("fixture.unregistered", {"name": "alpha"})
    request = ActionRequest(category=ActionCategory.CREATE_OR_EDIT_FILE, target="test marker alpha",
                            summary="Write a test marker file.")
    result = BrokerSession(launcher, accept_timeout_seconds=20, reply_timeout_seconds=20).perform(
        action, request, grant_for(request))
    assert result.outcome == "unknown_action" and not (tmp_path / "markers").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Peer process ids are checked on Windows.")
def test_a_process_other_than_the_launched_one_is_never_sent_the_action(tmp_path, launchers):
    launcher = ChildLauncher(tmp_path, imposter=True)
    launchers(launcher)
    action, request, grant = marker_request("alpha")
    with pytest.raises(BrokerUnavailable, match="did not connect"):
        BrokerSession(launcher, accept_timeout_seconds=10, reply_timeout_seconds=5).perform(action, request, grant)
    assert not (tmp_path / "markers").exists()


def test_only_a_gate_grant_for_this_exact_request_is_accepted(tmp_path):
    action, request, grant = marker_request("alpha")
    other = ActionRequest(category=ActionCategory.CREATE_OR_EDIT_FILE, target="test marker beta",
                          summary="Write a test marker file.")
    with pytest.raises(BrokerUnavailable, match="does not belong"):
        build_approval(action, other, grant, bytes(32))
    with pytest.raises(BrokerUnavailable, match="Only a permission-gate grant"):
        BrokerSession(ChildLauncher(tmp_path)).perform(action, request, object())  # type: ignore[arg-type]
    with pytest.raises(BrokerUnavailable, match="expired"):
        build_approval(action, request, grant, bytes(32), monotonic=lambda: grant.expires_at + 1)


def test_the_approval_lifetime_is_capped_at_the_broker_limit():
    action, request, grant = marker_request("alpha")
    approval = build_approval(action, request, grant, bytes(32), wall_clock=lambda: 1000.0)
    assert approval["issuedAt"] == 1000.0 and 0 < approval["expiresAt"] - 1000.0 <= 180.0


@windows_only
def test_a_launch_that_fails_is_reported_and_nothing_is_sent(tmp_path):
    class Failing:
        def launch(self, address, key, runtime_pid):
            raise UacDeclined("declined")

    action, request, grant = marker_request("alpha")
    with pytest.raises(BrokerUnavailable, match="not started"):
        BrokerSession(Failing()).perform(action, request, grant)


# ------------------------------------------------------------- UAC launcher


class FakeWin32:
    """Records what would be asked of Windows; calls nothing."""

    def __init__(self, succeed=True, error=0, pid=4321):
        self.info = None
        self.closed = []
        self.succeed, self.error, self.pid = succeed, error, pid

    def build(self) -> Win32:
        def shell_execute_ex(pointer):
            # Windows reads the structure during the call; so does this fake.
            info = ctypes.cast(pointer, ctypes.POINTER(SHELLEXECUTEINFOW)).contents
            self.info = {name: getattr(info, name) for name, _ in SHELLEXECUTEINFOW._fields_}
            if self.succeed:
                info.hProcess = 99
            return 1 if self.succeed else 0

        return Win32(
            shell_execute_ex=shell_execute_ex,
            get_process_id=lambda handle: self.pid,
            wait_for_single_object=lambda handle, ms: 0,
            get_exit_code_process=lambda handle, code: (setattr(ctypes.cast(code, ctypes.POINTER(ctypes.c_ulong)).contents, "value", 0) or 1),
            close_handle=lambda handle: self.closed.append(handle) or 1,
            last_error=lambda: self.error,
        )


def test_the_broker_is_started_through_the_normal_uac_prompt(tmp_path):
    fake = FakeWin32()
    executable = tmp_path / "qronos-runtime.exe"
    address = r"\\.\pipe\qronos-action-broker-" + "a" * 32
    broker = UacBrokerLauncher(executable, fake.build()).launch(address, bytes(range(32)), 1234)

    assert fake.info["lpVerb"] == "runas"
    assert fake.info["lpFile"] == str(executable)
    assert fake.info["lpParameters"] == " ".join(broker_arguments(address, bytes(range(32)), 1234))
    assert fake.info["fMask"] & uac_launch.SEE_MASK_NOCLOSEPROCESS
    assert fake.info["cbSize"] == ctypes.sizeof(SHELLEXECUTEINFOW)
    assert broker.pid == 4321 and broker.wait(1.0) == 0 and fake.closed == [99]


def test_declining_uac_is_reported_as_declined():
    fake = FakeWin32(succeed=False, error=uac_launch.ERROR_CANCELLED)
    with pytest.raises(UacDeclined):
        UacBrokerLauncher(Path("C:/Qronos/qronos-runtime.exe"), fake.build()).launch(
            r"\\.\pipe\qronos-action-broker-" + "a" * 32, bytes(32), 1)


@pytest.mark.parametrize("executable", ["qronos-runtime.exe", "C:/Qronos/qronos-runtime.bat", "C:/Qronos/cmd"])
def test_only_an_absolute_exe_can_be_the_broker(executable):
    with pytest.raises(ValueError):
        UacBrokerLauncher(Path(executable), FakeWin32().build())


@pytest.mark.parametrize("argument", ["a b", "a&b", "\"quoted\"", "a|b", "%PATH%", "", "x" * 200])
def test_no_unusual_argument_reaches_the_elevated_command_line(argument):
    with pytest.raises(ValueError):
        command_parameters(["action-broker", argument])


def test_the_uac_module_never_suppresses_or_preanswers_the_prompt():
    source = Path(uac_launch.__file__).read_text(encoding="utf-8")
    for forbidden in ("ConsentPromptBehavior", "EnableLUA", "SEE_MASK_FLAG_NO_UI", "winreg", "subprocess",
                      "CreateProcessAsUser", "ImpersonateLoggedOnUser", "fodhelper", "computerdefaults"):
        assert forbidden not in source
