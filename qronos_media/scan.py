"""Finding video files under approved roots, and noticing what changed (handoff s5.1).

A scan only stats files. Size, modification time and file id decide
"unchanged"; any change queues the file for a full hash, because a partial
fingerprint can miss an edit in the middle of a film and the hash is what
evidence is bound to. Hashing and everything after it belong to the runner.

The walk and the stat calls run with no database transaction open; the
results are written afterwards in short batches, so pause, cancel and status
are never blocked behind a large directory tree.

Bounded: depth and entries visited have caps. A scan that hit a cap says so
and does **not** mark unseen files missing, because it did not look for them.
Every scan, capped or not, does sweep the root's rows against its current
scope (qronos_media.scope): rows beyond a narrowed depth, outside the root or
inside the media index's own data folder are marked ``missing``/``out_of_scope`` and are
never read again unless a later scope admits them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Callable, ContextManager

from qronos_media.identity import FileState
from qronos_media.jobs import admitted_files, ensure_stages
from qronos_media.paths import VIDEO_EXTENSIONS, UnsafeMediaPath, has_link_component
from qronos_media.scope import OUT_OF_SCOPE, RootScope, inside, mark, outside_scope, root_inside_state
from qronos_media.stages import subtitles_input_fingerprint

DEFAULT_MAX_DEPTH = 12
DEFAULT_MAX_ENTRIES = 200_000
WRITE_BATCH = 500


@dataclass
class ScanSummary:
    root_id: int
    root: str
    found: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    missing: int = 0
    skipped_links: int = 0
    errors: int = 0
    truncated: bool = False
    requeued_stages: int = 0
    out_of_scope: int = 0
    skipped_media_state: int = 0
    notes: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        return dict(self.__dict__)


def _entry_is_link(entry: os.DirEntry) -> bool:
    try:
        if entry.is_symlink():
            return True
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _under(path: str, excluded: tuple[Path, ...]) -> bool:
    return any(inside(path, root) for root in excluded)


def walk_videos(root: Path, *, max_depth: int, max_entries: int, summary: ScanSummary,
                excluded: tuple[Path, ...] = ()) -> list[Path]:
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    visited = 0
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > max_entries:
                        summary.truncated = True
                        summary.notes.append(f"stopped after {max_entries} entries")
                        return found
                    if _entry_is_link(entry):
                        summary.skipped_links += 1
                        continue
                    if _under(entry.path, excluded):
                        summary.skipped_media_state += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth + 1 <= max_depth:
                            stack.append((Path(entry.path), depth + 1))
                        else:
                            summary.truncated = True
                    elif entry.is_file(follow_symlinks=False):
                        if os.path.splitext(entry.name)[1].lower() in VIDEO_EXTENSIONS:
                            found.append(Path(entry.path))
        except OSError:
            summary.errors += 1
    if summary.truncated and not summary.notes:
        summary.notes.append(f"directories deeper than {max_depth} were not scanned")
    return found


def collect(root: sqlite3.Row, *, max_entries: int = DEFAULT_MAX_ENTRIES,
            excluded: tuple[Path, ...] = ()) -> tuple[ScanSummary, list[tuple[Path, FileState]]]:
    """Walk and stat one root. Touches the filesystem only, never the database."""
    root_path = Path(root["path"])
    summary = ScanSummary(root_id=root["root_id"], root=root["path"])
    shield = tuple(excluded)
    if root_inside_state(root_path, shield):
        summary.truncated = True
        summary.notes.append("root is inside the media index's own data folder; not scanned")
        return summary, []
    if has_link_component(root_path):
        summary.errors += 1
        summary.truncated = True
        summary.notes.append("root is (or is inside) a link or junction; not scanned")
        return summary, []
    if root["kind"] == "file":
        paths = [root_path] if root_path.is_file() else []
        if not paths:
            summary.notes.append("approved file is not present")
    elif root_path.is_dir():
        paths = walk_videos(root_path, max_depth=root["max_depth"], max_entries=max_entries, summary=summary,
                            excluded=shield)
    else:
        summary.notes.append("approved folder is not present; files were not marked missing")
        summary.truncated = True
        paths = []
    entries = []
    for path in paths:
        try:
            entries.append((path, FileState.of(path)))
        except OSError:
            summary.errors += 1
    summary.found = len(entries)
    return summary, entries


def apply(transaction: Callable[[], ContextManager[sqlite3.Connection]], root: sqlite3.Row,
          summary: ScanSummary, entries: list[tuple[Path, FileState]], now: float | None = None,
          excluded: tuple[Path, ...] = ()) -> ScanSummary:
    """Write one root's scan results in short transactions."""
    now = time.time() if now is None else now
    shield = tuple(excluded)
    touched: set[str] = set()
    seen: set[str] = set()
    for start in range(0, len(entries), WRITE_BATCH):
        with transaction() as db:
            for path, state in entries[start:start + WRITE_BATCH]:
                key = str(path)
                seen.add(key)
                row = db.execute("SELECT * FROM files WHERE path = ?", (key,)).fetchone()
                if row is not None and row["root_id"] != root["root_id"]:
                    continue  # owned by another approved root that also covers it
                same = row is not None and (row["size"], row["mtime_ns"], row["file_id"]) == (
                    state.size, state.mtime_ns, state.file_id)
                if same and row["status"] in {"present", "pending_identity"}:
                    db.execute("UPDATE files SET seen_at = ? WHERE path = ?", (now, key))
                    summary.unchanged += 1
                    if row["asset_id"]:
                        touched.add(row["asset_id"])
                elif row is None:
                    db.execute(
                        "INSERT INTO files(path, root_id, size, mtime_ns, file_id, status, seen_at) "
                        "VALUES (?,?,?,?,?,'pending_identity',?)",
                        (key, root["root_id"], state.size, state.mtime_ns, state.file_id, now),
                    )
                    summary.new += 1
                else:
                    db.execute(
                        "UPDATE files SET size = ?, mtime_ns = ?, file_id = ?, asset_id = NULL, hashed_size = NULL, "
                        "hashed_mtime_ns = NULL, hashed_file_id = NULL, status = 'pending_identity', error = NULL, "
                        "seen_at = ? WHERE path = ?",
                        (state.size, state.mtime_ns, state.file_id, now, key),
                    )
                    summary.changed += 1
    with transaction() as db:
        if not summary.truncated:
            for row in db.execute("SELECT path FROM files WHERE root_id = ? AND status != 'missing'",
                                  (root["root_id"],)).fetchall():
                if row["path"] not in seen:
                    db.execute("UPDATE files SET status = 'missing', seen_at = ? WHERE path = ?", (now, row["path"]))
                    summary.missing += 1
        candidates = [row["path"] for row in db.execute(
            "SELECT path FROM files WHERE root_id = ? AND NOT (status = 'missing' AND error = ?)",
            (root["root_id"], OUT_OF_SCOPE))]
    # Truncated or not, nothing outside the root's current scope stays readable.
    # The path checks touch the filesystem, so they run with no transaction open.
    failing = outside_scope(candidates, RootScope.from_row(root), shield)
    with transaction() as db:
        summary.out_of_scope = mark(db, failing)
    # Admission and sidecar fingerprints touch the filesystem: no transaction open.
    copies = {asset: admitted_files(db, asset, None, shield)[0] for asset in sorted(touched)}
    fingerprints = {asset: subtitles_input_fingerprint(files, shield) for asset, files in copies.items() if files}
    with transaction() as db:
        for asset, fingerprint in fingerprints.items():
            summary.requeued_stages += len(ensure_stages(db, asset, {"subtitles": fingerprint}, now))
        db.execute("UPDATE roots SET last_scan_at = ?, last_scan_json = ? WHERE root_id = ?",
                   (now, json.dumps(summary.as_json()), root["root_id"]))
    return summary


def validate_root(path: Path, kind: str) -> None:
    if has_link_component(path):
        raise UnsafeMediaPath("roots must not be links or junctions, or inside one")
    if kind == "folder" and not path.is_dir():
        raise UnsafeMediaPath("folder does not exist")
    if kind == "file":
        if not path.is_file():
            raise UnsafeMediaPath("file does not exist")
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise UnsafeMediaPath("not a supported video file extension")
