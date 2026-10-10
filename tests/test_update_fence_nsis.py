"""The NSIS half of the update hand-over (installer-hooks/qronos-update-fence.nsh), run as compiled probes.

Safety boundary (audited before running; see tests/fixtures/nsis_fence_probe.py):
- Each probe is a throwaway silent NSIS executable compiled by Tauri's existing makensis into tmp_path. It runs
  ONLY the fence/receipt functions, installs nothing, shows no window, writes no registry value and touches only
  its own tmp folder and the tmp data root it was compiled with.
- Every fence is a unique PRIVATE mutex name (Local\\QronosUpdateTest-<uuid>); the production name, the user's
  data folder and the real installer are never used.
- Child processes (probes, the guard child, the writer child, a probe's grandchild) are the probe executable or
  the base interpreter (tests/fixtures/child_python.py, with -B -I); each is owned, waited with bounds and, if it
  must be ended, ended only through its own handle after its creation time was checked.
- No UAC, SCM, network, model or device.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from core import store_admission, update_service
from core.local_update import UpdateError
from tests.fixtures.child_python import child_python
from tests.fixtures.nsis_fence_probe import MAKENSIS, build_probe
from tests.test_update_service import BASE

ROOT = Path(__file__).resolve().parent.parent
pytestmark = [
    pytest.mark.skipif(os.name != "nt", reason="NSIS probes, named mutexes and LockFileEx are Windows"),
    pytest.mark.skipif(MAKENSIS is None or not MAKENSIS.is_file(), reason="Tauri's NSIS compiler is not installed"),
]

VERSION = "0.1.0"
COMPILES: list = []  # native compiler identities (pid, start, script), recorded as they start

# A cooperating writer: admission first, only then the store (as in tests/test_store_admission.py).
WRITER = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core.local_update import UpdateError
from core.store_admission import admit_writer
data, log, fence = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
try:
    admission = admit_writer(data, fence=fence)
except UpdateError as error:
    log.write_text("refused:" + error.code)
    raise SystemExit(0)
with open(data / "store.json", "a", encoding="utf-8") as store:
    store.write("written\n")
log.write_text("admitted")
stop = Path(str(log) + ".stop")
deadline = time.monotonic() + 60
while not stop.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""


def unique_fence() -> str:
    return f"Local\\QronosUpdateTest-nsis-{uuid.uuid4().hex}"


class Writer:
    def __init__(self, tmp: Path, data: Path, fence: str, name: str) -> None:
        self.log = tmp / f"writer-{name}.log"
        executable, environment = child_python()
        self.process = subprocess.Popen([executable, "-B", "-I", "-c", WRITER, str(ROOT), str(data), str(self.log),
                                         fence], env=environment)

    def state(self, timeout: float = 30.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.log.exists() and self.log.read_text():
                return self.log.read_text()
            time.sleep(0.02)
        raise AssertionError("the writer never reported")

    def finish(self) -> None:
        Path(str(self.log) + ".stop").write_text("1")
        try:
            self.process.wait(timeout=30)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=30)


def wait_for(path: Path, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return True
        time.sleep(0.05)
    return False


def finish_probe(process: subprocess.Popen, timeout: float = 120.0) -> int:
    try:
        return process.wait(timeout=timeout)
    finally:
        if process.poll() is None:
            process.kill()  # exactly this test's own probe, by its own handle
            process.wait(30)


def tree(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*")) if folder.exists() else []


def make_probe(tmp_path: Path, name: str, **options):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    fence = unique_fence()
    options.setdefault("version", VERSION)
    probe = build_probe(tmp_path / name, fence=fence, data_root=data, compile_log=COMPILES, **options)
    return probe, data, fence


# ------------------------------------------------------------------------------- manual install


def test_a_manual_install_holds_the_fence_and_the_exclusive_admission_until_it_exits(tmp_path):
    probe, data, fence = make_probe(tmp_path, "manual")
    process = subprocess.Popen([str(probe.exe), "/S"], stdin=subprocess.DEVNULL)
    try:
        assert wait_for(probe.state), probe.read_result()
        assert probe.state.read_text(encoding="utf-8", errors="replace").startswith("fenced update=0")
        assert store_admission.update_in_progress(fence) is True
        with pytest.raises(UpdateError) as refused:
            store_admission.admit_exclusive(data)  # the probe holds it: nobody else gets it
        assert refused.value.code == "writers_active"
        late = Writer(tmp_path, data, fence, "late")
        try:
            assert late.state() == "refused:update_in_progress"
        finally:
            late.finish()
        assert not (data / "store.json").exists()
    finally:
        probe.go.write_text("1")
        code = finish_probe(process)
    assert code == 0 and probe.read_result().startswith("installed receipt=[]")  # manual: no receipt
    assert tree(data / "Updates") == []
    assert store_admission.update_in_progress(fence) is False
    after = Writer(tmp_path, data, fence, "after")
    try:
        assert after.state() == "admitted"
    finally:
        after.finish()


def test_a_manual_install_refuses_a_held_fence_and_changes_nothing(tmp_path):
    probe, data, fence = make_probe(tmp_path, "held")
    hold = update_service.MutexHold((fence,))
    try:
        before = tree(data)
        code = finish_probe(subprocess.Popen([str(probe.exe), "/S"], stdin=subprocess.DEVNULL))
    finally:
        hold.release()
    assert code != 0 and "Another Qronos update or installation is running" in probe.read_result()
    assert not probe.state.exists() and tree(data) == before


def test_a_manual_install_refuses_while_a_writer_is_inside_and_kills_nothing(tmp_path):
    probe, data, fence = make_probe(tmp_path, "writer")
    inside = Writer(tmp_path, data, fence, "inside")
    try:
        assert inside.state() == "admitted"
        code = finish_probe(subprocess.Popen([str(probe.exe), "/S"], stdin=subprocess.DEVNULL))
        assert inside.process.poll() is None  # the running writer was not killed
    finally:
        inside.finish()
    assert code != 0 and "Qronos is still running with its data open" in probe.read_result()
    assert not probe.state.exists()


def test_an_absent_data_root_is_fenced_and_a_new_writer_is_refused_until_exit(tmp_path):
    probe, data, fence = make_probe(tmp_path, "absent")
    data.rmdir()
    process = subprocess.Popen([str(probe.exe), "/S"], stdin=subprocess.DEVNULL)
    try:
        assert wait_for(probe.state), probe.read_result()
        late = Writer(tmp_path, data, fence, "late")  # creates the root, then meets the fence
        try:
            assert late.state() == "refused:update_in_progress"
        finally:
            late.finish()
    finally:
        probe.go.write_text("1")
        assert finish_probe(process) == 0
    assert store_admission.update_in_progress(fence) is False


# ------------------------------------------------------------------------------- uninstall


def test_the_uninstaller_fences_refuses_held_fence_and_writers_and_never_takes_the_update_switch(tmp_path):
    probe, data, fence = make_probe(tmp_path, "un", uninstaller=True)
    probe.go.write_text("1")  # the install half finishes at once and writes uninstall.exe
    assert finish_probe(subprocess.Popen([str(probe.exe), "/S"], stdin=subprocess.DEVNULL)) == 0
    uninstaller = probe.work / "uninstall.exe"
    assert uninstaller.is_file()
    in_place = f"_?={probe.work}"

    # held fence -> refused, nothing done
    hold = update_service.MutexHold((fence,))
    try:
        code = finish_probe(subprocess.Popen([str(uninstaller), "/S", in_place], stdin=subprocess.DEVNULL))
    finally:
        hold.release()
    assert code != 0 and "Another Qronos update or installation is running" in probe.read_result()
    assert not (probe.work / "un-state.txt").exists()

    # an active writer -> refused
    inside = Writer(tmp_path, data, fence, "un-inside")
    try:
        assert inside.state() == "admitted"
        code = finish_probe(subprocess.Popen([str(uninstaller), "/S", in_place], stdin=subprocess.DEVNULL))
    finally:
        inside.finish()
    assert code != 0 and "Qronos is still running with its data open" in probe.read_result()

    # the update switch is never accepted by the uninstaller
    code = finish_probe(subprocess.Popen([str(uninstaller), "/S", "/QRONOSUPDATE", in_place],
                                         stdin=subprocess.DEVNULL))
    assert code != 0 and "does not take an update hand-over" in probe.read_result()

    # free -> fenced for its whole run; a new writer is refused until it exits
    process = subprocess.Popen([str(uninstaller), "/S", in_place], stdin=subprocess.DEVNULL)
    try:
        assert wait_for(probe.work / "un-state.txt"), probe.read_result()
        late = Writer(tmp_path, data, fence, "un-late")
        try:
            assert late.state() == "refused:update_in_progress"
        finally:
            late.finish()
    finally:
        Path(str(probe.go) + ".un").write_text("1")
        assert finish_probe(process) == 0
    assert probe.read_result().startswith("uninstalled")
    assert store_admission.update_in_progress(fence) is False


# ------------------------------------------------------------------------------- update mode: forged input


def _record(fields: dict) -> bytes:
    lines = ["QRONOS-UPDATE-FENCE 1"] + [f"{key}={value}" for key, value in fields.items()]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _valid_fields(process, hold, data: Path, give=None) -> dict:
    """A correct record for ``process``; ``give`` "none" duplicates nothing, a MutexHold duplicates that object."""
    import _winapi

    me = _winapi.GetCurrentProcess()
    if give == "none":
        duplicated = 4
    else:
        source = (give or hold).fence_handle()
        duplicated = _winapi.DuplicateHandle(me, source, int(process._handle), 0x00100000, False, 0)
    return {"nonce": os.urandom(32).hex(), "fence": str(duplicated), "receipts": str(data / "Updates" / "receipts"),
            "version": VERSION, "package": "a" * 64, "manifest": "b" * 64,
            "guard": f"{os.getpid()}:{update_service._process_creation_time(me)}"}


FORGERIES = {
    "empty": None,
    "oversize": "oversize",
    "bad_header": {"_header": "QRONOS-UPDATE-FENCE 2"},
    "wrong_order": {"_swap": ("version", "receipts")},
    "extra_line": {"extra": "1"},
    "missing_line": {"_drop": "manifest"},
    "upper_nonce": {"nonce": "A" * 64},
    "short_package": {"package": "a" * 63},
    "foreign_receipts": {"receipts": "C:\\Elsewhere\\Updates\\receipts"},
    "other_version": {"version": "9.9.9"},
    "guard_not_parent": {"guard": "4:1"},
    "guard_other_time": {"_guard_time": "1"},
    "not_the_fence": {"_give": "other"},  # a real handle, to another (private) mutex
    "no_handle_given": {"_give": "none"},
    "nul_in_record": {"package": "a" * 32 + "\0" + "a" * 31},
}


@pytest.mark.parametrize("forgery", sorted(FORGERIES))
def test_update_mode_refuses_every_bad_hand_over_with_zero_writes(tmp_path, forgery):
    probe, data, fence = make_probe(tmp_path, "forged")
    (data / "Updates" / "receipts").mkdir(parents=True)
    before = tree(data)
    hold = update_service.MutexHold((fence,))  # a guard owning the fence, as in a real launch
    process = subprocess.Popen([str(probe.exe), "/S", "/QRONOSUPDATE"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        change = FORGERIES[forgery]
        if change is None:
            data_bytes = b""
        elif change == "oversize":
            fields = _valid_fields(process, hold, data)
            fields["receipts"] = fields["receipts"] + "\\x" * 600
            data_bytes = _record(fields)
        else:
            other = update_service.MutexHold((unique_fence(),)) if change.get("_give") == "other" else None
            try:
                fields = _valid_fields(process, hold, data, other or change.get("_give"))
            finally:
                if other is not None:
                    other.release()  # the probe's duplicate keeps that object alive; it is not the fence
            header = change.get("_header")
            if "_swap" in change:
                a, b = change["_swap"]
                keys = list(fields)
                i, j = keys.index(a), keys.index(b)
                keys[i], keys[j] = keys[j], keys[i]
                fields = {key: fields[key] for key in keys}
            if "_drop" in change:
                del fields[change["_drop"]]
            if "_guard_time" in change:
                fields["guard"] = f"{os.getpid()}:{change['_guard_time']}"
            fields.update({k: v for k, v in change.items() if not k.startswith("_")})
            data_bytes = _record(fields)
            if header:
                data_bytes = data_bytes.replace(b"QRONOS-UPDATE-FENCE 1", header.encode(), 1)
        process.stdin.write(data_bytes)
        process.stdin.close()
        code = finish_probe(process, 90)
        ack = process.stdout.read()
    finally:
        hold.release()
    assert code != 0, probe.read_result()
    assert probe.read_result().startswith("refused: "), probe.read_result()
    assert not probe.state.exists() and tree(data) == before  # zero writes: no receipt, no lock file
    assert ack == b""  # nothing was acknowledged


def test_update_mode_without_a_pipe_is_refused_not_treated_as_manual(tmp_path):
    probe, data, fence = make_probe(tmp_path, "nopipe")
    code = finish_probe(subprocess.Popen([str(probe.exe), "/S", "/QRONOSUPDATE"], stdin=subprocess.DEVNULL))
    assert code != 0 and probe.read_result().startswith("refused: ") and not probe.state.exists()
    assert store_admission.update_in_progress(fence) is False


# ------------------------------------------------------------------------------- update mode: real hand-over

GUARD_SERVICE = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core import update_service
from tests.test_update_service import World, staged
tmp, fence, probe, out = Path(sys.argv[2]), sys.argv[3], sys.argv[4], Path(sys.argv[5])
world = World(tmp)
started = {}
def launcher(path, arguments, handover):
    process = update_service.start_fenced([probe, "/S", "/QRONOSUPDATE"], str(Path(probe).parent), handover)
    started["pid"] = process.pid
    return process
service = world.service("0.1.0", quiesce=lambda: update_service.LaunchHold(world.data, (fence,)),
                        launcher=launcher, liveness=lambda: update_service.update_in_progress(fence))
_, approved = staged(service, *world.release("0.1.0"))
handoff = service.launch(approved["launchToken"])
out.write_text(json.dumps({"handoff": handoff, "probePid": started["pid"]}))
"""


def _open_owned(pid: int, started_after: float):
    import ctypes
    from ctypes import wintypes

    import _winapi

    handle = _winapi.OpenProcess(0x1000 | 0x100000 | 0x0001, False, pid)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    times = [wintypes.FILETIME() for _ in range(4)]
    assert kernel.GetProcessTimes(handle, *[ctypes.byref(x) for x in times])
    created = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 10_000_000 - 11_644_473_600
    assert created >= started_after - 2, "the pid does not belong to this test's own process"
    return handle


def test_the_real_update_service_hands_over_to_the_compiled_installer_and_its_receipt_confirms_the_repair(
        tmp_path):
    """A real UpdateService.launch (in a guard child, so the guard can exit as in production) -> start_fenced ->
    the compiled NSIS probe: ACK, fence take-over, exclusive admission, guard exit, receipt; then the start-up
    barrier confirms the same-version repair with that receipt. A grandchild started with inheritance does not
    keep the fence."""
    import _winapi

    world_dir = tmp_path / "world"
    world_dir.mkdir()
    fence = unique_fence()
    probe = build_probe(tmp_path / "update", fence=fence, data_root=world_dir / "data", version=VERSION,
                        python=child_python()[0], grandchild=True, compile_log=COMPILES)
    out = tmp_path / "guard.json"
    executable, environment = child_python()
    started = time.time()
    # Not -I: the guard imports the test world (pytest); -B and the child environment as the store guard.
    guard = subprocess.Popen([executable, "-B", "-c", GUARD_SERVICE, str(ROOT), str(world_dir), fence,
                              str(probe.exe), str(out)], env=environment)
    installer = grandchild = None
    try:
        assert guard.wait(timeout=90) == 0, "the guard child failed"
        result = json.loads(out.read_text())
        assert result["handoff"]["handedOver"] is True  # the guard let go only after the ACK, then exited
        installer = _open_owned(result["probePid"], started)
        assert wait_for(probe.state), probe.read_result()
        assert probe.state.read_text(encoding="utf-8", errors="replace").startswith("fenced update=1")
        assert _winapi.WaitForSingleObject(installer, 0) == _winapi.WAIT_TIMEOUT  # installing
        assert store_admission.update_in_progress(fence) is True
        during = Writer(tmp_path, world_dir / "data", fence, "during")
        try:
            assert during.state() == "refused:update_in_progress"
        finally:
            during.finish()
        assert wait_for(Path(str(probe.go) + ".grandchild.pid"))
        grandchild = _open_owned(int(Path(str(probe.go) + ".grandchild.pid").read_text()), started)
        probe.go.write_text("1")
        assert _winapi.WaitForSingleObject(installer, 60_000) == _winapi.WAIT_OBJECT_0
        assert _winapi.WaitForSingleObject(grandchild, 0) == _winapi.WAIT_TIMEOUT  # still alive
        assert store_admission.update_in_progress(fence) is False  # nothing it started keeps the fence
        after = Writer(tmp_path, world_dir / "data", fence, "after")
        try:
            assert after.state() == "admitted"
        finally:
            after.finish()
    finally:
        probe.go.write_text("1")
        Path(str(probe.go) + ".grandchild").write_text("1")
        if guard.poll() is None:
            guard.kill()
            guard.wait(30)
        for handle in (installer, grandchild):
            if handle is not None:
                if _winapi.WaitForSingleObject(handle, 30_000) != _winapi.WAIT_OBJECT_0:
                    _winapi.TerminateProcess(handle, 1)  # verified above as this test's own process
                _winapi.CloseHandle(handle)
    assert probe.read_result() == "installed receipt=[]", probe.read_result()
    receipts = list((world_dir / "data" / "Updates" / "receipts").glob("*.receipt"))
    assert len(receipts) == 1
    lines = receipts[0].read_bytes().decode("ascii").split("\r\n")
    assert lines[0] == "QRONOS-INSTALL-RECEIPT 1" and lines[4] == f"version={VERSION}" and lines[5] == "result=installed"
    barrier = update_service.UpdateService(world_dir / "data", VERSION, keys_loader=lambda: {},
                                           liveness=lambda: store_admission.update_in_progress(fence), environ={},
                                           profile=BASE)
    recovered = barrier.recover_before_stores()
    assert recovered["result"] == "applied" and recovered["receipt"] == "verified", recovered
    assert not receipts[0].exists()


GUARD_CRASH = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core import update_service
data, fence, probe, info = Path(sys.argv[2]), sys.argv[3], sys.argv[4], Path(sys.argv[5])
hold = update_service.LaunchHold(data, (fence,))
handover = update_service.Handover(os.urandom(32), hold.fence_handle(), data / "Updates" / "receipts", "0.1.0",
                                   "a" * 64, "b" * 64)
installer = update_service.start_fenced([probe, "/S", "/QRONOSUPDATE"], str(Path(probe).parent), handover)
info.write_text(str(installer.pid))
os._exit(9)  # the guard crashes before reading the acknowledgement: nothing is released by this process
"""


def test_a_guard_that_crashes_leaves_the_compiled_installer_fenced_until_it_ends(tmp_path):
    import _winapi

    probe, data, fence = make_probe(tmp_path, "crash")
    (data / "Updates" / "receipts").mkdir(parents=True)
    info = tmp_path / "installer.pid"
    executable, environment = child_python()
    started = time.time()
    guard = subprocess.Popen([executable, "-B", "-I", "-c", GUARD_CRASH, str(ROOT), str(data), fence,
                              str(probe.exe), str(info)], env=environment)
    installer = None
    try:
        assert guard.wait(timeout=60) == 9
        installer = _open_owned(int(info.read_text()), started)
        assert wait_for(probe.state), probe.read_result()  # took the abandoned fence and the free admission
        assert store_admission.update_in_progress(fence) is True
        during = Writer(tmp_path, data, fence, "crash-during")
        try:
            assert during.state() == "refused:update_in_progress"
        finally:
            during.finish()
        assert not (data / "store.json").exists()
        probe.go.write_text("1")
        assert _winapi.WaitForSingleObject(installer, 60_000) == _winapi.WAIT_OBJECT_0
    finally:
        probe.go.write_text("1")
        if guard.poll() is None:
            guard.kill()
            guard.wait(30)
        if installer is not None:
            if _winapi.WaitForSingleObject(installer, 30_000) != _winapi.WAIT_OBJECT_0:
                _winapi.TerminateProcess(installer, 1)  # verified above as this test's own probe
            _winapi.CloseHandle(installer)
    assert probe.read_result() == "installed receipt=[]", probe.read_result()
    assert store_admission.update_in_progress(fence) is False
    after = Writer(tmp_path, data, fence, "crash-after")
    try:
        assert after.state() == "admitted"
    finally:
        after.finish()


def test_without_the_install_complete_step_a_real_hand_over_writes_no_receipt(tmp_path):
    """The receipt's own guard: update mode, a valid hand-over, but the install-complete step never ran."""
    import _winapi

    from tests.test_installer_template import MINI_GUARD

    probe, data, fence = make_probe(tmp_path, "incomplete", complete=False)
    (data / "Updates" / "receipts").mkdir(parents=True)
    probe.go.write_text("1")
    out = tmp_path / "guard.json"
    executable, environment = child_python()
    started = time.time()
    guard = subprocess.Popen([executable, "-B", "-I", "-c", MINI_GUARD, str(ROOT), str(data), fence,
                              json.dumps([str(probe.exe), "/S", "/QRONOSUPDATE"]), str(out)], env=environment)
    installer = None
    try:
        assert guard.wait(timeout=120) == 0
        result = json.loads(out.read_text())
        assert result["ack"] is True
        installer = _open_owned(result["pid"], started)
        assert _winapi.WaitForSingleObject(installer, 60_000) == _winapi.WAIT_OBJECT_0
    finally:
        if guard.poll() is None:
            guard.kill()
            guard.wait(30)
        if installer is not None:
            if _winapi.WaitForSingleObject(installer, 0) != _winapi.WAIT_OBJECT_0:
                _winapi.TerminateProcess(installer, 1)  # verified above as this test's own probe
            _winapi.CloseHandle(installer)
    assert probe.read_result() == ("installed receipt=[Not every install step succeeded; "
                                   "no installation receipt was written.]")
    assert list((data / "Updates" / "receipts").glob("*.receipt")) == []


def _controller() -> str:
    """This test process as the unopenable guard's exact controller (pid:creation FILETIME)."""
    import _winapi

    return f"{os.getpid()}:{update_service._process_creation_time(_winapi.GetCurrentProcess())}"


CONTROLLER = r"""
import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import _winapi
from core import update_service
argv = json.loads(sys.argv[2])
argv.append(f"{os.getpid()}:{update_service._process_creation_time(_winapi.GetCurrentProcess())}")
guard = subprocess.Popen(argv)
Path(sys.argv[3]).write_text(str(guard.pid))
time.sleep(300)  # killed by the test through its own handle
"""


def test_the_unopenable_guard_fixture_ends_itself_and_only_its_child_when_its_controller_dies(tmp_path):
    """Root round 12, fixture stop safety: nobody can open the guard, so it must end by itself (bounded) when its
    exact controller dies, killing ONLY its own installer child (here a stand-in that never reads its input)."""
    import _winapi

    work = tmp_path / "guardwork"
    work.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    executable, environment = child_python()
    stand_in = [executable, "-B", "-I", "-c", "import time; time.sleep(200)"]
    guard_argv = [executable, "-B", "-I", str(ROOT / "tests" / "fixtures" / "unopenable_guard_child.py"), str(ROOT),
                  unique_fence(), str(data), VERSION, str(work), json.dumps(stand_in)]
    started = time.time()
    controller = subprocess.Popen([executable, "-B", "-I", "-c", CONTROLLER, str(ROOT), json.dumps(guard_argv),
                                   str(tmp_path / "guard.pid")], env=environment)
    child = None
    try:
        deadline = time.monotonic() + 60
        while not (work / "guard.ready").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        ready = json.loads((work / "guard.ready").read_text(encoding="utf-8"))
        child = _open_owned(ready["probePid"], started)
        with pytest.raises(OSError) as denied:
            _winapi.OpenProcess(0x101000, False, ready["guardPid"])
        assert denied.value.winerror == 5  # alive and unopenable: no outside stop can reach it
        assert _winapi.WaitForSingleObject(child, 0) == _winapi.WAIT_TIMEOUT
        controller.kill()  # the exact controller, by its own handle
        controller.wait(30)
        assert _winapi.WaitForSingleObject(child, 20_000) == _winapi.WAIT_OBJECT_0, "its child outlived the guard"
        deadline = time.monotonic() + 20
        while not (work / "guard.report").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        report = json.loads((work / "guard.report").read_text(encoding="utf-8"))
        assert report["ended"] == "controller gone" and report.get("probeKilled") is True
        gone = False
        while time.monotonic() < deadline and not gone:
            try:
                _winapi.CloseHandle(_winapi.OpenProcess(0x101000, False, ready["guardPid"]))
            except OSError as error:
                gone = error.winerror == 87  # proved gone (not merely unopenable)
            time.sleep(0.1)
        assert gone, "the guard is still alive"
    finally:
        (work / "guard.stop").write_text("1")
        if controller.poll() is None:
            controller.kill()
            controller.wait(30)
        if child is not None:
            if _winapi.WaitForSingleObject(child, 0) != _winapi.WAIT_OBJECT_0:
                _winapi.TerminateProcess(child, 1)  # verified above as this test's own stand-in
            _winapi.CloseHandle(child)


def run_unopenable_guard(tmp_path: Path, fence: str, data: Path, argv: list[str], go: Path | None = None) -> dict:
    """Media15 F15-1 (adapted, see tests/fixtures/unopenable_guard_child.py): an ALIVE guard nobody can open hands
    over to `argv`. Returns what happened while that guard was still alive."""
    import ctypes

    work = tmp_path / "guardwork"
    work.mkdir()
    executable, environment = child_python()
    guard = subprocess.Popen([executable, "-B", "-I", str(ROOT / "tests" / "fixtures" / "unopenable_guard_child.py"),
                              str(ROOT), fence, str(data), VERSION, str(work), json.dumps(argv), _controller()],
                             env=environment)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        deadline = time.monotonic() + 60
        while not (work / "guard.ready").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        ready = json.loads((work / "guard.ready").read_text(encoding="utf-8"))
        handle = kernel32.OpenProcess(0x101000, False, ready["guardPid"])
        denied = (handle == 0, ctypes.get_last_error())
        if handle:
            kernel32.CloseHandle(handle)
        deadline = time.monotonic() + 120
        while not (work / "guard.report").exists() and time.monotonic() < deadline:
            time.sleep(0.1)  # the installer must have ENDED while the guard still lives
        if go is not None:
            go.write_text("1")  # let a wrongly-passing installer finish instead of waiting for its go-file
        alive = guard.poll() is None
    finally:
        (work / "guard.stop").write_text("1")
        try:
            guard.wait(timeout=180)
        finally:
            if guard.poll() is None:
                guard.kill()  # exactly this test's own guard, by the handle held since creation
                guard.wait(30)
    report = json.loads((work / "guard.report").read_text(encoding="utf-8"))
    return {"denied": denied, "guardAliveAfterInstallerEnded": alive, "report": report}


def test_an_alive_guard_that_cannot_be_opened_is_refused_before_the_acknowledgement(tmp_path):
    """Media15 F15-1: OpenProcess(parent) failing with ACCESS_DENIED is not proof the guard exited. The installer
    refuses before the ACK and before any change while that guard (and its image) is alive."""
    probe, data, fence = make_probe(tmp_path, "unopenable")
    (data / "Updates" / "receipts").mkdir(parents=True)
    outcome = run_unopenable_guard(tmp_path, fence, data, [str(probe.exe), "/S", "/QRONOSUPDATE"], probe.go)
    assert outcome["denied"] == (True, 5), f"precondition (an alive guard nobody can open) not met: {outcome}"
    assert outcome["guardAliveAfterInstallerEnded"] is True and outcome["report"].get("guardAliveAtProbeExit")
    assert outcome["report"]["ack"] == "" and outcome["report"]["probeExit"] != 0
    assert "cannot be checked (error 5)" in probe.read_result(), probe.read_result()
    assert not probe.state.exists() and list((data / "Updates" / "receipts").iterdir()) == []


def test_compiler_identities_were_recorded():
    """Quota bookkeeping: every native compiler started by this module was recorded at start (pid, time, script)."""
    assert all({"pid", "startedAt", "script"} <= set(entry) for entry in COMPILES)


# ------------------------------------------------------------------------------- the hook files


HOOKS = ROOT / "desktop" / "src-tauri" / "installer-hooks"


def _macro(text: str, name: str) -> str:
    body = text[text.index(f"!macro {name}"):]
    return body[:body.index("!macroend")]


def test_the_fence_runs_first_in_preinstall_and_preuninstall_and_the_receipt_last():
    hook = (HOOKS / "webview2-offline.nsh").read_text(encoding="utf-8")
    assert '!include "${__FILEDIR__}\\qronos-update-fence.nsh"' in hook
    preinstall = _macro(hook, "NSIS_HOOK_PREINSTALL")
    # Round 12 (Root's order; adapted from round 10, where the WebView2 child came before the app check and the
    # removal): fence, install root, whole-list validation, app check, owned removal, then the WebView2 child.
    validate = preinstall.index('StrCpy $QronosValidateOnly "1"')
    order = [preinstall.index("!insertmacro QRONOS_FENCE_INSTALL"), preinstall.index("Call QronosCheckInstallRoot"),
             validate, preinstall.index("Call QronosRemoveOwnedPayload", validate),
             preinstall.index("CheckIfAppIsRunning"),
             preinstall.index("Call QronosRemoveOwnedPayload", preinstall.index("CheckIfAppIsRunning")),
             preinstall.index("QRONOS_WEBVIEW2_VERSION_TO_R9"), preinstall.index("QRONOS_WEBVIEW2_RUN_INSTALLER")]
    assert order == sorted(order) and len(set(order)) == len(order)
    first = [line.strip() for line in preinstall.splitlines()[1:] if line.strip() and not line.strip().startswith(";")]
    assert first[:5] == ["Push $R7", "Push $R8", "Push $R9", "!insertmacro QRONOS_FENCE_INSTALL",
                         '${If} $QronosFenceMessage != ""']
    assert _macro(hook, "NSIS_HOOK_POSTINSTALL").split("\n")[1].strip() == "Call QronosWriteReceipt"
    assert "Abort" not in _macro(hook, "NSIS_HOOK_POSTINSTALL")  # a receipt failure never undoes an install
    preuninstall = _macro(hook, "NSIS_HOOK_PREUNINSTALL")
    assert preuninstall.split("\n")[1].strip() == "Call un.QronosFenceUninstall"
    assert 'QRONOS_REFUSE "$QronosFenceMessage"' in preuninstall
    uninstall_order = [preuninstall.index(step) for step in (
        "Call un.QronosFenceUninstall", "Call un.QronosCheckInstallRoot", 'StrCpy $QronosValidateOnly "1"',
        "CheckIfAppIsRunning")]
    assert uninstall_order == sorted(uninstall_order)
    assert preuninstall.rindex("Call un.QronosRemoveOwnedPayload") > preuninstall.index("CheckIfAppIsRunning")
    assert "un.QronosRemoveOwnedPayload" not in _macro(hook, "NSIS_HOOK_POSTUNINSTALL")  # moved before deletion


def test_the_production_hook_compiles_in_tauris_include_order_and_is_never_run(tmp_path):
    from build_support import desktop_prerequisites as prerequisites

    payload = tmp_path / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    payload.write_bytes(b"never run")
    pin = prerequisites.load_pin()
    (tmp_path / "hooks.nsh").write_text(prerequisites.hook_wrapper(payload, pin), encoding="utf-8")
    script = tmp_path / "tauri-order.nsi"
    script.write_text("\n".join((
        "Unicode true", 'Name "compile-only"', f'OutFile "{tmp_path / "compile-only.exe"}"',
        "RequestExecutionLevel user", f'!include "{tmp_path / "hooks.nsh"}"',
        '!define VERSION "0.1.0"',  # after the hooks, as in Tauri 2.9.4's installer.nsi
        "Section Install", '  SetOutPath "$INSTDIR"', "  !insertmacro NSIS_HOOK_PREINSTALL",
        '  WriteUninstaller "$INSTDIR\\uninstall.exe"',"  !insertmacro NSIS_HOOK_POSTINSTALL", "SectionEnd",
        "Section Uninstall", "  !insertmacro NSIS_HOOK_PREUNINSTALL", "  !insertmacro NSIS_HOOK_POSTUNINSTALL",
        "SectionEnd", "")), encoding="utf-8-sig")
    compiler = subprocess.Popen([str(MAKENSIS), "/V3", str(script)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    COMPILES.append({"pid": compiler.pid, "startedAt": time.time(), "script": str(script)})
    try:
        output, _ = compiler.communicate(timeout=120)
    finally:
        if compiler.poll() is None:
            compiler.kill()
            compiler.wait(30)
    text = output.decode("utf-8", "replace")
    assert compiler.returncode == 0, text[-4000:]
    assert (tmp_path / "compile-only.exe").is_file()  # compiled only; never run
    assert "{VERSION}" not in text and "unknown variable" not in text.lower(), text[-4000:]


def test_no_fence_function_depends_on_a_define_made_after_the_hooks_are_included():
    """Tauri's template includes the hooks before !define VERSION: a Function body is expanded at that point."""
    fence = (HOOKS / "qronos-update-fence.nsh").read_text(encoding="utf-8")
    outside_macros = re.sub(r"!macro QRONOS_FENCE_INSTALL.*?!macroend", "", fence, flags=re.S)
    for name in ("VERSION", "MAINBINARYNAME", "PRODUCTNAME", "BUNDLEID", "INSTALLMODE"):
        assert "${" + name + "}" not in outside_macros, name


def test_the_build_wrapper_never_sets_a_probe_only_define(tmp_path):
    from build_support import desktop_prerequisites as prerequisites

    wrapper = prerequisites.hook_wrapper(tmp_path / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
                                         prerequisites.load_pin())
    for name in ("QRONOS_UPDATE_FENCE_NAME", "QRONOS_PROBE_"):
        assert name not in wrapper
        for source in (ROOT / "build_support").rglob("*.py"):
            # build_support may NAME them only to refuse them (verify_hook_wrapper/verify_generated_order);
            # it never emits a define of one.
            assert not re.search(r"!define\s+" + name, source.read_text(encoding="utf-8")), source
    for hook in HOOKS.glob("*.nsh"):  # only the fence file's own default and !ifdef guards name them
        text = hook.read_text(encoding="utf-8")
        assert "!define QRONOS_PROBE_" not in text
        defines = re.findall(r"!define\s+QRONOS_UPDATE_FENCE_NAME\s+\"([^\"]*)\"", text)
        assert defines in ([], ["Local\\Qronos.UpdateInProgress.v1"]), hook.name


def test_a_build_without_a_version_refuses_every_update_hand_over(tmp_path):
    probe, data, fence = make_probe(tmp_path, "noversion", version=None)
    (data / "Updates" / "receipts").mkdir(parents=True)
    hold = update_service.MutexHold((fence,))
    process = subprocess.Popen([str(probe.exe), "/S", "/QRONOSUPDATE"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        fields = _valid_fields(process, hold, data)
        fields["version"] = "${VERSION}"  # what a Function expanded before the define would have expected
        process.stdin.write(_record(fields))
        process.stdin.close()
        code = finish_probe(process, 90)
        ack = process.stdout.read()
    finally:
        hold.release()
    assert code != 0 and "invalid value" in probe.read_result() and ack == b""
