"""
Round 14 (Runtime17 F1/F2/O1, Installer N9): the start-up order on the RESOLVED data root, bound per process,
re-checked under the shared admission, and run by the source update CLI itself.

Native boundary of these fixtures (audited):
- real LockFileEx admission (core.store_admission) ONLY on tmp roots; the update fence is a PRIVATE mutex
  "Local\\QronosR9Test-<uuid>-fence" (store_admission and update_service defaults replaced in children);
- the known-folder Local AppData is a private tmp folder (tests/conftest.py in-process;
  tests/fixtures/startup_entry_child.py in children); the real one is never queried or resolved;
- junctions only through tests/fixtures/owned_junction.py (link removed, target kept); 8.3 names are read with
  GetShortPathNameW of tmp folders only;
- children: base interpreter (tests/fixtures/child_python.py) with -B, bounded waits, killed only by their own
  Popen handle; tmp data, profile and checkout; the real UpdateService barrier on tmp roots only.
No production mutex, store, profile, device, model, server or network.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from core import startup_admission, store_admission
from core.startup_admission import STARTUP_REFUSED_EXIT, StartupRefused
from tests.fixtures.child_python import child_python
from tests.fixtures.owned_junction import owned_junction

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows admission, junctions and mutexes")
ROOT = Path(__file__).resolve().parent.parent
CHILD = ROOT / "tests" / "fixtures" / "startup_entry_child.py"


@pytest.fixture(autouse=True)
def fresh_process_admission():
    startup_admission._reset_for_tests()
    yield
    startup_admission._reset_for_tests()


def private(tag: str) -> str:
    return f"Local\\QronosR9Test-{uuid.uuid4().hex}-{tag}"


def _unprefixed(text: str) -> str:
    return text[4:] if text.startswith("\\\\?\\") else text


def key(path) -> str:
    """The test's OWN resolution (independent of the product's): Windows' final path, no extended prefix, normcase."""
    return os.path.normcase(os.path.normpath(_unprefixed(os.path.realpath(_unprefixed(os.fspath(path))))))


def source_enter(environ: dict, roots: list, **extra):
    """A source (non-installed) process start with seams that record where it would be admitted."""
    return startup_admission.enter(
        "settings", installed=False, windows=True, environ=environ, version="0.1.0",
        recover=lambda v: {"ok": True},
        admit=lambda r: roots.append(Path(r)) or f"admission:{key(r)}", **extra)


def short_path(path: Path) -> str:
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    return buffer.value


# ===================================================================== F1: resolved installed targeting


@pytest.fixture
def installed(tmp_path):
    local = tmp_path / "local"
    root = local / "Qronos"
    root.mkdir(parents=True)
    return local, root


@pytest.mark.parametrize("spelling", ["plain", "case", "extended", "junction", "junction_new_folder", "short_8_3"])
def test_f1_an_override_reaching_the_installed_root_by_any_spelling_is_admitted_on_that_root(tmp_path, installed,
                                                                                              spelling):
    local, root = installed
    roots: list = []
    if spelling == "short_8_3":
        spaced = tmp_path / "a long folder name with spaces"
        (spaced / "Qronos").mkdir(parents=True)
        short = short_path(spaced / "Qronos")
        if not short or short.lower() == str(spaced / "Qronos").lower():
            pytest.skip("this volume makes no 8.3 names")
        source_enter({"LOCALAPPDATA": str(spaced), "QRONOS_SETTINGS_PATH": short + "\\settings.json"}, roots)
        assert [key(r) for r in roots] == [key(spaced / "Qronos")]
        return
    if spelling.startswith("junction"):
        with owned_junction(root, tmp_path / "alias") as link:
            leaf = link / "new" / "deeper" / "settings.json" if spelling == "junction_new_folder" else link / "s.json"
            source_enter({"LOCALAPPDATA": str(local), "QRONOS_SETTINGS_PATH": str(leaf)}, roots)
    else:
        value = {"plain": str(root / "s.json"), "case": str(root).upper() + "\\S.JSON",
                 "extended": "\\\\?\\" + str(root / "s.json")}[spelling]
        source_enter({"LOCALAPPDATA": str(local), "QRONOS_SETTINGS_PATH": value}, roots)
    assert [key(r) for r in roots] == [key(root)], roots


def test_f1_a_symbolic_link_alias_is_resolved_too(tmp_path, installed):
    local, root = installed
    link = tmp_path / "symlink-alias"
    try:
        os.symlink(root, link, target_is_directory=True)
    except OSError:
        pytest.skip("creating symbolic links needs a privilege this account does not have")
    try:
        roots: list = []
        source_enter({"LOCALAPPDATA": str(local), "QRONOS_SETTINGS_PATH": str(link / "s.json")}, roots)
        assert [key(r) for r in roots] == [key(root)]
    finally:
        os.rmdir(link)  # the link itself only


def test_f1_the_known_folder_root_counts_even_when_localappdata_names_another_folder(tmp_path, monkeypatch):
    known = tmp_path / "known"
    (known / "Qronos").mkdir(parents=True)
    monkeypatch.setattr(startup_admission, "_known_local_appdata", lambda: known, raising=False)
    roots: list = []
    with owned_junction(known / "Qronos", tmp_path / "into-known") as link:
        source_enter({"LOCALAPPDATA": str(tmp_path / "elsewhere"), "QRONOS_APPROVAL_SECRET_PATH": str(link / "x.db")},
                     roots)
    assert [key(r) for r in roots] == [key(known / "Qronos")]


def test_f1_the_explicit_configured_root_contract_is_kept_and_resolved(tmp_path, installed):
    local, _ = installed
    configured = tmp_path / "configured-root"
    configured.mkdir()
    roots: list = []
    with owned_junction(configured, tmp_path / "configured-alias") as link:
        source_enter({"LOCALAPPDATA": str(local), "QRONOS_DATA_ROOT": str(configured),
                      "QRONOS_PERMISSION_POLICY_PATH": str(link / "p.json")}, roots)
    assert [key(r) for r in roots] == [key(configured)]


def test_f1_overrides_into_two_different_installed_roots_are_refused_not_first_wins(tmp_path, installed):
    local, root = installed
    configured = tmp_path / "configured-root"
    configured.mkdir()
    roots: list = []
    with pytest.raises(StartupRefused) as refused:
        source_enter({"LOCALAPPDATA": str(local), "QRONOS_DATA_ROOT": str(configured),
                      "QRONOS_SETTINGS_PATH": str(root / "s.json"),
                      "QRONOS_CONVERSATION_HISTORY_PATH": str(configured / "h.db")}, roots)
    assert refused.value.code == "store" and roots == [] and not startup_admission.admitted()


@pytest.mark.parametrize("bad", ["invalid_name", "known_folder_unavailable"])
def test_f1_undecidable_targeting_is_refused_never_a_lexical_waiver(tmp_path, installed, monkeypatch, bad):
    local, root = installed
    environ = {"LOCALAPPDATA": str(local), "QRONOS_SETTINGS_PATH": str(root / "s.json")}
    if bad == "invalid_name":
        environ["QRONOS_SETTINGS_PATH"] = str(tmp_path / "bad<>|name" / "s.json")
    else:
        monkeypatch.setattr(startup_admission, "_known_local_appdata",
                            lambda: (_ for _ in ()).throw(OSError("fixture: no known folder")), raising=False)
    roots: list = []
    with pytest.raises(StartupRefused) as refused:
        source_enter(environ, roots)
    assert refused.value.code == "store" and roots == [] and not startup_admission.admitted()


def test_f1_the_source_default_takes_nothing_and_asks_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(startup_admission, "_known_local_appdata",
                        lambda: (_ for _ in ()).throw(AssertionError("the known folder was asked without overrides")),
                        raising=False)
    roots: list = []
    assert source_enter({"LOCALAPPDATA": str(tmp_path / "local")}, roots) is None and roots == []
    startup_admission._reset_for_tests()
    roots2: list = []
    monkeypatch.setattr(startup_admission, "_known_local_appdata", lambda: tmp_path / "known", raising=False)
    assert source_enter({"LOCALAPPDATA": str(tmp_path / "local"),
                         "QRONOS_SETTINGS_PATH": str(tmp_path / "dev" / "s.json")}, roots2) is None and roots2 == []


# ===================================================================== F2: idempotence bound to root and mode


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
deadline = time.monotonic() + 90
while not os.path.exists(go) and time.monotonic() < deadline:
    time.sleep(0.02)
"""


def clean_env() -> tuple[str, dict]:
    executable, environment = child_python()
    for name in list(environment):
        if name.startswith("QRONOS_"):
            environment.pop(name)
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "QRONOS_RUN_LIVE_DEVICE_TESTS": "0",
                        "QRONOS_OLLAMA_AUTO_START": "0"})
    return executable, environment


class Holder:
    """A child holding a SHARED (a live writer) or EXCLUSIVE (a restore) admission on ROOT until ended."""

    def __init__(self, tmp: Path, mode: str, root: Path, fence: str) -> None:
        tag = uuid.uuid4().hex[:8]
        self.ready, self.go = tmp / f"hold-ready-{tag}", tmp / f"hold-go-{tag}"
        executable, environment = clean_env()
        self.process = subprocess.Popen([executable, "-B", "-c", WRITER, str(ROOT), mode, str(root), fence,
                                         str(self.ready), str(self.go)], env=environment)
        deadline = time.monotonic() + 30
        while not self.ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.ready.exists(), "the holder child did not take its admission"

    def end(self) -> None:
        self.go.write_text("1")
        try:
            self.process.wait(30)
        except subprocess.TimeoutExpired:
            self.process.kill()  # exactly this test's own child
            self.process.wait(10)


def installed_enter(root: Path, fence: str, **extra):
    return startup_admission.enter("settings", installed=True, windows=True, data_root=root, version="0.1.0",
                                   recover=lambda v: {"ok": True},
                                   admit=lambda r: store_admission.admit_writer(r, fence=fence), **extra)


def test_f2_an_admission_on_one_root_never_authorises_another_root_under_a_restore(tmp_path):
    fence = private("fence")
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    first = installed_enter(a, fence)
    restore = Holder(tmp_path, "exclusive", b, fence)
    try:
        with pytest.raises(StartupRefused) as refused:
            installed_enter(b, fence)
    finally:
        restore.end()
    assert refused.value.code == "store"
    assert startup_admission._held["admission"] is first  # the process keeps exactly its own first admission
    with pytest.raises(Exception):
        store_admission.admit_exclusive(a)  # A's shared admission is still held


def test_f2_a_not_applicable_source_start_never_authorises_an_installed_call(tmp_path):
    fence = private("fence")
    roots: list = []
    assert source_enter({"LOCALAPPDATA": str(tmp_path / "local")}, roots) is None
    with pytest.raises(StartupRefused) as refused:
        installed_enter(tmp_path / "inst", fence)
    assert refused.value.code == "store" and startup_admission._held["admission"] is None


def test_f2_an_installed_admission_never_turns_into_a_not_applicable_answer(tmp_path):
    fence = private("fence")
    (tmp_path / "inst").mkdir()
    installed_enter(tmp_path / "inst", fence)
    with pytest.raises(StartupRefused):
        source_enter({"LOCALAPPDATA": str(tmp_path / "local")}, [])


def test_f2_equivalent_spellings_of_the_same_resolved_root_are_idempotent(tmp_path):
    fence = private("fence")
    root = tmp_path / "Inst"
    root.mkdir()
    calls: list = []

    def admit(r):
        calls.append(r)
        return store_admission.admit_writer(r, fence=fence)

    seams = {"installed": True, "windows": True, "version": "0.1.0", "recover": lambda v: {"ok": True},
             "admit": admit}
    first = startup_admission.enter("settings", data_root=root, **seams)
    assert startup_admission.enter("settings", data_root=Path(str(root).upper()), **seams) is first
    assert startup_admission.enter("settings", data_root=Path("\\\\?\\" + str(root)), **seams) is first
    with owned_junction(root, tmp_path / "inst-alias") as link:
        assert startup_admission.enter("settings", data_root=link, **seams) is first
    assert len(calls) == 1


# ===================================================================== O1: re-checked under the admission


def write_settings(path: Path, scale: int) -> None:
    """The PRODUCT settings document (core.json_store, SCHEMA_VERSION 2; a real key with a real choice)."""
    path.write_text(json.dumps({"schemaVersion": 2, "payload": {"appearance.ui_scale_percent": scale}}, indent=2),
                    encoding="utf-8")


def read_scale(path: Path) -> int:
    from core.settings_service import DEFAULT_DEFINITIONS, SettingsService

    return SettingsService(DEFAULT_DEFINITIONS, path=path).get("appearance.ui_scale_percent")


def pending_restore(root: Path, fence: str) -> None:
    """A real interrupted rollback (the Installer's own state): a safety snapshot of the REAL settings document
    (scale 110), then the current document (scale 90) and a 'restoring' claim."""
    from core.update_service import UpdateService

    (root / "Updates").mkdir(parents=True, exist_ok=True)
    write_settings(root / "settings.json", 110)
    service = UpdateService(root, "0.2.0", liveness=lambda: store_admission.update_in_progress(fence), environ={})
    snapshot = service._take_snapshot("pre_restore", "0.1.0", ["settings"])
    write_settings(root / "settings.json", 90)
    service._change(lambda state: state.update(launching={
        "claimedAt": 1_800_000_000.0, "pid": 1, "version": "0.1.0", "packageSha256": "b" * 64, "step": "restoring",
        "restoredSnapshot": snapshot["id"], "safetySnapshot": snapshot["id"]}))


HANDOFF = {"version": "0.1.0", "fromVersion": "0.1.0", "kind": "repair", "packageSha256": "a" * 64,
           "manifestSha256": "b" * 64, "launchedAt": 1_800_000_000.0, "restoredSnapshot": None,
           "safetySnapshot": None, "dataStores": {}, "nonceSha256": "c" * 64}
UNCONFIRMED = {"result": "unconfirmed", "kind": "repair", "version": "0.1.0", "runningVersion": "0.1.0",
               "at": 1_800_000_100.0, "dataMismatch": [], "dataRestored": False, "receipt": "absent"}


def set_lifecycle(root: Path, **fields) -> None:
    from core.update_service import UpdateService

    (root / "Updates").mkdir(parents=True, exist_ok=True)
    UpdateService(root, "0.1.0", liveness=lambda: False, environ={})._change(lambda state: state.update(**fields))


def claim(root: Path):
    return json.loads((root / "Updates" / "update-lifecycle.json").read_text(encoding="utf-8"))["launching"]


class Guard:
    def __init__(self, log: list) -> None:
        self.log = log

    def close(self) -> None:
        self.log.append("instance-released")


def real_barrier(root: Path, fence: str):
    def recover(version):
        from core.local_update import UpdateError
        from core.update_service import UpdateService

        try:
            UpdateService(root, version, liveness=lambda: store_admission.update_in_progress(fence),
                          environ={}).recover_before_stores()
        except UpdateError as error:
            return {"ok": False, "code": error.code}
        return {"ok": True}
    return recover


@pytest.mark.parametrize("window", ["claim", "unresolved_handoff"])
def test_o1_an_update_that_ran_between_the_barrier_and_the_admission_is_refused_and_nothing_is_kept(tmp_path, window):
    fence = private("fence")
    root = tmp_path / "data"
    (root / "Updates").mkdir(parents=True)
    write_settings(root / "settings.json", 100)
    barrier = real_barrier(root, fence)

    def recover_then_update(version):
        outcome = barrier(version)  # the real barrier: nothing pending yet
        if window == "claim":
            pending_restore(root, fence)  # an update ran and was interrupted in the window
        else:
            set_lifecycle(root, handoff=dict(HANDOFF), outcome=None)  # an installer was handed over, then died
        return outcome

    log: list = []
    with pytest.raises(StartupRefused) as refused:
        startup_admission.enter("bridge", instance=True, installed=True, windows=True, data_root=root,
                                version="0.1.0", recover=recover_then_update,
                                acquire_instance=lambda retry: Guard(log),
                                admit=lambda r: store_admission.admit_writer(r, fence=fence))
    assert refused.value.code == "recovery_pending", refused.value.code
    assert log == ["instance-released"] and not startup_admission.admitted() and startup_admission._held == {}
    store_admission.admit_exclusive(root).release()  # our shared admission was released
    if window == "claim":
        assert claim(root) is not None and read_scale(root / "settings.json") == 90  # nothing restored while shared


def test_o1_a_refusal_lets_a_real_exclusive_restore_child_run_next(tmp_path):
    fence = private("fence")
    root = tmp_path / "data"
    (root / "Updates").mkdir(parents=True)
    barrier = real_barrier(root, fence)

    def recover_then_update(version):
        outcome = barrier(version)
        pending_restore(root, fence)
        return outcome

    with pytest.raises(StartupRefused):
        startup_admission.enter("settings", installed=True, windows=True, data_root=root, version="0.1.0",
                                recover=recover_then_update,
                                admit=lambda r: store_admission.admit_writer(r, fence=fence))
    restore = Holder(tmp_path, "exclusive", root, fence)  # would fail to start if our admission were kept
    restore.end()


def test_o1_a_repair_resolved_as_unconfirmed_is_not_a_new_pending_update(tmp_path):
    fence = private("fence")
    root = tmp_path / "data"
    set_lifecycle(root, handoff=dict(HANDOFF), outcome=dict(UNCONFIRMED))
    admission = startup_admission.enter("settings", installed=True, windows=True, data_root=root, version="0.1.0",
                                        recover=lambda v: {"ok": True},
                                        admit=lambda r: store_admission.admit_writer(r, fence=fence))
    assert admission is not None and startup_admission.admitted()


def test_o1_an_unreadable_lifecycle_under_the_admission_refuses_and_releases(tmp_path):
    fence = private("fence")
    root = tmp_path / "data"
    (root / "Updates").mkdir(parents=True)

    def recover_then_corrupt(version):
        (root / "Updates" / "update-lifecycle.json").write_text("{ broken", encoding="utf-8")
        return {"ok": True}

    with pytest.raises(StartupRefused) as refused:
        startup_admission.enter("settings", installed=True, windows=True, data_root=root, version="0.1.0",
                                recover=recover_then_corrupt,
                                admit=lambda r: store_admission.admit_writer(r, fence=fence))
    assert refused.value.code in {"recovery_failed", "store"} and not startup_admission.admitted()
    store_admission.admit_exclusive(root).release()


STARTUP = r"""
import json, os, sys, time
from pathlib import Path
target, root, fence, start, go, out = sys.argv[1:7]
sys.path.insert(0, target)
from core import startup_admission, store_admission, update_service
from core.local_update import UpdateError
from core.update_service import UpdateService
from core.settings_service import DEFAULT_DEFINITIONS, SettingsService
store_admission.admit_writer.__defaults__ = (fence,)
update_service.update_in_progress.__defaults__ = (fence,)
root = Path(root)

def recover(version):
    try:
        UpdateService(root, version, liveness=lambda: store_admission.update_in_progress(fence),
                      environ={}).recover_before_stores()
    except UpdateError as error:
        return {"ok": False, "code": error.code}
    return {"ok": True}

deadline = time.monotonic() + 60
while not os.path.exists(start) and time.monotonic() < deadline:
    time.sleep(0.001)
result = {"pid": os.getpid()}
try:
    startup_admission.enter("settings", installed=True, windows=True, data_root=root, version="0.1.0",
                            recover=recover, admit=lambda r: store_admission.admit_writer(r, fence=fence))
    lifecycle = json.loads((root / "Updates" / "update-lifecycle.json").read_text(encoding="utf-8"))
    # the FIRST store, through the product's own service
    store = SettingsService(DEFAULT_DEFINITIONS, path=root / "settings.json")
    result.update(admitted=True, claimAtAdmission=lifecycle.get("launching"),
                  scaleAtFirstStore=store.get("appearance.ui_scale_percent"))
except startup_admission.StartupRefused as refused:
    result.update(admitted=False, code=refused.code)
Path(out + ".tmp").write_text(json.dumps(result), encoding="utf-8")
os.replace(out + ".tmp", out)
deadline = time.monotonic() + 60
while result.get("admitted") and not os.path.exists(go) and time.monotonic() < deadline:
    time.sleep(0.02)
"""


def test_o1_concurrent_real_startups_on_a_pending_restore_see_the_restored_product_settings_first(tmp_path):
    rounds = []
    for index in range(3):
        root, fence = tmp_path / f"data{index}", private("fence")
        pending_restore(root, fence)
        start, go = tmp_path / f"start{index}", tmp_path / f"go{index}"
        outs = [tmp_path / f"out{index}-{n}.json" for n in range(3)]
        executable, environment = clean_env()
        children = [subprocess.Popen([executable, "-B", "-c", STARTUP, str(ROOT), str(root), fence, str(start),
                                      str(go), str(out)], env=environment) for out in outs]
        try:
            time.sleep(1.5)
            start.write_text("1")
            deadline = time.monotonic() + 60
            while not all(o.exists() for o in outs) and time.monotonic() < deadline:
                time.sleep(0.05)
            results = [json.loads(o.read_text(encoding="utf-8")) for o in outs if o.exists()]
        finally:
            go.write_text("1")
            for child in children:
                try:
                    child.wait(30)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(10)
        rounds.append(results)
        assert len(results) == 3, results
        admitted = [r for r in results if r["admitted"]]
        assert admitted, results
        for r in admitted:
            assert r["claimAtAdmission"] is None and r["scaleAtFirstStore"] == 110, results
        for r in results:
            if not r["admitted"]:
                assert r["code"] in {"recovery_pending", "writers_active", "busy", "update_in_progress"}, results
        assert read_scale(root / "settings.json") == 110 and claim(root) is None


# ===================================================================== children: frozen dispatch and source entries


def run_child(tmp: Path, mode: str, name: str, args: list, *, watch: Path, fence: str, env: dict | None = None,
              stdin: bytes = b"") -> dict:
    executable, environment = clean_env()
    environment.update({"LOCALAPPDATA": str(tmp / "local"), "APPDATA": str(tmp / "roaming"),
                        "USERPROFILE": str(tmp / "user"), "HOME": str(tmp / "user"), "TEMP": str(tmp / "t"),
                        "TMP": str(tmp / "t")})
    (tmp / "t").mkdir(exist_ok=True)
    environment.update(env or {})
    out = tmp / f"child-{uuid.uuid4().hex[:8]}.json"
    done = subprocess.run([executable, "-B", str(CHILD), str(ROOT), str(watch), fence, str(tmp / "known"),
                           str(tmp / "checkout"), str(out), mode, name, *args],
                          env=environment, input=stdin, capture_output=True, timeout=300)
    assert out.exists(), done.stderr.decode("utf-8", "replace")[-3000:]
    result = json.loads(out.read_text(encoding="utf-8"))
    result["stdout"] = done.stdout.decode("utf-8", "replace")
    result["lines"] = [line for line in result["stdout"].splitlines() if line.strip()]
    return result


def frozen_env(root: Path, version: str = "0.1.0") -> dict:
    return {"QRONOS_PACKAGED_RUNTIME": "1", "QRONOS_DATA_ROOT": str(root), "QRONOS_APP_VERSION": version}


def test_restore_happens_before_the_first_store_with_the_real_settings_schema(tmp_path):
    root, fence = tmp_path / "local" / "Qronos", private("fence")
    pending_restore(root, fence)
    result = run_child(tmp_path, "frozen", "settings", ["snapshot"], watch=root, fence=fence, env=frozen_env(root))
    answer = json.loads(result["lines"][-1])
    assert result["code"] == 0 and answer["ok"] is True, result["stdout"]
    assert answer["values"]["appearance.ui_scale_percent"] == 110 and answer["problems"] == [], answer
    settings_opens = [e for e in result["events"] if e["path"] == "settings.json" and "core.settings_cli" in
                      e["serviceImported"]]
    assert settings_opens and all(e["admitted"] for e in settings_opens), result["events"]
    assert claim(root) is None


def test_f1_a_real_source_cli_reaching_installed_data_through_a_junction_is_admitted_before_its_store(tmp_path):
    fence = private("fence")
    root = tmp_path / "known" / "Qronos"
    (root / "Updates").mkdir(parents=True)
    write_settings(root / "settings.json", 110)
    with owned_junction(root, tmp_path / "alias") as link:
        # watched through the ALIAS: the settings store is opened by that spelling
        result = run_child(tmp_path, "source", "core.settings_cli", ["snapshot"], watch=link, fence=fence,
                           env={"QRONOS_SETTINGS_PATH": str(link / "settings.json")})
    answer = json.loads(result["lines"][-1])
    assert result["code"] in (0, None) and answer["values"]["appearance.ui_scale_percent"] == 110, result["stdout"]
    assert result["admittedAtEnd"] and result["binding"][0] is True, result
    store_opens = [e for e in result["events"] if e["path"] == "settings.json"]
    assert store_opens and all(e["admitted"] for e in store_opens), result["events"]
    assert store_admission.ADMISSION_FILE in os.listdir(root)
