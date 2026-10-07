"""
Default storage roots pass the same policy as stored ones.

The development checkout commonly lives at Documents\\Qronos, which is also
the established parent of the default Library files and Backups folders. These
tests make the program folder exactly that fake Documents\\Qronos and prove
that no locator state - missing, corrupt or invalid - ever creates Library,
Backups or files inside it, and that existing data there is left alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import library_cli, storage_locations as sl
from core.artifact_registry import ArtifactRegistry
from core.storage_locations import (
    StorageLocationError,
    change_storage_location,
    ensure_storage_root,
    load_storage_locations,
    read_storage_state,
)


class Checkout:
    def __init__(self, tmp_path: Path) -> None:
        self.local = tmp_path / "Local"
        self.home = tmp_path / "Home"
        self.documents = self.home / "Documents"
        self.program = self.documents / "Qronos"
        self.safe = self.documents / "Qronos Files"
        self.cwd = tmp_path / "cwd"
        for folder in (self.local, self.program / "core", self.cwd):
            folder.mkdir(parents=True)
        (self.program / "core" / "config.py").write_text("# fake checkout", encoding="utf-8")
        # Pre-existing user data in the checkout, as left by older versions.
        (self.program / "Library").mkdir()
        (self.program / "Library" / "older-artifact.md").write_text("keep", encoding="utf-8")
        self.before = self.snapshot()

    @property
    def locator(self) -> Path:
        return self.local / "Qronos" / "storage_locations.json"

    def write_locator(self, document: object | bytes) -> bytes:
        self.locator.parent.mkdir(parents=True, exist_ok=True)
        data = document if isinstance(document, bytes) else json.dumps(document).encode("utf-8")
        self.locator.write_bytes(data)
        return data

    def snapshot(self) -> dict[str, bytes | None]:
        return {
            str(p.relative_to(self.program)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(self.program.rglob("*"))
        }


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Checkout:
    c = Checkout(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(c.local))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("USERPROFILE", str(c.home))
    monkeypatch.setenv("HOME", str(c.home))
    monkeypatch.setattr(sl, "PROGRAM_ROOT", c.program)
    monkeypatch.chdir(c.cwd)
    yield c
    assert c.snapshot() == c.before, "the program folder must be byte-for-byte unchanged"
    assert list(c.cwd.iterdir()) == []


def _bootstrap_everything() -> dict[str, object]:
    """Every production path that may create storage folders."""
    load_storage_locations(create=True)
    snapshot = library_cli.run("snapshot", {})
    for key in ("historyRoot", "memoryRoot", "cacheRoot"):
        ensure_storage_root(key)
    return snapshot


def test_missing_locator_moves_document_defaults_out_of_the_checkout(checkout: Checkout) -> None:
    state = read_storage_state()

    assert state.locations.artifact_root == str(checkout.safe / "Library")
    assert state.locations.backup_root == str(checkout.safe / "Backups")
    notes = {(p.key, p.code, p.severity) for p in state.problems}
    assert notes == {
        ("artifactRoot", "default_relocated", "info"),
        ("backupRoot", "default_relocated", "info"),
    }
    message = next(p.message for p in state.problems if p.key == "artifactRoot")
    assert "Qronos Files" in message and "Nothing was moved" in message

    snapshot = _bootstrap_everything()

    assert snapshot["ok"] is True
    assert snapshot["locations"]["artifactRoot"] == str(checkout.safe / "Library")
    assert (checkout.safe / "Library").is_dir()
    assert (checkout.safe / "Backups").is_dir()
    stored = json.loads(checkout.locator.read_text(encoding="utf-8"))
    assert stored["artifactRoot"] == str(checkout.safe / "Library")


@pytest.mark.parametrize("content", [b"{corrupt", b'{"schemaVersion": 99}'])
def test_corrupt_or_newer_locator_never_reaches_the_checkout(checkout: Checkout, content: bytes) -> None:
    checkout.write_locator(content)

    state = read_storage_state()
    assert state.locations.artifact_root == str(checkout.safe / "Library")

    _bootstrap_everything()
    assert checkout.locator.read_bytes() == content


@pytest.mark.parametrize(
    "stored, code",
    [
        ("Library", "relative"),
        ("{program}\\Library", "protected"),
        ("{program}\\Library\\Nested", "protected"),
        ("{documents}", "contains_program"),
    ],
)
def test_invalid_stored_library_root_falls_back_outside_the_checkout(
    checkout: Checkout, stored: str, code: str,
) -> None:
    value = stored.format(program=checkout.program, documents=checkout.documents)
    original = checkout.write_locator({"artifactRoot": value, "backupRoot": "Backups"})

    state = read_storage_state()
    problems = {(p.key, p.code) for p in state.problems}
    assert ("artifactRoot", code) in problems and ("backupRoot", "relative") in problems
    assert state.locations.artifact_root == str(checkout.safe / "Library")

    _bootstrap_everything()
    assert checkout.locator.read_bytes() == original


def test_valid_existing_choices_are_kept_unchanged(checkout: Checkout) -> None:
    chosen = checkout.documents / "My Qronos Library"
    checkout.write_locator({"artifactRoot": str(chosen)})

    state = read_storage_state()
    assert state.locations.artifact_root == str(chosen)
    assert {p.key for p in state.problems} == {"backupRoot"}, "only the defaulted root gets a note"


def test_no_safe_default_leaves_the_root_unavailable_instead_of_unsafe(checkout: Checkout) -> None:
    checkout.safe.write_text("a file where the fallback folder would go", encoding="utf-8")

    state = read_storage_state()
    assert state.locations.artifact_root == ""
    (problem,) = [p for p in state.problems if p.key == "artifactRoot"]
    assert problem.code == "no_safe_default" and problem.severity == "warning"

    with pytest.raises(StorageLocationError) as caught:
        ArtifactRegistry()
    assert caught.value.code == "unavailable"
    load_storage_locations(create=True)
    stored = json.loads(checkout.locator.read_text(encoding="utf-8"))
    assert "artifactRoot" not in stored, "an unavailable root is never persisted"

    snapshot = library_cli.run("storage_status", {})
    assert snapshot["locations"]["artifactRoot"] == ""

    fixed = library_cli.run("set_storage_location", {"key": "artifactRoot", "destination": str(checkout.documents / "Library Files")})
    assert fixed["ok"] is True
    assert library_cli.run("snapshot", {})["ok"] is True


@pytest.mark.parametrize("inside", ["Library", "core", "."])
def test_explicit_registry_roots_inside_the_program_are_refused(checkout: Checkout, inside: str) -> None:
    target = checkout.program / inside / "Artifacts"
    with pytest.raises(StorageLocationError):
        ArtifactRegistry(checkout.local / "db" / "library.sqlite3", target)
    with pytest.raises(StorageLocationError):
        ArtifactRegistry(checkout.program / inside / "library.sqlite3", checkout.safe / "Library")
    assert not (checkout.local / "db").exists()


def test_explicit_change_into_the_program_is_refused(checkout: Checkout) -> None:
    for key in ("artifactRoot", "backupRoot", "historyRoot"):
        with pytest.raises(StorageLocationError) as caught:
            change_storage_location(key, str(checkout.program / "Library"))
        assert caught.value.code == "protected"
    assert not checkout.locator.exists()
