"""
R3/R4/N9: the start-up order of every store-opening process (core/startup_admission.py) and
its frozen dispatch (build_support/runtime_entry.py).

Native boundary of these fixtures (audited):
- real LockFileEx admission (imported core.store_admission) ONLY on tmp data roots;
- the update fence is a PRIVATE mutex "Local\\QronosR9Test-<uuid>-fence" (never the product
  name); store_admission.update_in_progress opens it read-only;
- the real UpdateService barrier only on a tmp data root with a private fence liveness;
- singletons only under private names; the pre-import probe is a base-interpreter child
  (tests/fixtures/child_python.py, -B) with QRONOS_PACKAGED_RUNTIME=1 and every Qronos path
  in tmp, which imports modules and lists the tmp data root, then exits by itself.
No production mutex, store, profile, device, model, server or network.
"""

from __future__ import annotations

import ctypes
import functools
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import redirect_stdout
from ctypes import wintypes
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from build_support import runtime_entry
from core import startup_admission, store_admission
from core.local_update import UpdateError
from core.startup_admission import STARTUP_REFUSED_EXIT, StartupRefused
from tests.fixtures.child_python import child_python

windows = pytest.mark.skipif(os.name != "nt", reason="Windows admission and mutexes")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def fresh_process_admission():
    startup_admission._reset_for_tests()
    yield
    startup_admission._reset_for_tests()


def private(tag: str) -> str:
    return f"Local\\QronosR9Test-{uuid.uuid4().hex}-{tag}"


class HeldFence:
    def __init__(self, name: str) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel, self.handle = kernel, kernel.CreateMutexW(None, False, name)

    def close(self) -> None:
        self.kernel.CloseHandle(self.handle)


class Guard:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def close(self) -> None:
        self.calls.append("instance-released")


def scripted(calls: list, *, instance=None, recover=None, admit=None):
    """Seams that record the order; each may be replaced by a failure."""
    def acquire_instance(retry):
        calls.append("instance")
        if instance is not None:
            raise instance
        return Guard(calls)

    def do_recover(version):
        calls.append(f"recover:{version}")
        return recover(version) if callable(recover) else (recover if recover is not None else {"ok": True})

    def do_admit(root):
        calls.append("admit")
        if admit is not None:
            raise admit
        return "admission"

    return {"acquire_instance": acquire_instance, "recover": do_recover, "admit": do_admit}


def service_start(calls: list, **options) -> None:
    """A store-opening service: start-up order, then its first store."""
    startup_admission.enter("fixture", installed=True, windows=True, data_root=Path("unused"), **options)
    calls.append("store-opened")


# ------------------------------------------------------------------ the order itself


def test_the_order_is_ownership_version_barrier_admission_then_stores_once():
    calls: list = []
    service_start(calls, instance=True, version="0.2.0", **scripted(calls))
    assert calls == ["instance", "recover:0.2.0", "admit", "store-opened"]
    # idempotent for the SAME root (round 14: re-entry names the root it was admitted on; another root is refused)
    startup_admission.enter("fixture", instance=True, installed=True, windows=True, data_root=Path("unused"))
    assert calls == ["instance", "recover:0.2.0", "admit", "store-opened"]


def _recover_raises(version):
    raise RuntimeError("fixture: recovery blew up")


REFUSALS = {
    "instance_update": ({"instance": __import__("core.instance_ownership").instance_ownership.UpdateInProgress("x")},
                        "update_in_progress", ["instance"]),
    "instance_busy": ({"instance": __import__("core.runtime_instance_guard").runtime_instance_guard
                       .RuntimeAlreadyRunning("x")}, "already_running", ["instance"]),
    "recovery_failed": ({"recover": {"ok": False, "code": "recovery_failed"}}, "recovery_failed",
                        ["instance", "recover:0.2.0", "instance-released"]),
    "recovery_unknown": ({"recover": _recover_raises}, "recovery_failed",
                         ["instance", "recover:0.2.0", "instance-released"]),
    "recovery_not_a_result": ({"recover": "nonsense"}, "recovery_failed",
                              ["instance", "recover:0.2.0", "instance-released"]),
    "update_live_at_barrier": ({"recover": {"ok": False, "code": "update_in_progress"}}, "update_in_progress",
                               ["instance", "recover:0.2.0", "instance-released"]),
    "admission_refused": ({"admit": UpdateError("update_in_progress", "fixture")}, "update_in_progress",
                          ["instance", "recover:0.2.0", "admit", "instance-released"]),
    "admission_writers": ({"admit": UpdateError("writers_active", "fixture")}, "writers_active",
                          ["instance", "recover:0.2.0", "admit", "instance-released"]),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_every_refusal_is_terminal_with_zero_store_opens(case):
    options, code, order = REFUSALS[case]
    calls: list = []
    with pytest.raises(StartupRefused) as refused:
        service_start(calls, instance=True, version="0.2.0", **scripted(calls, **options))
    assert refused.value.code == code
    assert calls == order and "store-opened" not in calls
    assert not startup_admission.admitted()


def test_an_unknown_packaged_version_refuses_before_the_barrier():
    calls: list = []
    with pytest.raises(StartupRefused) as refused:
        startup_admission.enter("fixture", installed=True, windows=True, environ={"QRONOS_PACKAGED_RUNTIME": "1"},
                                **scripted(calls))
    assert refused.value.code == "version_unknown" and calls == []


def test_the_version_comes_from_the_desktop_package_information_not_a_constant():
    assert startup_admission.product_version({"QRONOS_APP_VERSION": "1.2.3", "QRONOS_PACKAGED_RUNTIME": "1"}) == "1.2.3"
    source = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))["version"]
    assert startup_admission.product_version({}) == source  # from source: tauri.conf.json itself


def test_a_source_checkout_is_not_the_installed_runtime_and_takes_nothing():
    calls: list = []
    assert startup_admission.enter("fixture", installed=False, instance=True, **scripted(calls)) is None
    assert calls == []
    startup_admission.enter("fixture", installed=False, instance=True)  # a repeat with instance is fine
    assert calls == []


# -------------------------------------------------------------- real admission (tmp)


def real(tmp_path: Path, fence: str, **options):
    def admit(root):
        return store_admission.admit_writer(root, fence=fence)

    base = {"installed": True, "windows": True, "data_root": tmp_path / "data", "version": "0.1.0",
            "recover": lambda version: {"ok": True}, "admit": admit}
    base.update(options)
    return base


@windows
def test_a_lifetime_writer_admission_blocks_the_installer_exclusive_admission_until_the_process_ends(tmp_path):
    fence = private("fence")
    startup_admission.enter("settings", **real(tmp_path, fence))
    with pytest.raises(UpdateError) as refused:
        store_admission.admit_exclusive(tmp_path / "data")  # the updater / installer side
    assert refused.value.code == "writers_active"
    startup_admission._reset_for_tests()  # the process ends: Windows releases it
    store_admission.admit_exclusive(tmp_path / "data").release()


@windows
def test_a_held_fence_refuses_the_writer_admission_and_keeps_nothing(tmp_path):
    fence_name = private("fence")
    fence = HeldFence(fence_name)
    try:
        with pytest.raises(StartupRefused) as refused:
            startup_admission.enter("settings", **real(tmp_path, fence_name))
        assert refused.value.code == "update_in_progress"
    finally:
        fence.close()
    store_admission.admit_exclusive(tmp_path / "data").release()  # the shared lock was given back


def recover_on(root: Path, fence: str):
    """core.update_service.recover_before_stores, on a tmp data root with a private fence liveness."""
    from core.update_service import UpdateService

    def recover(version):
        try:
            result = UpdateService(root, version, liveness=lambda: store_admission.update_in_progress(fence)) \
                .recover_before_stores()
        except UpdateError as error:
            return {"ok": False, "code": error.code}
        return {"ok": True, **result}

    return recover


@windows
def test_a_real_recovery_that_cannot_read_its_record_opens_no_store(tmp_path):
    data, fence = tmp_path / "data", private("fence")
    (data / "Updates").mkdir(parents=True)
    (data / "Updates" / "update-lifecycle.json").write_text("{ not a lifecycle", encoding="utf-8")
    opened = []
    with pytest.raises(StartupRefused):
        startup_admission.enter("bridge", **real(tmp_path, fence, recover=recover_on(data, fence)))
        opened.append("settings")
    assert opened == [] and not (data / "settings.json").exists()
    store_admission.admit_exclusive(data).release()  # no shared admission was taken


@windows
def test_the_bridge_barrier_never_deadlocks_on_its_own_or_the_supervisor_singleton(tmp_path):
    from core.instance_ownership import OwnedMutex
    from core.runtime_instance_guard import RuntimeInstanceGuard

    data, fence = tmp_path / "data", private("fence")
    (data / "Updates").mkdir(parents=True)  # the barrier really runs under the update store lock
    supervisor = OwnedMutex.acquire(private("supervisor"))  # held by "the supervisor"
    bridge_name = private("bridge")
    try:
        started = time.monotonic()
        startup_admission.enter("bridge", instance=True, **real(
            tmp_path, fence, recover=recover_on(data, fence),
            acquire_instance=lambda retry: RuntimeInstanceGuard.acquire(name=bridge_name, fence_exists=lambda: False)))
        assert time.monotonic() - started < 5
        assert startup_admission.admitted()
    finally:
        supervisor.release()


# ------------------------------------------------------------ frozen dispatch (R4/N9)


def test_update_is_registered_literally_and_only_store_services_are_admitted():
    assert runtime_entry.SERVICES["update"] == "core.update_cli"
    assert set(runtime_entry.STORE_SERVICES) <= set(runtime_entry.SERVICES)
    assert set(runtime_entry.STORE_SERVICES) == {"bridge", "settings", "library", "memory", "permissions", "update"}
    assert runtime_entry.STORE_SERVICES["bridge"] == {"instance": True}
    assert "launch" in runtime_entry.UPDATE_EXCLUSIVE_OPERATIONS


@pytest.mark.parametrize("arguments,admitted", [
    (["settings", "snapshot"], "settings"), (["bridge"], "bridge"), (["update", "status"], "update"),
    (["update", "launch"], None), (["update", " LAUNCH "], None), (["audio", "devices"], None),
    (["doctor"], None), (["supervisor", "--desktop"], None),
])
def test_admission_runs_before_the_service_module_is_imported(arguments, admitted):
    order: list = []
    with patch.object(startup_admission, "enter", lambda service, **o: order.append(f"enter:{service}")), \
            patch.object(sys, "argv", ["runtime"]), \
            patch("build_support.runtime_entry.importlib.import_module") as imported:
        imported.side_effect = lambda module: order.append(f"import:{module}") or imported.return_value
        imported.return_value.main.return_value = 0
        assert runtime_entry.main(list(arguments)) == 0
    expected = [f"import:{runtime_entry.SERVICES[arguments[0]]}"]
    assert order == ([f"enter:{admitted}"] if admitted else []) + expected


@pytest.mark.parametrize("service", ["settings", "bridge", "update"])
def test_a_refused_start_up_never_imports_the_service(service):
    def refuse(name, **options):
        raise StartupRefused("update_in_progress")

    output = StringIO()
    with patch.object(startup_admission, "enter", refuse), patch.object(sys, "argv", ["runtime"]), \
            patch("build_support.runtime_entry.importlib.import_module") as imported, redirect_stdout(output):
        assert runtime_entry.main([service, "status"]) == STARTUP_REFUSED_EXIT
    imported.assert_not_called()
    printed = json.loads(output.getvalue())
    if service == "bridge":
        assert printed["eventType"] == "runtime_error" and "update_in_progress" in printed["message"]
    else:
        assert printed["ok"] is False and printed["code"] == "update_in_progress"


@windows
def test_launch_takes_no_shared_admission_so_its_exclusive_admission_cannot_refuse_itself(tmp_path):
    fence = private("fence")
    real_enter = functools.partial(startup_admission.enter, **real(tmp_path, fence))
    with patch.object(startup_admission, "enter", lambda service, **o: real_enter(service, **o)):
        assert runtime_entry._admit_before_import("update", ["launch"]) is None
        lock = store_admission.admit_exclusive(tmp_path / "data")  # what LaunchHold takes next: granted
        lock.release()
        assert runtime_entry._admit_before_import("update", ["status"]) is None  # a shared admission now
        with pytest.raises(UpdateError) as refused:
            store_admission.admit_exclusive(tmp_path / "data")
        assert refused.value.code == "writers_active"  # had launch taken it, launch would refuse itself


# --------------------------------------------------------------- pre-import boundary


PROBE = r"""
import importlib, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
root = Path(os.environ["QRONOS_DATA_ROOT"])
def tree():
    if not root.exists():
        return None
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))
from build_support import runtime_entry
seen = {"before": tree()}
for module in sorted(set(runtime_entry.SERVICES.values())):
    importlib.import_module(module)
    seen[module] = tree()
print(json.dumps(seen))
"""


@windows
def test_importing_every_service_module_opens_no_store_in_the_installed_data_root(tmp_path):
    executable, environment = child_python()
    environment.update({
        "QRONOS_PACKAGED_RUNTIME": "1", "QRONOS_DATA_ROOT": str(tmp_path / "data"),
        "QRONOS_ASSET_ROOT": str(tmp_path / "assets"), "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"), "USERPROFILE": str(tmp_path / "user"), "HOME": str(tmp_path / "user"),
        "QRONOS_OLLAMA_AUTO_START": "0", "QRONOS_RUN_LIVE_DEVICE_TESTS": "0", "PYTHONDONTWRITEBYTECODE": "1",
    })
    done = subprocess.run([executable, "-B", "-c", PROBE, str(ROOT)], env=environment, capture_output=True,
                          text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert all(listing is None or listing == [] for listing in seen.values()), seen


# ------------------------------------------------------- supervisor: terminal refusal


class _ExitedChild:
    def __init__(self, code: int) -> None:
        self.pid = 4242
        self.code = code

    def poll(self):
        return self.code

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.code


class _Launcher:
    def __init__(self, code: int) -> None:
        self.code = code
        self.starts = 0

    def launch(self, bootstrap: bytes):
        self.starts += 1
        return _ExitedChild(self.code)


@windows
@pytest.mark.parametrize("code,terminal", [(78, True), (1, False)])
def test_a_start_up_refusal_is_terminal_and_seen_without_waiting_for_the_connect_timeout(code, terminal):
    from core.startup_admission import STARTUP_REFUSED_EXIT
    from core.supervisor import Supervisor, SupervisorPolicy, SupervisorState

    assert STARTUP_REFUSED_EXIT == 78
    policy = SupervisorPolicy(connect_timeout_seconds=60, restart_backoff_initial_seconds=0.1,
                              restart_backoff_max_seconds=0.1, max_restarts=1, restart_window_seconds=60)
    launcher = _Launcher(code)
    states: list = []
    supervisor = Supervisor(launcher, policy, notify=lambda state, detail: states.append((state, detail)))
    started = time.monotonic()
    final = supervisor.run()
    assert time.monotonic() - started < 10  # not the 60 s connect timeout per start
    assert final is SupervisorState.DEGRADED
    assert launcher.starts == (1 if terminal else 2)
    details = " | ".join(detail for _, detail in states)
    assert "exited before connecting" in details
    if terminal:
        assert "refused to start" in details


# ------------------------------------------------- the installed-only rule cannot be bypassed


def test_a_source_checkout_never_resolves_installed_paths_or_overrides(tmp_path):
    from core.config import resolve_paths

    paths = resolve_paths(root=tmp_path / "checkout", packaged=False, environ={
        "QRONOS_DATA_ROOT": str(tmp_path / "chosen"), "LOCALAPPDATA": str(tmp_path / "local")})
    assert paths.data == (tmp_path / "checkout").resolve() / "data" and not paths.packaged


def test_the_frozen_runtime_is_always_the_installed_runtime(tmp_path, monkeypatch):
    from core.config import resolve_paths

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    paths = resolve_paths(root=tmp_path / "install", environ={"LOCALAPPDATA": str(tmp_path / "local"),
                                                              "QRONOS_PACKAGED_RUNTIME": "0"})
    assert paths.packaged and paths.data == (tmp_path / "local" / "Qronos").resolve()


def test_no_environment_value_waives_admission_in_the_installed_runtime(tmp_path, monkeypatch):
    from core import config
    from core.config import QronosConfig, resolve_paths

    installed = resolve_paths(root=tmp_path / "install", packaged=True, environ={"LOCALAPPDATA": str(tmp_path / "l")})
    monkeypatch.setattr(config, "CONFIG", QronosConfig(paths=installed))
    calls: list = []
    startup_admission.enter("settings", windows=True, version="0.1.0", data_root=tmp_path / "data",
                            environ={"QRONOS_PACKAGED_RUNTIME": "0", "QRONOS_SKIP_ADMISSION": "1"}, **scripted(calls))
    assert calls == ["recover:0.1.0", "admit"]


@pytest.mark.parametrize("variable", ["QRONOS_SETTINGS_PATH", "QRONOS_CONVERSATION_HISTORY_PATH",
                                      "QRONOS_APPROVAL_SECRET_PATH", "QRONOS_PERMISSION_POLICY_PATH"])
def test_a_source_process_opening_installed_stores_is_admitted_on_that_root(tmp_path, variable):
    installed = tmp_path / "local" / "Qronos"
    roots: list = []
    calls: list = []
    seams = scripted(calls)
    seams["admit"] = lambda root: roots.append(Path(root)) or calls.append("admit") or "admission"
    startup_admission.enter("settings", installed=False, windows=True, version="0.1.0",
                            environ={"LOCALAPPDATA": str(tmp_path / "local"), variable: str(installed / "x.db")},
                            **seams)
    assert calls == ["recover:0.1.0", "admit"] and roots == [installed]


def test_a_source_process_with_its_own_store_paths_takes_nothing(tmp_path):
    calls: list = []
    startup_admission.enter("settings", installed=False, windows=True, environ={
        "LOCALAPPDATA": str(tmp_path / "local"), "QRONOS_SETTINGS_PATH": str(tmp_path / "dev" / "settings.json")},
        **scripted(calls))
    assert calls == []


def test_runtime_entry_passes_no_waiver_to_the_start_up_order():
    seen: list = []
    with patch.object(startup_admission, "enter", lambda service, **options: seen.append((service, options))), \
            patch.object(sys, "argv", ["runtime"]), \
            patch("build_support.runtime_entry.importlib.import_module") as imported:
        imported.return_value.main.return_value = 0
        runtime_entry.main(["bridge", "--installed", "0", "--windows", "0"])
        runtime_entry.main(["settings", "snapshot"])
    assert seen == [("bridge", {"instance": True}), ("settings", {"instance": False})]


# ---------------------------------------------------- R3 across real processes (tmp roots)


WRITER = r"""
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core import store_admission
mode, root, fence, ready, go = sys.argv[2:7]
admission = (store_admission.admit_exclusive(Path(root)) if mode == "exclusive"
             else store_admission.admit_writer(Path(root), fence=fence))
Path(ready + ".tmp").write_text("held", encoding="ascii")
os.replace(ready + ".tmp", ready)
deadline = time.monotonic() + 60
while not os.path.exists(go) and time.monotonic() < deadline:
    time.sleep(0.02)
"""


class ChildAdmission:
    """A base-interpreter child holding a shared (a running bridge) or exclusive (a restore) admission."""

    def __init__(self, tmp: Path, mode: str, root: Path, fence: str) -> None:
        tag = uuid.uuid4().hex[:8]
        self.ready, self.go = tmp / f"ready-{tag}", tmp / f"go-{tag}"
        executable, environment = child_python()
        self.process = subprocess.Popen([executable, "-B", "-c", WRITER, str(ROOT), mode, str(root), fence,
                                         str(self.ready), str(self.go)], env=environment)
        deadline = time.monotonic() + 30
        while not self.ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.ready.exists(), "the child did not take its admission"

    def end(self) -> None:
        self.go.write_text("1")
        try:
            self.process.wait(30)
        except subprocess.TimeoutExpired:
            self.process.kill()  # exactly this test's own child, by its own handle
            self.process.wait(10)


def barrier_on(root: Path, fence: str):
    from core.update_service import UpdateService

    def recover(version):
        try:
            UpdateService(root, version, liveness=lambda: store_admission.update_in_progress(fence),
                          environ={}).recover_before_stores()
        except UpdateError as error:
            return {"ok": False, "code": error.code}
        return {"ok": True}

    return recover


@windows
def test_a_service_starts_normally_while_the_bridge_holds_its_lifetime_admission(tmp_path):
    root, fence = tmp_path / "data", private("fence")
    (root / "Updates").mkdir(parents=True)
    bridge = ChildAdmission(tmp_path, "shared", root, fence)
    try:
        started = time.monotonic()
        startup_admission.enter("settings", **real(tmp_path, fence, recover=barrier_on(root, fence)))
        assert startup_admission.admitted() and time.monotonic() - started < 5
    finally:
        bridge.end()


def pending_restore(root: Path, fence: str) -> None:
    """A real interrupted rollback: a safety snapshot of settings plus a 'restoring' claim."""
    from core.update_service import UpdateService

    (root / "Updates").mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text('{"schemaVersion": 2, "voice": "saved"}', encoding="utf-8")
    service = UpdateService(root, "0.2.0", liveness=lambda: store_admission.update_in_progress(fence), environ={})
    snapshot = service._take_snapshot("pre_restore", "0.1.0", ["settings"])
    (root / "settings.json").write_text('{"schemaVersion": 2, "voice": "current"}', encoding="utf-8")
    service._change(lambda state: state.update(launching={
        "claimedAt": 1_800_000_000.0, "pid": 1, "version": "0.1.0", "packageSha256": "a" * 64, "step": "restoring",
        "restoredSnapshot": snapshot["id"], "safetySnapshot": snapshot["id"]}))


def voice_of(root: Path) -> str:
    return json.loads((root / "settings.json").read_text(encoding="utf-8"))["voice"]


@windows
def test_a_pending_restore_with_a_live_writer_refuses_before_any_store_and_restores_nothing(tmp_path):
    root, fence = tmp_path / "data", private("fence")
    pending_restore(root, fence)
    lifecycle = root / "Updates" / "update-lifecycle.json"
    claim_before = json.loads(lifecycle.read_text(encoding="utf-8"))["launching"]
    writer = ChildAdmission(tmp_path, "shared", root, fence)
    opened = []
    try:
        with pytest.raises(StartupRefused) as refused:
            startup_admission.enter("bridge", **real(tmp_path, fence, data_root=root, recover=barrier_on(root, fence)))
            opened.append("stores")
        assert refused.value.code == "recovery_pending" and opened == []
        assert voice_of(root) == "current"  # nothing was put back under the live writer
        assert json.loads(lifecycle.read_text(encoding="utf-8"))["launching"] == claim_before
    finally:
        writer.end()
    startup_admission._reset_for_tests()
    startup_admission.enter("bridge", **real(tmp_path, fence, data_root=root, recover=barrier_on(root, fence)))
    assert voice_of(root) == "saved"  # restored once the writer was gone, before this process's stores
    assert json.loads(lifecycle.read_text(encoding="utf-8"))["launching"] is None


@windows
def test_no_writer_is_admitted_while_a_restore_holds_the_exclusive_admission(tmp_path):
    root, fence = tmp_path / "data", private("fence")
    root.mkdir(parents=True)
    restoring = ChildAdmission(tmp_path, "exclusive", root, fence)
    try:
        with pytest.raises(StartupRefused) as refused:
            startup_admission.enter("settings", **real(tmp_path, fence, data_root=root))
        assert refused.value.code == "update_in_progress"
    finally:
        restoring.end()
