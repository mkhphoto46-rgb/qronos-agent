"""Store admission (core/store_admission.py) and the update launch's lifetime guard.

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


def test_a_launch_hold_keeps_writers_out_until_the_installer_ends(tmp_path, world=None):
    """The launching process is the installer's lifetime guard: exclusion until the installer exits."""
    world = World(tmp_path)
    names = (f"Local\\QronosUpdateTest-guard-{uuid.uuid4().hex}",)
    installer_may_exit = tmp_path / "installer-may-exit"
    executable, _ = child_python()
    started: list = []

    def real_installer(path, arguments, handles):
        code = ("import os, sys, time\n"
                "deadline = time.monotonic() + 60\n"
                "while not os.path.exists(sys.argv[1]) and time.monotonic() < deadline:\n"
                "    time.sleep(0.05)\n")
        process = update_service.spawn_inheriting([executable, "-I", "-c", code, str(installer_may_exit)], handles)
        started.append(process)
        return process

    service = world.service(quiesce=lambda: update_service.LaunchHold(world.data, names), launcher=real_installer)
    _, approved = staged(service, *world.release("0.2.0"))
    announced = threading.Event()
    result: dict = {}

    def run():
        result["handoff"] = service.launch(approved["launchToken"], on_launched=lambda h: announced.set())

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert announced.wait(60), "the launch never announced the started installer"
        assert thread.is_alive()  # still guarding
        probe = Writer(tmp_path, world.data, "during-install", fence=names[0])
        try:
            assert probe.state() == "refused:update_in_progress"
        finally:
            probe.finish()
        assert update_service.update_in_progress(names[0]) is True
        installer_may_exit.write_text("1")
        thread.join(60)
        assert not thread.is_alive() and result["handoff"]["version"] == "0.2.0"
    finally:
        installer_may_exit.write_text("1")
        for process in started:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)
        thread.join(30)
    after = Writer(tmp_path, world.data, "after-install", fence=names[0])
    try:
        assert after.state() == "admitted"
    finally:
        after.finish()
    assert update_service.update_in_progress(names[0]) is False


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


# A launching guard that dies right after starting the installer: the byte-range lock dies with it,
# the update mutex lives on in the installer's inherited handle.
GUARD = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core import update_service
data, fence, go, info = Path(sys.argv[2]), sys.argv[3], sys.argv[4], Path(sys.argv[5])
hold = update_service.LaunchHold(data, (fence,))
code = ("import os, sys, time\n"
        "deadline = time.monotonic() + 90\n"
        "while not os.path.exists(sys.argv[1]) and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n")
installer = update_service.spawn_inheriting([sys.executable, "-I", "-c", code, go], hold.handles())
info.write_text(str(installer.pid))
os._exit(9)  # the guard crashes: nothing is released by this process
"""


def _open_owned(pid: int, started_after: float):
    """Open the fixture installer by pid, but only if it is a python process created during this test."""
    import ctypes
    from ctypes import wintypes

    import _winapi

    handle = _winapi.OpenProcess(0x1000 | 0x100000 | 0x0001, False, pid)  # QUERY_LIMITED | SYNCHRONIZE | TERMINATE
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    times = [wintypes.FILETIME() for _ in range(4)]
    assert kernel.GetProcessTimes(handle, *[ctypes.byref(x) for x in times])
    created = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 10_000_000 - 11_644_473_600
    assert created >= started_after - 2, "the pid does not belong to this test's fixture installer"
    return handle


def test_a_guard_that_dies_leaves_the_fence_up_until_the_installer_ends(tmp_path):
    """Root's failure case: the launching guard dies after starting the installer. No writer may open a store
    while that installer lives; once it ends, writers are admitted again."""
    import _winapi

    data = tmp_path / "data"
    data.mkdir()
    store = data / "store.json"
    fence = unique_fence()
    go, info = tmp_path / "installer-may-exit", tmp_path / "installer.pid"
    started = time.time()
    executable, environment = child_python()
    guard = subprocess.Popen([executable, "-c", GUARD, str(ROOT), str(data), fence, str(go), str(info)],
                             env=environment)
    installer = None
    try:
        guard.wait(timeout=60)
        assert guard.returncode == 9, "the guard should have crashed on purpose"
        installer = _open_owned(int(info.read_text()), started)
        assert _winapi.WaitForSingleObject(installer, 0) == _winapi.WAIT_TIMEOUT  # still installing
        assert store_admission.update_in_progress(fence) is True

        during = Writer(tmp_path, data, "after-guard-crash", fence=fence)
        try:
            assert during.state() == "refused:update_in_progress"
        finally:
            during.finish()
        assert not store.exists(), "a writer opened its store while the installer was still running"

        go.write_text("1")
        assert _winapi.WaitForSingleObject(installer, 60_000) == _winapi.WAIT_OBJECT_0
    finally:
        go.write_text("1")
        if guard.poll() is None:
            guard.kill()
            guard.wait(timeout=30)
        if installer is not None:
            if _winapi.WaitForSingleObject(installer, 30_000) != _winapi.WAIT_OBJECT_0:
                _winapi.TerminateProcess(installer, 1)  # verified as this test's own fixture above
            _winapi.CloseHandle(installer)
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
