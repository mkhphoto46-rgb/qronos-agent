"""Round 59: the update launch's composition, seam by seam (core.update_service.LAUNCH_DEPENDENCIES).

Each seam is already implemented and tested in its own module; this file checks they fit together in THIS source now
that the one enablement point (core.update_cli.LAUNCH_ENABLED) is open:
- every frozen backend service has its update refusal (writer admission, instance/sidecar guard, or the fence check);
- the updater's hold names exactly the singletons the runtime, supervisor and sidecars own, and the same fence;
- every store-opening entry runs the start-up order (barrier + lifetime writer admission) before its first store,
  the media host included, and the bridge closes the media host at shutdown;
- the product enables ONLY the owned launch hold, the class default stays refusing, the launch hands over through
  the fenced launcher, and the desktop launches only through its lifecycle when the backend reports it available.
Fakes only: no named mutex with a product name is created, no store opens, no process starts.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from build_support import runtime_entry
from core import instance_ownership, startup_admission, store_admission, update_cli, update_service

ROOT = Path(__file__).resolve().parent.parent


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# ------------------------------------------------------------------ every backend service refuses during an update

def test_every_frozen_backend_service_has_exactly_one_update_refusal_path():
    services = set(runtime_entry.SERVICES)
    store, fenced = set(runtime_entry.STORE_SERVICES), set(runtime_entry.FENCED_SERVICES)
    assert store.isdisjoint(fenced) and "supervisor" not in store | fenced
    assert services == store | fenced | {"supervisor"}  # a new service cannot slip in without a refusal path
    assert runtime_entry.STORE_SERVICES["bridge"] == {"instance": True}
    supervisor = _source("core/supervisor.py")
    assert "acquire_unless_updating" in supervisor and "class SupervisorInstanceGuard" in supervisor


@pytest.mark.parametrize("service", sorted(runtime_entry.FENCED_SERVICES))
def test_a_fenced_service_refuses_before_import_while_the_update_fence_exists(service, monkeypatch, capsys):
    import sys

    monkeypatch.setattr(sys, "argv", list(sys.argv))  # main() sets sys.argv for the dispatched service
    imported = []
    monkeypatch.setattr(runtime_entry.importlib, "import_module",
                        lambda name: imported.append(name) or type("M", (), {"main": staticmethod(lambda: 0)}))
    monkeypatch.setattr(instance_ownership, "update_fence_exists", lambda fence=None: True)
    assert runtime_entry.main([service]) == startup_admission.STARTUP_REFUSED_EXIT
    answer = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert answer["code"] == "update_in_progress" and imported == []  # refused BEFORE the module was imported

    def unknown(fence=None):
        raise OSError("the native answer was not understood")

    monkeypatch.setattr(instance_ownership, "update_fence_exists", unknown)
    assert runtime_entry.main([service]) == startup_admission.STARTUP_REFUSED_EXIT
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["code"] == "ownership_unavailable"
    assert imported == []  # an unknown fence never means "no update"
    monkeypatch.setattr(instance_ownership, "update_fence_exists", lambda fence=None: False)
    assert runtime_entry.main([service]) == 0 and imported == [runtime_entry.SERVICES[service]]


def test_store_services_are_not_fenced_twice_they_are_refused_by_their_writer_admission(monkeypatch):
    monkeypatch.setattr(instance_ownership, "update_fence_exists", lambda fence=None: True)
    for service in runtime_entry.STORE_SERVICES:
        assert runtime_entry._refuse_during_update(service) is None  # startup_admission.enter decides for these
    assert "if update_in_progress(fence):" in _source("core/store_admission.py")  # admit_writer refuses on the fence


# ------------------------------------------------------------------ the hold names what the runtime owns

def test_the_updaters_hold_names_exactly_the_runtime_singletons_and_the_one_fence():
    from core import chatterbox_runtime, whisper_server_runtime
    from core.runtime_instance_guard import RuntimeInstanceGuard
    from core.supervisor import SupervisorInstanceGuard

    owned = {RuntimeInstanceGuard.NAME, SupervisorInstanceGuard.NAME, chatterbox_runtime.CHATTERBOX_SIDECAR_MUTEX,
             whisper_server_runtime.WHISPER_SIDECAR_MUTEX}
    assert r"Local\Qronos.Memory.Sidecar.v1" in _source("core/memory_runtime.py")
    owned.add(r"Local\Qronos.Memory.Sidecar.v1")
    assert set(update_service.RUNTIME_MUTEXES) == owned
    assert update_service.UPDATE_IN_PROGRESS_MUTEX == store_admission.UPDATE_IN_PROGRESS_MUTEX
    defaults = update_service.LaunchHold.__init__.__defaults__
    assert defaults[0] == (store_admission.UPDATE_IN_PROGRESS_MUTEX, *update_service.RUNTIME_MUTEXES)
    assert defaults[1] is store_admission.admit_exclusive  # the EXCLUSIVE admission first, then the mutexes


# ------------------------------------------------------------------ store-opening entries: barrier + writer admission

def test_every_store_opening_entry_runs_the_start_up_order_before_its_first_store():
    for module in ("core/settings_cli.py", "core/library_cli.py", "core/memory_cli.py", "core/permissions_cli.py"):
        assert "startup_admission.enter_cli(" in _source(module), module
    assert 'startup_admission.enter_cli("update", running_version=running_version)' in _source("core/update_cli.py")
    bridge = _source("core/runtime_bridge.py")
    assert 'startup_admission.enter("bridge", instance=True)' in bridge
    assert runtime_entry.UPDATE_EXCLUSIVE_OPERATIONS == frozenset({"launch"})  # launch takes the EXCLUSIVE admission


def test_the_start_up_order_is_barrier_then_lifetime_admission_and_a_refusal_opens_nothing(tmp_path):
    """The real enter() with injected seams (a private data root; no mutex, lock file or store is touched)."""
    steps = []

    def recover(outcome):
        return lambda version: steps.append(("recover", version)) or outcome

    def admit(root):
        steps.append(("admit", Path(root)))
        return "admission"

    def confirm(version):
        steps.append(("confirm", version))
        return {"ok": True}

    common = dict(data_root=tmp_path, version="0.1.0", admit=admit, confirm=confirm, windows=True, installed=True)
    startup_admission._reset_for_tests()
    try:
        with pytest.raises(startup_admission.StartupRefused) as refused:
            startup_admission.enter("settings", recover=recover({"ok": False, "code": "recovery_failed"}), **common)
        assert refused.value.code == "recovery_failed" and steps == [("recover", "0.1.0")]  # nothing admitted/opened
        startup_admission._reset_for_tests()
        steps.clear()
        assert startup_admission.enter("settings", recover=recover({"ok": True}), **common) == "admission"
        assert steps == [("recover", "0.1.0"), ("admit", tmp_path), ("confirm", "0.1.0")]
        assert startup_admission.admitted()  # kept for the process's life (released by Windows at exit)
    finally:
        startup_admission._reset_for_tests()


def test_the_media_host_admits_its_store_before_opening_it_and_the_bridge_closes_it(tmp_path):
    from qronos_media.host import MediaHost

    from tests.test_media_semantic import authority

    auth, _ = authority(tmp_path)
    opened = []

    def refuse(root):
        raise startup_admission.StartupRefused("update_in_progress")

    host = MediaHost(auth, data_dir=lambda: tmp_path / "media", tools=lambda: None, permissions=lambda: None,
                     admit_store=refuse, service_factory=lambda *a, **k: opened.append(a) or object())
    with pytest.raises(startup_admission.StartupRefused):
        host._open()
    assert opened == []  # refused: nothing opened
    admitted = []
    host = MediaHost(auth, data_dir=lambda: tmp_path / "media", tools=lambda: None, permissions=lambda: None,
                     admit_store=lambda root: admitted.append(Path(root)),
                     service_factory=lambda *a, **k: opened.append(a) or type("S", (), {"close": lambda self: None})())
    host._open()
    assert admitted == [tmp_path / "media"] and len(opened) == 1  # admitted, THEN opened
    host.close()
    assert host.status()["storeOpen"] is False  # the store closes with the host
    tree = ast.parse(_source("qronos_media/host.py"))
    assert any(isinstance(node, ast.FunctionDef) and node.name == "media_store_admission" for node in tree.body)
    assert "enter_for_store(\"media\", \"QRONOS_MEDIA_DATA_DIR\"" in _source("qronos_media/host.py")
    bridge = _source("core/runtime_bridge.py")
    shutdown = bridge[bridge.index("media_host = self._media_host"):]
    assert shutdown.index("media_host.close()") < shutdown.index("self._approvals.close()")


# ------------------------------------------------------------------ the product enablement and the hand-over

def test_the_product_enables_only_the_owned_hold_and_the_hand_over_is_the_fenced_launcher(tmp_path, monkeypatch):
    assert update_cli.LAUNCH_ENABLED is True
    built = []
    monkeypatch.setattr(update_service, "LaunchHold", lambda data_root: built.append(Path(data_root)) or "hold")
    provider = update_cli.launch_quiescence(tmp_path)
    assert provider is not update_service.launch_unavailable and built == []  # nothing is taken until a launch
    assert provider() == "hold" and built == [tmp_path]
    defaults = update_service.UpdateService.__init__.__kwdefaults__
    assert defaults["quiesce"] is update_service.launch_unavailable  # the class default stays refusing
    assert defaults["launcher"] is update_service.fenced_launcher
    launcher = _source("core/update_service.py")
    assert "DuplicateHandle(me, handover.fence, int(process._handle), _SYNCHRONIZE, False, 0)" in launcher
    assert "close_fds=True" in launcher  # only the standard handles are inherited


def test_the_desktop_launches_only_through_its_lifecycle_and_only_when_the_backend_says_available():
    lifecycle = _source("desktop/src-tauri/src/runtime/lifecycle.rs")
    assert 'flag("launchAvailable")' in lifecycle or "launchAvailable" in lifecycle
    assert "pub(super) fn route_update(operation: &str) -> UpdateRoute" in lifecycle
    assert "fn close_and_drain(&self, bound: Duration)" in lifecycle
    panel = _source("desktop/src/components/UpdatePanel.tsx")
    assert "status.launchAvailable" in panel  # the button follows the backend, never a frontend flag
