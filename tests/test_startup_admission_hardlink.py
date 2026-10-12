"""
Round 16 (Media20 H20-1): a store override that is a file with more than one name (an NTFS hard link) is refused
before any store is opened or changed, because resolving its path cannot show whether its other names lie in
installed data.

Native boundary of these fixtures (audited): hard links are made with os.link (CreateHardLinkW) between tmp files
only; real LockFileEx admissions only on tmp roots with a PRIVATE fence "Local\\QronosR9Test-<uuid>-fence"; the
known-folder Local AppData is a private tmp folder (tests/conftest.py in-process,
tests/fixtures/startup_entry_child.py in children); children use the base interpreter with -B, bounded waits, and
are killed only by their own Popen handle. No production mutex, store, profile, device, model or network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import startup_admission
from core.startup_admission import STARTUP_REFUSED_EXIT, StartupRefused
import tests.test_startup_admission_resolution as r14

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows hard links, admission and mutexes")
SEED = b'{"seed": true}\n'


@pytest.fixture(autouse=True)
def fresh_process_admission():
    startup_admission._reset_for_tests()
    yield
    startup_admission._reset_for_tests()


@pytest.fixture
def known_root(tmp_path, monkeypatch):
    """The private known folder <tmp>/known; its Qronos folder is the installed data root."""
    known = tmp_path / "known"
    root = known / "Qronos"
    root.mkdir(parents=True)
    monkeypatch.setattr(startup_admission, "_known_local_appdata", lambda: known, raising=False)
    return root


def links(path: Path) -> int:
    return os.stat(path).st_nlink


# ===================================================================== in-process: the refusal and its bounds


@pytest.mark.parametrize("other_name", ["installed_root", "configured_root", "private_folder"])
def test_h20_a_hard_linked_store_override_is_refused_whatever_its_other_name(tmp_path, known_root, other_name):
    configured = tmp_path / "configured"
    configured.mkdir()
    original = {"installed_root": known_root, "configured_root": configured,
                "private_folder": tmp_path / "private-a"}[other_name] / "action-audit.jsonl"
    original.parent.mkdir(exist_ok=True)
    original.write_bytes(SEED)
    link = tmp_path / "dev" / "audit-link.jsonl"
    link.parent.mkdir()
    os.link(original, link)
    assert links(link) == 2
    roots: list = []
    with pytest.raises(StartupRefused) as refused:
        r14.source_enter({"LOCALAPPDATA": str(tmp_path / "local"), "QRONOS_DATA_ROOT": str(configured),
                          "QRONOS_PERMISSION_AUDIT_PATH": str(link)}, roots)
    assert refused.value.code == "store" and "more than one name" in str(refused.value), str(refused.value)
    assert roots == [] and not startup_admission.admitted() and startup_admission._held == {}
    assert original.read_bytes() == SEED and links(link) == 2


def test_h20_the_in_root_name_of_a_multi_name_file_is_refused_too(tmp_path, known_root):
    """Which root the other name is in cannot be decided without listing names, so even the in-root name refuses."""
    audit = known_root / "action-audit.jsonl"
    audit.write_bytes(SEED)
    os.link(audit, tmp_path / "elsewhere.jsonl")
    roots: list = []
    with pytest.raises(StartupRefused) as refused:
        r14.source_enter({"LOCALAPPDATA": str(tmp_path / "local"), "QRONOS_PERMISSION_AUDIT_PATH": str(audit)}, roots)
    assert refused.value.code == "store" and roots == [] and startup_admission._held == {}


@pytest.mark.parametrize("case", ["private_existing", "private_missing_leaf", "private_missing_folders",
                                  "installed_existing", "installed_missing_leaf", "junction_into_root"])
def test_h20_single_name_and_missing_overrides_keep_their_behaviour(tmp_path, known_root, case):
    dev = tmp_path / "dev"
    dev.mkdir()
    existing_private = dev / "audit.jsonl"
    existing_private.write_bytes(SEED)
    existing_installed = known_root / "action-audit.jsonl"
    existing_installed.write_bytes(SEED)
    roots: list = []
    environ = {"LOCALAPPDATA": str(tmp_path / "local")}
    if case == "junction_into_root":
        with r14.owned_junction(known_root, tmp_path / "alias") as alias:
            r14.source_enter({**environ, "QRONOS_PERMISSION_AUDIT_PATH": str(alias / "action-audit.jsonl")}, roots)
        assert [r14.key(r) for r in roots] == [r14.key(known_root)]
        return
    value = {"private_existing": existing_private, "private_missing_leaf": dev / "new.jsonl",
             "private_missing_folders": dev / "a" / "b" / "new.jsonl", "installed_existing": existing_installed,
             "installed_missing_leaf": known_root / "new.jsonl"}[case]
    r14.source_enter({**environ, "QRONOS_PERMISSION_AUDIT_PATH": str(value)}, roots)
    if case.startswith("installed"):
        assert [r14.key(r) for r in roots] == [r14.key(known_root)]
        assert startup_admission._held["binding"][0] is True
    else:
        assert roots == [] and startup_admission._held["binding"] == (False, None)
    assert existing_private.read_bytes() == SEED and existing_installed.read_bytes() == SEED


def test_h20_link_metadata_that_cannot_be_read_is_refused(tmp_path, known_root, monkeypatch):
    target = tmp_path / "dev" / "audit.jsonl"
    target.parent.mkdir()
    target.write_bytes(SEED)
    real_stat = os.stat
    wanted = os.path.normcase(os.path.realpath(target))

    def stat(path, *args, **kwargs):  # only this one file's metadata is unavailable
        if os.path.normcase(os.fspath(path)) == wanted:
            raise PermissionError(5, "fixture: metadata unavailable")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    roots: list = []
    with pytest.raises(StartupRefused) as refused:
        r14.source_enter({"LOCALAPPDATA": str(tmp_path / "local"), "QRONOS_PERMISSION_AUDIT_PATH": str(target)}, roots)
    assert refused.value.code == "store" and roots == [] and startup_admission._held == {}


def test_h20_the_check_is_one_metadata_read_per_override_and_nothing_is_opened(tmp_path, known_root, monkeypatch):
    """Bounded: no listing of folders or names, no file content opened, one stat per redirected store."""
    dev = tmp_path / "dev"
    dev.mkdir()
    for name in ("s.json", "p.json", "a.jsonl"):
        (dev / name).write_bytes(SEED)
    calls: list = []
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", lambda path, *a, **k: calls.append(os.fspath(path)) or real_stat(path, *a, **k))
    monkeypatch.setattr(os, "scandir", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a folder was listed")))
    monkeypatch.setattr(os, "listdir", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a folder was listed")))
    opened: list = []
    real_open = open
    monkeypatch.setattr("builtins.open", lambda f, *a, **k: opened.append(f) or real_open(f, *a, **k))
    roots: list = []
    r14.source_enter({"LOCALAPPDATA": str(tmp_path / "local"), "QRONOS_SETTINGS_PATH": str(dev / "s.json"),
                      "QRONOS_PERMISSION_POLICY_PATH": str(dev / "p.json"),
                      "QRONOS_PERMISSION_AUDIT_PATH": str(dev / "a.jsonl")}, roots)
    under_dev = [c for c in calls if os.path.normcase(c).startswith(os.path.normcase(os.path.realpath(dev)))]
    assert len(under_dev) == 3 and opened == [] and roots == [], (calls, opened)


def test_h20_a_refusal_never_changes_an_earlier_admission(tmp_path, known_root):
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "audit.jsonl").write_bytes(SEED)
    environ = {"LOCALAPPDATA": str(tmp_path / "local"), "QRONOS_PERMISSION_AUDIT_PATH": str(dev / "audit.jsonl")}
    roots: list = []
    assert r14.source_enter(environ, roots) is None and roots == []
    held = dict(startup_admission._held)
    os.link(dev / "audit.jsonl", tmp_path / "second-name.jsonl")  # the file gains a name after the first start
    with pytest.raises(StartupRefused) as refused:
        r14.source_enter(environ, roots)
    assert refused.value.code == "store" and roots == [] and startup_admission._held == held


def test_h20_a_hard_link_beside_an_override_into_another_root_is_refused(tmp_path, known_root):
    configured = tmp_path / "configured"
    configured.mkdir()
    (tmp_path / "private-a.jsonl").write_bytes(SEED)
    os.link(tmp_path / "private-a.jsonl", tmp_path / "private-b.jsonl")
    roots: list = []
    with pytest.raises(StartupRefused) as refused:
        r14.source_enter({"LOCALAPPDATA": str(tmp_path / "local"), "QRONOS_DATA_ROOT": str(configured),
                          "QRONOS_SETTINGS_PATH": str(configured / "s.json"),
                          "QRONOS_PERMISSION_AUDIT_PATH": str(tmp_path / "private-b.jsonl")}, roots)
    assert refused.value.code == "store" and roots == [] and startup_admission._held == {}


def test_h20_the_installed_runtime_is_unaffected(tmp_path, known_root):
    """The check belongs to source processes' override targeting; an installed start keeps its own root."""
    root = tmp_path / "installed-root"
    root.mkdir()
    (tmp_path / "a.jsonl").write_bytes(SEED)
    os.link(tmp_path / "a.jsonl", tmp_path / "b.jsonl")
    roots: list = []
    startup_admission.enter("settings", installed=True, windows=True, data_root=root, version="0.1.0",
                            environ={"QRONOS_PERMISSION_AUDIT_PATH": str(tmp_path / "b.jsonl")},
                            recover=lambda v: {"ok": True}, confirm=lambda v: {"ok": True},
                            admit=lambda r: roots.append(Path(r)) or "admission")
    assert roots == [root]


# ===================================================================== real source CLI children


def append_case(tmp_path: Path, spelling: str):
    root = tmp_path / "known" / "Qronos"  # the child's known folder is <tmp>/known
    (root / "Updates").mkdir(parents=True)
    audit = root / "action-audit.jsonl"
    audit.write_bytes(SEED)
    dev = tmp_path / "dev"
    dev.mkdir()
    if spelling == "hard_link":
        override = dev / "audit-link.jsonl"
        os.link(audit, override)
    elif spelling == "plain_path":
        override = audit
    else:
        override = dev / "audit.jsonl"
        override.write_bytes(SEED)
    return root, audit, dev, override


def set_policy(tmp_path: Path, root: Path, dev: Path, override: Path, fence: str) -> dict:
    return r14.run_child(tmp_path, "source", "core.permissions_cli", ["set_policy"], watch=root, fence=fence,
                         env={"QRONOS_PERMISSION_AUDIT_PATH": str(override),
                              "QRONOS_PERMISSION_POLICY_PATH": str(dev / "policy.json"),
                              "QRONOS_APP_VERSION": "0.1.0"},
                         stdin=json.dumps({"permissionId": "files", "policy": "ALWAYS_ASK"}).encode())


@pytest.mark.parametrize("spelling", ["hard_link", "plain_path"])
def test_h20_a_real_source_permissions_cli_never_writes_installed_audit_during_a_restore(tmp_path, spelling):
    fence = r14.private("fence")
    root, audit, dev, override = append_case(tmp_path, spelling)
    restore = r14.Holder(tmp_path, "exclusive", root, fence)
    try:
        result = set_policy(tmp_path, root, dev, override, fence)
        during = audit.read_bytes()
    finally:
        restore.end()
    assert during == SEED and audit.read_bytes() == SEED, (spelling, result)
    assert result["code"] == STARTUP_REFUSED_EXIT and len(result["lines"]) == 1, result
    answer = json.loads(result["lines"][0])
    expected = "store" if spelling == "hard_link" else "update_in_progress"
    assert answer == {"ok": False, "code": expected, "error": answer["error"], "errorType": "StartupRefused"}, answer
    assert not result["admittedAtEnd"], result
    if spelling == "hard_link":  # refused while choosing the root: nothing under it was opened, not even the barrier
        assert result["events"] == [], result
    else:  # the existing order: only the barrier's Updates/* and the admission lock, never the audit store
        assert all(e["path"].startswith("updates") or e["path"].endswith(".lock") for e in result["events"]), result
    assert sorted(p.name for p in dev.iterdir()) == ([override.name] if spelling == "hard_link" else []), \
        "the private policy store must not be created either"


def test_h20_a_hard_linked_source_cli_is_refused_even_with_no_restore_running(tmp_path):
    fence = r14.private("fence")
    root, audit, dev, override = append_case(tmp_path, "hard_link")
    result = set_policy(tmp_path, root, dev, override, fence)
    assert result["code"] == STARTUP_REFUSED_EXIT and len(result["lines"]) == 1, result
    assert json.loads(result["lines"][0])["code"] == "store" and audit.read_bytes() == SEED
    assert result["events"] == [] and sorted(p.name for p in dev.iterdir()) == [override.name]


def test_h20_an_ordinary_single_name_source_store_still_works(tmp_path):
    fence = r14.private("fence")
    root, audit, dev, override = append_case(tmp_path, "private_single_name")
    result = set_policy(tmp_path, root, dev, override, fence)
    answer = json.loads(result["lines"][-1])
    assert result["code"] in (0, None) and answer["ok"] is True, result
    assert result["binding"] == [False, None] and audit.read_bytes() == SEED
    appended = override.read_bytes()
    assert appended.startswith(SEED) and b"permission_changed" in appended[len(SEED):]


def test_h20_the_source_update_cli_refuses_a_hard_linked_override_with_one_answer(tmp_path):
    """N9 kept: the update CLI runs the same admission itself and answers once."""
    fence = r14.private("fence")
    root, audit, dev, override = append_case(tmp_path, "hard_link")
    result = r14.run_child(tmp_path, "source", "core.update_cli", ["status"], watch=root, fence=fence,
                           env={"QRONOS_PERMISSION_AUDIT_PATH": str(override), "QRONOS_APP_VERSION": "0.1.0"})
    assert result["code"] == STARTUP_REFUSED_EXIT and len(result["lines"]) == 1, result
    assert json.loads(result["lines"][0])["code"] == "store" and result["events"] == []
    assert audit.read_bytes() == SEED
