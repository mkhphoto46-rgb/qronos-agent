"""The media index database: the single source of truth (handoff s28, s38).

One SQLite file holds roots, files, assets, streams, subtitle tracks, dialogue,
scenes, keyframes and per-stage job state. Reports and search read from it and
never re-analyse a file. Two FTS5 indexes sit over the same dialogue rows:
``unicode61`` for word and phrase matching and ``trigram`` for fuzzy
candidates. Triggers keep both in step with the rows, so a crash between a
row and its index entry is impossible inside one transaction.

Dialogue rows are insert-or-delete in practice (a stage replaces an asset's
rows wholesale), and an UPDATE trigger keeps the indexes right even if a row
is ever edited in place.

Search opens the file read-only, so querying never needs the indexing stack
and never takes the writer lock (handoff s35).
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS roots (
    root_id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('folder', 'file')),
    approval_action_id TEXT NOT NULL,
    approval_decision TEXT NOT NULL,
    approved_at REAL NOT NULL,
    max_depth INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_scan_at REAL,
    last_scan_json TEXT
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    duration_ms INTEGER,           -- the container's claim
    start_offset_ms INTEGER,       -- container start time; the asset timeline starts here
    video_end_ms INTEGER,          -- measured by decoding, on the asset timeline
    container TEXT,
    title TEXT,
    probe_json TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES roots(root_id) ON DELETE CASCADE,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    asset_id TEXT REFERENCES assets(asset_id),
    -- The stat when the full hash was taken. Evidence is attached to the hash
    -- only while the file still has exactly this size, mtime and file id.
    hashed_size INTEGER,
    hashed_mtime_ns INTEGER,
    hashed_file_id INTEGER,
    status TEXT NOT NULL CHECK (status IN ('pending_identity', 'present', 'missing', 'failed')),
    error TEXT,
    seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS files_by_asset ON files(asset_id);
CREATE INDEX IF NOT EXISTS files_by_status ON files(status);

CREATE TABLE IF NOT EXISTS streams (
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    kind TEXT NOT NULL,
    codec TEXT,
    language TEXT,
    title TEXT,
    is_default INTEGER NOT NULL,
    forced INTEGER NOT NULL,
    channels INTEGER,
    channel_layout TEXT,
    width INTEGER,
    height INTEGER,
    fps REAL,
    start_ms INTEGER,
    time_base TEXT,
    PRIMARY KEY (asset_id, idx)
);

CREATE TABLE IF NOT EXISTS subtitle_tracks (
    track_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('embedded', 'sidecar')),
    stream_index INTEGER,
    sidecar_path TEXT,
    sidecar_sha256 TEXT,
    language TEXT,
    title TEXT,
    codec TEXT,
    forced INTEGER NOT NULL DEFAULT 0,
    encoding TEXT,
    status TEXT NOT NULL CHECK (status IN ('indexed', 'unsupported_image', 'failed')),
    detail TEXT,
    cue_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tracks_by_asset ON subtitle_tracks(asset_id);

CREATE TABLE IF NOT EXISTS dialogue (
    unit_id INTEGER PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    track_id TEXT NOT NULL REFERENCES subtitle_tracks(track_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('cue', 'pair')),
    seq INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    norm_text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dialogue_by_track ON dialogue(track_id, kind, seq);
CREATE INDEX IF NOT EXISTS dialogue_by_asset ON dialogue(asset_id, start_ms);

CREATE VIRTUAL TABLE IF NOT EXISTS dialogue_fts USING fts5(
    norm_text, content='dialogue', content_rowid='unit_id', tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS dialogue_tri USING fts5(
    norm_text, content='dialogue', content_rowid='unit_id', tokenize='trigram'
);
CREATE VIRTUAL TABLE IF NOT EXISTS dialogue_tri_vocab USING fts5vocab(dialogue_tri, 'row');
CREATE TRIGGER IF NOT EXISTS dialogue_ai AFTER INSERT ON dialogue BEGIN
    INSERT INTO dialogue_fts(rowid, norm_text) VALUES (new.unit_id, new.norm_text);
    INSERT INTO dialogue_tri(rowid, norm_text) VALUES (new.unit_id, new.norm_text);
END;
CREATE TRIGGER IF NOT EXISTS dialogue_au AFTER UPDATE OF norm_text ON dialogue BEGIN
    INSERT INTO dialogue_fts(dialogue_fts, rowid, norm_text) VALUES ('delete', old.unit_id, old.norm_text);
    INSERT INTO dialogue_tri(dialogue_tri, rowid, norm_text) VALUES ('delete', old.unit_id, old.norm_text);
    INSERT INTO dialogue_fts(rowid, norm_text) VALUES (new.unit_id, new.norm_text);
    INSERT INTO dialogue_tri(rowid, norm_text) VALUES (new.unit_id, new.norm_text);
END;
CREATE TRIGGER IF NOT EXISTS dialogue_ad AFTER DELETE ON dialogue BEGIN
    INSERT INTO dialogue_fts(dialogue_fts, rowid, norm_text) VALUES ('delete', old.unit_id, old.norm_text);
    INSERT INTO dialogue_tri(dialogue_tri, rowid, norm_text) VALUES ('delete', old.unit_id, old.norm_text);
END;

CREATE TABLE IF NOT EXISTS scenes (
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    scene_index INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    cut_score REAL,
    PRIMARY KEY (asset_id, scene_index)
);

CREATE TABLE IF NOT EXISTS keyframes (
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    scene_index INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    time_ms INTEGER NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    PRIMARY KEY (asset_id, scene_index, ordinal)
);

CREATE TABLE IF NOT EXISTS stages (
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('QUEUED', 'RUNNING', 'PAUSED', 'FAILED', 'COMPLETED', 'CANCELLED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    checkpoint TEXT,
    input_fp TEXT,
    note TEXT,
    queued_at REAL,
    started_at REAL,
    finished_at REAL,
    PRIMARY KEY (asset_id, stage)
);
CREATE INDEX IF NOT EXISTS stages_by_state ON stages(state);
"""


class StoreTooNew(RuntimeError):
    """The database was written by a newer media index."""


def _stored_version(db: sqlite3.Connection) -> int | None:
    try:
        row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return None
    try:
        return int(row[0]) if row else None
    except (TypeError, ValueError):
        raise StoreTooNew("media index schema version is unreadable") from None


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0,
                             isolation_level=None, check_same_thread=False)
    else:
        db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    return db


class MediaStore:
    """Owns the writable connection. One writer process at a time (see jobs.RunnerLock)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        if path.exists():
            # Read the version through a read-only connection first, so a
            # database from a newer build is refused without being touched
            # (no WAL switch, no schema statements).
            probe = _connect(path, read_only=True)
            try:
                version = _stored_version(probe)
            finally:
                probe.close()
            if version is not None and version > SCHEMA_VERSION:
                raise StoreTooNew(f"media index schema {version} is newer than {SCHEMA_VERSION}")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = _connect(path, read_only=False)
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        # executescript commits implicitly, so it runs outside an explicit
        # transaction; every statement in the schema is idempotent.
        self.db.executescript(_SCHEMA)
        with self.transaction():
            self.db.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def close(self) -> None:
        self.db.close()


def open_read_only(path: Path) -> sqlite3.Connection:
    """A query connection that cannot write and does not need the runner."""
    if not path.is_file():
        raise FileNotFoundError("media index does not exist yet")
    return _connect(path, read_only=True)
