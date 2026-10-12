"""Persistent stage jobs: queue, pause, resume, cancel, retry (handoff s33, s57, s58).

A job is one (asset, stage) row in the ``stages`` table, so job state survives
a crash with the data it describes. Rules:

* **One writer.** :class:`RunnerLock` is an OS file lock; a second runner
  gets "busy" instead of interleaving writes.
* **Nothing running survives as running.** A RUNNING row found when the lock
  is taken belonged to a dead runner. Stages are idempotent and checkpointed,
  so it is re-queued from its checkpoint and says it was interrupted.
* **Control is visible across processes.** Pause and cancel are rows in the
  database. The runner re-reads them while a child decoder runs and kills the
  child when either changes, so ``media pause`` from another process takes
  effect mid-stage.
* **Permission is re-asked, not remembered.** Each run (and every minute of a
  long run) re-checks every root with the Qronos gate; files under a root
  that is now denied or needs fresh approval are not read.
* **Evidence follows the hash.** A stage only reads copies whose size,
  modification time and file id still match the state they were hashed in,
  and checks again after the decoder finishes; a mismatch discards the
  result and sends the file back for a full hash.
* **A stage that did not finish leaves nothing behind** except its checkpoint:
  its result is committed in the same transaction that marks it COMPLETED,
  and only if the row is still RUNNING (a cancel that raced the finish wins).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import errno
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable

from qronos_media.identity import FileState, HashCancelled, asset_id_for, full_sha256
from qronos_media.permissions import Approval, MediaPermissionRefused, MediaPermissions
from qronos_media.resources import MediaResourceAuthority, ResourceRefused, StageNeeds
from qronos_media.scope import OUT_OF_SCOPE, OutOfScope, admit, load_scopes
from qronos_media.stages import (
    RUNNERS, SKIP_CLEANUP, SKIPS, STAGES, StageContext, StageFailed, downstream, subtitles_input_fingerprint,
)
from qronos_media.store import MediaStore
from qronos_media.tools import ChildCancelled, MediaToolError, MediaTools

CONTROL_POLL_SECONDS = 0.5
MAX_ERROR_CHARS = 500

_LOCK_HELD = frozenset({errno.EACCES, getattr(errno, "EDEADLK", 36), getattr(errno, "EDEADLOCK", 36)})
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class RunnerBusy(RuntimeError):
    """Another runner holds this media index."""


class RunnerLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None
        key = os.path.normcase(str(path))
        with _THREAD_LOCKS_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())

    def acquire(self) -> None:
        if not self._thread_lock.acquire(blocking=False):
            raise RunnerBusy("another runner in this process holds the media index")
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    except OSError as exc:
                        if exc.errno in _LOCK_HELD:
                            raise RunnerBusy("another process is indexing") from None
                        raise
                else:
                    import fcntl

                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise RunnerBusy("another process is indexing") from None
            except BaseException:
                os.close(fd)
                raise
            self._fd = fd
        except BaseException:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
            self._thread_lock.release()

    def __enter__(self) -> "RunnerLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# ------------------------------------------------------------- invalidation

def ensure_stages(db: sqlite3.Connection, asset_id: str, input_fps: dict[str, str] | None = None,
                  now: float | None = None) -> list[str]:
    """Create missing stage rows; re-queue stale ones and their dependents.

    Must be called inside a write transaction. Returns the stages re-queued.
    """
    now = time.time() if now is None else now
    inputs = input_fps or {}
    rows = {row["stage"]: row for row in db.execute("SELECT * FROM stages WHERE asset_id = ?", (asset_id,))}
    stale: list[str] = []
    for name, spec in STAGES.items():
        row = rows.get(name)
        if row is None:
            db.execute(
                "INSERT INTO stages(asset_id, stage, version, state, input_fp, queued_at) VALUES (?,?,?,?,?,?)",
                (asset_id, name, spec.version, "QUEUED", inputs.get(name), now),
            )
            continue
        changed_input = name in inputs and row["input_fp"] != inputs[name]
        if row["version"] != spec.version or changed_input:
            stale.append(name)
    requeued: list[str] = []
    for name in stale:
        for affected in downstream(name):
            if affected not in requeued:
                requeued.append(affected)
    for name in requeued:
        reason = "version" if rows.get(name) is not None and rows[name]["version"] != STAGES[name].version else "input"
        db.execute(
            "UPDATE stages SET version = ?, state = 'QUEUED', checkpoint = NULL, error = NULL, attempts = 0, "
            "note = ?, queued_at = ?, input_fp = COALESCE(?, input_fp) WHERE asset_id = ? AND stage = ?",
            (STAGES[name].version, f"invalidated:{reason}", now, inputs.get(name), asset_id, name),
        )
    return requeued


def requeue(db: sqlite3.Connection, asset_id: str, stage: str, note: str, now: float | None = None) -> list[str]:
    """Re-run ``stage`` and its dependents from scratch (``media.reindex``)."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    affected = downstream(stage)
    for name in affected:
        db.execute(
            "UPDATE stages SET state = 'QUEUED', checkpoint = NULL, error = NULL, attempts = 0, note = ?, "
            "queued_at = ? WHERE asset_id = ? AND stage = ?",
            (note, time.time() if now is None else now, asset_id, name),
        )
    return affected


def present_files(db: sqlite3.Connection, asset_id: str) -> list[Path]:
    return [Path(row["path"]) for row in db.execute(
        "SELECT path FROM files WHERE asset_id = ? AND status = 'present' ORDER BY path", (asset_id,)
    )]


def file_matches_hash(row: sqlite3.Row) -> bool:
    """True while the file still has the size, mtime and id it was hashed with."""
    try:
        state = FileState.of(Path(row["path"]))
    except OSError:
        return False
    return (state.size, state.mtime_ns, state.file_id) == (
        row["hashed_size"], row["hashed_mtime_ns"], row["hashed_file_id"])


def admitted_files(db: sqlite3.Connection, asset_id: str, roots: set[int] | None,
                   excluded: tuple[Path, ...]) -> tuple[list[Path], list[str]]:
    """Present copies the index may read now (qronos_media.scope.admit), and the ones it may not.

    ``roots`` limits the answer to roots the gate allows; ``None`` skips only
    that filter, never the path admission. ``excluded`` is the media index's
    own data folder.
    """
    scopes = load_scopes(db)
    shield = excluded
    inside: list[Path] = []
    outside: list[str] = []
    for row in db.execute("SELECT * FROM files WHERE asset_id = ? AND status = 'present' ORDER BY path", (asset_id,)):
        if roots is not None and row["root_id"] not in roots:
            continue
        scope = scopes.get(int(row["root_id"]))
        try:
            if scope is None:
                raise OutOfScope("no_root")
            inside.append(admit(scope, row["path"], shield))
        except OutOfScope:
            outside.append(row["path"])
    return inside, outside


def verified_files(db: sqlite3.Connection, asset_id: str, roots: set[int] | None,
                   excluded: tuple[Path, ...]) -> tuple[list[Path], list[str], list[str]]:
    """Admitted copies that still match their hash; admitted ones that do not; out-of-scope paths."""
    inside, outside = admitted_files(db, asset_id, roots, excluded)
    rows = {Path(row["path"]): row for row in db.execute(
        "SELECT * FROM files WHERE asset_id = ? AND status = 'present'", (asset_id,))}
    good: list[Path] = []
    stale: list[str] = []
    for path in inside:
        row = rows.get(path)
        if row is not None and file_matches_hash(row):
            good.append(path)
        else:
            stale.append(str(path))
    return good, stale, outside


def mark_out_of_scope(db: sqlite3.Connection, paths: list[str]) -> None:
    """Rows the index must not read any more; in a transaction. The asset is kept."""
    for path in paths:
        db.execute("UPDATE files SET status = 'missing', error = ? WHERE path = ?", (OUT_OF_SCOPE, path))


def mark_changed(db: sqlite3.Connection, paths: list[str]) -> None:
    """Send files whose content may have changed back for a full hash. In a transaction.

    The row takes the file's current stat, so the next hash starts from what
    is on disk now; a file that has gone is marked missing instead.
    """
    for path in paths:
        try:
            state = FileState.of(Path(path))
        except OSError:
            db.execute("UPDATE files SET asset_id = NULL, hashed_size = NULL, hashed_mtime_ns = NULL, "
                       "hashed_file_id = NULL, status = 'missing' WHERE path = ?", (path,))
            continue
        db.execute(
            "UPDATE files SET asset_id = NULL, hashed_size = NULL, hashed_mtime_ns = NULL, hashed_file_id = NULL, "
            "size = ?, mtime_ns = ?, file_id = ?, status = 'pending_identity', error = 'changed since hashed' "
            "WHERE path = ?",
            (state.size, state.mtime_ns, state.file_id, path),
        )


# ------------------------------------------------------------------- runner

@dataclass
class RunReport:
    hashed: int = 0
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    identity_changed: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    refused_roots: dict[str, str] = field(default_factory=dict)
    stopped: str = "idle"
    detail: str = ""

    def as_json(self) -> dict[str, object]:
        return {"hashed": self.hashed, "completed": self.completed, "failed": self.failed,
                "identityChanged": self.identity_changed, "outOfScope": self.out_of_scope, "refused": self.refused,
                "refusedRoots": self.refused_roots,
                "stopped": self.stopped, "detail": self.detail}


#: Production default: while a hash or decoder runs, the gate is asked again
#: for every root actually in use at least this often (a Files DENY or an
#: expired approval stops the work within about this long). At every read
#: handoff (before a file is stat'ed for hashing, before a stage starts,
#: before a decoder opens a copy) the gate is asked fresh, with no cache.
REVOCATION_CHECK_SECONDS = 2.0
LEASE_RENEW_SECONDS = 30.0


class Watch:
    """What a running hash or decoder child is checked against while it runs.

    ``poll`` is rate-limited for the child loop; ``now`` is the uncached form
    for chunk boundaries. Once tripped it stays tripped, and ``reason`` says
    why: paused, cancelled (job row changed), time_budget, permission or
    deferred (the resource lease could not be renewed).

    ``roots`` are the roots of the copies *actually opened* under this watch
    (:meth:`bind`). Every one of them must still be allowed; a different
    copy's root that is still allowed never carries a revoked one.
    """

    def __init__(self, runner: "StageRunner", *, deadline: float, holder: list, roots: set[int],
                 job: tuple[str, str] | None = None) -> None:
        self.runner = runner
        self.deadline = deadline
        self.holder = holder
        self.roots = set(roots)
        self.job = job
        self.reason: str | None = None
        self._last = -math.inf
        self._renewed = runner.monotonic()

    def bind(self, root_id: int) -> None:
        """A copy under ``root_id`` is being opened under this watch."""
        self.roots.add(int(root_id))

    def now(self) -> bool:
        if self.reason is not None:
            return True
        runner = self.runner
        clock = runner.monotonic()
        if runner.paused():
            self.reason = "paused"
        elif self.job is not None and runner._state(*self.job) != "RUNNING":
            self.reason = "cancelled"
        elif clock >= self.deadline:
            self.reason = "time_budget"
        elif (yielded := _yield_reason(runner.authority)) is not None:
            # The hosted authority's cheap check: the assistant's foreground turn,
            # Pause, Stop/shutdown/revocation or gaming/creator load.
            self.reason = f"deferred:{yielded}"
        elif self.roots and not self.roots <= runner.allowed_roots(max_age=REVOCATION_CHECK_SECONDS):
            self.reason = "permission"
        elif clock - self._renewed >= LEASE_RENEW_SECONDS:
            try:
                self.holder[0] = runner.authority.renew(self.holder[0])
                self._renewed = clock
            except ResourceRefused as exc:
                self.reason = f"deferred:{exc.reason}"
        return self.reason is not None

    def poll(self) -> bool:
        if self.reason is not None:
            return True
        clock = self.runner.monotonic()
        if clock - self._last < CONTROL_POLL_SECONDS:
            return False
        self._last = clock
        return self.now()


#: Refusals that will not change by waiting (no authority may grant this stage here, or its assets are not
#: provisioned as pinned): the stage stays QUEUED with the reason and the run carries on with other work.
UNAVAILABLE_REASONS = frozenset({
    "requires_qronos_governor", "requires_trusted_model_authority", "gpu_lease_contract_pending",
    "undeclared_model", "model_lease_required", "model_unavailable",
})


def backfill_stages(db: sqlite3.Connection, now: float | None = None) -> int:
    """Queue stages added since an asset was hashed (for example ``asr``). Inside a write transaction."""
    now = time.time() if now is None else now
    added = 0
    for name, spec in STAGES.items():
        added += db.execute(
            "INSERT INTO stages(asset_id, stage, version, state, queued_at) SELECT a.asset_id, ?, ?, 'QUEUED', ? "
            "FROM assets a WHERE NOT EXISTS (SELECT 1 FROM stages s WHERE s.asset_id = a.asset_id AND s.stage = ?)",
            (name, spec.version, now, name),
        ).rowcount
    return added


def _yield_reason(authority: object) -> str | None:
    check = getattr(authority, "yield_reason", None)
    return check() if callable(check) else None


class StageRunner:
    def __init__(self, store: MediaStore, *, tools: MediaTools | None, authority: MediaResourceAuthority,
                 permissions: MediaPermissions, cache_dir: Path, cache_quota_bytes: int,
                 excluded: tuple[Path, ...] = (), clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, embedder: object = None) -> None:
        self.store = store
        self.db = store.db
        self.tools = tools
        self.authority = authority
        self.permissions = permissions
        self.cache_dir = cache_dir
        self.cache_quota_bytes = cache_quota_bytes
        self.clock = clock
        self._roots: set[int] = set()
        self._roots_checked = -math.inf
        self._report: RunReport | None = None
        # The media index's own data folder: never an input.
        self._excluded: tuple[Path, ...] = tuple(excluded) or (cache_dir.parent,)
        self.monotonic = monotonic
        self._watch: Watch | None = None
        self.embedder = embedder  # the runtime's bge-m3 embedder (semantic stage) or None

    def _admit_now(self, path: Path) -> Path:
        """Admit a copy immediately before a decoder opens it: scope, and a fresh gate answer.

        The root asked is the root of *this* copy, and that root is bound to
        the current watch, so its revocation stops the work in progress.
        """
        row = self.db.execute("SELECT root_id FROM files WHERE path = ? AND status = 'present'",
                              (str(path),)).fetchone()
        scope = load_scopes(self.db).get(int(row["root_id"])) if row is not None else None
        if scope is None or not self.root_allowed_now(scope.root_id):
            raise OutOfScope("not_allowed")
        admitted = admit(scope, path, self._excluded)
        if self._watch is not None:
            self._watch.bind(scope.root_id)
        return admitted

    # ---- control as seen by other processes

    def paused(self) -> bool:
        return self.store.get_meta("paused", "0") == "1"

    def _state(self, asset_id: str, stage: str) -> str | None:
        row = self.db.execute("SELECT state FROM stages WHERE asset_id = ? AND stage = ?", (asset_id, stage)).fetchone()
        return row[0] if row else None

    def recover_interrupted(self) -> int:
        with self.store.transaction() as db:
            return db.execute(
                "UPDATE stages SET state = 'QUEUED', note = 'interrupted' WHERE state = 'RUNNING'"
            ).rowcount

    def _ask(self, row: sqlite3.Row) -> bool:
        approval = Approval(row["approval_action_id"], row["approval_decision"], row["approved_at"])
        try:
            self.permissions.recheck_root(Path(row["path"]), approval, row["kind"], row["max_depth"])
        except MediaPermissionRefused as exc:
            if self._report is not None:
                self._report.refused_roots[row["path"]] = exc.reason
            return False
        return True

    def allowed_roots(self, *, max_age: float = REVOCATION_CHECK_SECONDS) -> set[int]:
        """Roots the gate allows, from an answer no older than ``max_age`` seconds (0 = ask now)."""
        now = self.monotonic()
        if max_age > 0 and now - self._roots_checked < max_age:
            return set(self._roots)
        allowed = {int(row["root_id"]) for row in self.db.execute("SELECT * FROM roots WHERE enabled = 1").fetchall()
                   if self._ask(row)}
        self._roots, self._roots_checked = allowed, now
        return set(allowed)

    def root_allowed_now(self, root_id: int) -> bool:
        """A fresh gate answer for one root, used at every read handoff (no cache)."""
        row = self.db.execute("SELECT * FROM roots WHERE root_id = ? AND enabled = 1", (int(root_id),)).fetchone()
        allowed = row is not None and self._ask(row)
        if allowed:
            self._roots.add(int(root_id))
        else:
            self._roots.discard(int(root_id))
        return allowed

    def _roots_sql(self) -> tuple[str, list[int]]:
        roots = sorted(self.allowed_roots())
        if not roots:
            return " AND 0", []
        return f" AND f.root_id IN ({','.join('?' for _ in roots)})", roots

    # ---- identity work

    def _hash_pending(self, deadline: float, report: RunReport) -> bool:
        """Hash files found by scan. Returns False if the run must stop."""
        skipped: set[str] = set()
        while self.monotonic() < deadline:
            if self.paused():
                report.stopped = "paused"
                return False
            clause, params = self._roots_sql()
            rows = self.db.execute(
                "SELECT f.* FROM files f WHERE f.status = 'pending_identity'" + clause +
                " ORDER BY f.seen_at, f.path LIMIT 50", params,
            ).fetchall()
            row = next((r for r in rows if r["path"] not in skipped), None)
            if row is None:
                return True
            # The row is a record, not permission: the path must be inside its
            # root's current scope before it is even stat'ed for hashing.
            scope = load_scopes(self.db).get(int(row["root_id"]))
            try:
                if scope is None:
                    raise OutOfScope("no_root")
                admit(scope, row["path"], self._excluded)
            except OutOfScope as exc:
                skipped.add(row["path"])
                report.out_of_scope.append(f"{row['path']}:{exc.reason}")
                with self.store.transaction() as db:
                    mark_out_of_scope(db, [row["path"]])
                continue
            # A fresh gate answer for this file's own root, right before the read.
            if not self.root_allowed_now(int(row["root_id"])):
                skipped.add(row["path"])
                continue
            try:
                lease = self.authority.reserve(f"media:identity:{row['path']}", StageNeeds("identity"))
            except ResourceRefused as exc:
                report.stopped, report.detail = "deferred", str(exc)
                return False
            holder = [lease]
            watch = Watch(self, deadline=deadline, holder=holder, roots={int(row["root_id"])})
            try:
                path = Path(row["path"])
                try:
                    before = FileState.of(path)
                    if (before.size, before.mtime_ns, before.file_id) != (row["size"], row["mtime_ns"], row["file_id"]):
                        raise OSError("file changed after the scan; rescan to pick it up")
                    digest = full_sha256(path, cancelled=watch.poll)
                    if FileState.of(path) != before:
                        raise OSError("file changed while hashing")
                except HashCancelled:
                    report.stopped = (watch.reason or "paused").split(":")[0]
                    report.detail = watch.reason or ""
                    return False
                except OSError as exc:
                    skipped.add(row["path"])
                    with self.store.transaction() as db:
                        db.execute("UPDATE files SET status = 'failed', error = ? WHERE path = ?",
                                   (f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS], row["path"]))
                    continue
                asset_id = asset_id_for(digest)
                now = self.clock()
                with self.store.transaction() as db:
                    db.execute(
                        "INSERT INTO assets(asset_id, sha256, size, created_at, updated_at) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(asset_id) DO UPDATE SET updated_at = excluded.updated_at",
                        (asset_id, digest, before.size, now, now),
                    )
                    db.execute(
                        "UPDATE files SET asset_id = ?, status = 'present', error = NULL, hashed_size = ?, "
                        "hashed_mtime_ns = ?, hashed_file_id = ? WHERE path = ?",
                        (asset_id, before.size, before.mtime_ns, before.file_id, row["path"]),
                    )
                    copies, _ = admitted_files(db, asset_id, None, self._excluded)
                    ensure_stages(db, asset_id, {"subtitles": subtitles_input_fingerprint(copies, self._excluded)}, now)
                report.hashed += 1
            finally:
                self.authority.release(holder[0])
        report.stopped = "time_budget"
        return False

    # ---- stage work

    def _next_stage(self, skip: set[str], skip_pairs: frozenset | set = frozenset()) -> sqlite3.Row | None:
        clause, params = self._roots_sql()
        rows = self.db.execute(
            "SELECT s.asset_id, s.stage, s.checkpoint FROM stages s JOIN assets a ON a.asset_id = s.asset_id "
            "WHERE s.state = 'QUEUED' AND EXISTS (SELECT 1 FROM files f WHERE f.asset_id = s.asset_id "
            "AND f.status = 'present'" + clause + ") ORDER BY a.priority DESC, s.queued_at, s.asset_id LIMIT 500",
            params,
        ).fetchall()
        order = list(STAGES)
        rows = sorted(rows, key=lambda row: order.index(row["stage"]) if row["stage"] in order else 99)
        states_by_asset: dict[str, dict[str, str]] = {}
        for row in rows:
            spec = STAGES.get(row["stage"])
            if spec is None or row["asset_id"] in skip or (row["asset_id"], row["stage"]) in skip_pairs:
                continue
            states = states_by_asset.setdefault(row["asset_id"], {
                r["stage"]: r["state"] for r in self.db.execute(
                    "SELECT stage, state FROM stages WHERE asset_id = ?", (row["asset_id"],))
            })
            if all(states.get(dep) == "COMPLETED" for dep in spec.depends):
                return row
        return None

    def run(self, *, max_seconds: float = 60.0, max_stages: int = 100) -> RunReport:
        report = RunReport()
        self._report = report
        self._roots_checked = -math.inf
        deadline = self.monotonic() + max(0.1, max_seconds)
        if self.paused():
            report.stopped = "paused"
            return report
        if not self._hash_pending(deadline, report):
            return report
        with self.store.transaction() as db:
            backfill_stages(db, self.clock())
        skip: set[str] = set()
        skip_pairs: set[tuple[str, str]] = set()
        while len(report.completed) + len(report.failed) < max_stages:
            if self.monotonic() >= deadline:
                report.stopped = "time_budget"
                return report
            if self.paused():
                report.stopped = "paused"
                return report
            row = self._next_stage(skip, skip_pairs)
            if row is None:
                report.stopped = "idle"
                return report
            outcome = self._run_one(row["asset_id"], row["stage"], row["checkpoint"], report, deadline)
            if outcome == "unavailable":
                skip_pairs.add((row["asset_id"], row["stage"]))
                continue
            if outcome in {"deferred", "stop"}:
                return report
            if outcome in {"identity_changed", "no_verified_file", "cancelled", "refused"}:
                skip.add(row["asset_id"])
        report.stopped = "stage_budget"
        return report

    def _verified(self, asset_id: str) -> list[Path]:
        # A stage is a read handoff: ask the gate fresh for every root.
        good, stale, outside = verified_files(self.db, asset_id, self.allowed_roots(max_age=0), self._excluded)
        if stale or outside:
            with self.store.transaction() as db:
                mark_changed(db, stale)
                mark_out_of_scope(db, outside)
        if self._report is not None:
            self._report.out_of_scope.extend(f"{path}:{OUT_OF_SCOPE}" for path in outside)
        return good

    def _run_one(self, asset_id: str, stage: str, checkpoint: str | None, report: RunReport,
                 deadline: float) -> str:
        spec = STAGES[stage]
        key = f"{asset_id}/{stage}"
        files = self._verified(asset_id)
        if not files:
            # Every allowed copy changed since hashing; they are queued for a
            # new hash and this asset waits. Nothing attaches to the old hash.
            report.identity_changed.append(key)
            return "no_verified_file"
        task = f"media:{stage}:{asset_id}"
        skip_check = SKIPS.get(stage)
        skipped = skip_check(self.db, asset_id) if skip_check is not None else None
        if skipped:
            # Nothing to do for this asset (for ASR: subtitles exist, or no audio). Completed without any
            # resource reservation; the stage's own cleanup removes rows that no longer apply.
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE stages SET state = 'COMPLETED', finished_at = ?, checkpoint = NULL, note = ?, error = NULL "
                    "WHERE asset_id = ? AND stage = ? AND state = 'QUEUED'",
                    (self.clock(), f"skipped:{skipped}", asset_id, stage))
                cleanup = SKIP_CLEANUP.get(stage)
                if cleanup is not None:
                    cleanup(db, asset_id)
            report.completed.append(key)
            return "completed"
        sidecar_binding = None
        if getattr(spec.needs.model, "served_by", "") == "memory_sidecar":
            # No runtime embedder in this process, or no owned sidecar process behind it right now: nothing could
            # run, so no model slot is reserved for it (a reservation would claim a model that was never loaded).
            # Otherwise the sidecar generation (owner + lifecycle capability) is captured ONCE, here, for this run.
            missing = "model_unavailable" if self.embedder is None else None
            if missing is None:
                try:
                    sidecar_binding = self.embedder.bind()
                except ResourceRefused as refused:
                    missing = refused.reason
            if missing is not None:
                with self.store.transaction() as db:
                    db.execute("UPDATE stages SET note = ? WHERE asset_id = ? AND stage = ? AND state = 'QUEUED'",
                               (f"unavailable:{missing}", asset_id, stage))
                report.refused.append(f"{key}:{missing}")
                return "unavailable"
        try:
            lease = self.authority.reserve(task, spec.needs)
        except ResourceRefused as exc:
            if exc.reason in UNAVAILABLE_REASONS:
                with self.store.transaction() as db:
                    db.execute("UPDATE stages SET note = ? WHERE asset_id = ? AND stage = ? AND state = 'QUEUED'",
                               (f"unavailable:{exc.reason}", asset_id, stage))
                report.refused.append(f"{key}:{exc.reason}")
                return "unavailable"
            report.stopped, report.detail = "deferred", str(exc)
            return "deferred"
        holder = [lease]
        # Roots are bound as copies are actually opened (_admit_now).
        watch = Watch(self, deadline=deadline, holder=holder, roots=set(), job=(asset_id, stage))
        self._watch = watch
        try:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE stages SET state = 'RUNNING', attempts = attempts + 1, started_at = ?, error = NULL "
                    "WHERE asset_id = ? AND stage = ? AND state = 'QUEUED'",
                    (self.clock(), asset_id, stage),
                )

            def save(data: dict, write: Callable[[sqlite3.Connection], None] | None = None) -> None:
                # ``write`` (chunk results) commits in the SAME transaction as the checkpoint.
                with self.store.transaction() as db:
                    if write is not None:
                        running = db.execute(
                            "SELECT 1 FROM stages WHERE asset_id = ? AND stage = ? AND state = 'RUNNING'",
                            (asset_id, stage)).fetchone()
                        if running is None:  # cancelled elsewhere: no chunk result outlives its job
                            raise _Superseded()
                        write(db)
                    db.execute("UPDATE stages SET checkpoint = ? WHERE asset_id = ? AND stage = ? AND state = 'RUNNING'",
                               (json.dumps(data), asset_id, stage))

            def renew() -> None:
                holder[0] = self.authority.renew(holder[0])

            ctx = StageContext(
                db=self.db, asset_id=asset_id, files=files, tools=self.tools,
                cache_dir=self.cache_dir, cache_quota_bytes=self.cache_quota_bytes,
                cancelled=watch.poll, renew=renew, save_checkpoint=save,
                checkpoint=json.loads(checkpoint) if checkpoint else {},
                interrupted=watch.now,
                excluded=self._excluded,
                admit=self._admit_now,
                lease=lambda: holder[0],
                embedder=self.embedder,
                sidecar_binding=sidecar_binding,
            )
            try:
                commit = RUNNERS[stage](ctx)
            except ChildCancelled:
                return self._interrupted(asset_id, stage, report, watch.reason)
            except OutOfScope as exc:
                # A copy could not be admitted at a read handoff (its root was
                # revoked or its approval expired, or it left scope): refused,
                # requeued with the reason, never a crash or a stuck RUNNING row.
                self._set(asset_id, stage, "QUEUED", note=f"refused:{exc.reason}")
                report.refused.append(f"{key}:{exc.reason}")
                permission = exc.reason in {"not_allowed", "no_admitted_copy"}
                report.stopped = "permission" if permission else "out_of_scope"
                report.detail = f"refused:{exc.reason}"
                return "stop"
            except ResourceRefused as exc:
                if exc.reason in UNAVAILABLE_REASONS:
                    self._set(asset_id, stage, "QUEUED", note=f"unavailable:{exc.reason}")
                    report.refused.append(f"{key}:{exc.reason}")
                    return "unavailable"
                self._set(asset_id, stage, "QUEUED", note=f"deferred:{exc.reason}")
                report.stopped, report.detail = "deferred", str(exc)
                return "deferred"
            except (StageFailed, MediaToolError, OSError, ValueError) as exc:
                self._set(asset_id, stage, "FAILED", error=f"{type(exc).__name__}: {exc}")
                report.failed.append(key)
                return "failed"
            # The decoder opened files by name; make sure they were still the hashed ones.
            _, stale, _ = verified_files(self.db, asset_id, None, self._excluded)
            if any(Path(path) in files for path in stale):
                with self.store.transaction() as db:
                    mark_changed(db, stale)
                    db.execute(
                        "UPDATE stages SET state = 'QUEUED', checkpoint = NULL, note = 'identity_changed' "
                        "WHERE asset_id = ? AND stage = ? AND state = 'RUNNING'", (asset_id, stage))
                report.identity_changed.append(key)
                return "identity_changed"
            # Last look before anything is committed: work finished under a
            # tripped watch (pause, deadline, lost lease) or after any bound
            # root lost permission (asked fresh here) is requeued, not kept.
            if watch.now() or (watch.roots and not watch.roots <= self.allowed_roots(max_age=0)):
                return self._interrupted(asset_id, stage, report, watch.reason or "permission")
            with self.store.transaction() as db:
                current = db.execute("SELECT state FROM stages WHERE asset_id = ? AND stage = ?",
                                     (asset_id, stage)).fetchone()
                if current is None or current[0] != "RUNNING":
                    raise _Superseded()
                commit(db)
                db.execute(
                    "UPDATE stages SET state = 'COMPLETED', finished_at = ?, checkpoint = NULL, note = NULL "
                    "WHERE asset_id = ? AND stage = ?",
                    (self.clock(), asset_id, stage),
                )
            report.completed.append(key)
            return "completed"
        except _Superseded:
            return self._interrupted(asset_id, stage, report, "cancelled")
        finally:
            self._watch = None
            self.authority.release(holder[0])

    def _interrupted(self, asset_id: str, stage: str, report: RunReport, reason: str | None) -> str:
        """A child was stopped mid-stage. Requeue with the checkpoint kept, and say why."""
        if self._state(asset_id, stage) == "RUNNING":
            why = reason if reason and reason != "cancelled" else "paused"
            self._set(asset_id, stage, "QUEUED", note=why)
            report.stopped, report.detail = why.split(":")[0], why
            return "stop"
        # Cancelled or paused per job by another process: its row already says
        # so; the run carries on with other work.
        return "cancelled"

    def _set(self, asset_id: str, stage: str, state: str, *, error: str | None = None, note: str | None = None) -> None:
        with self.store.transaction() as db:
            db.execute(
                "UPDATE stages SET state = ?, error = ?, note = COALESCE(?, note), finished_at = ? "
                "WHERE asset_id = ? AND stage = ? AND state = 'RUNNING'",
                (state, error[:MAX_ERROR_CHARS] if error else None, note, self.clock(), asset_id, stage),
            )


class _Superseded(Exception):
    pass
