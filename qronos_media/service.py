"""The media service contract (handoff s50, s51).

Every method takes and returns plain JSON-able values, so the same object
backs the command-line tool and, once Root leases the hook, the backend IPC
service. Method names map one-to-one onto the handoff's ``media.*`` commands.

Searching opens the index read-only and never scans, hashes or decodes.
Indexing happens only in :meth:`MediaService.index`, under the runner lock
and the resource authority.

Permission is never a caller's say-so (:mod:`qronos_media.permissions`).
Adding a root and exporting a report either run because the user's Files
policy allows them, or return ``approval_required`` with a single-use id for
the exact prepared action; :meth:`approve_root` and :meth:`approve_export`
consume that id once. Every scan and index run asks the gate again.

Everything the service writes or deletes lives under its data folder, which
must not sit behind a link or junction, and every cache path is checked with
:func:`qronos_media.paths.owned_child` before use. Reports kept there are
Qronos-owned state, pruned to the newest :data:`MAX_CACHED_REPORTS`.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
from typing import Any, Callable

from qronos_media.identity import FileState, HashCancelled, checked_asset_id, full_sha256
from qronos_media.jobs import (
    RunnerBusy, RunnerLock, StageRunner, Watch, admitted_files, ensure_stages, mark_changed, mark_out_of_scope,
    requeue, verified_files,
)
from qronos_media.paths import UnsafeMediaPath, has_link_component, local_path, owned_child
from qronos_media.permissions import Approval, MediaPermissionRefused, MediaPermissions, Pending
from qronos_media.reports import FORMATS, KINDS, gather, levels, render, write_report
from qronos_media.resources import MediaResourceAuthority, ResourceRefused, StageNeeds
from qronos_media.scope import OUT_OF_SCOPE, RootScope, mark, outside_scope, root_inside_state
from qronos_media.scan import DEFAULT_MAX_DEPTH, apply, collect, validate_root
from qronos_media.search import search_dialogue
from qronos_media.stages import PENDING_CAPABILITIES, STAGES, subtitles_input_fingerprint
from qronos_media.store import MediaStore, open_read_only
from qronos_media.tools import MediaTools

DEFAULT_CACHE_QUOTA_BYTES = 512 * 1024**2
#: Strict verification is a full hash; these bound one interactive call.
DEFAULT_VERIFY_SECONDS = 30.0
MAX_VERIFY_SECONDS = 600.0
MAX_CACHED_REPORTS = 50
_PLAYERS = {"mpv": "--start={seconds}", "vlc": "--start-time={seconds}"}


class MediaRequestError(ValueError):
    """A request was refused before doing anything."""


@dataclass(frozen=True)
class MediaLayout:
    root: Path

    @property
    def database(self) -> Path:
        return self.root / "media-index.sqlite3"

    @property
    def lock(self) -> Path:
        return self.root / "runner.lock"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def exports(self) -> Path:
        return self.root / "exports"


def _pending_json(pending: Pending) -> dict[str, Any]:
    return {"status": "approval_required", "approvalId": pending.approval_id, "decision": pending.decision,
            "preview": pending.preview}


def _decision(value: object):
    from security.permissions import PermissionDecision

    try:
        return PermissionDecision(str(value))
    except ValueError:
        raise MediaRequestError("unknown approval decision") from None


class MediaService:
    def __init__(
        self,
        data_dir: Path,
        *,
        tools: MediaTools | None,
        authority: MediaResourceAuthority,
        permissions: MediaPermissions,
        cache_quota_bytes: int = DEFAULT_CACHE_QUOTA_BYTES,
        monotonic: Callable[[], float] = time.monotonic,
        admit_store: Callable[[Path], object] | None = None,
        embedder: object = None,
    ) -> None:
        self.layout = MediaLayout(local_path(data_dir))
        if admit_store is not None:
            # The product entry points (qronos_media.cli, qronos_media.host) admit the ACTUAL folder through
            # core.startup_admission.enter_for_store before anything is created or opened; a refusal raises here.
            admit_store(self.layout.root)
        if has_link_component(self.layout.root):
            raise UnsafeMediaPath("the media data folder must not be behind a link or junction")
        self.layout.root.mkdir(parents=True, exist_ok=True)
        for owned in (self.layout.cache, self.layout.exports):
            if os.path.lexists(owned):
                owned_child(self.layout.root, owned.name)
        self.store = MediaStore(self.layout.database)
        self.tools = tools
        self.authority = authority
        self.permissions = permissions
        self.cache_quota_bytes = int(cache_quota_bytes)
        # The index's own data folder is never an input (database, cache, reports).
        self._excluded: tuple[Path, ...] = (self.layout.root,)
        self._monotonic = monotonic
        # The runtime's bge-m3 embedder (qronos_media.semantic.MediaEmbedder) or None: semantic stays unavailable.
        self.embedder = embedder

    def _runner(self) -> StageRunner:
        return StageRunner(self.store, tools=self.tools, authority=self.authority, permissions=self.permissions,
                           cache_dir=self.layout.cache, cache_quota_bytes=self.cache_quota_bytes,
                           excluded=self._excluded, monotonic=self._monotonic, embedder=self.embedder)

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------- roots

    def add_root(self, path: str, *, kind: str = "folder", max_depth: int = DEFAULT_MAX_DEPTH) -> dict[str, Any]:
        """Record a folder or file the user selected, if the Files permission allows it."""
        if kind not in {"folder", "file"}:
            raise MediaRequestError("kind must be folder or file")
        if not (0 <= int(max_depth) <= 64):
            raise MediaRequestError("max_depth must be between 0 and 64")
        root = local_path(path)
        validate_root(root, kind)
        if root_inside_state(root, self._excluded):
            raise MediaRequestError("that location is the media index's own data folder; choose another")
        decided = self.permissions.request_root(root, kind, int(max_depth))
        if isinstance(decided, Pending):
            return _pending_json(decided)
        return self._record_root(root, kind, int(max_depth), decided)

    def approve_root(self, approval_id: str, decision: str, *, secret: str | None = None) -> dict[str, Any]:
        """Consume the single-use approval for one prepared root, exactly as prepared.

        ``secret`` is only for a typed-secret policy and is passed in-process;
        it is never stored or echoed.
        """
        payload, approval = self.permissions.approve_root(approval_id, _decision(decision), secret)
        root = local_path(payload["path"])
        validate_root(root, payload["kind"])  # the folder may have changed while the question was open
        if root_inside_state(root, self._excluded):
            raise MediaRequestError("that location is the media index's own data folder; choose another")
        return self._record_root(root, payload["kind"], int(payload["maxDepth"]), approval)

    def _record_root(self, root: Path, kind: str, max_depth: int, approval: Approval) -> dict[str, Any]:
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO roots(path, kind, approval_action_id, approval_decision, approved_at, max_depth) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                "approval_action_id = excluded.approval_action_id, approval_decision = excluded.approval_decision, "
                "approved_at = excluded.approved_at, max_depth = excluded.max_depth, kind = excluded.kind, enabled = 1",
                (str(root), kind, approval.action_id, approval.decision, approval.approved_at, max_depth),
            )
            row = db.execute("SELECT * FROM roots WHERE path = ?", (str(root),)).fetchone()
            candidates = [r["path"] for r in db.execute(
                "SELECT path FROM files WHERE root_id = ? AND NOT (status = 'missing' AND error = ?)",
                (row["root_id"], OUT_OF_SCOPE))]
        # A re-approval may narrow the scope (kind or depth): rows outside it stop
        # being readable now, not at the next scan. Path checks run with no
        # transaction open.
        failing = outside_scope(candidates, RootScope.from_row(row), self._excluded)
        with self.store.transaction() as db:
            narrowed = mark(db, failing)
        return {"status": "added", **self._root_json(row), "outOfScope": narrowed}

    def remove_root(self, root_id: int) -> dict[str, Any]:
        """Forget a root and every asset that no other root still has a file for.

        Every cache folder to delete is validated first; a malformed asset id
        in the index refuses the whole removal before anything is deleted.
        """
        db = self.store.db
        row = db.execute("SELECT * FROM roots WHERE root_id = ?", (int(root_id),)).fetchone()
        if row is None:
            raise MediaRequestError("unknown root")
        candidates = [r[0] for r in db.execute(
            "SELECT DISTINCT asset_id FROM files WHERE root_id = ? AND asset_id IS NOT NULL", (row["root_id"],))]
        orphaned = [a for a in candidates if db.execute(
            "SELECT 1 FROM files WHERE asset_id = ? AND root_id != ? LIMIT 1", (a, row["root_id"])).fetchone() is None]
        targets = []
        try:
            for asset_id in orphaned:
                targets.append(owned_child(self.layout.cache, "keyframes", checked_asset_id(asset_id)))
        except (ValueError, UnsafeMediaPath) as exc:
            raise MediaRequestError(f"refused: {exc}; nothing was removed") from None
        with self.store.transaction() as tx:
            tx.execute("DELETE FROM files WHERE root_id = ?", (row["root_id"],))
            for asset_id in orphaned:
                tx.execute("DELETE FROM dialogue WHERE asset_id = ?", (asset_id,))
                tx.execute("DELETE FROM assets WHERE asset_id = ?", (asset_id,))
            tx.execute("DELETE FROM roots WHERE root_id = ?", (row["root_id"],))
        failed = []
        for target in targets:
            if not os.path.lexists(target):
                continue
            try:
                owned_child(self.layout.cache, "keyframes", target.name)  # re-check right before deleting
                shutil.rmtree(target)
            except (OSError, UnsafeMediaPath) as exc:
                failed.append({"path": str(target), "error": type(exc).__name__})
        return {"removedRoot": row["path"], "removedAssets": orphaned, "cacheCleanupFailed": failed}

    def list_roots(self) -> list[dict[str, Any]]:
        return [self._root_json(row) for row in self.store.db.execute("SELECT * FROM roots ORDER BY root_id")]

    @staticmethod
    def _root_json(row: sqlite3.Row) -> dict[str, Any]:
        return {"rootId": row["root_id"], "path": row["path"], "kind": row["kind"],
                "approval": {"actionId": row["approval_action_id"], "decision": row["approval_decision"],
                             "approvedAt": row["approved_at"]},
                "maxDepth": row["max_depth"], "lastScanAt": row["last_scan_at"]}

    # -------------------------------------------------------------- scan

    def scan(self, root_id: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM roots WHERE enabled = 1" + (" AND root_id = ?" if root_id is not None else "")
        roots = self.store.db.execute(query, (() if root_id is None else (int(root_id),))).fetchall()
        if root_id is not None and not roots:
            raise MediaRequestError("unknown root")
        summaries = []
        for root in roots:
            approval = Approval(root["approval_action_id"], root["approval_decision"], root["approved_at"])
            try:
                self.permissions.recheck_root(Path(root["path"]), approval, root["kind"], root["max_depth"])
            except MediaPermissionRefused as exc:
                summaries.append({"root_id": root["root_id"], "root": root["path"], "refused": exc.reason,
                                  "detail": str(exc)})
                continue
            summary, entries = collect(root, excluded=self._excluded)
            summaries.append(apply(self.store.transaction, root, summary, entries, excluded=self._excluded).as_json())
        return summaries

    # ------------------------------------------------------------- index

    def index(self, *, max_seconds: float = 60.0, max_stages: int = 100) -> dict[str, Any]:
        runner = self._runner()
        lock = RunnerLock(self.layout.lock)
        try:
            lock.acquire()
        except RunnerBusy as exc:
            return {"stopped": "busy", "detail": str(exc)}
        try:
            recovered = runner.recover_interrupted()
            report = runner.run(max_seconds=max_seconds, max_stages=max_stages).as_json()
            report["recoveredInterrupted"] = recovered
            return report
        finally:
            lock.release()

    def pause(self) -> dict[str, Any]:
        self.store.set_meta("paused", "1")
        return {"paused": True}

    def resume(self) -> dict[str, Any]:
        self.store.set_meta("paused", "0")
        return {"paused": False}

    def _job_transition(self, asset_id: str, stage: str, allowed: tuple[str, ...], target: str,
                        note: str) -> dict[str, Any]:
        if stage not in STAGES:
            raise MediaRequestError("unknown stage")
        placeholders = ",".join("?" for _ in allowed)
        with self.store.transaction() as db:
            changed = db.execute(
                f"UPDATE stages SET state = ?, note = ?, queued_at = ? WHERE asset_id = ? AND stage = ? "
                f"AND state IN ({placeholders})",
                (target, note, time.time(), asset_id, stage, *allowed),
            ).rowcount
            row = db.execute("SELECT state FROM stages WHERE asset_id = ? AND stage = ?", (asset_id, stage)).fetchone()
        if row is None:
            raise MediaRequestError("unknown job")
        return {"assetId": asset_id, "stage": stage, "changed": bool(changed), "state": row[0]}

    def cancel_job(self, asset_id: str, stage: str) -> dict[str, Any]:
        return self._job_transition(asset_id, stage, ("QUEUED", "RUNNING", "PAUSED"), "CANCELLED", "cancelled by user")

    def pause_job(self, asset_id: str, stage: str) -> dict[str, Any]:
        return self._job_transition(asset_id, stage, ("QUEUED", "RUNNING"), "PAUSED", "paused by user")

    def resume_job(self, asset_id: str, stage: str) -> dict[str, Any]:
        return self._job_transition(asset_id, stage, ("PAUSED",), "QUEUED", "resumed by user")

    def retry(self, asset_id: str, stage: str) -> dict[str, Any]:
        return self._job_transition(asset_id, stage, ("FAILED", "CANCELLED"), "QUEUED", "retry requested")

    def reindex(self, asset_id: str, stage: str) -> dict[str, Any]:
        if stage not in STAGES:
            raise MediaRequestError("unknown stage")
        if self.store.db.execute("SELECT 1 FROM assets WHERE asset_id = ?", (asset_id,)).fetchone() is None:
            raise MediaRequestError("unknown asset")
        copies, _ = admitted_files(self.store.db, asset_id, None, self._excluded)
        fingerprint = subtitles_input_fingerprint(copies, self._excluded)
        with self.store.transaction() as db:
            ensure_stages(db, asset_id, {"subtitles": fingerprint})
            affected = requeue(db, asset_id, stage, "reindex requested")
        return {"assetId": asset_id, "requeued": affected}

    def prioritize(self, asset_id: str, priority: int = 10) -> dict[str, Any]:
        with self.store.transaction() as db:
            changed = db.execute("UPDATE assets SET priority = ? WHERE asset_id = ?",
                                 (max(-100, min(100, int(priority))), asset_id)).rowcount
        if not changed:
            raise MediaRequestError("unknown asset")
        return {"assetId": asset_id, "priority": priority}

    # ------------------------------------------------------------ status

    def status(self, asset_id: str | None = None) -> dict[str, Any]:
        db = self.store.db
        if asset_id is not None:
            if db.execute("SELECT 1 FROM assets WHERE asset_id = ?", (asset_id,)).fetchone() is None:
                raise MediaRequestError("unknown asset")
            return {"assetId": asset_id, **levels(db, asset_id)}
        count = lambda sql, *args: {row[0]: row[1] for row in db.execute(sql, args)}  # noqa: E731
        return {
            "paused": self.store.get_meta("paused", "0") == "1",
            "roots": db.execute("SELECT COUNT(*) FROM roots").fetchone()[0],
            "assets": db.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
            "files": count("SELECT status, COUNT(*) FROM files GROUP BY status"),
            "stages": {name: count("SELECT state, COUNT(*) FROM stages WHERE stage = ? GROUP BY state", name)
                       for name in STAGES},
            "dialogueCues": db.execute("SELECT COUNT(*) FROM dialogue WHERE kind = 'cue'").fetchone()[0],
            "scenes": db.execute("SELECT COUNT(*) FROM scenes").fetchone()[0],
            "keyframeBytes": db.execute("SELECT COALESCE(SUM(bytes), 0) FROM keyframes").fetchone()[0],
            "cacheQuotaBytes": self.cache_quota_bytes,
            "toolsAvailable": self.tools is not None,
            "pending": list(PENDING_CAPABILITIES),
        }

    # ------------------------------------------------------------ search

    def search_semantic(self, query: str, *, limit: int = 10, language: str | None = None,
                        asset_id: str | None = None, budget_seconds: float = 3.0) -> dict[str, Any]:
        """Exact dialogue first (lexical), then bge-m3 semantic matches as a separate, uncalibrated block.

        The query vector needs the model, so it goes through the same authority as indexing (one declared
        model slot; busy or unavailable is reported and the lexical answer still returned). The index is
        read through a read-only connection; nothing is scanned, hashed or decoded.
        """
        from qronos_media import semantic
        from qronos_media.resources import BGE_M3
        from qronos_media.search import Budget, BudgetExhausted, _evidence

        if not isinstance(query, str) or not query.strip() or len(query) > semantic.MAX_TEXT_CHARS:
            raise MediaRequestError("query must be 1..1200 characters")
        limit = max(1, min(50, int(limit)))
        budget_seconds = max(0.2, min(10.0, float(budget_seconds)))
        exact = self.search_dialogue(query, mode="exact", limit=limit, language=language, asset_id=asset_id)
        block: dict[str, Any] = {"status": "unavailable", "results": [], "scoreNote": semantic.SCORE_NOTE,
                                 "queryScript": semantic.script_of(query)}
        if self.embedder is None:
            block["reason"] = "no bge-m3 sidecar capability in this process"
            return {"query": query, "exact": exact, "semantic": block}
        try:
            # No owned sidecar now: refused before any reservation. Otherwise ONE generation is captured here.
            binding = self.embedder.bind()
        except ResourceRefused as exc:
            block["reason"] = exc.reason
            return {"query": query, "exact": exact, "semantic": block}
        try:
            lease = self.authority.reserve(f"media:semantic_query:{time.monotonic_ns()}",
                                           StageNeeds("semantic_query", model=BGE_M3))
        except ResourceRefused as exc:
            block["reason"] = exc.reason
            return {"query": query, "exact": exact, "semantic": block}
        try:
            if hasattr(lease, "bind_capability"):
                lease.bind_capability(binding.capability)
            vector = self.embedder.embed([query.strip()], timeout=semantic.QUERY_TIMEOUT_SECONDS,
                                         on_dispatch=getattr(lease, "mark_dispatched", None), binding=binding)[0]
        except (ResourceRefused, semantic.EmbeddingInvalid) as exc:
            if getattr(exc, "reason", "") == "embedding_request_failed" and hasattr(lease, "mark_unresolved"):
                lease.mark_unresolved()
            block["reason"] = getattr(exc, "reason", "embedding_invalid")
            return {"query": query, "exact": exact, "semantic": block}
        finally:
            self.authority.release(lease)  # the synchronous request has returned: drained
        try:
            db = open_read_only(self.layout.database)
        except FileNotFoundError:
            raise MediaRequestError("the media index is empty") from None
        budget = Budget(budget_seconds, self._excluded)
        try:
            try:
                found, scan = semantic.semantic_candidates(db, vector, limit=limit, language=language,
                                                           asset_id=asset_id, check=budget.check)
                results = []
                for row in found:
                    item = {"row": {"asset_id": row["asset_id"], "track_id": row["track_id"],
                                    "start_ms": row["start_ms"], "end_ms": row["end_ms"], "text": row["text"],
                                    "kind": "window"},
                            "match": "semantic", "score": row["score"], "scoreKind": "cosine_uncalibrated"}
                    evidence = _evidence(db, item, budget)
                    evidence.pop("lexicalScore", None)
                    evidence.update({"similarity": row["score"], "seqFrom": row["seq_from"], "seqTo": row["seq_to"],
                                     "windowScript": semantic.script_of(row["text"]),
                                     "crossScript": semantic.script_of(row["text"]) != block["queryScript"]})
                    results.append(evidence)
                block.update({"status": "partial" if scan["truncated"] else "complete", "results": results,
                              "scanned": scan["scanned"]})
            except BudgetExhausted as exc:
                block.update({"status": "partial", "stoppedBy": exc.reason})
        finally:
            db.close()
        return {"query": query, "exact": exact, "semantic": block}

    def search_dialogue(self, query: str, **options: Any) -> dict[str, Any]:
        try:
            db = open_read_only(self.layout.database)
        except FileNotFoundError:
            raise MediaRequestError("the media index is empty") from None
        try:
            return search_dialogue(db, query, excluded=self._excluded, **options)
        finally:
            db.close()

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        db = self.store.db
        asset = db.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
        if asset is None:
            raise MediaRequestError("unknown asset")
        result = {key: asset[key] for key in ("asset_id", "sha256", "size", "duration_ms", "container", "title")}
        result["files"] = [dict(row) for row in db.execute(
            "SELECT path, status FROM files WHERE asset_id = ? ORDER BY path", (asset_id,))]
        result["streams"] = [dict(row) for row in db.execute(
            "SELECT * FROM streams WHERE asset_id = ? ORDER BY idx", (asset_id,))]
        result["subtitleTracks"] = [dict(row) for row in db.execute(
            "SELECT track_id, source, language, codec, status, detail, cue_count FROM subtitle_tracks "
            "WHERE asset_id = ? ORDER BY track_id", (asset_id,))]
        result["sceneCount"] = db.execute("SELECT COUNT(*) FROM scenes WHERE asset_id = ?", (asset_id,)).fetchone()[0]
        result["possibleVariants"] = self.possible_variants(asset_id)
        result.update(levels(db, asset_id))
        return result

    def possible_variants(self, asset_id: str) -> list[dict[str, Any]]:
        """Other assets that look like another cut or release of the same film.

        A hint from file names and container titles only. Variants stay
        separate assets with their own timestamps (handoff s40).
        """
        def key(text: str | None) -> str:
            words = re.sub(r"[\W_]+", " ", (text or "").casefold()).split()
            noise = {"1080p", "720p", "2160p", "4k", "x264", "x265", "hevc", "bluray", "webrip", "web", "dl",
                     "remux", "hdr", "extended", "directors", "director's", "cut", "theatrical", "remastered", "uhd"}
            return " ".join(w for w in words if w not in noise and not re.fullmatch(r"\d{3,4}p", w))

        db = self.store.db
        mine = db.execute("SELECT title FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
        names = {key(Path(r[0]).stem) for r in db.execute("SELECT path FROM files WHERE asset_id = ?", (asset_id,))}
        names.add(key(mine["title"]) if mine else "")
        names.discard("")
        if not names:
            return []
        found = []
        for row in db.execute("SELECT a.asset_id, a.title, a.duration_ms, f.path FROM assets a JOIN files f "
                              "ON f.asset_id = a.asset_id WHERE a.asset_id != ?", (asset_id,)):
            if key(Path(row["path"]).stem) in names or (row["title"] and key(row["title"]) in names):
                if all(item["assetId"] != row["asset_id"] for item in found):
                    found.append({"assetId": row["asset_id"], "durationMs": row["duration_ms"], "path": row["path"]})
        return found

    def get_scene(self, asset_id: str, scene_index: int) -> dict[str, Any]:
        db = self.store.db
        scene = db.execute("SELECT * FROM scenes WHERE asset_id = ? AND scene_index = ?",
                           (asset_id, int(scene_index))).fetchone()
        if scene is None:
            raise MediaRequestError("unknown scene")
        dialogue = [dict(row) for row in db.execute(
            "SELECT track_id, start_ms, end_ms, text FROM dialogue WHERE asset_id = ? AND kind = 'cue' "
            "AND start_ms < ? AND end_ms > ? ORDER BY start_ms LIMIT 500",
            (asset_id, scene["end_ms"], scene["start_ms"]))]
        frames = [dict(row) for row in db.execute(
            "SELECT ordinal, time_ms, path, bytes FROM keyframes WHERE asset_id = ? AND scene_index = ? "
            "ORDER BY ordinal", (asset_id, int(scene_index)))]
        return {"assetId": asset_id, "sceneIndex": scene["scene_index"], "startMs": scene["start_ms"],
                "endMs": scene["end_ms"], "cutScore": scene["cut_score"], "keyframes": frames,
                "dialogue": dialogue, "visualDescription": None,
                "visualNote": "No visual analysis exists yet; keyframes are pictures only."}

    # ----------------------------------------------------------- outputs

    def _allowed_roots_now(self, root_ids: set[int]) -> tuple[set[int], dict[str, str]]:
        """Ask the gate now, for each root of interest; nothing is read before this."""
        allowed: set[int] = set()
        refused: dict[str, str] = {}
        for row in self.store.db.execute("SELECT * FROM roots WHERE enabled = 1").fetchall():
            if int(row["root_id"]) not in root_ids:
                continue
            approval = Approval(row["approval_action_id"], row["approval_decision"], row["approved_at"])
            try:
                self.permissions.recheck_root(Path(row["path"]), approval, row["kind"], row["max_depth"])
            except MediaPermissionRefused as exc:
                refused[row["path"]] = exc.reason
                continue
            allowed.add(int(row["root_id"]))
        return allowed, refused

    def verify_asset(self, asset_id: str, *, max_seconds: float = DEFAULT_VERIFY_SECONDS,
                     cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Strict check: re-hash a copy in full and compare it with the asset's hash.

        Search uses a cheap stat comparison; this is the proof used before a
        playback plan or an export, because only the hash shows the timestamps
        belong to these exact bytes. It is never replaced by a sample.

        It is an owned, bounded operation like indexing: the gate is asked for
        every root first (nothing is read for a refused or revoked root); each
        candidate is admitted against its root's current scope; the read holds
        a resource lease (so it defers to gaming/creator work); and while it
        hashes it observes pause, ``cancelled`` (Stop), the time budget,
        permission revocation and lease renewal. Outcomes:

        ``verified``  a copy's bytes hash to the asset (``verified`` lists it);
        ``mismatch``  every admitted copy hashed differently (sent for re-identity);
        ``no_copy``   no present, in-scope copy whose stat still matches;
        ``refused``   the gate refused every root holding a copy (``refusedRoots``);
        ``deferred``  the resource authority said not now (``reason``);
        ``incomplete`` stopped mid-hash (``reason``: paused, cancelled,
                      time_budget, permission, deferred:...); nothing is claimed.
        """
        if not (0.05 <= float(max_seconds) <= MAX_VERIFY_SECONDS):
            raise MediaRequestError(f"max_seconds must be between 0.05 and {MAX_VERIFY_SECONDS}")
        db = self.store.db
        asset = db.execute("SELECT sha256 FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
        if asset is None:
            raise MediaRequestError("unknown asset")
        result: dict[str, Any] = {"assetId": asset_id, "sha256": asset["sha256"], "status": "no_copy",
                                  "verified": [], "mismatched": [], "outOfScope": [], "refusedRoots": {},
                                  "refusedCopies": [],
                                  "reason": ""}
        root_ids = {int(r[0]) for r in db.execute(
            "SELECT DISTINCT root_id FROM files WHERE asset_id = ? AND status = 'present'", (asset_id,))}
        allowed, refused = self._allowed_roots_now(root_ids)
        result["refusedRoots"] = refused
        if root_ids and not allowed:
            result["status"] = "refused"
            return result
        excluded = self._excluded
        good, stale, outside = verified_files(db, asset_id, allowed, excluded)
        result["outOfScope"] = outside
        if stale or outside:
            with self.store.transaction() as tx:
                mark_changed(tx, stale)
                mark_out_of_scope(tx, outside)
        if not good:
            return result
        try:
            lease = self.authority.reserve(f"media:verify:{asset_id}", StageNeeds("verify"))
        except ResourceRefused as exc:
            result["status"], result["reason"] = "deferred", str(exc)
            return result
        runner = self._runner()
        holder = [lease]
        # Roots are bound per copy as it is opened: the copy being hashed is
        # carried only by its own root's permission.
        watch = Watch(runner, deadline=self._monotonic() + float(max_seconds), holder=holder, roots=set())

        def stop() -> bool:
            if cancelled is not None and cancelled():
                watch.reason = watch.reason or "cancelled"
            return watch.poll()

        try:
            for path in good:
                # Admitted and stat-matched a moment ago; check scope and a fresh
                # gate answer for this copy's own root again at the open.
                admitted, _ = admitted_files(db, asset_id, allowed, excluded)
                if path not in admitted:
                    result["outOfScope"].append(str(path))
                    continue
                row = db.execute("SELECT root_id FROM files WHERE path = ?", (str(path),)).fetchone()
                if row is None or not runner.root_allowed_now(int(row["root_id"])):
                    result["refusedCopies"].append(str(path))
                    continue
                watch.bind(int(row["root_id"]))
                try:
                    before = FileState.of(path)
                    digest = full_sha256(path, cancelled=stop)
                    unchanged = FileState.of(path) == before
                except HashCancelled:
                    result["status"], result["reason"] = "incomplete", watch.reason or "cancelled"
                    result["verified"] = []
                    return result
                except OSError:
                    digest, unchanged = "", False
                if digest == asset["sha256"] and unchanged:
                    result["verified"].append(str(path))
                    result["status"] = "verified"
                    return result
                result["mismatched"].append(str(path))
                with self.store.transaction() as tx:
                    mark_changed(tx, [str(path)])
            if result["mismatched"]:
                result["status"] = "mismatch"
            elif result["refusedCopies"]:
                result["status"] = "refused"
            else:
                result["status"] = "no_copy"
            return result
        finally:
            self.authority.release(holder[0])

    def generate_report(self, asset_id: str, *, fmt: str = "json", kind: str = "full") -> dict[str, Any]:
        if fmt not in FORMATS or kind not in KINDS:
            raise MediaRequestError(f"format must be one of {FORMATS}; kind one of {KINDS}")
        try:
            checked_asset_id(asset_id)
            exports = owned_child(self.layout.root, "exports")
            written = write_report(self.store.db, asset_id, kind=kind, fmt=fmt, exports_dir=exports)
        except KeyError:
            raise MediaRequestError("unknown asset") from None
        except (ValueError, UnsafeMediaPath) as exc:
            raise MediaRequestError(str(exc)) from None
        reports = sorted((p for p in exports.glob("a_*.*") if p.is_file() and not p.is_symlink()),
                         key=lambda item: item.stat().st_mtime_ns, reverse=True)
        for old in reports[MAX_CACHED_REPORTS:]:
            owned_child(exports, old.name).unlink(missing_ok=True)
        return written

    def export_report(self, asset_id: str, destination: str, *, fmt: str = "json", kind: str = "full",
                      verify_seconds: float = DEFAULT_VERIFY_SECONDS,
                      cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Write a report to a user-chosen new file, as a gated CREATE_OR_EDIT_FILE action.

        Order: destination checks, then the report is rendered from the index
        (no media read), then the file-write gate decides on those exact bytes.
        Only an allowed or approved export goes on to the strict verification
        (which asks the read gate itself) and the write. A copy that cannot be
        verified now returns ``status: "not_verified"`` with the verification
        result, and nothing is written.
        """
        if fmt not in FORMATS or kind not in KINDS:
            raise MediaRequestError(f"format must be one of {FORMATS}; kind one of {KINDS}")
        target = self._export_target(destination, fmt)
        try:
            content = render(gather(self.store.db, asset_id, kind), fmt).encode("utf-8")
        except KeyError:
            raise MediaRequestError("unknown asset") from None
        digest = hashlib.sha256(content).hexdigest()
        decided = self.permissions.request_export(target, content, digest,
                                                  {"assetId": asset_id, "format": fmt, "kind": kind})
        if isinstance(decided, Pending):
            return _pending_json(decided)
        return self._verified_write(asset_id, target, content, digest, fmt, kind, decided, verify_seconds, cancelled)

    def approve_export(self, approval_id: str, decision: str, *, secret: str | None = None,
                       verify_seconds: float = DEFAULT_VERIFY_SECONDS,
                       cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        payload, approval = self.permissions.approve_export(approval_id, _decision(decision), secret)
        target = self._export_target(payload["destination"], payload["format"])
        content = payload["content"]
        if hashlib.sha256(content).hexdigest() != payload["sha256"]:
            raise MediaRequestError("prepared report content does not match its approved hash")
        return self._verified_write(payload["assetId"], target, content, payload["sha256"], payload["format"],
                                    payload["kind"], approval, verify_seconds, cancelled)

    def _verified_write(self, asset_id: str, target: Path, content: bytes, digest: str, fmt: str, kind: str,
                        approval: Approval, verify_seconds: float,
                        cancelled: Callable[[], bool] | None) -> dict[str, Any]:
        verification = self.verify_asset(asset_id, max_seconds=verify_seconds, cancelled=cancelled)
        if verification["status"] != "verified":
            return {"status": "not_verified", "assetId": asset_id, "verification": verification,
                    "written": False}
        written = self._write_export(target, content, digest, fmt, kind, approval)
        written["verifiedCopy"] = verification["verified"][0]
        return written

    @staticmethod
    def _export_target(destination: str, fmt: str) -> Path:
        target = local_path(destination)
        if target.suffix.lower() != f".{fmt}":
            raise MediaRequestError(f"destination must end in .{fmt}")
        if os.path.lexists(target) or not target.parent.is_dir() or has_link_component(target.parent):
            raise MediaRequestError("destination must be a new file in an existing, non-linked folder")
        return target

    @staticmethod
    def _write_export(target: Path, content: bytes, digest: str, fmt: str, kind: str,
                      approval: Approval) -> dict[str, Any]:
        try:
            with open(target, "xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            raise MediaRequestError("destination appeared while exporting; nothing was overwritten") from None
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return {"status": "written", "path": str(target), "sha256": digest, "bytes": len(content), "format": fmt,
                "kind": kind, "approval": {"actionId": approval.action_id, "decision": approval.decision}}

    def prepare_open(self, asset_id: str, start_ms: int, *, verify_seconds: float = DEFAULT_VERIFY_SECONDS,
                     cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Product route: open the verified copy at ``start_ms`` in a trusted catalog player (mpv or VLC).

        The copy must pass :meth:`verify_asset` now (current Files read gate plus the full hash); the player comes
        only from the trusted application catalog; then the OPEN_APPLICATION gate decides (single-use approval,
        typed secret when the policy is that strong). An allowed open launches at once.
        """
        from core.computer_executor import trusted_media_players

        start_ms = int(start_ms)
        if not (0 <= start_ms <= 7 * 24 * 3600 * 1000):
            raise MediaRequestError("startMs is out of range")
        verification = self.verify_asset(asset_id, max_seconds=verify_seconds, cancelled=cancelled)
        if verification["status"] != "verified":
            return {"status": "not_verified", "assetId": asset_id, "verification": verification}
        players = trusted_media_players()
        if not players:
            return {"status": "no_player", "assetId": asset_id,
                    "detail": "no mpv or VLC is installed in the trusted application catalog"}
        app_id = sorted(players)[0]
        spec = players[app_id]
        path = Path(verification["verified"][0])
        sha256 = self.store.db.execute("SELECT sha256 FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()[0]
        decided = self.permissions.request_open(path, app_id, spec.display_name, start_ms / 1000.0, sha256)
        if isinstance(decided, Pending):
            return _pending_json(decided)
        return self._launch_open(asset_id, path, app_id, start_ms / 1000.0, decided)

    def approve_open(self, approval_id: str, decision: str, *, secret: str | None = None,
                     verify_seconds: float = DEFAULT_VERIFY_SECONDS,
                     cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        payload, approval = self.permissions.approve_open(approval_id, _decision(decision), secret)
        asset_id = self.store.db.execute("SELECT asset_id FROM assets WHERE sha256 = ?",
                                         (payload["sha256"],)).fetchone()
        if asset_id is None:
            raise MediaRequestError("the prepared video is no longer in the index")
        # Re-verified now: the copy may have been replaced or its root revoked while the question was open.
        verification = self.verify_asset(asset_id[0], max_seconds=verify_seconds, cancelled=cancelled)
        if verification["status"] != "verified" or str(payload["path"]) not in verification["verified"]:
            return {"status": "not_verified", "assetId": asset_id[0], "verification": verification,
                    "launched": False}
        return self._launch_open(asset_id[0], Path(payload["path"]), payload["appId"], payload["startSeconds"],
                                 approval)

    @staticmethod
    def _launch_open(asset_id: str, path: Path, app_id: str, start_seconds: float,
                     approval: Approval) -> dict[str, Any]:
        from core import computer_executor
        from core.actions import InvalidAction

        spec = computer_executor.trusted_media_players().get(app_id)
        if spec is None:
            return {"status": "no_player", "assetId": asset_id, "launched": False,
                    "detail": "the approved player is no longer in the trusted catalog"}
        try:
            computer_executor.open_media_at_time(spec, str(path), start_seconds)
        except InvalidAction as exc:
            return {"status": "refused", "assetId": asset_id, "launched": False, "detail": str(exc)[:200]}
        except OSError as exc:
            return {"status": "failed", "assetId": asset_id, "launched": False, "detail": type(exc).__name__}
        return {"status": "launched", "assetId": asset_id, "launched": True, "player": spec.display_name,
                "startSeconds": round(float(start_seconds), 3), "name": path.name,
                "approval": {"actionId": approval.action_id, "decision": approval.decision}}

    def open_timestamp(self, asset_id: str, start_ms: int, *, player: str | None = None,
                       verify_seconds: float = DEFAULT_VERIFY_SECONDS,
                       cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Plan opening a file at a time. Launching is left to Qronos's approval flow.

        The copy offered must pass :meth:`verify_asset` now, so the timestamp
        refers to the exact cut it was indexed from. Otherwise the result is
        ``status: "not_verified"`` with the verification outcome and no path.
        """
        executable = None
        if player:
            executable = local_path(player)
            if executable.stem.casefold() not in _PLAYERS or not executable.is_file():
                raise MediaRequestError("player must be an existing mpv or vlc executable")
        verification = self.verify_asset(asset_id, max_seconds=verify_seconds, cancelled=cancelled)
        if verification["status"] != "verified":
            return {"status": "not_verified", "assetId": asset_id, "verification": verification,
                    "launch": "not_planned"}
        path = verification["verified"][0]
        seconds = max(0.0, int(start_ms) / 1000.0)
        plan: dict[str, Any] = {"status": "planned", "assetId": asset_id, "path": path, "startSeconds": seconds,
                                "hashVerified": True, "launch": "requires_qronos_approved_executor"}
        if executable is not None:
            plan["argv"] = [str(executable), _PLAYERS[executable.stem.casefold()].format(seconds=f"{seconds:.3f}"),
                            path]
        return plan


#: Moves the media index folder (one of core.startup_admission.STORE_PATH_OVERRIDES, so start-up admission
#: sees it); the product default is the Qronos data folder.
MEDIA_DATA_DIR_ENV = "QRONOS_MEDIA_DATA_DIR"


def default_data_dir(environ: dict | None = None) -> Path:
    from core.config import CONFIG

    configured = (os.environ if environ is None else environ).get(MEDIA_DATA_DIR_ENV, "").strip()
    if configured:
        return local_path(configured)
    return CONFIG.paths.data / "Media"


def tools_from_environment() -> MediaTools | None:
    from core.config import CONFIG

    return MediaTools.locate(environ=os.environ, runtime_assets=CONFIG.paths.runtime_assets)
