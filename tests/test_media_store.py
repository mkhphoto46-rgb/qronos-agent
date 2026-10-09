"""The index database: schema refusal without mutation, FTS consistency, read-only search."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest

from qronos_media.store import SCHEMA_VERSION, MediaStore, StoreTooNew, open_read_only


def _seed(store: MediaStore) -> None:
    with store.transaction() as db:
        db.execute("INSERT INTO assets(asset_id, sha256, size, created_at, updated_at) VALUES ('a1','h1',1,0,0)")
        db.execute("INSERT INTO subtitle_tracks(track_id, asset_id, source, status) VALUES ('t1','a1','sidecar','indexed')")
        db.execute("INSERT INTO dialogue(unit_id, asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                   "VALUES (1,'a1','t1','cue',0,0,1000,'I never asked for this','i never asked for this')")


def _phrase(db: sqlite3.Connection, phrase: str) -> list[int]:
    return [row[0] for row in db.execute("SELECT rowid FROM dialogue_fts WHERE dialogue_fts MATCH ?", (f'"{phrase}"',))]


def _trigram(db: sqlite3.Connection, text: str) -> list[int]:
    return [row[0] for row in db.execute("SELECT rowid FROM dialogue_tri WHERE dialogue_tri MATCH ?", (f'"{text}"',))]


def test_newer_schema_is_refused_without_touching_the_file(tmp_path: Path) -> None:
    path = tmp_path / "media-index.sqlite3"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    raw.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION + 1),))
    raw.commit()
    raw.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(StoreTooNew):
        MediaStore(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not (tmp_path / "media-index.sqlite3-wal").exists()
    tables = sqlite3.connect(path).execute("SELECT name FROM sqlite_master").fetchall()
    assert tables == [("meta",), ("sqlite_autoindex_meta_1",)]


def test_reopening_a_current_store_keeps_its_data(tmp_path: Path) -> None:
    store = MediaStore(tmp_path / "i.sqlite3")
    _seed(store)
    store.close()
    again = MediaStore(tmp_path / "i.sqlite3")
    assert _phrase(again.db, "never asked") == [1]
    again.close()


def test_fts_indexes_follow_insert_update_and_delete(tmp_path: Path) -> None:
    store = MediaStore(tmp_path / "i.sqlite3")
    _seed(store)
    db = store.db
    assert _phrase(db, "never asked") == [1] and _trigram(db, "aske") == [1]
    with store.transaction():
        db.execute("UPDATE dialogue SET norm_text = 'he left when i was nine' WHERE unit_id = 1")
    assert _phrase(db, "never asked") == [] and _trigram(db, "aske") == []
    assert _phrase(db, "when i was nine") == [1] and _trigram(db, "nine") == [1]
    with store.transaction():
        db.execute("DELETE FROM dialogue WHERE unit_id = 1")
    assert _phrase(db, "when i was nine") == [] and _trigram(db, "nine") == []
    for table in ("dialogue_fts", "dialogue_tri"):
        db.execute(f"INSERT INTO {table}({table}) VALUES ('integrity-check')")
    store.close()


def test_a_rolled_back_stage_leaves_the_previous_rows_and_index(tmp_path: Path) -> None:
    store = MediaStore(tmp_path / "i.sqlite3")
    _seed(store)
    with pytest.raises(RuntimeError):
        with store.transaction() as db:
            db.execute("DELETE FROM dialogue WHERE asset_id = 'a1'")
            db.execute("INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                       "VALUES ('a1','t1','cue',0,0,1,'new','new words')")
            raise RuntimeError("stage interrupted before commit")
    assert _phrase(store.db, "never asked") == [1] and _phrase(store.db, "new words") == []
    store.close()


def test_removing_an_asset_cascades_out_of_both_indexes(tmp_path: Path) -> None:
    store = MediaStore(tmp_path / "i.sqlite3")
    _seed(store)
    with store.transaction() as db:
        db.execute("DELETE FROM assets WHERE asset_id = 'a1'")
    assert store.db.execute("SELECT COUNT(*) FROM dialogue").fetchone()[0] == 0
    assert _phrase(store.db, "never asked") == [] and _trigram(store.db, "aske") == []
    store.close()


def test_search_connection_cannot_write(tmp_path: Path) -> None:
    store = MediaStore(tmp_path / "i.sqlite3")
    _seed(store)
    reader = open_read_only(tmp_path / "i.sqlite3")
    assert _phrase(reader, "never asked") == [1]
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("DELETE FROM dialogue")
    reader.close()
    store.close()
    with pytest.raises(FileNotFoundError):
        open_read_only(tmp_path / "missing.sqlite3")
