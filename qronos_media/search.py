"""Dialogue search over the index, lexical first (handoff s5.2, s5.3, s20, s21, s26, s54).

Exact: the normalised query as an FTS5 phrase, then confirmed as a
word-bounded substring of the stored normal form. A line split across two
subtitle cues is found through the indexed cue pairs, and only reported as a
pair when neither cue matched alone.

Fuzzy: candidates come from the trigram index, one bounded lookup per trigram
starting with the query's *rarest* trigrams (from ``fts5vocab``); rows that
share the most trigrams with the query are scored by the best
``SequenceMatcher`` ratio against a window of similar length in the cue.

Every query, including assembling the evidence for each hit, runs under one
:class:`Budget`: a wall-clock deadline enforced inside SQLite by a progress
handler and a busy timeout no longer than the budget, and between candidates,
windows and results; a cap on similarity computations; and fixed limits on
words, characters, copy paths and dialogue text per hit. Running out, or a
database another process holds locked, returns what was found with
``status: "partial"`` and the reason; it never blocks, decodes, scans or writes.

Scores are lexical evidence, not probabilities. ``confidence`` is reported as
``"uncalibrated"`` until a labelled benchmark exists (handoff s13, s69).
Semantic search is pending and says so.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import sqlite3
import time
from typing import Any

from qronos_media.jobs import file_matches_hash
from qronos_media.scope import OutOfScope, admit, load_scopes
from qronos_media.text import normalise_for_search

MAX_LIMIT = 100
MAX_QUERY_CHARS = 500
EXACT_CANDIDATES = 2_000
FUZZY_TRIGRAMS = 24
ROWS_PER_TRIGRAM = 2_000
FUZZY_CANDIDATES = 400
MAX_QUERY_WORDS = 32
MAX_CANDIDATE_WORDS = 48
MAX_NEEDLE_CHARS = 160
MAX_WORD_CHARS = 32
MAX_RATIO_CALLS = 20_000
DEFAULT_MIN_SIMILARITY = 0.6
DEFAULT_BUDGET_SECONDS = 1.5
MAX_BUDGET_SECONDS = 10.0
MAX_EVIDENCE_PATHS = 8
MAX_DIALOGUE_CHARS = 2_000

_TIMING = {
    "embedded": "embedded subtitle times, normalised by ffmpeg to the asset timeline (container start removed); "
                "as authored, not verified against the audio",
    "sidecar": "sidecar subtitle times as authored, read as relative to playback start; "
               "not verified against the audio or this exact cut",
}


class SearchError(ValueError):
    pass


class BudgetExhausted(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Budget:
    """Wall-clock and work limits for one query."""

    def __init__(self, seconds: float, excluded: tuple = ()) -> None:
        self.deadline = time.monotonic() + seconds
        self.ratio_calls = 0
        self.excluded = tuple(excluded)

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise BudgetExhausted("time_budget")

    def ratio(self) -> None:
        self.ratio_calls += 1
        if self.ratio_calls > MAX_RATIO_CALLS:
            raise BudgetExhausted("work_budget")
        if self.ratio_calls % 64 == 0:
            self.check()

    def expired(self) -> int:
        """SQLite progress handler: a non-zero return interrupts the statement."""
        return 1 if time.monotonic() >= self.deadline else 0


def _phrase(tokens: list[str]) -> str:
    return '"' + " ".join(token.replace('"', '""') for token in tokens) + '"'


def _filters(language: str | None, asset_id: str | None) -> tuple[str, list[Any]]:
    # An asset whose every copy fell outside its root's current scope (a
    # narrowed depth, a removed folder) is not offered any more.
    clauses = ["NOT (EXISTS (SELECT 1 FROM files f WHERE f.asset_id = d.asset_id) "
               "AND NOT EXISTS (SELECT 1 FROM files f WHERE f.asset_id = d.asset_id "
               "AND (f.error IS NULL OR f.error != 'out_of_scope')))"]
    params: list[Any] = []
    if language:
        clauses.append("t.language = ?")
        params.append(language)
    if asset_id:
        clauses.append("d.asset_id = ?")
        params.append(asset_id)
    return ("".join(f" AND {clause}" for clause in clauses), params)


def _exact(db: sqlite3.Connection, norm: str, language: str | None, asset_id: str | None) -> list[dict]:
    where, params = _filters(language, asset_id)
    rows = db.execute(
        "SELECT d.*, bm25(dialogue_fts) AS rank_score FROM dialogue_fts "
        "JOIN dialogue d ON d.unit_id = dialogue_fts.rowid JOIN subtitle_tracks t ON t.track_id = d.track_id "
        f"WHERE dialogue_fts MATCH ?{where} ORDER BY rank LIMIT {EXACT_CANDIDATES}",
        [_phrase(norm.split()), *params],
    ).fetchall()
    needle = f" {norm} "
    hits = [row for row in rows if needle in f" {row['norm_text']} "]
    cue_keys = {(row["track_id"], row["seq"]) for row in hits if row["kind"] == "cue"}
    results = []
    for row in hits:
        if row["kind"] == "pair" and ({(row["track_id"], row["seq"]), (row["track_id"], row["seq"] + 1)} & cue_keys):
            continue
        results.append({"row": row, "match": "exact", "score": round(-float(row["rank_score"]), 4),
                        "scoreKind": "bm25"})
    return results


def _rare_trigrams(db: sqlite3.Connection, norm: str) -> list[str]:
    grams = sorted({norm[i:i + 3] for i in range(len(norm) - 2)} - {""})
    if not grams:
        return []
    placeholders = ",".join("?" for _ in grams)
    counts = {row[0]: row[1] for row in db.execute(
        f"SELECT term, doc FROM dialogue_tri_vocab WHERE term IN ({placeholders})", grams
    )}
    # Grams never seen in the index cannot produce candidates; skip them.
    present = [gram for gram in grams if counts.get(gram)]
    return sorted(present, key=lambda gram: (counts[gram], gram))[:FUZZY_TRIGRAMS]


def _bounded_words(text: str, limit: int) -> list[str]:
    return [word[:MAX_WORD_CHARS] for word in text.split()[:limit]]


def similarity(query: str, text: str, budget: Budget | None = None) -> float:
    """Best ratio between ``query`` and any window of ``text`` of similar word length.

    Both sides are cut to fixed word and character limits first, so one long
    or repetitive cue cannot make a single comparison expensive.
    """
    q_words = _bounded_words(query, MAX_QUERY_WORDS)
    t_words = _bounded_words(text, MAX_CANDIDATE_WORDS)
    if not q_words or not t_words:
        return 0.0
    needle = " ".join(q_words)[:MAX_NEEDLE_CHARS]
    window_chars = 2 * len(needle) + 16
    best = 0.0
    n = len(q_words)
    for size in sorted({max(1, n - 1), n, n + 1}):
        size = min(size, len(t_words))
        for start in range(0, len(t_words) - size + 1):
            window = " ".join(t_words[start:start + size])[:window_chars]
            matcher = SequenceMatcher(None, needle, window, autojunk=False)
            if matcher.real_quick_ratio() <= best or matcher.quick_ratio() <= best:
                continue
            if budget is not None:
                budget.ratio()
            best = max(best, matcher.ratio())
    return best


def _fuzzy(db: sqlite3.Connection, norm: str, language: str | None, asset_id: str | None,
           min_similarity: float, budget: Budget, notes: list[str]) -> list[dict]:
    grams = _rare_trigrams(db, norm)
    if not grams:
        return []
    hits: dict[int, int] = {}
    for gram in grams:
        budget.check()
        rowids = [row[0] for row in db.execute(
            f"SELECT rowid FROM dialogue_tri WHERE dialogue_tri MATCH ? LIMIT {ROWS_PER_TRIGRAM}",
            ('"' + gram.replace('"', '""') + '"',),
        )]
        if len(rowids) == ROWS_PER_TRIGRAM and "common_terms" not in notes:
            notes.append("common_terms")  # some candidate lists were cut at the per-trigram limit
        for rowid in rowids:
            hits[rowid] = hits.get(rowid, 0) + 1
    ranked = sorted(hits, key=lambda rowid: (-hits[rowid], rowid))[:FUZZY_CANDIDATES]
    if not ranked:
        return []
    where, params = _filters(language, asset_id)
    placeholders = ",".join("?" for _ in ranked)
    rows = db.execute(
        "SELECT d.* FROM dialogue d JOIN subtitle_tracks t ON t.track_id = d.track_id "
        f"WHERE d.unit_id IN ({placeholders}){where}", [*ranked, *params],
    ).fetchall()
    order = {rowid: position for position, rowid in enumerate(ranked)}
    scored = []
    for row in sorted(rows, key=lambda row: order[row["unit_id"]]):
        budget.check()
        score = similarity(norm, row["norm_text"], budget)
        if score >= min_similarity:
            scored.append({"row": row, "match": "fuzzy", "score": round(score, 4), "scoreKind": "similarity"})
    best_cues = {}
    for item in scored:
        row = item["row"]
        if row["kind"] == "cue":
            best_cues[(row["track_id"], row["seq"])] = item["score"]
    results = []
    for item in scored:
        row = item["row"]
        if row["kind"] == "pair":
            neighbours = [best_cues.get((row["track_id"], row["seq"] + k), 0.0) for k in (0, 1)]
            if max(neighbours) >= item["score"]:
                continue  # one cue alone matches as well as the pair; report the cue
        results.append(item)
    return sorted(results, key=lambda item: -item["score"])


def _evidence(db: sqlite3.Connection, item: dict, budget: Budget) -> dict[str, Any]:
    row = item["row"]
    asset = db.execute("SELECT asset_id, sha256, title, duration_ms, video_end_ms FROM assets WHERE asset_id = ?",
                       (row["asset_id"],)).fetchone()
    track = db.execute("SELECT * FROM subtitle_tracks WHERE track_id = ?", (row["track_id"],)).fetchone()
    paths, stale = [], []
    scopes = load_scopes(db)
    copies = db.execute(
        f"SELECT * FROM files WHERE asset_id = ? AND status = 'present' ORDER BY path LIMIT {MAX_EVIDENCE_PATHS + 1}",
        (row["asset_id"],)).fetchall()
    for copy in copies[:MAX_EVIDENCE_PATHS]:
        budget.check()
        scope = scopes.get(int(copy["root_id"]))
        try:
            if scope is None:
                raise OutOfScope("no_root")
            admit(scope, copy["path"], budget.excluded)
        except OutOfScope:
            continue  # not offered as a path to open
        (paths if file_matches_hash(copy) else stale).append(copy["path"])
    scene = db.execute(
        "SELECT scene_index, start_ms, end_ms FROM scenes WHERE asset_id = ? AND start_ms <= ? AND end_ms > ?",
        (row["asset_id"], row["start_ms"], row["start_ms"]),
    ).fetchone()
    return {
        "assetId": asset["asset_id"],
        "sha256": asset["sha256"],
        "title": asset["title"],
        "durationMs": asset["duration_ms"],
        "videoEndMs": asset["video_end_ms"],
        "paths": paths,
        "stalePaths": stale,
        "pathsTruncated": len(copies) > MAX_EVIDENCE_PATHS,
        "available": bool(paths),
        "identityCheck": "stat: size, mtime and file id match the hashed state; content not re-hashed at query time",
        "startMs": row["start_ms"],
        "endMs": row["end_ms"],
        "timeline": "asset (milliseconds from the container start)",
        "dialogue": row["text"][:MAX_DIALOGUE_CHARS],
        "unit": row["kind"],
        "match": item["match"],
        "lexicalScore": item["score"],
        "scoreKind": item["scoreKind"],
        "confidence": "uncalibrated",
        "timing": _TIMING.get(track["source"], "as authored; not verified"),
        "track": {
            "trackId": track["track_id"],
            "source": track["source"],
            "language": track["language"],
            "streamIndex": track["stream_index"],
            "sidecarPath": track["sidecar_path"],
            "sidecarSha256": track["sidecar_sha256"],
            "codec": track["codec"],
            "forced": bool(track["forced"]),
        },
        "scene": dict(scene) if scene else None,
    }


def search_dialogue(
    db: sqlite3.Connection,
    query: str,
    *,
    mode: str = "auto",
    limit: int = 10,
    language: str | None = None,
    asset_id: str | None = None,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    excluded: tuple = (),
) -> dict[str, Any]:
    if mode not in {"auto", "exact", "fuzzy"}:
        raise SearchError("mode must be auto, exact or fuzzy")
    if not isinstance(query, str) or not query.strip():
        raise SearchError("query is empty")
    if len(query) > MAX_QUERY_CHARS:
        raise SearchError("query is too long")
    limit = max(1, min(MAX_LIMIT, int(limit)))
    if not (0.0 < float(min_similarity) <= 1.0):
        raise SearchError("min_similarity must be in (0, 1]")
    if not (0.05 <= float(budget_seconds) <= MAX_BUDGET_SECONDS):
        raise SearchError(f"budget_seconds must be between 0.05 and {MAX_BUDGET_SECONDS}")
    norm = normalise_for_search(query)
    if not norm:
        raise SearchError("query has no searchable words")
    budget = Budget(float(budget_seconds), excluded)
    notes: list[str] = []
    stopped: str | None = None
    items: list[dict] = []
    results: list[dict[str, Any]] = []
    previous_timeout = int(db.execute("PRAGMA busy_timeout").fetchone()[0])
    db.execute(f"PRAGMA busy_timeout = {max(1, int(float(budget_seconds) * 1000))}")
    db.set_progress_handler(budget.expired, 1_000)
    try:
        try:
            if mode in {"auto", "exact"}:
                items += _exact(db, norm, language, asset_id)
            if mode == "fuzzy" or (mode == "auto" and len(items) < limit):
                seen = {(item["row"]["track_id"], item["row"]["start_ms"]) for item in items}
                for item in _fuzzy(db, norm, language, asset_id, float(min_similarity), budget, notes):
                    key = (item["row"]["track_id"], item["row"]["start_ms"])
                    if key not in seen:
                        seen.add(key)
                        items.append(item)
            for item in items[:limit]:
                budget.check()
                results.append(_evidence(db, item, budget))
        except BudgetExhausted as exc:
            stopped = exc.reason
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "interrupt" in message:
                stopped = "time_budget"
            elif "locked" in message or "busy" in message:
                stopped = "busy"
            else:
                raise
    finally:
        db.set_progress_handler(None, 0)
        db.execute(f"PRAGMA busy_timeout = {previous_timeout}")
    return {
        "query": query,
        "normalisedQuery": norm,
        "mode": mode,
        "status": "partial" if stopped or notes else "complete",
        "stoppedBy": stopped,
        "notes": notes,
        "budgetMs": int(float(budget_seconds) * 1000),
        "results": results,
        "truncated": len(items) > limit,
        # Semantic matches are a separate, model-bearing call (MediaService.search_semantic), implemented but not
        # part of THIS lexical call; ASR dialogue is searched here once indexed. Still not built: subtitle/ASR
        # agreement labels and topics.
        "notInThisCall": ["semantic_dialogue"],
        "pending": ["subtitle_asr_fusion", "topics"],
    }
