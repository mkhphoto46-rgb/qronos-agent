"""
Round 14 (Runtime17 F1/F2): the start-up order on the RESOLVED data root, bound per process, and run before the
first store by both the frozen dispatch and a source CLI.

Native boundary of these fixtures (audited):
- real LockFileEx admission (core.store_admission) ONLY on tmp roots;
- the known-folder Local AppData is a private tmp folder (tests/conftest.py in-process;
  tests/fixtures/startup_entry_child.py in children); the real one is never queried or resolved;
- junctions only through tests/fixtures/owned_junction.py (link removed, target kept); 8.3 names are read with
  GetShortPathNameW of tmp folders only;
- children: base interpreter (tests/fixtures/child_python.py) with -B, bounded waits, killed only by their own
  Popen handle; tmp data, profile and checkout.
No production mutex, store, profile, device, model, server or network.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
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
        "settings", installed=False, windows=True, environ=environ,
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
mode, root, ready, go = sys.argv[2:6]
admission = (store_admission.admit_exclusive(Path(root)) if mode == "exclusive"
             else store_admission.admit_writer(Path(root)))
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
    """A child holding a SHARED (a live writer) or EXCLUSIVE admission on ROOT until ended."""

    def __init__(self, tmp: Path, mode: str, root: Path) -> None:
        tag = uuid.uuid4().hex[:8]
        self.ready, self.go = tmp / f"hold-ready-{tag}", tmp / f"hold-go-{tag}"
        executable, environment = clean_env()
        self.process = subprocess.Popen([executable, "-B", "-c", WRITER, str(ROOT), mode, str(root),
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


def installed_enter(root: Path, **extra):
    return startup_admission.enter("settings", installed=True, windows=True, data_root=root,
                                   admit=store_admission.admit_writer, **extra)


def test_f2_an_admission_on_one_root_never_authorises_another_root_under_an_exclusive_holder(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    first = installed_enter(a)
    exclusive = Holder(tmp_path, "exclusive", b)
    try:
        with pytest.raises(StartupRefused) as refused:
            installed_enter(b)
    finally:
        exclusive.end()
    assert refused.value.code == "store"
    assert startup_admission._held["admission"] is first  # the process keeps exactly its own first admission
    with pytest.raises(Exception):
        store_admission.admit_exclusive(a)  # A's shared admission is still held


def test_f2_a_not_applicable_source_start_never_authorises_an_installed_call(tmp_path):
    roots: list = []
    assert source_enter({"LOCALAPPDATA": str(tmp_path / "local")}, roots) is None
    with pytest.raises(StartupRefused) as refused:
        installed_enter(tmp_path / "inst")
    assert refused.value.code == "store" and startup_admission._held["admission"] is None


def test_f2_an_installed_admission_never_turns_into_a_not_applicable_answer(tmp_path):
    (tmp_path / "inst").mkdir()
    installed_enter(tmp_path / "inst")
    with pytest.raises(StartupRefused):
        source_enter({"LOCALAPPDATA": str(tmp_path / "local")}, [])


def test_f2_equivalent_spellings_of_the_same_resolved_root_are_idempotent(tmp_path):
    root = tmp_path / "Inst"
    root.mkdir()
    calls: list = []

    def admit(r):
        calls.append(r)
        return store_admission.admit_writer(r)

    seams = {"installed": True, "windows": True, "admit": admit}
    first = startup_admission.enter("settings", data_root=root, **seams)
    assert startup_admission.enter("settings", data_root=Path(str(root).upper()), **seams) is first
    assert startup_admission.enter("settings", data_root=Path("\\\\?\\" + str(root)), **seams) is first
    with owned_junction(root, tmp_path / "inst-alias") as link:
        assert startup_admission.enter("settings", data_root=link, **seams) is first
    assert len(calls) == 1


def write_settings(path: Path, scale: int) -> None:
    """The PRODUCT settings document (core.json_store, SCHEMA_VERSION 2; a real key with a real choice)."""
    path.write_text(json.dumps({"schemaVersion": 2, "payload": {"appearance.ui_scale_percent": scale}}, indent=2),
                    encoding="utf-8")


# ===================================================================== children: frozen dispatch and source entries


def run_child(tmp: Path, mode: str, name: str, args: list, *, watch: Path, env: dict | None = None,
              stdin: bytes = b"") -> dict:
    executable, environment = clean_env()
    environment.update({"LOCALAPPDATA": str(tmp / "local"), "APPDATA": str(tmp / "roaming"),
                        "USERPROFILE": str(tmp / "user"), "HOME": str(tmp / "user"), "TEMP": str(tmp / "t"),
                        "TMP": str(tmp / "t")})
    (tmp / "t").mkdir(exist_ok=True)
    environment.update(env or {})
    out = tmp / f"child-{uuid.uuid4().hex[:8]}.json"
    done = subprocess.run([executable, "-B", str(CHILD), str(ROOT), str(watch), str(tmp / "known"),
                           str(tmp / "checkout"), str(out), mode, name, *args],
                          env=environment, input=stdin, capture_output=True, timeout=300)
    assert out.exists(), done.stderr.decode("utf-8", "replace")[-3000:]
    result = json.loads(out.read_text(encoding="utf-8"))
    result["stdout"] = done.stdout.decode("utf-8", "replace")
    result["lines"] = [line for line in result["stdout"].splitlines() if line.strip()]
    return result


def frozen_env(root: Path) -> dict:
    return {"QRONOS_PACKAGED_RUNTIME": "1", "QRONOS_DATA_ROOT": str(root), "QRONOS_APP_VERSION": "0.1.0"}


def test_the_frozen_settings_service_is_admitted_before_its_first_store(tmp_path):
    root = tmp_path / "local" / "Qronos"
    root.mkdir(parents=True)
    write_settings(root / "settings.json", 110)
    result = run_child(tmp_path, "frozen", "settings", ["snapshot"], watch=root, env=frozen_env(root))
    answer = json.loads(result["lines"][-1])
    assert result["code"] == 0 and answer["ok"] is True, result["stdout"]
    assert answer["values"]["appearance.ui_scale_percent"] == 110 and answer["problems"] == [], answer
    settings_opens = [e for e in result["events"] if e["path"] == "settings.json" and "core.settings_cli" in
                      e["serviceImported"]]
    assert settings_opens and all(e["admitted"] for e in settings_opens), result["events"]
    assert result["admittedAtEnd"]  # the frozen dispatch, then the CLI's own main on the same root: idempotent


def test_f1_a_real_source_cli_reaching_installed_data_through_a_junction_is_admitted_before_its_store(tmp_path):
    root = tmp_path / "known" / "Qronos"
    root.mkdir(parents=True)
    write_settings(root / "settings.json", 110)
    with owned_junction(root, tmp_path / "alias") as link:
        # watched through the ALIAS: the settings store is opened by that spelling
        result = run_child(tmp_path, "source", "core.settings_cli", ["snapshot"], watch=link,
                           env={"QRONOS_SETTINGS_PATH": str(link / "settings.json")})
    answer = json.loads(result["lines"][-1])
    assert result["code"] in (0, None) and answer["values"]["appearance.ui_scale_percent"] == 110, result["stdout"]
    assert result["admittedAtEnd"] and result["binding"][0] is True, result
    store_opens = [e for e in result["events"] if e["path"] == "settings.json"]
    assert store_opens and all(e["admitted"] for e in store_opens), result["events"]
    assert store_admission.ADMISSION_FILE in os.listdir(root)


def test_a_refused_source_cli_prints_one_typed_answer_and_opens_no_store(tmp_path):
    root = tmp_path / "known" / "Qronos"
    root.mkdir(parents=True)
    write_settings(root / "settings.json", 110)
    holder = Holder(tmp_path, "exclusive", root)
    try:
        result = run_child(tmp_path, "source", "core.settings_cli", ["snapshot"], watch=root,
                           env={"QRONOS_SETTINGS_PATH": str(root / "settings.json")})
    finally:
        holder.end()
    assert result["code"] == STARTUP_REFUSED_EXIT and len(result["lines"]) == 1, result
    answer = json.loads(result["lines"][0])
    assert answer["ok"] is False and answer["errorType"] == "StartupRefused" and answer["code"] == "busy", answer
    assert not result["admittedAtEnd"]
    assert [e for e in result["events"] if e["path"] != store_admission.ADMISSION_FILE] == []
