"""Local metadata-only filename index, built off the command execution thread.

Only explicitly requested roots are scanned. A complete snapshot expires after
60 seconds; paths are rechecked by the resolver before they can be executed.
This is a search accelerator, not an authorization or filesystem truth source.
"""
from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import os
import queue
import sqlite3
import threading
import time
from typing import Callable


class LocalFileIndex:
    def __init__(self, database: Path, normalise: Callable[[str], str], *,
                 ttl_seconds: float = 60.0, max_entries: int = 1_000_000,
                 build_seconds: float = 120.0) -> None:
        self.database = database
        self.normalise = normalise
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.build_seconds = build_seconds
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._queue: queue.Queue[Path] = queue.Queue(maxsize=32)
        self._worker: threading.Thread | None = None
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS scopes (root TEXT PRIMARY KEY, ready INTEGER, updated REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS entries (root TEXT, path TEXT, name TEXT, directory INTEGER, PRIMARY KEY(root,path))")
            db.execute("CREATE INDEX IF NOT EXISTS by_name ON entries(root,directory,name)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.database), timeout=0.05)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _key(root: Path) -> str:
        return os.path.normcase(os.path.abspath(str(root)))

    @staticmethod
    def _linked(path: Path) -> bool:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())

    def request_refresh(self, root: Path) -> None:
        key = self._key(root)
        with self._lock:
            if key in self._pending or self._queue.full():
                return
            self._pending.add(key)
            self._queue.put_nowait(root)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="qronos-file-index", daemon=True)
                self._worker.start()

    def _run(self) -> None:
        while True:
            root = self._queue.get()
            try:
                self.refresh(root)
            except (OSError, sqlite3.Error):
                # An unavailable/locked index cannot break ordinary resolution.
                pass
            finally:
                with self._lock:
                    self._pending.discard(self._key(root))
                self._queue.task_done()

    def refresh(self, root: Path) -> bool:
        """Build one bounded snapshot. Intended for the worker and tests only."""
        key = self._key(root)
        if not root.is_absolute() or str(root).startswith(("\\\\", "//")) or self._linked(root):
            return False
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO scopes VALUES (?,0,?)", (key, time.time()))
        deadline = time.monotonic() + self.build_seconds
        complete = True
        count = 0
        pending = [(root, 0)]
        with self._connect() as db:
            db.execute("DELETE FROM entries WHERE root=?", (key,))
            while pending:
                folder, depth = pending.pop()
                try:
                    with os.scandir(folder) as children:
                        for child in children:
                            count += 1
                            if count > self.max_entries or time.monotonic() >= deadline:
                                complete = False
                                pending.clear()
                                break
                            path = Path(child.path)
                            if self._linked(path):
                                continue
                            directory = child.is_dir(follow_symlinks=False)
                            name = path.name if directory else path.stem
                            db.execute("INSERT OR REPLACE INTO entries VALUES (?,?,?,?)",
                                       (key, str(path), self.normalise(name), int(directory)))
                            if directory:
                                if depth < 64:
                                    pending.append((path, depth + 1))
                                else:
                                    complete = False
                except OSError:
                    # Do not represent an inaccessible or disappearing subtree
                    # as proof that a filename is unique across the scope.
                    complete = False
            db.execute("UPDATE scopes SET ready=?,updated=? WHERE root=?",
                       (int(complete), time.time(), key))
        return complete

    def lookup(self, root: Path, query: str, *, directory: bool) -> tuple[Path, ...] | None:
        """Return exact-name candidates from a fresh complete snapshot.

        Partial/fuzzy queries retain the resolver's scoring and bounded scan.
        A snapshot is never used as evidence that no such file exists.
        """
        key = self._key(root)
        try:
            with self._connect() as db:
                state = db.execute("SELECT ready,updated FROM scopes WHERE root=?", (key,)).fetchone()
                if state and state[0] and 0 <= time.time() - state[1] <= self.ttl_seconds:
                    rows = db.execute("SELECT path FROM entries WHERE root=? AND directory=? AND name=? LIMIT 257",
                                      (key, int(directory), self.normalise(query))).fetchall()
                    if rows and len(rows) <= 256:
                        return tuple(Path(row[0]) for row in rows)
                    return None
        except sqlite3.Error:
            return None
        self.request_refresh(root)
        return None
