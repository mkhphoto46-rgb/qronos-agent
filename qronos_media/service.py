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

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import threading
import time
from typing import Any, Callable

from qronos_media.identity import FileState, HashCancelled, checked_asset_id, full_sha256
from qronos_media.jobs import (
    RunnerBusy, RunnerLock, StageRunner, Watch, admitted_files, ensure_stages, mark_changed, mark_out_of_scope,
    requeue, verified_files,
)
from qronos_media.paths import UnsafeMediaPath, has_link_component, local_path, owned_child
from qronos_media.permissions import Approval, MediaPermissionRefused, MediaPermissions, Pending
from qronos_media.reports import FORMATS, KINDS, ReportUnavailable, gather, levels, preview, render_bytes, write_report
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
#: Server-side snapshots of multimodal answers a report may be composed from (qronos_media.search_report).
MAX_SEARCH_SNAPSHOTS = 8
SEARCH_SNAPSHOT_SECONDS = 1800.0


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
        brain: object = None,
        vision: object = None,
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
        # The runtime's governed Fast-brain gateway (qronos_media.brain_gateway) or None: generation stays unavailable.
        self.brain = brain
        # The runtime's Vision gateway (qronos_media.vision_gateway) or None: visual evidence stays unavailable.
        self.vision = vision
        # resultId -> (monotonic time, snapshot): the exact answers this process gave, bounded and expiring.
        self._search_snapshots: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._search_snapshot_lock = threading.Lock()

    def _runner(self) -> StageRunner:
        return StageRunner(self.store, tools=self.tools, authority=self.authority, permissions=self.permissions,
                           cache_dir=self.layout.cache, cache_quota_bytes=self.cache_quota_bytes,
                           excluded=self._excluded, monotonic=self._monotonic, embedder=self.embedder,
                           brain=self.brain, vision=self.vision)

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
                for kind in ("keyframes", "visual"):  # visual: the text-check frames of visual_evidence
                    targets.append(owned_child(self.layout.cache, kind, checked_asset_id(asset_id)))
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
                owned_child(self.layout.cache, target.parent.name, target.name)  # re-check right before deleting
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
            # Which ffmpeg build is in use: the pinned shipping identity (with its known limits) or an unpinned override.
            "tools": self.tools.provenance() if self.tools is not None and hasattr(self.tools, "provenance") else None,
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
        from qronos_media.search import Budget, BudgetExhausted, _evidence

        if not isinstance(query, str) or not query.strip() or len(query) > semantic.MAX_TEXT_CHARS:
            raise MediaRequestError("query must be 1..1200 characters")
        limit = max(1, min(50, int(limit)))
        budget_seconds = max(0.2, min(10.0, float(budget_seconds)))
        exact = self.search_dialogue(query, mode="exact", limit=limit, language=language, asset_id=asset_id)
        block: dict[str, Any] = {"status": "unavailable", "results": [], "scoreNote": semantic.SCORE_NOTE,
                                 "queryScript": semantic.script_of(query)}
        vector, reason = self._query_vector(query)
        if vector is None:
            block["reason"] = reason
            return {"query": query, "exact": exact, "semantic": block}
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

    def _query_vector(self, query: str) -> tuple[bytes | None, str | None]:
        """ONE bge-m3 query vector through the same authority as indexing (one declared model slot, the captured
        sidecar generation, dispatch/unresolved evidence), or (None, reason) when it cannot be had now."""
        vectors, reason = self._embed_texts([query.strip()])
        return (vectors[0] if vectors else None), reason

    def _embed_texts(self, texts: list[str]) -> tuple[list[bytes], str | None]:
        """At most one bge-m3 batch (EMBED_BATCH texts) under the same governed path as the query vector."""
        from qronos_media import semantic
        from qronos_media.resources import BGE_M3

        if self.embedder is None:
            return [], "no bge-m3 sidecar capability in this process"
        try:
            # No owned sidecar now: refused before any reservation. Otherwise ONE generation is captured here.
            binding = self.embedder.bind()
        except ResourceRefused as exc:
            return [], exc.reason
        try:
            lease = self.authority.reserve(f"media:semantic_query:{time.monotonic_ns()}",
                                           StageNeeds("semantic_query", model=BGE_M3))
        except ResourceRefused as exc:
            return [], exc.reason
        try:
            if hasattr(lease, "bind_capability"):
                lease.bind_capability(binding.capability)
            vectors = self.embedder.embed(texts, timeout=semantic.QUERY_TIMEOUT_SECONDS,
                                          on_dispatch=getattr(lease, "mark_dispatched", None), binding=binding)
        except (ResourceRefused, semantic.EmbeddingInvalid) as exc:
            if getattr(exc, "reason", "") == "embedding_request_failed" and hasattr(lease, "mark_unresolved"):
                lease.mark_unresolved()
            return [], getattr(exc, "reason", "embedding_invalid")
        finally:
            self.authority.release(lease)  # the synchronous request has returned: drained
        return vectors, None

    def search_topics(self, query: str, *, limit: int = 10, language: str | None = None,
                      asset_id: str | None = None, budget_seconds: float = 3.0,
                      brain_hints: bool = False) -> dict[str, Any]:
        """Exact dialogue first (lexical, unchanged), then evidence-backed topic segments (qronos_media.topics).

        One governed bge-m3 query vector; expansion and reranking use only stored vectors and indexed text, so no
        further model call is made. Every topic hit carries its actual indexed lines and its score components."""
        from qronos_media import brain_labels, semantic, topics
        from qronos_media.search import Budget, BudgetExhausted

        if not isinstance(query, str) or not query.strip() or len(query) > semantic.MAX_TEXT_CHARS:
            raise MediaRequestError("query must be 1..1200 characters")
        limit = max(1, min(50, int(limit)))
        budget_seconds = max(0.2, min(10.0, float(budget_seconds)))
        exact = self.search_dialogue(query, mode="exact", limit=limit, language=language, asset_id=asset_id)
        block: dict[str, Any] = {"status": "unavailable", "results": [], "scoreNote": topics.SCORE_NOTE,
                                 "queryScript": semantic.script_of(query), "expansion": {"applied": False}}
        vector, reason = self._query_vector(query)
        if vector is None:
            block["reason"] = reason
            return {"query": query, "exact": exact, "topic": block}
        try:
            db = open_read_only(self.layout.database)
        except FileNotFoundError:
            raise MediaRequestError("the media index is empty") from None
        budget = Budget(budget_seconds, self._excluded)
        try:
            try:
                found = topics.search_topics(db, query, vector, limit=limit, language=language, asset_id=asset_id,
                                             check=budget.check)
                hints_block: dict[str, Any] = {"requested": bool(brain_hints), "applied": False}
                # Selective: only when asked AND the first pass is weak or the query very short (the same trigger as
                # the evidence-based expansion). Hints are generated search hints; results stay indexed segments.
                if brain_hints and found["expansion"].get("applied"):
                    hints_block.update(self._brain_hints(query))
                    if hints_block.get("hints"):
                        vectors, reason = self._embed_texts(hints_block["hints"])
                        if vectors:
                            # Its own budget (the brain's answer took time); running out keeps the first pass.
                            hinted = Budget(budget_seconds, self._excluded)
                            try:
                                found = topics.search_topics(db, query, vector, limit=limit, language=language,
                                                             asset_id=asset_id, check=hinted.check,
                                                             hint_vectors=vectors)
                                hints_block["applied"] = True
                            except BudgetExhausted as exc:
                                hints_block["reason"] = f"budget:{exc.reason}"
                        else:
                            hints_block["reason"] = reason
                elif brain_hints:
                    hints_block["reason"] = "first_pass_strong"
                found["brainHints"] = hints_block
                labels_by_asset: dict[str, dict] = {}
                for hit in found["results"]:
                    row = db.execute("SELECT title FROM assets WHERE asset_id = ?", (hit["assetId"],)).fetchone()
                    hit["title"] = row["title"] if row else None
                    if hit["assetId"] not in labels_by_asset:
                        labels_by_asset[hit["assetId"]] = brain_labels.asset_labels(db, hit["assetId"])
                    record = labels_by_asset[hit["assetId"]].get(hit["segmentKey"])
                    hit["generated"] = None if not record or record["status"] != "labelled" else {
                        "label": record["label"], "summary": record["summary"], "cites": record["cites"],
                        "verified": False, "note": "generated by the Fast brain from the cited lines; not a quotation"}
                block.update(found)
            except BudgetExhausted as exc:
                block.update({"status": "partial", "stoppedBy": exc.reason})
        finally:
            db.close()
        return {"query": query, "exact": exact, "topic": block}

    def _brain_hints(self, query: str) -> dict[str, Any]:
        """At most MAX_HINTS generated search phrases for a vague topic query, from the governed Fast brain, validated;
        or a reason. Never results, never facts."""
        from qronos_media import brain_labels
        from qronos_media.brain_gateway import prompt_hash

        if self.brain is None:
            return {"reason": "model_unavailable"}
        try:
            binding = self.brain.bind()
            user = brain_labels.hints_prompt(query)
            answer = self.brain.ask(binding, brain_labels.HINTS_SYSTEM, user)
            hints = brain_labels.parse_hints(answer, query)
        except ResourceRefused as exc:
            return {"reason": exc.reason}
        except brain_labels.LabelRejected as rejected:
            return {"reason": f"rejected:{rejected.reason}"}
        return {"hints": hints, "generated": True, "verified": False,
                "note": "generated search hints from the Fast brain: not facts, not results",
                "provenance": brain_labels.provenance(binding, prompt_digest=prompt_hash(brain_labels.HINTS_SYSTEM, user),
                                                      input_digest=prompt_hash(query), kind="query_hints")}

    def search_visual(self, query: str, *, limit: int = 10, asset_id: str | None = None,
                      budget_seconds: float = 3.0) -> dict[str, Any]:
        """Exact dialogue first (lexical, unchanged), then the SEPARATE visual channel: a lexical match over the stored
        generated visual text (captions, text the Vision model read, observations; qronos_media.visual_evidence),
        then - only when text vectors exist - the semantic channel: ONE governed bge-m3 query vector against the stored
        vectors of that same generated TEXT (never the image), its hits appended after the lexical ones. Each hit
        says its channels, carries its frame time and SHA-256 and the dialogue lines that overlap that time (joined
        by time, not by meaning). No Vision or other model call is made; no image embedding exists."""
        from qronos_media import semantic, visual_evidence
        from qronos_media.text import normalise_for_search

        if not isinstance(query, str) or not query.strip() or len(query) > semantic.MAX_TEXT_CHARS:
            raise MediaRequestError("query must be 1..1200 characters")
        limit = max(1, min(50, int(limit)))
        budget_seconds = max(0.2, min(10.0, float(budget_seconds)))
        exact = self.search_dialogue(query, mode="exact", limit=limit, asset_id=asset_id)
        block: dict[str, Any] = {"status": "complete", "results": [],
                                 "method": "lexical match over generated visual text (FTS5), not image similarity",
                                 "note": "generated by the Vision model from stored keyframes; unverified, not OCR"}
        tokens = normalise_for_search(query).split()
        try:
            db = open_read_only(self.layout.database)
        except FileNotFoundError:
            raise MediaRequestError("the media index is empty") from None
        try:
            try:
                # Counted in the CURRENT scope (and asset filter), as the candidates are: nothing out of scope
                # changes the answer.
                indexed = db.execute(
                    "SELECT COUNT(*) FROM visual_evidence v WHERE v.status = 'described' AND "
                    "NOT (EXISTS (SELECT 1 FROM files f WHERE f.asset_id = v.asset_id) AND NOT EXISTS (SELECT 1 FROM files f WHERE f.asset_id = v.asset_id AND (f.error IS NULL OR f.error != 'out_of_scope')))"
                    + (" AND v.asset_id = ?" if asset_id else ""), [asset_id] if asset_id else []).fetchone()[0]
            except sqlite3.OperationalError:
                block.update({"status": "unavailable", "reason": "no_visual_index"})
                return {"query": query, "exact": exact, "visual": block}
            if not indexed:
                block.update({"status": "unavailable", "reason": "no_visual_evidence"})
                return {"query": query, "exact": exact, "visual": block}
            if not tokens:
                return {"query": query, "exact": exact, "visual": block}
            match = " ".join('"' + token.replace('"', '""') + '"' for token in tokens[:16])
            clauses, params = "", [match]
            if asset_id:
                clauses, params = " AND v.asset_id = ?", [match, asset_id]
            rows = db.execute(
                "SELECT v.*, bm25(visual_fts) AS rank_score FROM visual_fts JOIN visual_evidence v "
                "ON v.row_id = visual_fts.rowid WHERE visual_fts MATCH ? AND v.status = 'described'"
                " AND NOT (EXISTS (SELECT 1 FROM files f WHERE f.asset_id = v.asset_id) AND NOT EXISTS (SELECT 1 FROM "
                "files f WHERE f.asset_id = v.asset_id AND (f.error IS NULL OR f.error != 'out_of_scope')))"
                f"{clauses} ORDER BY rank LIMIT {limit}", params).fetchall()
            for row in rows:
                hit = visual_evidence.row_json(row)
                title = db.execute("SELECT title FROM assets WHERE asset_id = ?", (row["asset_id"],)).fetchone()
                hit.update({"title": title["title"] if title else None, "score": round(-float(row["rank_score"]), 4),
                            "scoreKind": "bm25 over generated visual text", "channels": ["lexical"],
                            "dialogue": visual_evidence.joined_dialogue(db, row["asset_id"], row["time_ms"])})
                hit["rowId"] = row["row_id"]
                block["results"].append(hit)
            block["semantic"] = self._visual_semantic(db, query, limit, asset_id, budget_seconds, block["results"])
            for hit in block["results"]:
                hit.pop("rowId", None)
        finally:
            db.close()
        return {"query": query, "exact": exact, "visual": block}

    def _visual_semantic(self, db: sqlite3.Connection, query: str, limit: int, asset_id: str | None,
                         budget_seconds: float, results: list[dict[str, Any]]) -> dict[str, Any]:
        """The semantic visual channel, merged into ``results`` after the lexical hits; its own status/reason."""
        from qronos_media import visual_evidence
        from qronos_media.search import Budget, BudgetExhausted

        channel: dict[str, Any] = {"status": "unavailable", "scoreNote": visual_evidence.SEMANTIC_NOTE}
        try:
            # In the CURRENT scope (and asset filter) before any query vector is asked for.
            vectors = db.execute(
                "SELECT COUNT(*) FROM visual_semantic s WHERE "
                "NOT (EXISTS (SELECT 1 FROM files f WHERE f.asset_id = s.asset_id) AND NOT EXISTS (SELECT 1 FROM files f WHERE f.asset_id = s.asset_id AND (f.error IS NULL OR f.error != 'out_of_scope')))"
                + (" AND s.asset_id = ?" if asset_id else ""), [asset_id] if asset_id else []).fetchone()[0]
        except sqlite3.OperationalError:  # an index from before schema 7
            vectors = 0
        if not vectors:
            channel["reason"] = "no_visual_vectors"
            return channel
        vector, reason = self._query_vector(query)  # ONE governed bge-m3 call, as every semantic query
        if vector is None:
            channel["reason"] = reason  # busy/unavailable: the lexical visual hits above still stand
            return channel
        by_row = {hit["rowId"]: hit for hit in results}
        try:
            found, scan = visual_evidence.visual_candidates(db, vector, limit=limit, asset_id=asset_id,
                                                            check=Budget(budget_seconds, self._excluded).check)
        except BudgetExhausted as exc:
            channel.update({"status": "partial", "stoppedBy": exc.reason})
            return channel
        added = 0
        for item in found:
            hit = by_row.get(item["rowId"])
            if hit is not None:
                hit["channels"].append("semantic")
                hit["similarity"] = item["similarity"]
                continue
            if len(results) >= limit:
                continue
            row = db.execute("SELECT * FROM visual_evidence WHERE row_id = ?", (item["rowId"],)).fetchone()
            if row is None:
                continue
            hit = visual_evidence.row_json(row)
            title = db.execute("SELECT title FROM assets WHERE asset_id = ?", (row["asset_id"],)).fetchone()
            hit.update({"title": title["title"] if title else None, "channels": ["semantic"],
                        "similarity": item["similarity"], "scoreKind": "cosine over generated visual text (uncalibrated)",
                        "dialogue": visual_evidence.joined_dialogue(db, row["asset_id"], row["time_ms"])})
            results.append(hit)
            added += 1
        channel.update({"status": "partial" if scan["truncated"] else "complete", "scanned": scan["scanned"],
                        "added": added, "model": visual_evidence.semantic_provenance_model()})
        return channel

    def search_multimodal(self, dialogue: str, visual: str, *, window_ms: int = 15_000, dialogue_match: str = "exact",
                          visual_match: str = "lexical", limit: int = 10, budget_seconds: float = 4.0) -> dict[str, Any]:
        """A dialogue constraint AND a visual constraint, fused by time in the same current asset (qronos_media.multimodal).

        Order: the counts of the CURRENT scope first (nothing out of scope is counted, queried or embedded); then at most
        ONE governed bge-m3 batch, only for a semantic channel that was asked for and has stored vectors in scope; then
        the stored visual evidence (generated frame text), the stored dialogue (subtitle/ASR) of the assets that have it,
        and the time fusion. No Vision, ASR or other model runs and nothing is decoded. Paths are never returned."""
        from qronos_media import multimodal

        try:
            spec = multimodal.check_constraints(dialogue, visual, window_ms=window_ms, dialogue_match=dialogue_match,
                                                visual_match=visual_match)
        except multimodal.ConstraintRefused as exc:
            raise MediaRequestError(str(exc)) from None
        limit = max(1, min(50, int(limit)))
        budget_seconds = max(0.2, min(10.0, float(budget_seconds)))
        block: dict[str, Any] = {
            "status": "complete", "outcome": "neither", "results": [], "constraints": spec.as_json(),
            "fusionVersion": multimodal.FUSION_VERSION, "rankNote": multimodal.RANK_NOTE, "notes": [],
            "dialogueChannel": {"lexical": "fuzzy" if spec.dialogue_match == "fuzzy" else "exact",
                                "semantic": {"requested": spec.dialogue_match == "semantic", "status": "not_requested"}},
            "visualChannel": {"lexical": "every word in the generated frame text (Vision model, unverified, not OCR)",
                              "semantic": {"requested": spec.visual_match == "semantic", "status": "not_requested"}},
            "absent": {"speakerTurns": "not built", "ocr": "not built", "imageEncoder": "not built (no image similarity)"},
        }
        try:
            db = open_read_only(self.layout.database)
        except FileNotFoundError:
            raise MediaRequestError("the media index is empty") from None
        try:
            self._multimodal_into(db, spec, limit, budget_seconds, block)
        finally:
            db.set_progress_handler(None, 0)
            db.close()
        answer = {"query": spec.as_json(), "multimodal": block}
        if block["status"] != "unavailable":
            block["resultId"] = self._keep_search_snapshot(answer)
        return answer

    # ------------------------------------------------- search-result reports (qronos_media.search_report)

    def _keep_search_snapshot(self, answer: dict[str, Any]) -> str:
        from qronos_media import search_report

        kept = search_report.snapshot(answer)
        result_id = secrets.token_urlsafe(18)
        now = self._monotonic()
        with self._search_snapshot_lock:
            self._search_snapshots[result_id] = (now, kept)
            while len(self._search_snapshots) > MAX_SEARCH_SNAPSHOTS:
                self._search_snapshots.popitem(last=False)
        return result_id

    def _search_snapshot(self, result_id: str) -> dict[str, Any]:
        if not isinstance(result_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", result_id):
            raise MediaRequestError("result_unavailable: the result id is malformed")
        with self._search_snapshot_lock:
            held = self._search_snapshots.get(result_id)
            if held is not None and self._monotonic() - held[0] > SEARCH_SNAPSHOT_SECONDS:
                self._search_snapshots.pop(result_id, None)
                held = None
        if held is None:
            raise MediaRequestError("result_unavailable: this search result is no longer held; search again")
        return held[1]

    def search_report(self, result_id: str) -> dict[str, Any]:
        """The report of ONE held multimodal answer, rechecked NOW: every hit's asset must still be in the current
        scope, carried by a root the Files gate allows now, and its line and frames must still be stored as they were
        (frames by time and SHA-256). Anything else is removed and counted. Nothing is searched again."""
        from qronos_media import multimodal, search_report

        snap = self._search_snapshot(result_id)
        try:
            db = open_read_only(self.layout.database)
        except FileNotFoundError:
            raise MediaRequestError("the media index is empty") from None
        removed = {"outOfScope": 0, "permission": 0, "changed": 0}
        kept, sha_by_asset, allowed_by_asset = [], {}, {}
        try:
            for hit in snap["results"]:
                asset_id = hit["assetId"]
                row = db.execute("SELECT a.sha256 FROM assets a WHERE a.asset_id = ? AND " + multimodal.in_scope("a"),
                                 (asset_id,)).fetchone()
                if row is None:
                    removed["outOfScope"] += 1
                    continue
                if asset_id not in allowed_by_asset:
                    roots = {int(r[0]) for r in db.execute(
                        "SELECT DISTINCT root_id FROM files WHERE asset_id = ? AND status = 'present'", (asset_id,))}
                    allowed, _ = self._allowed_roots_now(roots)
                    allowed_by_asset[asset_id] = not roots or bool(roots & allowed)
                if not allowed_by_asset[asset_id]:
                    removed["permission"] += 1
                    continue
                line, track = hit["dialogue"], hit["dialogue"]["track"]["trackId"]
                if line["unit"] == "window":
                    stored = db.execute("SELECT 1 FROM semantic_windows WHERE asset_id = ? AND track_id = ? AND "
                                        "start_ms = ? AND end_ms = ?", (asset_id, track, line["startMs"],
                                                                       line["endMs"])).fetchone()
                else:
                    stored = db.execute("SELECT 1 FROM dialogue WHERE asset_id = ? AND track_id = ? AND kind = ? AND "
                                        "start_ms = ? AND end_ms = ?", (asset_id, track, line["unit"],
                                                                       line["startMs"], line["endMs"])).fetchone()
                frames = [frame for frame in hit["frames"] if db.execute(
                    "SELECT 1 FROM visual_evidence WHERE asset_id = ? AND time_ms = ? AND frame_sha256 = ? "
                    "AND status = 'described'", (asset_id, frame["timeMs"], frame["frameSha256"])).fetchone()]
                if stored is None or not frames:
                    removed["changed"] += 1
                    continue
                kept.append(dict(hit, frames=frames))
                sha_by_asset[asset_id] = row["sha256"]
        finally:
            db.close()
        return search_report.compose(snap, kept, removed, sha_by_asset)

    def preview_search_report(self, result_id: str) -> dict[str, Any]:
        from qronos_media import search_report

        return search_report.preview(self.search_report(result_id))

    def generate_search_report(self, result_id: str, *, fmt: str = "json") -> dict[str, Any]:
        """Write the report into Qronos's own exports folder (as generate_report does for an asset)."""
        from datetime import datetime, timezone

        content = self._search_report_bytes(result_id, fmt)
        exports = owned_child(self.layout.root, "exports")
        exports.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = owned_child(exports, f"search-{stamp}.{fmt}")
        with open(target, "xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        reports = sorted((p for p in exports.glob("search-*.*") if p.is_file() and not p.is_symlink()),
                         key=lambda item: item.stat().st_mtime_ns, reverse=True)
        for old in reports[MAX_CACHED_REPORTS:]:
            owned_child(exports, old.name).unlink(missing_ok=True)
        return {"path": str(target), "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                "format": fmt, "kind": "search"}

    def export_search_report(self, result_id: str, destination: str, *, fmt: str = "json") -> dict[str, Any]:
        """Write the report to a user-chosen new file through the same CREATE_OR_EDIT_FILE gate as asset reports.
        The bytes the gate decides on are the bytes written; no media copy is verified because the report claims
        nothing about the files beyond the index (it says so)."""
        target = self._export_target(destination, fmt)
        content = self._search_report_bytes(result_id, fmt)
        digest = hashlib.sha256(content).hexdigest()
        decided = self.permissions.request_export(target, content, digest, {"kind": "search", "format": fmt})
        if isinstance(decided, Pending):
            return _pending_json(decided)
        return self._write_export(target, content, digest, fmt, "search", decided)

    def _search_report_bytes(self, result_id: str, fmt: str) -> bytes:
        from qronos_media import search_report

        if fmt not in search_report.SEARCH_REPORT_FORMATS:
            raise MediaRequestError(f"format must be one of {search_report.SEARCH_REPORT_FORMATS}")
        try:
            return search_report.render_bytes(self.search_report(result_id), fmt)
        except ReportUnavailable as exc:
            raise MediaRequestError(f"report_unavailable: {exc}") from None

    def _multimodal_into(self, db: sqlite3.Connection, spec: Any, limit: int, budget_seconds: float,
                         block: dict[str, Any]) -> None:
        from qronos_media import multimodal, semantic, visual_evidence
        from qronos_media.search import Budget, BudgetExhausted, search_dialogue as dialogue_search

        scope = multimodal.in_scope
        try:
            frames_in_scope = db.execute("SELECT COUNT(*) FROM visual_evidence v WHERE v.status = 'described' AND "
                                         + scope("v")).fetchone()[0]
        except sqlite3.OperationalError:  # an index from before schema 6
            block.update({"status": "unavailable", "reason": "no_visual_index"})
            return
        lines_in_scope = db.execute("SELECT COUNT(*) FROM dialogue d WHERE d.kind = 'cue' AND " + scope("d")).fetchone()[0]
        block["scope"] = {"dialogueLines": lines_in_scope, "describedFrames": frames_in_scope}
        if not frames_in_scope or not lines_in_scope:
            block.update({"status": "unavailable", "reason": "no_visual_evidence" if not frames_in_scope else "no_dialogue"})
            return
        wanted: list[tuple[str, str]] = []
        for channel, table, alias, asked, text in (
                ("dialogue", "semantic_windows", "w", spec.dialogue_match == "semantic", spec.dialogue),
                ("visual", "visual_semantic", "s", spec.visual_match == "semantic", spec.visual)):
            if not asked:
                continue
            try:
                stored = db.execute(f"SELECT COUNT(*) FROM {table} {alias} WHERE " + scope(alias)).fetchone()[0]
            except sqlite3.OperationalError:
                stored = 0
            if stored:
                wanted.append((channel, text))
            else:
                block[f"{channel}Channel"]["semantic"].update({"status": "unavailable", "reason": f"no_{channel}_vectors"})
        vectors: dict[str, bytes] = {}
        if wanted:
            got, reason = self._embed_texts([text for _, text in wanted])  # ONE governed bge-m3 batch
            if got:
                vectors = {channel: vector for (channel, _), vector in zip(wanted, got)}
            else:
                for channel, _ in wanted:
                    block[f"{channel}Channel"]["semantic"].update({"status": "unavailable", "reason": reason})
        budget = Budget(budget_seconds, self._excluded)
        frames: list[dict[str, Any]] = []
        dialogue: list[dict[str, Any]] = []
        notes: list[str] = block["notes"]
        try:
            try:
                db.set_progress_handler(budget.expired, 1_000)
                by_row: dict[int, dict[str, Any]] = {}
                rows = db.execute(
                    "SELECT v.*, bm25(visual_fts) AS rank_score FROM visual_fts JOIN visual_evidence v "
                    "ON v.row_id = visual_fts.rowid WHERE visual_fts MATCH ? AND v.status = 'described' AND "
                    + scope("v") + f" ORDER BY rank LIMIT {multimodal.MAX_VISUAL_CANDIDATES + 1}",
                    [multimodal.visual_match_expression(spec.visual_tokens)]).fetchall()
                if len(rows) > multimodal.MAX_VISUAL_CANDIDATES:
                    notes.append("visual_candidates_cut")
                    rows = rows[:multimodal.MAX_VISUAL_CANDIDATES]
                for rank, row in enumerate(rows, 1):
                    hit = visual_evidence.row_json(row)
                    hit.update({"rank": rank, "channels": ["lexical"], "lexicalScore": round(-float(row["rank_score"]), 4),
                                "scoreKind": "bm25 over generated visual text"})
                    by_row[int(row["row_id"])] = hit
                    frames.append(hit)
                if "visual" in vectors:
                    found, scan = visual_evidence.visual_candidates(
                        db, vectors["visual"], limit=multimodal.MAX_SEMANTIC_VISUAL, asset_id=None, check=budget.check)
                    found = [item for item in found if item["similarity"] >= multimodal.MIN_SEMANTIC_COSINE]
                    for position, item in enumerate(found, 1):
                        hit = by_row.get(int(item["rowId"]))
                        if hit is not None:
                            hit["channels"].append("semantic")
                            hit["similarity"] = item["similarity"]
                            continue
                        row = db.execute("SELECT * FROM visual_evidence WHERE row_id = ?", (item["rowId"],)).fetchone()
                        if row is None:
                            continue
                        hit = visual_evidence.row_json(row)
                        hit.update({"rank": len(rows) + position, "channels": ["semantic"], "similarity": item["similarity"],
                                    "scoreKind": "cosine over generated visual text (uncalibrated)"})
                        frames.append(hit)
                    block["visualChannel"]["semantic"].update({
                        "status": "partial" if scan["truncated"] else "complete", "scanned": scan["scanned"],
                        "considered": multimodal.MAX_SEMANTIC_VISUAL, "minCosine": multimodal.MIN_SEMANTIC_COSINE,
                        "matched": len(found), "note": multimodal.SEMANTIC_NOTE,
                        "model": visual_evidence.semantic_provenance_model()})
                assets: list[str] = []
                for hit in sorted(frames, key=lambda h: h["rank"]):
                    if hit["assetId"] not in assets:
                        assets.append(hit["assetId"])
                if len(assets) > multimodal.MAX_FUSION_ASSETS:
                    notes.append("fusion_assets_cut")
                    assets = assets[:multimodal.MAX_FUSION_ASSETS]
                    frames[:] = [hit for hit in frames if hit["assetId"] in assets]
                lexical_mode = "fuzzy" if spec.dialogue_match == "fuzzy" else "exact"
                seen: set[tuple] = set()

                def lexical(asset_id: str | None, cap: int) -> dict[str, Any]:
                    found = dialogue_search(db, spec.dialogue, mode=lexical_mode, limit=cap, asset_id=asset_id,
                                            excluded=self._excluded,
                                            budget_seconds=max(0.05, min(10.0, budget.deadline - time.monotonic())))
                    db.set_progress_handler(budget.expired, 1_000)  # search_dialogue removed its own handler
                    for item in found["results"]:
                        key = (item["track"]["trackId"], item["startMs"], item["endMs"])
                        if key in seen:
                            continue
                        seen.add(key)
                        dialogue.append({"assetId": item["assetId"], "startMs": item["startMs"], "endMs": item["endMs"],
                                         "text": item["dialogue"][:multimodal.MAX_DIALOGUE_CHARS], "unit": item["unit"],
                                         "channels": [item["match"]], "lexicalScore": item["lexicalScore"],
                                         "scoreKind": item["scoreKind"], "timing": item["timing"],
                                         "track": {name: item["track"][name] for name in ("trackId", "source", "language")},
                                         "rank": len(dialogue) + 1})
                    if found["stoppedBy"]:
                        raise BudgetExhausted(found["stoppedBy"])
                    return found

                def cut(found: dict[str, Any]) -> bool:
                    """The look did not see every candidate: more results than its cap, or a candidate limit inside
                    the search (search_dialogue's notes, e.g. the fuzzy per-trigram or candidate cut)."""
                    return bool(found["truncated"] or found["notes"])

                # A bounded look over the whole current scope (it also says whether the dialogue exists elsewhere);
                # when it was cut in any way, every asset that has visual evidence is searched on its own (the
                # search's candidate limits then apply to that asset alone). A cut that remains is reported.
                overall = lexical(None, multimodal.MAX_GLOBAL_DIALOGUE)
                if cut(overall):
                    for asset_id in assets:
                        budget.check()
                        found = lexical(asset_id, multimodal.MAX_DIALOGUE_PER_ASSET)
                        if cut(found):
                            notes.extend(note for note in ["dialogue_candidates_cut",
                                                           *(f"dialogue:{n}" for n in found["notes"])]
                                         if note not in notes)
                    if not dialogue and "dialogue_candidates_cut" not in notes:
                        notes.append("dialogue_candidates_cut")  # nothing found by a cut look: absence is unproven
                if "dialogue" in vectors:
                    found, scan = semantic.semantic_candidates(db, vectors["dialogue"],
                                                               limit=multimodal.MAX_SEMANTIC_DIALOGUE, language=None,
                                                               asset_id=None, check=budget.check)
                    found = [row for row in found if row["score"] >= multimodal.MIN_SEMANTIC_COSINE]
                    offset = len(dialogue)
                    for position, row in enumerate(found, 1):
                        track = db.execute("SELECT source, language FROM subtitle_tracks WHERE track_id = ?",
                                           (row["track_id"],)).fetchone()
                        dialogue.append({"assetId": row["asset_id"], "startMs": row["start_ms"], "endMs": row["end_ms"],
                                         "text": row["text"][:multimodal.MAX_DIALOGUE_CHARS], "unit": "window",
                                         "channels": ["semantic"], "similarity": row["score"],
                                         "scoreKind": "cosine_uncalibrated",
                                         "track": {"trackId": row["track_id"], "source": track["source"] if track else None,
                                                   "language": track["language"] if track else None},
                                         "rank": offset + position})
                    block["dialogueChannel"]["semantic"].update({
                        "status": "partial" if scan["truncated"] else "complete", "scanned": scan["scanned"],
                        "considered": multimodal.MAX_SEMANTIC_DIALOGUE, "minCosine": multimodal.MIN_SEMANTIC_COSINE,
                        "matched": len(found), "note": multimodal.SEMANTIC_NOTE})
            except BudgetExhausted as exc:
                block.update({"status": "partial", "stoppedBy": exc.reason})
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "interrupt" in message:
                    block.update({"status": "partial", "stoppedBy": "time_budget"})
                elif "locked" in message or "busy" in message:
                    block.update({"status": "partial", "stoppedBy": "busy"})
                else:
                    raise
        finally:
            db.set_progress_handler(None, 0)
        # Every pair below is checked on the evidence gathered so far: a partial run gives fewer results, never wrong ones.
        results, facts = multimodal.fuse(dialogue, frames, spec.window_ms, limit)
        for result in results:
            title = db.execute("SELECT title FROM assets WHERE asset_id = ?", (result["assetId"],)).fetchone()
            result["title"] = title["title"] if title else None
        block["results"] = results
        block["outcome"] = multimodal.outcome(results, visual_found=bool(frames), dialogue_found=bool(dialogue),
                                              same_asset_outside=bool(facts["sameAssetOutsideWindow"]))
        block["counts"] = {"dialogueEvidence": len(dialogue), "dialogueAssets": len({d["assetId"] for d in dialogue}),
                           "visualEvidence": len(frames), "visualAssets": len({f["assetId"] for f in frames}),
                           "sameAssetOutsideWindow": len(facts["sameAssetOutsideWindow"]),
                           "pairedResults": facts["pairedResults"]}
        if facts["truncated"]:
            notes.append("results_cut_at_limit")
        unavailable = [name for name in ("dialogue", "visual")
                       if block[f"{name}Channel"]["semantic"]["requested"]
                       and block[f"{name}Channel"]["semantic"]["status"] == "unavailable"]
        if block["status"] == "complete" and (unavailable or set(notes) - {"results_cut_at_limit"}):
            block["status"] = "partial"
        notes += [f"{name}_semantic_unavailable" for name in unavailable]

    def interpret_multimodal(self, query: str) -> dict[str, Any]:
        """A PROPOSED split of one natural request into the dialogue and visual constraints; nothing is searched.

        The person's own labels are used without any model. Otherwise ONE call to the runtime's governed Fast brain
        (its own admission, generation binding and foreground rules) proposes the split; it must be made of the
        request's own words and stays generated/unverified. Unavailable or ambiguous says so; the explicit inputs stay
        usable either way."""
        from qronos_media import multimodal

        if not isinstance(query, str) or not query.strip() or len(query) > 1200:
            raise MediaRequestError("query must be 1..1200 characters")
        labelled = multimodal.split_labelled(query)
        if labelled is not None:
            return {"query": query, "interpretation": {
                "status": "proposed", "dialogue": labelled[0], "visual": labelled[1], "method": "labels",
                "generated": False, "verified": False,
                "note": "taken from your own labels; confirm or edit the two parts, then search"}}
        if self.brain is None:
            return {"query": query, "interpretation": {"status": "unavailable", "reason": "model_unavailable",
                                                       "note": "type the dialogue and the visual parts yourself"}}
        try:
            binding = self.brain.bind()
            answer = self.brain.ask(binding, multimodal.SPLIT_SYSTEM, multimodal.split_prompt(query))
            dialogue, visual = multimodal.parse_split(answer, query)
        except ResourceRefused as exc:
            return {"query": query, "interpretation": {"status": "unavailable", "reason": exc.reason,
                                                       "note": "type the dialogue and the visual parts yourself"}}
        except multimodal.SplitRejected as rejected:
            return {"query": query, "interpretation": {"status": "ambiguous", "reason": rejected.reason,
                                                       "note": "the request could not be split safely; type both parts"}}
        return {"query": query, "interpretation": {
            "status": "proposed", "dialogue": dialogue, "visual": visual, "method": "fast_brain", "generated": True,
            "verified": False, "note": "a split proposed by the Fast brain from your own words; unverified - confirm "
                                       "or edit the two parts, then search",
            "provenance": multimodal.split_provenance(binding, query)}}

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
        except ReportUnavailable as exc:
            raise MediaRequestError(f"report_unavailable: {exc}") from None
        except (ValueError, UnsafeMediaPath) as exc:
            raise MediaRequestError(str(exc)) from None
        reports = sorted((p for p in exports.glob("a_*.*") if p.is_file() and not p.is_symlink()),
                         key=lambda item: item.stat().st_mtime_ns, reverse=True)
        for old in reports[MAX_CACHED_REPORTS:]:
            owned_child(exports, old.name).unlink(missing_ok=True)
        return written

    def preview_report(self, asset_id: str, *, kind: str = "full") -> dict[str, Any]:
        """A bounded view of the same report schema for the desktop (read-only connection, nothing written)."""
        if kind not in KINDS:
            raise MediaRequestError(f"kind must be one of {KINDS}")
        try:
            checked_asset_id(asset_id)
            db = open_read_only(self.layout.database)
        except FileNotFoundError:
            raise MediaRequestError("the media index is empty") from None
        except ValueError as exc:
            raise MediaRequestError(str(exc)) from None
        try:
            return preview(gather(db, asset_id, kind))
        except KeyError:
            raise MediaRequestError("unknown asset") from None
        finally:
            db.close()

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
            content = render_bytes(gather(self.store.db, asset_id, kind), fmt)
        except KeyError:
            raise MediaRequestError("unknown asset") from None
        except ReportUnavailable as exc:
            raise MediaRequestError(f"report_unavailable: {exc}") from None
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
        if payload.get("kind") == "search":  # a search-result report: exactly the approved bytes, nothing verified
            return self._write_export(target, content, payload["sha256"], payload["format"], "search", approval)
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
