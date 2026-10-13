"""Store admission (core/store_admission.py) and the update launch's hand-over to the installer.

Safety: LockFileEx byte-range locks on a fixture file in tmp_path; synthetic "store" files in
tmp_path; base-interpreter child processes (tests/fixtures/child_python.py) owned, waited with
bounds and killed through their own handles; named mutexes only with unique test names. No
installer, runtime or real store is touched.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from core import store_admission, update_service
from core.local_update import UpdateError
from tests.fixtures.child_python import child_python
from tests.test_update_service import SECRET, Recorder, World, staged  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(os.name != "nt", reason="LockFileEx and named mutexes are Windows")

# A cooperating writer: admission first, only then the store; reports each step in a file.
WRITER = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core.local_update import UpdateError
from core.store_admission import admit_writer
data, log, go, stop = (Path(a) for a in sys.argv[2:6])
fence = sys.argv[6]
try:
    admission = admit_writer(data, fence=fence)
except UpdateError as error:
    log.write_text("refused:" + error.code)
    raise SystemExit(0)
log.write_text("admitted")
deadline = time.monotonic() + 60
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
with open(data / "store.json", "a", encoding="utf-8") as store:   # opened only after admission
    store.write("written\n")
while not stop.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
admission.release()
"""


def unique_fence() -> str:
    return f"Local\\QronosUpdateTest-fence-{uuid.uuid4().hex}"


class Writer:
    def __init__(self, tmp: Path, data: Path, name: str, fence: str | None = None) -> None:
        self.log, self.go, self.stop = tmp / f"{name}.log", tmp / f"{name}.go", tmp / f"{name}.stop"
        executable, environment = child_python()
        self.process = subprocess.Popen(
            [executable, "-c", WRITER, str(ROOT), str(data), str(self.log), str(self.go), str(self.stop),
             fence or unique_fence()],
            env=environment)

    def state(self, timeout: float = 30.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.log.exists() and self.log.read_text():
                return self.log.read_text()
            time.sleep(0.02)
        raise AssertionError("the writer child never reported")

    def finish(self) -> None:
        self.go.write_text("1")
        self.stop.write_text("1")
        try:
            self.process.wait(timeout=30)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=30)


def test_writers_share_admission_and_the_updater_cannot_take_it_while_one_is_inside(tmp_path):
    data = tmp_path / "data"
    fence = unique_fence()
    first, second = store_admission.admit_writer(data, fence), store_admission.admit_writer(data, fence)
    try:
        with pytest.raises(UpdateError) as refused:
            store_admission.admit_exclusive(data)
        assert refused.value.code == "writers_active"
    finally:
        first.release()
        second.release()
    store_admission.admit_exclusive(data).release()


def test_the_check_and_the_open_are_one_step_across_processes(tmp_path):
    """No check-to-open race: a writer is either admitted (and the updater refused) or refused (and never
    opens its store), whatever the interleaving."""
    data = tmp_path / "data"
    data.mkdir()
    store = data / "store.json"

    inside = Writer(tmp_path, data, "inside")
    try:
        assert inside.state() == "admitted"
        with pytest.raises(UpdateError) as refused:
            store_admission.admit_exclusive(data)  # a writer is in (or about to open) its store
        assert refused.value.code == "writers_active"
    finally:
        inside.finish()
    assert store.read_text() == "written\n"

    exclusive = store_admission.admit_exclusive(data)
    late = Writer(tmp_path, data, "late")
    try:
        assert late.state() == "refused:update_in_progress"
    finally:
        late.finish()
        exclusive.release()
    assert store.read_text() == "written\n"  # the refused writer never opened the store


def test_the_admission_file_must_be_this_profile_s_own(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"")
    os.link(outside, data / store_admission.ADMISSION_FILE)
    with pytest.raises(UpdateError) as refused:
        store_admission.admit_writer(data, unique_fence())
    assert refused.value.code == "store"


INSTALLER_CHILD = ROOT / "tests" / "fixtures" / "update_installer_child.py"


class StandIn:
    """The stand-in installer (tests/fixtures/update_installer_child.py), started by the REAL start_fenced."""

    def __init__(self, tmp: Path, data: Path, fence: str, mode: str) -> None:
        self.go, self.report = tmp / "installer-may-finish", tmp / "installer-report.json"
        self.data, self.fence, self.mode = data, fence, mode
        self.processes: list = []

    def argv(self) -> list[str]:
        executable, _ = child_python()
        return [executable, "-B", "-I", str(INSTALLER_CHILD), str(ROOT), self.fence, str(self.data), self.mode,
                str(self.go), str(self.report)]

    def launcher(self, path, arguments, handover):
        process = update_service.start_fenced(self.argv(), str(self.go.parent), handover)
        self.processes.append(process)
        return process

    def state(self, key: str = "installing", timeout: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.report.exists():
                try:
                    found = __import__("json").loads(self.report.read_text(encoding="utf-8"))
                except ValueError:
                    found = {}
                if key in found or found.get("refused"):
                    return found
            time.sleep(0.05)
        raise AssertionError("the stand-in installer never reported")

    def finish(self) -> None:
        self.go.write_text("1")
        for process in self.processes:
            try:
                process.wait(timeout=60)
            finally:
                if process.poll() is None:
                    process.kill()  # exactly this test's own child, by its own handle
                    process.wait(timeout=30)


def _launch_in_thread(service, token):
    result: dict = {}
    announced = threading.Event()

    def on_launched(answer):
        result["answer"] = answer
        announced.set()

    def run():
        try:
            result["handoff"] = service.launch(token, on_launched=on_launched)
        except BaseException as error:  # reported to the test
            result["error"] = error
            announced.set()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, announced, result


def test_the_installer_takes_the_exclusion_over_and_writers_stay_out_until_it_ends(tmp_path):
    """Round-7 G: after the acknowledgement the launching process lets go and returns; the installer itself
    holds the fence and the exclusive admission, so no writer gets in until it ends."""
    world = World(tmp_path)
    fence = unique_fence()
    installer = StandIn(tmp_path, world.data, fence, "update,receipt,keep_guard")
    service = world.service(quiesce=lambda: update_service.LaunchHold(world.data, (fence,)),
                            launcher=installer.launcher)
    _, approved = staged(service, *world.release("0.2.0"))
    thread, announced, result = _launch_in_thread(service, approved["launchToken"])
    try:
        assert announced.wait(60) and "error" not in result, result
        thread.join(30)
        assert not thread.is_alive() and result["handoff"]["handedOver"] is True  # the guard returned
        report = installer.state()
        assert report["steps"][:5] == ["record", "fence_owned_by_guard", "ack", "fence_taken_over", "admission"]
        assert report["fenceInheritable"] is False  # the duplicated fence cannot pass to a grandchild
        assert installer.processes[0].poll() is None  # still installing
        assert store_admission.update_in_progress(fence) is True
        probe = Writer(tmp_path, world.data, "during-install", fence=fence)
        try:
            assert probe.state() == "refused:update_in_progress"
        finally:
            probe.finish()
    finally:
        installer.finish()
        thread.join(30)
    assert installer.state("refused")["steps"][-2:] == ["receipt", "installed"]
    assert store_admission.update_in_progress(fence) is False
    after = Writer(tmp_path, world.data, "after-install", fence=fence)
    try:
        assert after.state() == "admitted"
    finally:
        after.finish()
    recovered = world.service("0.2.0", liveness=lambda: store_admission.update_in_progress(fence)) \
        .recover_before_stores()
    assert recovered["result"] == "applied" and recovered["receipt"] == "verified"


def test_an_installer_that_does_not_acknowledge_stays_under_the_guard(tmp_path):
    """No hand-over: the launching process keeps the whole exclusion until that installer ends."""
    world = World(tmp_path)
    fence = unique_fence()
    installer = StandIn(tmp_path, world.data, fence, "update,silent")
    service = world.service(quiesce=lambda: update_service.LaunchHold(world.data, (fence,)),
                            launcher=installer.launcher,
                            acknowledged=lambda process, nonce: update_service.read_acknowledgement(process, nonce,
                                                                                                    2.0))
    _, approved = staged(service, *world.release("0.2.0"))
    thread, announced, result = _launch_in_thread(service, approved["launchToken"])
    try:
        assert announced.wait(60) and result["answer"]["handedOver"] is False
        assert installer.state()["steps"][-1] == "under_guard"
        assert thread.is_alive()  # still guarding
        probe = Writer(tmp_path, world.data, "during-install", fence=fence)
        try:
            assert probe.state() == "refused:update_in_progress"
        finally:
            probe.finish()
    finally:
        installer.finish()
        thread.join(60)
    assert not thread.is_alive() and result["handoff"]["handedOver"] is False
    assert store_admission.update_in_progress(fence) is False


def test_nothing_the_installer_starts_can_keep_the_fence_alive(tmp_path):
    """Control r5 observation: a grandchild started WITH handle inheritance must not pin the fence."""
    import json
    import _winapi

    world = World(tmp_path)
    fence = unique_fence()
    installer = StandIn(tmp_path, world.data, fence, "update,grandchild,keep_guard")
    service = world.service(quiesce=lambda: update_service.LaunchHold(world.data, (fence,)),
                            launcher=installer.launcher)
    _, approved = staged(service, *world.release("0.2.0"))
    started = time.time()
    thread, announced, result = _launch_in_thread(service, approved["launchToken"])
    grandchild = None
    try:
        assert announced.wait(60) and result["answer"]["handedOver"] is True
        grandchild = _open_owned(installer.state("grandchildPid")["grandchildPid"], started)
        installer.finish()  # the installer ends; its grandchild keeps running
        assert _winapi.WaitForSingleObject(grandchild, 0) == _winapi.WAIT_TIMEOUT
        assert store_admission.update_in_progress(fence) is False
        after = Writer(tmp_path, world.data, "with-grandchild-alive", fence=fence)
        try:
            assert after.state() == "admitted"
        finally:
            after.finish()
    finally:
        installer.finish()
        thread.join(30)
        (tmp_path / "installer-may-finish.grandchild").write_text("1")
        if grandchild is not None:
            if _winapi.WaitForSingleObject(grandchild, 30_000) != _winapi.WAIT_OBJECT_0:
                _winapi.TerminateProcess(grandchild, 1)  # verified as this test's own grandchild above
            _winapi.CloseHandle(grandchild)


def test_a_writer_inside_its_store_makes_the_launch_refuse(tmp_path):
    world = World(tmp_path)
    names = (f"Local\\QronosUpdateTest-busy-{uuid.uuid4().hex}",)
    service = world.service(quiesce=lambda: update_service.LaunchHold(world.data, names))
    _, approved = staged(service, *world.release("0.2.0"))
    inside = Writer(tmp_path, world.data, "inside", fence=names[0])
    try:
        assert inside.state() == "admitted"
        with pytest.raises(UpdateError) as refused:
            service.launch(approved["launchToken"])
        assert refused.value.code == "writers_active"
    finally:
        inside.finish()
    assert world.launcher.calls == [] and service.status()["launchReady"] is not None


# A launching guard that dies right after starting the installer (before it could read the acknowledgement):
# its byte-range lock and mutex ownership die with it; the fence lives on in the installer's duplicated handle,
# and the installer takes the abandoned fence and the free admission over.
GUARD = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core import update_service
data, fence, info, child = Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), sys.argv[5:]
hold = update_service.LaunchHold(data, (fence,))
handover = update_service.Handover(os.urandom(32), hold.fence_handle(), data / "Updates" / "receipts", "0.2.0",
                                   "a" * 64, "b" * 64)
installer = update_service.start_fenced(child, str(data), handover)
info.write_text(str(installer.pid))
os._exit(9)  # the guard crashes: nothing is released by this process
"""


def _open_owned(pid: int, started_after: float):
    """Open a fixture process by pid, but only if it is a process created during this test."""
    import ctypes
    from ctypes import wintypes

    import _winapi

    handle = _winapi.OpenProcess(0x1000 | 0x100000 | 0x0001, False, pid)  # QUERY_LIMITED | SYNCHRONIZE | TERMINATE
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    times = [wintypes.FILETIME() for _ in range(4)]
    assert kernel.GetProcessTimes(handle, *[ctypes.byref(x) for x in times])
    created = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 10_000_000 - 11_644_473_600
    assert created >= started_after - 2, "the pid does not belong to this test's fixture process"
    return handle


def test_a_guard_that_dies_leaves_the_fence_up_until_the_installer_ends(tmp_path):
    """Root's failure case, with the round-7 hand-over: the launching guard dies after starting the installer.
    The installer takes the abandoned fence and the admission over; no writer may open a store while it lives;
    once it ends, writers are admitted again."""
    import json
    import _winapi

    data = tmp_path / "data"
    (data / "Updates" / "receipts").mkdir(parents=True)
    store = data / "store.json"
    fence = unique_fence()
    info = tmp_path / "installer.pid"
    stand_in = StandIn(tmp_path, data, fence, "update")
    started = time.time()
    executable, environment = child_python()
    guard = subprocess.Popen([executable, "-c", GUARD, str(ROOT), str(data), fence, str(info), *stand_in.argv()],
                             env=environment)
    installer = None
    try:
        guard.wait(timeout=60)
        assert guard.returncode == 9, "the guard should have crashed on purpose"
        installer = _open_owned(int(info.read_text()), started)
        report = stand_in.state()
        assert report.get("refused") is None and "admission" in report["steps"] and "guard_gone" in report["steps"]
        assert _winapi.WaitForSingleObject(installer, 0) == _winapi.WAIT_TIMEOUT  # still installing
        assert store_admission.update_in_progress(fence) is True

        during = Writer(tmp_path, data, "after-guard-crash", fence=fence)
        try:
            assert during.state() == "refused:update_in_progress"
        finally:
            during.finish()
        assert not store.exists(), "a writer opened its store while the installer was still running"

        stand_in.go.write_text("1")
        assert _winapi.WaitForSingleObject(installer, 60_000) == _winapi.WAIT_OBJECT_0
    finally:
        stand_in.go.write_text("1")
        if guard.poll() is None:
            guard.kill()
            guard.wait(timeout=30)
        if installer is not None:
            if _winapi.WaitForSingleObject(installer, 30_000) != _winapi.WAIT_OBJECT_0:
                _winapi.TerminateProcess(installer, 1)  # verified as this test's own fixture above
            _winapi.CloseHandle(installer)
    assert json.loads(stand_in.report.read_text(encoding="utf-8"))["steps"][-1] == "installed"
    assert store_admission.update_in_progress(fence) is False
    after = Writer(tmp_path, data, "after-install-ended", fence=fence)
    try:
        assert after.state() == "admitted"
    finally:
        after.finish()
    assert store.read_text() == "written\n"


def test_a_writer_holding_its_admission_never_sees_the_fence_appear(tmp_path):
    """Order: shared lock, then fence. While a writer holds the lock the updater cannot take the exclusive
    admission, so it cannot create the fence; the launch is refused instead."""
    data = tmp_path / "data"
    fence = unique_fence()
    writer = store_admission.admit_writer(data, fence)
    try:
        with pytest.raises(UpdateError) as refused:
            update_service.LaunchHold(data, (fence,))
        assert refused.value.code == "writers_active"
        assert store_admission.update_in_progress(fence) is False  # no fence was created
    finally:
        writer.release()


# ---------------------------------------------------------------- the fence probe: present, absent, or unknown
# A FULLY FAKE kernel: ctypes.WinDLL and ctypes.get_last_error are replaced for the call, so no DLL function runs and
# no named object (of any name) is opened; the fake only records which name it was asked for.

ERROR_FILE_NOT_FOUND, ERROR_ACCESS_DENIED, ERROR_NOT_ENOUGH_MEMORY, ERROR_INVALID_PARAMETER = 2, 5, 8, 87


class FakeKernel:
    class Function:
        def __init__(self, body) -> None:
            self.body = body

        def __call__(self, *args):
            return self.body(*args)

    def __init__(self, handle, error: int) -> None:
        self.handle, self.error = handle, error
        self.opened: list = []
        self.closed: list = []
        self.OpenMutexW = self.Function(self._open)
        self.CloseHandle = self.Function(lambda handle: self.closed.append(handle) or 1)

    def _open(self, access, inherit, name):
        self.opened.append(name)
        if isinstance(self.handle, BaseException):
            raise self.handle  # the call itself failed (e.g. a broken binding)
        return self.handle


def fake_kernel(monkeypatch, handle, error: int) -> FakeKernel:
    import ctypes

    kernel = FakeKernel(handle, error)
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, use_last_error=False: kernel)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: kernel.error)
    return kernel


def test_the_fence_probe_answers_present_absent_or_unknown_never_a_guess(monkeypatch):
    fence = unique_fence()
    present = fake_kernel(monkeypatch, 0x1234, 0)
    assert store_admission.update_in_progress(fence) is True and present.closed == [0x1234]  # its handle is closed
    absent = fake_kernel(monkeypatch, None, ERROR_FILE_NOT_FOUND)  # the one documented "does not exist"
    assert store_admission.update_in_progress(fence) is False and absent.opened == [fence] and absent.closed == []
    denied = fake_kernel(monkeypatch, None, ERROR_ACCESS_DENIED)  # exists but cannot be opened: an update runs
    assert store_admission.update_in_progress(fence) is True and denied.closed == []
    for error in (ERROR_NOT_ENOUGH_MEMORY, ERROR_INVALID_PARAMETER, 0, 3, 6, 0xFFFFFFFF):
        unknown = fake_kernel(monkeypatch, None, error)
        with pytest.raises(UpdateError) as refused:  # never "no update"
            store_admission.update_in_progress(fence)
        assert refused.value.code == "ownership_unavailable" and str(error) in str(refused.value)
        assert unknown.opened == [fence]


def test_off_windows_there_is_no_fence_and_nothing_is_asked(monkeypatch):
    import types

    kernel = fake_kernel(monkeypatch, None, ERROR_NOT_ENOUGH_MEMORY)
    monkeypatch.setattr(store_admission, "os", types.SimpleNamespace(name="posix"))
    assert store_admission.update_in_progress(unique_fence()) is False and kernel.opened == []


class HeldAdmission:
    def __init__(self) -> None:
        self.released = 0

    def release(self) -> None:
        self.released += 1


def test_a_writer_admission_already_taken_is_given_back_when_the_fence_is_unknown(monkeypatch, tmp_path):
    from core import startup_admission

    taken: list[HeldAdmission] = []
    monkeypatch.setattr(store_admission, "_admit", lambda root, exclusive: taken.append(HeldAdmission()) or taken[-1])
    for error, code in ((ERROR_NOT_ENOUGH_MEMORY, "ownership_unavailable"), (ERROR_INVALID_PARAMETER,
                                                                          "ownership_unavailable"),
                        (ERROR_ACCESS_DENIED, "update_in_progress")):
        fake_kernel(monkeypatch, None, error)
        with pytest.raises(UpdateError) as refused:
            store_admission.admit_writer(tmp_path, fence=unique_fence())
        assert refused.value.code == code and taken[-1].released == 1  # the shared lock is given back at once
    fake_kernel(monkeypatch, OSError("the call failed"), 0)  # a probe that raises: still given back, typed
    with pytest.raises(UpdateError) as refused:
        store_admission.admit_writer(tmp_path, fence=unique_fence())
    assert refused.value.code == "ownership_unavailable" and taken[-1].released == 1
    fake_kernel(monkeypatch, None, ERROR_FILE_NOT_FOUND)
    assert store_admission.admit_writer(tmp_path, fence=unique_fence()) is taken[-1] and taken[-1].released == 0
    # Through the start-up order (its default admission is admit_writer on the default fence, asked of the fake):
    # a typed refusal, nothing kept.
    kernel = fake_kernel(monkeypatch, None, ERROR_INVALID_PARAMETER)
    startup_admission._reset_for_tests()
    try:
        with pytest.raises(startup_admission.StartupRefused) as start:
            startup_admission.enter("settings", data_root=tmp_path, version="0.1.0", recover=lambda v: {"ok": True},
                                    confirm=lambda v: {"ok": True}, windows=True, installed=True)
        assert start.value.code == "ownership_unavailable" and taken[-1].released == 1
        assert not startup_admission.admitted() and kernel.opened == [store_admission.UPDATE_IN_PROGRESS_MUTEX]
    finally:
        startup_admission._reset_for_tests()


@pytest.mark.parametrize("error, code", [(ERROR_NOT_ENOUGH_MEMORY, "ownership_unavailable"),
                                         (ERROR_INVALID_PARAMETER, "ownership_unavailable"),
                                         (ERROR_ACCESS_DENIED, "update_in_progress")])
def test_a_fenced_backend_service_is_refused_before_import_on_an_unknown_or_denied_fence(error, code, monkeypatch,
                                                                                         capsys):
    import json

    from build_support import runtime_entry
    from core import startup_admission

    monkeypatch.setattr(sys, "argv", list(sys.argv))
    imported: list = []
    monkeypatch.setattr(runtime_entry.importlib, "import_module",
                        lambda name: imported.append(name) or type("M", (), {"main": staticmethod(lambda: 0)}))
    kernel = fake_kernel(monkeypatch, None, error)
    assert runtime_entry.main(["audio"]) == startup_admission.STARTUP_REFUSED_EXIT
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["code"] == code and imported == []
    assert kernel.opened == [store_admission.UPDATE_IN_PROGRESS_MUTEX]  # asked of the fake only
    fake_kernel(monkeypatch, None, ERROR_FILE_NOT_FOUND)  # control: a documented absence lets it start
    assert runtime_entry.main(["audio"]) == 0 and imported == [runtime_entry.SERVICES["audio"]]


def test_the_updaters_own_checks_and_the_instance_guard_refuse_on_an_unknown_fence(monkeypatch, tmp_path):
    from core.instance_ownership import MutexApi, OwnershipUnavailable, acquire_unless_updating

    world = World(tmp_path)
    service = world.service("0.1.0", liveness=update_service.update_in_progress)
    service.store.root.mkdir(parents=True, exist_ok=True)
    fake_kernel(monkeypatch, None, ERROR_INVALID_PARAMETER)
    for check in (service.status, service.recover_before_stores):
        with pytest.raises(UpdateError) as refused:
            check()
        assert refused.value.code == "ownership_unavailable"
    calls: list = []
    api = MutexApi(lambda name: calls.append("create") or 77, lambda handle, ms: calls.append("wait") or 0,
                   lambda handle: calls.append("release") or True, lambda handle: calls.append("close") or True,
                   lambda: 0)
    with pytest.raises(OwnershipUnavailable):
        acquire_unless_updating("Local\\QronosR61Test-singleton", api=api)
    assert calls[-2:] == ["release", "close"]  # the singleton it had taken is given back
