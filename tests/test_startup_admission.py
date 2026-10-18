"""
R3/R4/N9: the start-up order of every store-opening process (core/startup_admission.py) and
its frozen dispatch (build_support/runtime_entry.py).

Native boundary of these fixtures (audited):
- real LockFileEx admission (imported core.store_admission) ONLY on tmp data roots;
- singletons only under private names; the pre-import probe is a base-interpreter child
  (tests/fixtures/child_python.py, -B) with QRONOS_PACKAGED_RUNTIME=1 and every Qronos path
  in tmp, which imports modules and lists the tmp data root, then exits by itself.
No production mutex, store, profile, device, model, server or network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from build_support import runtime_entry
from core import startup_admission, store_admission
from core.startup_admission import STARTUP_REFUSED_EXIT, StartupRefused
from core.store_admission import AdmissionError
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


class Guard:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def close(self) -> None:
        self.calls.append("instance-released")


def scripted(calls: list, *, instance=None, admit=None):
    """Seams that record the order; each may be replaced by a failure."""
    def acquire_instance():
        calls.append("instance")
        if instance is not None:
            raise instance
        return Guard(calls)

    def do_admit(root):
        calls.append("admit")
        if admit is not None:
            raise admit
        return "admission"

    return {"acquire_instance": acquire_instance, "admit": do_admit}


def service_start(calls: list, **options) -> None:
    """A store-opening service: start-up order, then its first store."""
    startup_admission.enter("fixture", installed=True, windows=True, data_root=Path("unused"), **options)
    calls.append("store-opened")


# ------------------------------------------------------------------ the order itself


def test_the_order_is_ownership_then_admission_then_stores_once():
    calls: list = []
    service_start(calls, instance=True, **scripted(calls))
    assert calls == ["instance", "admit", "store-opened"]
    # idempotent for the SAME root (re-entry names the root it was admitted on; another root is refused)
    startup_admission.enter("fixture", instance=True, installed=True, windows=True, data_root=Path("unused"))
    assert calls == ["instance", "admit", "store-opened"]


REFUSALS = {
    "instance_busy": ({"instance": __import__("core.runtime_instance_guard").runtime_instance_guard
                       .RuntimeAlreadyRunning("x")}, "already_running", ["instance"]),
    "instance_unavailable": ({"instance": __import__("core.instance_ownership").instance_ownership
                              .OwnershipUnavailable("x")}, "ownership_unavailable", ["instance"]),
    "admission_busy": ({"admit": AdmissionError("busy", "fixture")}, "busy",
                       ["instance", "admit", "instance-released"]),
    "admission_store": ({"admit": AdmissionError("store", "fixture")}, "store",
                        ["instance", "admit", "instance-released"]),
    "admission_unknown_code": ({"admit": AdmissionError("something_else", "fixture")}, "store",
                               ["instance", "admit", "instance-released"]),
    "admission_os_error": ({"admit": OSError("fixture")}, "store", ["instance", "admit", "instance-released"]),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_every_refusal_is_terminal_with_zero_store_opens(case):
    options, code, order = REFUSALS[case]
    calls: list = []
    with pytest.raises(StartupRefused) as refused:
        service_start(calls, instance=True, **scripted(calls, **options))
    assert refused.value.code == code
    assert calls == order and "store-opened" not in calls
    assert not startup_admission.admitted()


def test_a_source_checkout_is_not_the_installed_runtime_and_takes_nothing():
    calls: list = []
    assert startup_admission.enter("fixture", installed=False, instance=True, **scripted(calls)) is None
    assert calls == []
    startup_admission.enter("fixture", installed=False, instance=True)  # a repeat with instance is fine
    assert calls == []


# -------------------------------------------------------------- real admission (tmp)


def real(tmp_path: Path, **options):
    base = {"installed": True, "windows": True, "data_root": tmp_path / "data",
            "admit": store_admission.admit_writer}
    base.update(options)
    return base


@windows
def test_a_lifetime_writer_admission_blocks_an_exclusive_admission_until_the_process_ends(tmp_path):
    startup_admission.enter("settings", **real(tmp_path))
    with pytest.raises(AdmissionError) as refused:
        store_admission.admit_exclusive(tmp_path / "data")
    assert refused.value.code == "writers_active"
    startup_admission._reset_for_tests()  # the process ends: Windows releases it
    store_admission.admit_exclusive(tmp_path / "data").release()


@windows
def test_the_bridge_never_deadlocks_on_the_supervisor_singleton(tmp_path):
    from core.instance_ownership import OwnedMutex
    from core.runtime_instance_guard import RuntimeInstanceGuard

    supervisor = OwnedMutex.acquire(private("supervisor"))  # held by "the supervisor"
    bridge_name = private("bridge")
    try:
        started = time.monotonic()
        startup_admission.enter("bridge", instance=True, **real(
            tmp_path, acquire_instance=lambda: RuntimeInstanceGuard.acquire(name=bridge_name)))
        assert time.monotonic() - started < 5
        assert startup_admission.admitted()
    finally:
        supervisor.release()


# ------------------------------------------------------------ frozen dispatch (R4/N9)


def test_only_store_services_are_admitted():
    assert set(runtime_entry.STORE_SERVICES) <= set(runtime_entry.SERVICES)
    assert set(runtime_entry.STORE_SERVICES) == {"bridge", "settings", "library", "memory", "permissions"}
    assert runtime_entry.STORE_SERVICES["bridge"] == {"instance": True}


@pytest.mark.parametrize("arguments,admitted", [
    (["settings", "snapshot"], "settings"), (["bridge"], "bridge"), (["audio", "devices"], None),
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


@pytest.mark.parametrize("service", ["settings", "bridge"])
def test_a_refused_start_up_never_imports_the_service(service):
    def refuse(name, **options):
        raise StartupRefused("busy")

    output = StringIO()
    with patch.object(startup_admission, "enter", refuse), patch.object(sys, "argv", ["runtime"]), \
            patch("build_support.runtime_entry.importlib.import_module") as imported, redirect_stdout(output):
        assert runtime_entry.main([service, "status"]) == STARTUP_REFUSED_EXIT
    imported.assert_not_called()
    printed = json.loads(output.getvalue())
    if service == "bridge":
        assert printed["eventType"] == "runtime_error" and "busy" in printed["message"]
    else:
        assert printed["ok"] is False and printed["code"] == "busy"


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
    startup_admission.enter("settings", windows=True, data_root=tmp_path / "data",
                            environ={"QRONOS_PACKAGED_RUNTIME": "0", "QRONOS_SKIP_ADMISSION": "1"}, **scripted(calls))
    assert calls == ["admit"]


@pytest.mark.parametrize("variable", ["QRONOS_SETTINGS_PATH", "QRONOS_CONVERSATION_HISTORY_PATH",
                                      "QRONOS_APPROVAL_SECRET_PATH", "QRONOS_PERMISSION_POLICY_PATH"])
def test_a_source_process_opening_installed_stores_is_admitted_on_that_root(tmp_path, variable):
    installed = tmp_path / "local" / "Qronos"
    roots: list = []
    calls: list = []
    seams = scripted(calls)
    seams["admit"] = lambda root: roots.append(Path(root)) or calls.append("admit") or "admission"
    startup_admission.enter("settings", installed=False, windows=True,
                            environ={"LOCALAPPDATA": str(tmp_path / "local"), variable: str(installed / "x.db")},
                            **seams)
    assert calls == ["admit"] and roots == [installed]


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
mode, root, ready, go = sys.argv[2:6]
admission = (store_admission.admit_exclusive(Path(root)) if mode == "exclusive"
             else store_admission.admit_writer(Path(root)))
Path(ready + ".tmp").write_text("held", encoding="ascii")
os.replace(ready + ".tmp", ready)
deadline = time.monotonic() + 60
while not os.path.exists(go) and time.monotonic() < deadline:
    time.sleep(0.02)
"""


class ChildAdmission:
    """A base-interpreter child holding a shared (a running bridge) or exclusive admission."""

    def __init__(self, tmp: Path, mode: str, root: Path) -> None:
        tag = uuid.uuid4().hex[:8]
        self.ready, self.go = tmp / f"ready-{tag}", tmp / f"go-{tag}"
        executable, environment = child_python()
        self.process = subprocess.Popen([executable, "-B", "-c", WRITER, str(ROOT), mode, str(root),
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


@windows
def test_a_service_starts_normally_while_the_bridge_holds_its_lifetime_admission(tmp_path):
    root = tmp_path / "data"
    root.mkdir(parents=True)
    bridge = ChildAdmission(tmp_path, "shared", root)
    try:
        started = time.monotonic()
        startup_admission.enter("settings", **real(tmp_path))
        assert startup_admission.admitted() and time.monotonic() - started < 5
    finally:
        bridge.end()


@windows
def test_no_writer_is_admitted_while_another_process_holds_the_exclusive_admission(tmp_path):
    root = tmp_path / "data"
    root.mkdir(parents=True)
    exclusive = ChildAdmission(tmp_path, "exclusive", root)
    try:
        with pytest.raises(StartupRefused) as refused:
            startup_admission.enter("settings", **real(tmp_path, data_root=root))
        assert refused.value.code == "busy"
    finally:
        exclusive.end()
