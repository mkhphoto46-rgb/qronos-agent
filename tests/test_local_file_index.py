from pathlib import Path

import pytest

from core.file_resolver import KnownFolderFileResolver, NamedFileAmbiguous, NamedFileRequest, _canonical_name
from core.local_file_index import LocalFileIndex


@pytest.fixture
def indexed(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    index = LocalFileIndex(tmp_path / "cache" / "index.sqlite3", _canonical_name)
    return root, index


def test_persistent_exact_lookup_avoids_recursive_scan(indexed, monkeypatch):
    root, index = indexed
    target = root / "گزارش.txt"
    target.write_text("private content", encoding="utf-8")
    assert index.refresh(root)
    index = LocalFileIndex(index.database, _canonical_name)
    resolver = KnownFolderFileResolver(roots={"documents": (root,)}, index=index)
    monkeypatch.setattr(resolver, "_entries", lambda *args: pytest.fail("unexpected disk scan"))
    assert resolver.resolve(NamedFileRequest("گزارش", "documents")).path == target
    assert b"private content" not in index.database.read_bytes()


def test_index_preserves_ambiguity_between_same_names(indexed):
    root, index = indexed
    for folder in ("one", "two"):
        child = root / folder
        child.mkdir()
        (child / "report.txt").write_text("x")
    assert index.refresh(root)
    resolver = KnownFolderFileResolver(roots={"documents": (root,)}, index=index)
    with pytest.raises(NamedFileAmbiguous):
        resolver.resolve(NamedFileRequest("report", "documents"))


def test_deleted_cached_file_is_revalidated(indexed):
    root, index = indexed
    target = root / "report.txt"
    target.write_text("x")
    assert index.refresh(root)
    target.unlink()
    replacement = root / "report final.txt"
    replacement.write_text("y")
    resolver = KnownFolderFileResolver(roots={"documents": (root,)}, index=index)
    assert resolver.resolve(NamedFileRequest("report", "documents")).path == replacement


def test_incomplete_snapshot_is_not_used(indexed, monkeypatch):
    root, index = indexed
    (root / "a.txt").write_text("x")
    (root / "b.txt").write_text("x")
    index.max_entries = 1
    assert not index.refresh(root)
    monkeypatch.setattr(index, "request_refresh", lambda root: None)
    assert index.lookup(root, "a", directory=False) is None


def test_expired_snapshot_requests_background_refresh(indexed, monkeypatch):
    root, index = indexed
    (root / "a.txt").write_text("x")
    assert index.refresh(root)
    index.ttl_seconds = -1
    requested = []
    monkeypatch.setattr(index, "request_refresh", requested.append)
    assert index.lookup(root, "a", directory=False) is None
    assert requested == [root]
