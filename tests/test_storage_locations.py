"""
Storage-location validation: no cwd-relative roots, no silent rewrites.

Every test runs with LOCALAPPDATA, APPDATA, USERPROFILE and HOME pointing into
tmp_path and with the working directory moved to an empty sandbox folder, so a
regression that resolves a relative root against the cwd shows up as a
directory in that folder rather than in the source checkout.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from core import library_cli, path_safety, storage_locations as sl
from core.artifact_registry import ArtifactRegistry
from core.conversation_history import ConversationHistory
from core.storage_locations import (
    LOCATION_KEYS,
    StorageLocationError,
    change_storage_location,
    load_storage_locations,
    locator_path,
    read_storage_state,
)
from core.user_memory import DeterministicEmbeddingProvider, MemoryStore


ROOT = Path(__file__).resolve().parents[1]


class Profile:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.local = tmp_path / "Local"
        self.roaming = tmp_path / "Roaming"
        self.home = tmp_path / "Home"
        self.cwd = tmp_path / "cwd"
        self.custom = tmp_path / "Custom"
        for folder in (self.local, self.roaming, self.home, self.cwd, self.custom):
            folder.mkdir()

    @property
    def locator(self) -> Path:
        return self.local / "Qronos" / "storage_locations.json"

    def write_locator(self, document: object | bytes) -> bytes:
        self.locator.parent.mkdir(parents=True, exist_ok=True)
        data = document if isinstance(document, bytes) else json.dumps(document).encode("utf-8")
        self.locator.write_bytes(data)
        return data

    def tree(self, folder: Path) -> list[str]:
        return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Profile:
    p = Profile(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(p.local))
    monkeypatch.setenv("APPDATA", str(p.roaming))
    monkeypatch.setenv("USERPROFILE", str(p.home))
    monkeypatch.setenv("HOME", str(p.home))
    monkeypatch.delenv("QRONOS_CONVERSATION_HISTORY_PATH", raising=False)
    monkeypatch.chdir(p.cwd)
    assert not (ROOT / "Library").exists()
    yield p
    assert p.tree(p.cwd) == [], "nothing may be created relative to the cwd"
    assert not (ROOT / "Library").exists(), "nothing may be created in the program folder"


# ---------------------------------------------------------------- read-only


def test_reading_without_a_locator_writes_nothing(profile: Profile) -> None:
    state = read_storage_state()
    assert state.locator_status == "missing" and state.problems == ()
    assert load_storage_locations().artifact_root == str(profile.home / "Documents" / "Qronos" / "Library")
    assert profile.tree(profile.local) == [] and profile.tree(profile.home) == []


def test_explicit_bootstrap_writes_a_versioned_locator_and_profile_roots(profile: Profile) -> None:
    load_storage_locations(create=True)

    stored = json.loads(profile.locator.read_text(encoding="utf-8"))
    assert stored["schemaVersion"] == 1
    assert set(LOCATION_KEYS) <= set(stored)
    assert (profile.local / "Qronos" / "History").is_dir()
    assert (profile.home / "Documents" / "Qronos" / "Library").is_dir()


# ---------------------------------------------------------------- invalid stored values


def _invalid_values(profile: Profile) -> list[tuple[str, object, str]]:
    a_file = profile.custom / "a-file.txt"
    a_file.write_text("x", encoding="utf-8")
    return [
        ("artifactRoot", "Library", "relative"),
        ("historyRoot", "History", "relative"),
        ("memoryRoot", "C:Memory", "relative"),
        ("cacheRoot", "\\Cache", "relative"),
        ("backupRoot", "..\\Backups", "relative"),
        ("backupRoot", "\\\\server\\share\\Backups", "network_or_device"),
        ("historyRoot", "//server/share/History", "network_or_device"),
        ("memoryRoot", "\\\\?\\C:\\Memory", "network_or_device"),
        ("cacheRoot", "\\\\.\\PhysicalDrive0", "network_or_device"),
        ("artifactRoot", "", "empty"),
        ("artifactRoot", "   ", "empty"),
        ("historyRoot", 123, "not_text"),
        ("historyRoot", None, "not_text"),
        ("historyRoot", ["C:\\x"], "not_text"),
        ("cacheRoot", str(profile.custom / "bad\x07name"), "invalid_characters"),
        ("backupRoot", str(a_file), "not_directory"),
        ("historyRoot", str(ROOT / "data"), "protected"),
        ("artifactRoot", str(profile.local / "Qronos" / "Artifacts"), "protected"),
        ("artifactRoot", str(profile.roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"), "protected"),
    ]


def test_invalid_stored_values_are_reported_defaulted_and_never_created(profile: Profile) -> None:
    for key, value, code in _invalid_values(profile):
        original = profile.write_locator({key: value})
        state = read_storage_state()

        (problem,) = state.problems
        assert (problem.key, problem.code) == (key, code), (key, value)
        assert state.locations.get(key) == sl._defaults().get(key)
        assert "kept the stored setting unchanged" in problem.message
        public = json.dumps(problem.public())
        assert "\x07" not in public

        # Explicit bootstrap creates only valid (default) roots, never the bad value.
        load_storage_locations(create=True)
        assert profile.locator.read_bytes() == original, "stored evidence must not be rewritten"


def test_system_root_is_protected_when_known(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_windows = profile.tmp / "Windows"
    fake_windows.mkdir()
    monkeypatch.setenv("SystemRoot", str(fake_windows))
    profile.write_locator({"historyRoot": str(fake_windows / "Qronos")})

    (problem,) = read_storage_state().problems
    assert problem.code == "protected"
    assert not (fake_windows / "Qronos").exists()


def test_relative_artifact_root_never_reaches_the_cwd_through_the_library(profile: Profile) -> None:
    original = profile.write_locator({"artifactRoot": "Library", "libraryDbRoot": "Library"})

    result = library_cli.run("snapshot", {})

    assert result["ok"] is True
    codes = {(p["key"], p["code"]) for p in result["storage"]["problems"]}
    assert codes == {("artifactRoot", "relative"), ("libraryDbRoot", "relative")}
    assert Path(result["locations"]["artifactRoot"]).is_absolute()
    assert profile.locator.read_bytes() == original


def test_valid_custom_and_cloud_synced_roots_are_kept(profile: Profile) -> None:
    synced = profile.home / "Synced Folder - Contoso" / "Documents" / "Qronos" / "Library"
    not_yet = profile.custom / "History-not-created-yet"
    document = {
        "artifactRoot": str(synced),
        "historyRoot": str(not_yet),
        "backupRoot": str(profile.custom / "Backups"),
        "futureOption": {"kept": True},
    }
    profile.write_locator(document)

    state = read_storage_state()
    assert state.problems == () and state.locator_status == "ok"
    assert state.locations.artifact_root == str(synced)
    assert state.locations.history_root == str(not_yet)
    assert not not_yet.exists() and not synced.exists(), "reading must not create roots"


def _directory_link(link: Path, target: Path) -> None:
    """A real redirecting directory link: an NTFS junction on Windows, a
    directory symlink on POSIX (where unprivileged symlinks are allowed)."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _remove_directory_link(link: Path) -> None:
    # Removes the link itself, never the target's contents.
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


def test_existing_junction_root_is_allowed_but_judged_by_its_target(
    profile: Profile, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = profile.tmp / "OtherDrive" / "QronosHistory"
    real.mkdir(parents=True)
    link = profile.custom / "HistoryLink"
    _directory_link(link, real)
    try:
        profile.write_locator({"historyRoot": str(link)})
        assert read_storage_state().problems == ()

        protected = profile.tmp / "Windows"
        protected.mkdir()
        monkeypatch.setenv("SystemRoot", str(protected))
        sneaky = profile.custom / "LooksHarmless"
        _directory_link(sneaky, protected)
        try:
            profile.write_locator({"historyRoot": str(sneaky)})
            (problem,) = read_storage_state().problems
            assert problem.code == "protected"
        finally:
            _remove_directory_link(sneaky)
    finally:
        _remove_directory_link(link)


# ---------------------------------------------------------------- corrupt and newer locators


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"\xff\xfe\x00garbage", b"[1, 2, 3]", b'"just a string"',
     b'{"schemaVersion": "1"}', b'{"schemaVersion": true}', b'{"schemaVersion": 0}'],
)
def test_corrupt_locator_is_reported_and_preserved(profile: Profile, content: bytes) -> None:
    profile.write_locator(content)

    state = read_storage_state()
    assert state.locator_status == "corrupt"
    assert [p.code for p in state.problems] == ["locator_corrupt"]
    assert state.locations == sl._defaults()

    load_storage_locations(create=True)
    ArtifactRegistry()
    assert profile.locator.read_bytes() == content


def test_explicit_change_moves_a_corrupt_locator_aside_instead_of_overwriting(profile: Profile) -> None:
    content = profile.write_locator(b"{definitely corrupt")
    target = profile.custom / "History"

    result = change_storage_location("historyRoot", str(target))

    preserved = profile.locator.parent / str(result["preservedLocator"])
    assert preserved.read_bytes() == content
    stored = json.loads(profile.locator.read_text(encoding="utf-8"))
    assert stored["historyRoot"] == str(target) and stored["schemaVersion"] == 1
    assert read_storage_state().locator_status == "ok"


def test_newer_locator_is_never_written(profile: Profile) -> None:
    content = profile.write_locator({"schemaVersion": 2, "artifactRoot": str(profile.custom), "newField": 1})

    state = read_storage_state()
    assert state.locator_status == "newer" and state.writable is False
    load_storage_locations(create=True)
    with pytest.raises(StorageLocationError) as caught:
        change_storage_location("historyRoot", str(profile.custom / "H"))
    assert caught.value.code == "locator_newer"
    assert profile.locator.read_bytes() == content
    assert not (profile.custom / "H").exists()


# ---------------------------------------------------------------- explicit change


def test_change_keeps_other_choices_unknown_keys_and_existing_files(profile: Profile) -> None:
    old_history = profile.custom / "OldHistory"
    old_history.mkdir()
    (old_history / "conversation_history.sqlite3").write_bytes(b"user data")
    profile.write_locator({
        "historyRoot": str(old_history),
        "cacheRoot": "relative-cache",  # invalid evidence stays until the user fixes it
        "futureOption": [1, 2],
    })
    new_history = profile.custom / "NewHistory"

    result = change_storage_location("historyRoot", str(new_history))

    assert result["moved"] is False and "were not moved" in str(result["message"])
    assert result["previous"] == str(old_history) and result["current"] == str(new_history)
    assert (old_history / "conversation_history.sqlite3").read_bytes() == b"user data"
    assert new_history.is_dir() and list(new_history.iterdir()) == []
    stored = json.loads(profile.locator.read_text(encoding="utf-8"))
    assert stored["cacheRoot"] == "relative-cache"
    assert stored["futureOption"] == [1, 2]
    assert stored["historyRoot"] == str(new_history)


@pytest.mark.parametrize(
    "destination, code",
    [
        ("Relative\\Folder", "relative"),
        ("\\\\server\\share", "network_or_device"),
        ("", "empty"),
    ],
)
def test_change_refuses_invalid_destinations_without_creating_anything(
    profile: Profile, destination: str, code: str,
) -> None:
    with pytest.raises(StorageLocationError) as caught:
        change_storage_location("artifactRoot", destination)
    assert caught.value.code == code
    assert not profile.locator.exists()


def test_change_creates_only_the_final_folder(profile: Profile) -> None:
    deep = profile.custom / "missing-parent" / "History"
    with pytest.raises(StorageLocationError) as caught:
        change_storage_location("historyRoot", str(deep))
    assert caught.value.code == "parent_missing"
    assert not (profile.custom / "missing-parent").exists()


def test_library_root_may_not_overlap_qronos_state(profile: Profile) -> None:
    profile.write_locator({"historyRoot": str(profile.custom / "State" / "History")})
    with pytest.raises(StorageLocationError) as caught:
        change_storage_location("artifactRoot", str(profile.custom / "State"))
    assert caught.value.code == "overlaps_state"

    change_storage_location("artifactRoot", str(profile.custom / "Library"))
    with pytest.raises(StorageLocationError) as caught:
        change_storage_location("memoryRoot", str(profile.custom / "Library" / "Memory"))
    assert caught.value.code == "overlaps_state"


def test_stored_overlap_falls_back_for_the_library_root(profile: Profile) -> None:
    shared = profile.custom / "Shared"
    profile.write_locator({"artifactRoot": str(shared), "historyRoot": str(shared / "History")})

    state = read_storage_state()
    (problem,) = state.problems
    assert (problem.key, problem.code) == ("artifactRoot", "overlaps_state")
    assert state.locations.history_root == str(shared / "History")
    assert state.locations.artifact_root == sl._defaults().artifact_root


def test_change_is_audited_by_key_without_the_path(profile: Profile) -> None:
    target = profile.custom / "Private Folder Name"
    change_storage_location("backupRoot", str(target))
    audit = Path(os.environ["QRONOS_PERMISSION_AUDIT_PATH"]).read_text(encoding="utf-8")
    record = json.loads(audit.splitlines()[-1])
    assert record["event"] == "storage_location_changed" and record["target"] == "backupRoot"
    assert "Private Folder Name" not in audit


def test_concurrent_processes_do_not_lose_each_others_changes(profile: Profile) -> None:
    code = (
        "import sys; sys.path.insert(0, sys.argv[3]);"
        "from core.storage_locations import change_storage_location;"
        "change_storage_location(sys.argv[1], sys.argv[2])"
    )
    procs = []
    for key in ("historyRoot", "memoryRoot", "cacheRoot", "backupRoot"):
        target = profile.custom / key
        procs.append(subprocess.Popen(
            [sys.executable, "-c", code, key, str(target), str(ROOT)],
            env={**os.environ}, cwd=profile.cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ))
    try:
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err.decode("utf-8", "replace")
    finally:
        for proc in procs:  # only the children started here
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)

    stored = json.loads(profile.locator.read_text(encoding="utf-8"))
    for key in ("historyRoot", "memoryRoot", "cacheRoot", "backupRoot"):
        assert stored[key] == str(profile.custom / key)
    # The lock file is persistent by design (exclusion is an OS lock on it).
    assert [p.name for p in profile.locator.parent.glob("*.lock")] == [profile.locator.name + ".lock"]
    assert not list(profile.locator.parent.glob("*.tmp"))


def test_a_leftover_lock_file_is_not_a_lock(profile: Profile) -> None:
    # Exclusion is an OS lock on an open handle, not the file's existence. A
    # lock file left by an older version or a crashed process, of any age or
    # content, neither blocks a change nor gets deleted. Held-lock, busy and
    # abrupt-exit behaviour is covered with real processes in
    # tests/test_storage_lock.py.
    profile.locator.parent.mkdir(parents=True)
    lock = profile.locator.with_name(profile.locator.name + ".lock")
    lock.write_text("held", encoding="utf-8")
    for age in (0, 3600):
        os.utime(lock, (time.time() - age, time.time() - age))
        change_storage_location("historyRoot", str(profile.custom / f"H{age}"))
        assert lock.is_file()


# ---------------------------------------------------------------- profile


@pytest.mark.parametrize("value", ["relative\\appdata", "\\\\server\\profile"])
def test_unusable_profile_fails_instead_of_using_the_cwd(
    profile: Profile, monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", value)
    with pytest.raises(StorageLocationError) as caught:
        read_storage_state()
    assert caught.value.code == "profile_unavailable"
    with pytest.raises(StorageLocationError):
        load_storage_locations(create=True)
    with pytest.raises(StorageLocationError):
        locator_path()


# ---------------------------------------------------------------- explicit paths stay explicit


def test_explicit_paths_do_not_read_the_locator_or_create_other_roots(
    profile: Profile, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_locator(*args, **kwargs):
        raise AssertionError("explicit paths must not read the storage locator")

    monkeypatch.setattr(sl, "read_storage_state", no_locator)
    import core.artifact_registry as registry_module
    import core.user_memory as memory_module
    monkeypatch.setattr(registry_module, "read_storage_state", no_locator)
    monkeypatch.setattr(memory_module, "ensure_storage_root", no_locator)

    ArtifactRegistry(profile.custom / "db" / "library.sqlite3", profile.custom / "Artifacts")
    MemoryStore(profile.custom / "mem" / "user_memory.sqlite3", provider=DeterministicEmbeddingProvider())
    ConversationHistory(path=profile.custom / "hist" / "conversation_history.sqlite3")

    assert profile.tree(profile.local) == []
    assert profile.tree(profile.home) == []


def test_default_history_creates_only_the_history_root(profile: Profile) -> None:
    ConversationHistory()
    assert (profile.local / "Qronos" / "History").is_dir()
    assert sorted(p.name for p in (profile.local / "Qronos").iterdir()) == ["History"]
    assert profile.tree(profile.home) == []


def test_relative_explicit_library_paths_are_refused(profile: Profile) -> None:
    with pytest.raises(ValueError):
        ArtifactRegistry(Path("db") / "library.sqlite3", profile.custom / "Artifacts")
    with pytest.raises(ValueError):
        ArtifactRegistry(profile.custom / "db" / "library.sqlite3", Path("Library"))


# ---------------------------------------------------------------- CLI and UI path


def test_cli_status_and_explicit_fix_path(profile: Profile) -> None:
    profile.write_locator({"artifactRoot": "Library"})

    status = library_cli.run("storage_status", {})
    assert [p["code"] for p in status["storage"]["problems"]] == ["relative"]

    refused = library_cli.run("set_storage_location", {"key": "artifactRoot", "destination": "Still\\Relative"})
    assert refused == {"ok": False, "code": "relative", "error": sl._REASONS["relative"]}

    fixed = library_cli.run("set_storage_location", {"key": "artifactRoot", "destination": str(profile.custom / "Library")})
    assert fixed["ok"] is True and fixed["storage"]["problems"] == []
    assert library_cli.run("storage_status", {})["locations"]["artifactRoot"] == str(profile.custom / "Library")


def test_unknown_key_is_refused(profile: Profile) -> None:
    result = library_cli.run("set_storage_location", {"key": "systemRoot", "destination": str(profile.custom)})
    assert result["ok"] is False and result["code"] == "unknown_key"


def test_library_view_offers_the_fix() -> None:
    view = (ROOT / "desktop" / "src" / "components" / "LibraryView.tsx").read_text(encoding="utf-8")
    store = (ROOT / "desktop" / "src" / "libraryStore.ts").read_text(encoding="utf-8")
    assert "CHOOSE FOLDER" in view and "librarySetStorageLocation" in view
    assert '"set_storage_location"' in store and '"storage_status"' in store


# ---------------------------------------------------------------- link classification


def _fake_stat(attributes: int, tag: int, mode: int = 0o040777):
    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_reparse_tag=tag)


@pytest.mark.parametrize(
    "attributes, tag, is_link",
    [
        (0x10, 0, False),  # plain folder
        (0x410, 0xA0000003, True),  # junction / mount point
        (0x410, 0xA000000C, True),  # symlink
        (0x400, 0x8000001B, True),  # app execution alias
        (0x410, 0x9000601A, False),  # cloud-sync placeholder folder
        (0x400, 0x9000001A, False),  # cloud placeholder file
        (0x400, 0, True),  # reparse point with no readable tag: fail closed
    ],
)
def test_only_redirecting_reparse_points_count_as_links(
    monkeypatch: pytest.MonkeyPatch, attributes: int, tag: int, is_link: bool,
) -> None:
    monkeypatch.setattr(path_safety.os, "lstat", lambda p: _fake_stat(attributes, tag))
    assert path_safety.is_link_or_reparse_point("C:\\anything") is is_link


def test_a_real_junction_is_a_link(tmp_path: Path) -> None:
    # Windows: a real NTFS junction (name-surrogate reparse point).
    # POSIX: a real directory symlink, the equivalent redirecting link.
    target = tmp_path / "t"
    target.mkdir()
    link = tmp_path / "j"
    _directory_link(link, target)
    try:
        assert path_safety.is_link_or_reparse_point(link) is True
        assert path_safety.is_link_or_reparse_point(target) is False
        assert path_safety.is_link_or_reparse_point(tmp_path / "missing") is False
    finally:
        _remove_directory_link(link)
