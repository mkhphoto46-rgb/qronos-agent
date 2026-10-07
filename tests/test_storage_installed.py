"""
Installed-runtime routing, whole-installation protection and drive policy.

Packaged configuration is produced by the real ``core.config.resolve_paths``
over a fake installation tree; nothing is installed or launched. Drive types
are injected, so the Windows drive policy is exercised without real network
drives, and the platform-neutral classifier runs on any OS.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.config as config_module
from core import path_safety, storage_locations as sl
from core.approval_secret import approval_secret_path
from core.artifact_registry import ArtifactRegistry
from core.artifact_service import ArtifactProducerRegistry
from core.config import resolve_paths
from core.library_executor import LibraryActionRefused, LibraryExecutor, default_protected_roots
from core.storage_locations import (
    StorageLocationError,
    change_storage_location,
    load_storage_locations,
    locator_path,
    read_storage_state,
    validate_location,
)


windows_only = pytest.mark.skipif(os.name != "nt", reason="exercises Windows path semantics")


def _tree(folder: Path) -> dict[str, bytes | None]:
    return {
        str(p.relative_to(folder)): (p.read_bytes() if p.is_file() else None)
        for p in sorted(folder.rglob("*"))
    }


class Installed:
    def __init__(self, tmp_path: Path, *, markers: bool = True) -> None:
        self.install = tmp_path / "Programs" / "qronos-desktop"
        self.bundle = self.install / "qronos-runtime"
        self.internal = self.bundle / "_internal"
        (self.internal / "core").mkdir(parents=True)
        self.executable = self.bundle / "qronos-runtime.exe"
        self.executable.write_bytes(b"MZ fake runtime")
        if markers:
            (self.install / "qronos-desktop.exe").write_bytes(b"MZ fake desktop")
            (self.install / "uninstall.exe").write_bytes(b"MZ fake uninstaller")
        self.local = tmp_path / "Local"
        self.home = tmp_path / "Home"
        self.data = self.local / "com.qronos.agent" / "Qronos"
        self.local.mkdir(exist_ok=True)
        self.home.mkdir(exist_ok=True)
        self.before = _tree(self.install)

    def configure(self, monkeypatch: pytest.MonkeyPatch, data: Path | None = None) -> None:
        paths = resolve_paths(
            root=self.internal,
            environ={"LOCALAPPDATA": str(self.local), "QRONOS_DATA_ROOT": str(data or self.data)},
            packaged=True,
        )
        config = SimpleNamespace(paths=paths)
        monkeypatch.setattr(sl, "CONFIG", config)
        monkeypatch.setattr(config_module, "CONFIG", config)
        monkeypatch.setattr(sl, "PROGRAM_ROOT", self.internal)
        monkeypatch.setattr(sl, "sys", SimpleNamespace(executable=str(self.executable), frozen=True))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, sub in (("LOCALAPPDATA", "Local"), ("APPDATA", "Roaming"), ("USERPROFILE", "Home"), ("HOME", "Home")):
        (tmp_path / sub).mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.delenv("QRONOS_APPROVAL_SECRET_PATH", raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    yield tmp_path
    assert list(cwd.iterdir()) == [], "nothing may be written relative to the cwd"


# ---------------------------------------------------------------- packaged routing


def test_installed_state_uses_the_packaged_profile(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = Installed(env)
    app.configure(monkeypatch)

    assert locator_path() == app.data / "storage_locations.json"
    state = read_storage_state()
    assert state.locations.data_root == str(app.data)
    assert state.locations.history_root == str(app.data / "History")
    assert approval_secret_path() == app.data / "approval_secret.sqlite3"

    load_storage_locations(create=True)
    assert json.loads((app.data / "storage_locations.json").read_text(encoding="utf-8"))["schemaVersion"] == 1
    assert (app.data / "Library").is_dir()
    assert _tree(app.install) == app.before, "no write into the installation"


def test_the_whole_installation_is_protected(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = Installed(env)
    app.configure(monkeypatch)

    roots = sl.program_roots()
    assert app.install in roots and app.bundle in roots and app.internal in roots
    for inside in (app.install / "Library", app.bundle / "data", app.internal / "x"):
        for key in ("artifactRoot", "historyRoot", "backupRoot"):
            with pytest.raises(StorageLocationError) as caught:
                validate_location(key, str(inside))
            assert caught.value.code == "protected"
    assert app.install in default_protected_roots()


def test_without_installer_markers_only_the_bundle_is_protected(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = Installed(env, markers=False)
    app.configure(monkeypatch)

    roots = sl.program_roots()
    assert app.bundle in roots and app.install not in roots
    validate_location("historyRoot", str(app.install / "Sibling"))


def test_a_drive_root_is_never_taken_for_an_installation(tmp_path: Path) -> None:
    # A runtime folder directly under a drive root: only the bundle is
    # protected, never the whole drive, even if markers were present there.
    anchor = Path(tmp_path.anchor)
    roots = sl.installed_bundle_roots(anchor / "qronos-runtime" / "qronos-runtime.exe")
    assert roots == (anchor / "qronos-runtime",)


def test_installed_data_inside_the_installation_is_refused_without_writing(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Installed(env)
    # Allowed by the bundle check in config (not inside _internal), but it is
    # still inside the installed application folder.
    app.configure(monkeypatch, data=app.install / "UserData")

    for action in (locator_path, read_storage_state, lambda: load_storage_locations(create=True)):
        with pytest.raises(StorageLocationError) as caught:
            action()
        assert caught.value.code == "profile_unavailable"
    assert _tree(app.install) == app.before


# ---------------------------------------------------------------- drive classification (any OS)


@pytest.mark.parametrize(
    "kind, expected",
    [
        (path_safety.DRIVE_FIXED, "local"),
        (path_safety.DRIVE_REMOVABLE, "local"),
        (path_safety.DRIVE_RAMDISK, "local"),
        (path_safety.DRIVE_REMOTE, "remote"),
        (path_safety.DRIVE_UNKNOWN, "unknown"),
        (path_safety.DRIVE_NO_ROOT_DIR, "unknown"),
        (path_safety.DRIVE_CDROM, "unknown"),
        (99, "unknown"),
    ],
)
def test_windows_drive_types_are_classified_fail_closed(
    monkeypatch: pytest.MonkeyPatch, kind: int, expected: str,
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(path_safety, "PLATFORM", "nt")
    monkeypatch.setattr(path_safety, "windows_drive_type", lambda anchor: seen.append(anchor) or kind)

    assert path_safety.drive_status("Z:\\Users\\me\\Qronos") == expected
    assert seen == ["Z:\\"]


def test_a_failed_drive_query_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(anchor: str) -> int:
        raise OSError("query failed")

    monkeypatch.setattr(path_safety, "PLATFORM", "nt")
    monkeypatch.setattr(path_safety, "windows_drive_type", broken)
    assert path_safety.drive_status("C:\\Data") == "unknown"


@pytest.mark.parametrize("value", ["\\\\server\\share\\x", "relative\\x", "C:relative", ""])
def test_paths_without_a_drive_root_are_unknown(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr(path_safety, "PLATFORM", "nt")
    monkeypatch.setattr(path_safety, "windows_drive_type", lambda anchor: pytest.fail("must not query"))
    assert path_safety.drive_status(value) == "unknown"


def test_other_platforms_report_not_inspected_without_guessing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(path_safety, "PLATFORM", "posix")
    monkeypatch.setattr(path_safety, "windows_drive_type", lambda anchor: pytest.fail("must not query"))
    assert path_safety.drive_status("/home/me/Qronos") == "not_applicable"


# ---------------------------------------------------------------- drive policy (Windows paths)


def _drives(monkeypatch: pytest.MonkeyPatch, remote: set[str], unknown: set[str] = frozenset()) -> None:
    def kind(anchor: str) -> int:
        letter = anchor[0].upper()
        if letter in remote:
            return path_safety.DRIVE_REMOTE
        if letter in unknown:
            return path_safety.DRIVE_NO_ROOT_DIR
        return path_safety.DRIVE_FIXED

    monkeypatch.setattr(path_safety, "windows_drive_type", kind)


@windows_only
def test_mapped_network_drives_are_refused_for_every_root(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _drives(monkeypatch, remote={"Z"}, unknown={"Q"})
    for key in sl.LOCATION_KEYS:
        with pytest.raises(StorageLocationError) as caught:
            validate_location(key, "Z:\\Qronos\\" + key)
        assert caught.value.code == "network_drive"
        with pytest.raises(StorageLocationError) as caught:
            validate_location(key, "Q:\\Qronos\\" + key)
        assert caught.value.code == "drive_unknown"


@windows_only
def test_stored_network_location_is_reported_and_not_used(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _drives(monkeypatch, remote={"Z"})
    locator = env / "Local" / "Qronos" / "storage_locations.json"
    locator.parent.mkdir(parents=True)
    original = json.dumps({"historyRoot": "Z:\\Shared\\History", "artifactRoot": "Z:\\Shared\\Library"}).encode()
    locator.write_bytes(original)

    state = read_storage_state()
    assert {(p.key, p.code) for p in state.problems} >= {
        ("historyRoot", "network_drive"), ("artifactRoot", "network_drive"),
    }
    assert not state.locations.history_root.upper().startswith("Z:")
    with pytest.raises(StorageLocationError):
        change_storage_location("backupRoot", "Z:\\Shared\\Backups")
    assert locator.read_bytes() == original


@windows_only
def test_profile_on_a_network_drive_is_unavailable(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drive = str(env)[0].upper()
    _drives(monkeypatch, remote={drive})
    with pytest.raises(StorageLocationError) as caught:
        read_storage_state()
    assert caught.value.code == "profile_unavailable"


@windows_only
def test_library_will_not_copy_to_a_network_drive(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ArtifactRegistry(env / "db" / "library.sqlite3", env / "Artifacts")
    path = env / "Artifacts" / "Documents" / "report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    item = ArtifactProducerRegistry(registry).register_file(path, origin="Generated by Qronos")
    destination = env / "Share"
    destination.mkdir()
    executor = LibraryExecutor(registry, protected_roots=(env / "protected",))

    for code_drive, message in (({"remote": {str(env)[0].upper()}}, "network drive"),
                                ({"remote": set(), "unknown": {str(env)[0].upper()}}, "could not confirm")):
        _drives(monkeypatch, **code_drive)
        with pytest.raises(LibraryActionRefused, match=message):
            executor.prepare("copy", [item.id], destination=str(destination))
    assert list(destination.iterdir()) == []
