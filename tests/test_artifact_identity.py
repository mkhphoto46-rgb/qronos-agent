"""
Artifact identity must survive file-number reuse.

Linux file systems commonly give a file created after a delete the same inode
number the deleted file had, so (st_dev, st_ino) cannot prove that the file at
a registered path is still the registered file. NTFS rarely does this, which
is how the original bug stayed green on Windows. These tests reproduce the
reuse deterministically on every platform through the registry's stat seam.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import artifact_registry as registry_module
from core.artifact_registry import ArtifactRegistry, file_identity, identity_matches
from core.artifact_service import ArtifactProducerRegistry
from core.library_executor import LibraryActionRefused, LibraryExecutor


def _spoof(real: os.stat_result, **overrides: int) -> SimpleNamespace:
    fields = {
        name: getattr(real, name)
        for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
    }
    if getattr(real, "st_birthtime_ns", None) is not None:
        fields["st_birthtime_ns"] = real.st_birthtime_ns
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _marker_field(recorded: dict[str, object]) -> str:
    return "st_birthtime_ns" if recorded["marker_kind"] == "birth" else "st_ctime_ns"


def _replace(path: Path, content: str) -> None:
    path.unlink()
    path.write_text(content, encoding="utf-8")


def _reuse(monkeypatch: pytest.MonkeyPatch, path: Path, recorded: dict[str, object], **extra: int) -> None:
    """Make ``path`` report the recorded volume and file number, as Linux may."""
    real_stat = os.stat

    def stat(target: Path) -> object:
        st = real_stat(target)
        if Path(target) == path:
            return _spoof(st, st_dev=int(recorded["dev"]), st_ino=int(recorded["ino"]), **extra)
        return st

    monkeypatch.setattr(registry_module, "_file_stat", stat)


@pytest.fixture
def artifact(tmp_path: Path) -> Path:
    path = tmp_path / "report.md"
    path.write_text("registered content", encoding="utf-8")
    return path


# ---------------------------------------------------------------- hashed (normal) artifacts


def test_unchanged_file_matches_and_identity_records_exact_state(artifact: Path) -> None:
    recorded = file_identity(artifact)
    assert set(recorded) == {"size", "sha256", "dev", "ino", "mtime_ns", "marker_kind", "marker_ns"}
    assert identity_matches(recorded, artifact) is True


def test_replacement_reusing_the_file_number_is_refused(
    artifact: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = file_identity(artifact)
    _replace(artifact, "a different user file!")
    _reuse(monkeypatch, artifact, recorded)

    assert identity_matches(recorded, artifact) is False


def test_same_size_replacement_with_every_stat_field_forged_is_refused(
    artifact: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = file_identity(artifact)
    _replace(artifact, "xxxxxxxxxxxxxxxxxx")  # same length as the original
    _reuse(
        monkeypatch, artifact, recorded,
        st_mtime_ns=int(recorded["mtime_ns"]),
        **{_marker_field(recorded): int(recorded["marker_ns"])},
    )

    assert identity_matches(recorded, artifact) is False, "content decides for hashed artifacts"


def test_real_replacement_is_refused_without_any_simulation(artifact: Path) -> None:
    recorded = file_identity(artifact)
    _replace(artifact, "a different user file!")
    assert identity_matches(recorded, artifact) is False


def test_in_place_edit_is_no_longer_the_registered_artifact(artifact: Path) -> None:
    recorded = file_identity(artifact)
    with artifact.open("r+", encoding="utf-8") as handle:
        handle.write("REGISTERED")
    assert identity_matches(recorded, artifact) is False


def test_metadata_only_changes_and_identical_copies_still_match(artifact: Path, tmp_path: Path) -> None:
    recorded = file_identity(artifact)
    os.utime(artifact, ns=(1_000_000_000, 2_000_000_000))
    assert identity_matches(recorded, artifact) is True

    copy = tmp_path / "elsewhere" / "report.md"
    copy.parent.mkdir()
    copy.write_bytes(artifact.read_bytes())
    assert identity_matches(recorded, copy) is True


def test_legacy_hashed_record_is_checked_by_content(artifact: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st = os.stat(artifact)
    legacy = {"size": st.st_size, "sha256": file_identity(artifact)["sha256"], "dev": st.st_dev, "ino": st.st_ino}
    assert identity_matches(legacy, artifact) is True

    _replace(artifact, "a different user file!")
    _reuse(monkeypatch, artifact, legacy)
    assert identity_matches(legacy, artifact) is False


# ---------------------------------------------------------------- unhashed (large) artifacts


@pytest.fixture
def large(artifact: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Treat every file as larger than the hashing limit, without writing GBs.
    monkeypatch.setattr(registry_module, "HASH_INLINE_MAX_BYTES", 4)
    return artifact


def test_large_files_are_never_hashed_for_identity(large: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_hashing(path: Path) -> str:
        raise AssertionError("large files must not be hashed")

    recorded = file_identity(large)
    monkeypatch.setattr(registry_module, "_sha256", no_hashing)
    assert recorded["sha256"] == ""
    assert identity_matches(recorded, large) is True


@pytest.mark.parametrize("changed", ["st_mtime_ns", "marker", "st_size", "st_dev"])
def test_large_file_with_a_reused_number_but_any_state_change_is_refused(
    large: Path, monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    recorded = file_identity(large)
    field = _marker_field(recorded) if changed == "marker" else changed
    original = {
        "st_mtime_ns": int(recorded["mtime_ns"]),
        _marker_field(recorded): int(recorded["marker_ns"]),
        "st_size": int(recorded["size"]),
        "st_dev": int(recorded["dev"]),
    }
    forged = dict(original)
    forged[field] = original[field] + 1
    real_stat = os.stat
    monkeypatch.setattr(
        registry_module, "_file_stat",
        lambda p: _spoof(real_stat(p), st_ino=int(recorded["ino"]), **forged),
    )

    assert identity_matches(recorded, large) is False


def test_large_file_cannot_be_proven_by_a_legacy_record(large: Path) -> None:
    st = os.stat(large)
    legacy = {"size": st.st_size, "sha256": "", "dev": st.st_dev, "ino": st.st_ino}
    assert identity_matches(legacy, large) is False


@pytest.mark.parametrize(
    "damage", [{"ino": 0}, {"mtime_ns": "not a number"}, {"marker_kind": "other"}, {"size": None}],
)
def test_damaged_large_file_records_fail_closed(large: Path, damage: dict[str, object]) -> None:
    recorded = {**file_identity(large), **damage}
    assert identity_matches(recorded, large) is False


def test_a_hashed_file_that_grew_past_the_limit_is_refused_without_hashing(
    artifact: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = file_identity(artifact)
    artifact.write_text(artifact.read_text(encoding="utf-8") + " plus much more", encoding="utf-8")
    monkeypatch.setattr(registry_module, "HASH_INLINE_MAX_BYTES", len("registered content") + 1)
    monkeypatch.setattr(registry_module, "_sha256", lambda p: pytest.fail("must not hash"))
    assert identity_matches(recorded, artifact) is False


# ---------------------------------------------------------------- Library and open


class Library:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.registry = ArtifactRegistry(tmp_path / "db" / "library.sqlite3", tmp_path / "Artifacts")
        self.producer = ArtifactProducerRegistry(self.registry)
        self.executor = LibraryExecutor(self.registry, recycler=self.fail, protected_roots=(tmp_path / "protected",))
        self.outside = tmp_path / "Outside"
        self.outside.mkdir()

    @staticmethod
    def fail(path: Path) -> None:
        raise AssertionError("nothing may be recycled")

    def artifact(self, name: str = "report.md"):
        path = self.registry.artifact_root / "Documents" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("registered content", encoding="utf-8")
        item = self.producer.register_file(path, origin="Generated by Qronos")
        return item, path, json.loads(item.file_identity)


def test_library_refuses_a_reused_number_before_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib = Library(tmp_path)
    item, path, recorded = lib.artifact()
    _replace(path, "an unrelated user file")
    _reuse(monkeypatch, path, recorded)

    for op, extra in (("rename", {"new_name": "x.md"}), ("copy", {"destination": str(lib.outside)})):
        with pytest.raises(LibraryActionRefused, match="no longer the artifact"):
            lib.executor.prepare(op, [item.id], **extra)
    assert path.read_text(encoding="utf-8") == "an unrelated user file"
    assert list(lib.outside.iterdir()) == []


def test_library_refuses_a_reused_number_after_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib = Library(tmp_path)
    item, path, recorded = lib.artifact()
    approval = lib.executor.prepare("rename", [item.id], new_name="renamed.md")
    _replace(path, "an unrelated user file")
    _reuse(monkeypatch, path, recorded)

    result = lib.executor.commit(approval["token"], operation="rename")

    assert result["refused"] == 1
    assert path.read_text(encoding="utf-8") == "an unrelated user file"
    assert not path.with_name("renamed.md").exists()


def test_open_refuses_a_reused_number(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda p: started.append(p), raising=False)
    lib = Library(tmp_path)
    item, path, recorded = lib.artifact()
    _replace(path, "an unrelated user file")
    _reuse(monkeypatch, path, recorded)

    with pytest.raises(PermissionError, match="no longer the artifact"):
        lib.registry.open(item.id)
    assert started == []
