"""Topic retrieval over indexed dialogue (handoff s5.5, s19, s24, s70-72), with the fixed bge-m3 vectors only.

What a "topic segment" is here: a run of consecutive semantic windows of ONE dialogue track (subtitles or the
unverified ASR transcript) whose stored bge-m3 vectors stay close to their running centroid, bounded in span and
size. Its vector is the normalised mean of those stored window vectors, so building segments makes no model call
at all. A segment has no label and no summary: labels would have to come from a brain model, and no brain authority
is wired to media, so none are generated. What a segment does have is lineage: its track, time range and the exact
windows (and so the exact cues) it is made of.

Topic search (``search_topics``) is evidence-first:

1. the query vector (one governed bge-m3 request, made by the caller) scores segment centroids;
2. each candidate is backed by its best ACTUAL windows (indexed text and times); a candidate without evidence is
   dropped;
3. only when the first pass is weak or the query is very short, the query is expanded by pseudo-relevance feedback:
   the stored vectors of the top evidence windows are blended into the query vector (bounded weight), and words
   from those windows are shown as search hints. Hints are never facts and never become results by themselves;
4. at most ``MAX_RERANK`` candidates are reranked with fixed, declared weights (segment similarity, best evidence
   similarity, query-term overlap, agreement of the expanded pass). The score is an uncalibrated rank score, not a
   probability or an accuracy; every hit carries its components as the rationale.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import heapq
import json
import re
import sqlite3
import time
from typing import Any, Callable

from qronos_media import semantic

TOPICS_VERSION = 1
METHOD = "centroid_of_bge_m3_dialogue_windows"
SEGMENT_SPAN_MS = 180_000
SEGMENT_MAX_WINDOWS = 12
#: A window whose vector is less similar than this to the segment's running centroid starts a new segment.
SEGMENT_DRIFT = 0.55
MAX_SEGMENTS_PER_ASSET = 4_000
SCAN_CHUNK = 2_000
MAX_SCAN_SEGMENTS = 100_000
MAX_RERANK = 50
EVIDENCE_PER_HIT = 2
#: Expansion runs only when the best first-pass segment similarity is below this, or the query has few words.
EXPANSION_TRIGGER = 0.5
EXPANSION_SHORT_QUERY_WORDS = 3
EXPANSION_WEIGHT = 0.35
EXPANSION_SOURCES = 3
MAX_HINTS = 6
WEIGHTS = {"segment": 0.45, "evidence": 0.35, "terms": 0.1, "expansion": 0.1}
SCORE_NOTE = ("rank score from fixed weights over bge-m3 cosine similarities and query-word overlap: uncalibrated, "
              "not a probability, percentage or accuracy")
HINT_NOTE = "words taken from the best matching dialogue to widen the search: search hints, not facts"

_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset("""
a an and are as at be but by for from had has have he her his i if in is it its me my no not of on or our she so
that the their them they this to was we were what when where who will with you your
و در به از که این آن را با برای هم یا اما تا من تو او ما شما آنها است بود هست نیست شد می را یک چه کجا کی چرا
""".split())


def words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(text or "") if len(w) >= 2 and w.casefold() not in _STOP]


# ------------------------------------------------------------------ segments (stage "topics")


@dataclass(frozen=True)
class Segment:
    segment_key: str
    track_id: str
    source: str
    language: str | None
    start_ms: int
    end_ms: int
    window_keys: tuple[str, ...]
    vector: bytes


def _normalised(vector):
    import numpy as np

    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-12 else None


def build_segments(db: sqlite3.Connection, asset_id: str, check: Callable[[], None] = lambda: None) -> list[Segment]:
    """Group the asset's searchable windows into topic segments, per track, from their stored vectors only."""
    import numpy as np

    rows = db.execute(
        "SELECT w.window_key, w.track_id, w.source, w.language, w.start_ms, w.end_ms, c.vector "
        "FROM semantic_windows w JOIN semantic_cache c ON c.cache_key = w.cache_key "
        "WHERE w.asset_id = ? ORDER BY w.track_id, w.start_ms, w.seq_from", (asset_id,)).fetchall()
    segments: list[Segment] = []
    current: list[sqlite3.Row] = []
    total = None

    def close() -> None:
        nonlocal current, total
        if current and len(segments) < MAX_SEGMENTS_PER_ASSET:
            centroid = _normalised(total)
            if centroid is not None:
                keys = tuple(row["window_key"] for row in current)
                first = current[0]
                segment_key = hashlib.sha256(json.dumps([asset_id, first["track_id"], keys, TOPICS_VERSION,
                                                         semantic.model_version()]).encode()).hexdigest()
                segments.append(Segment(segment_key, first["track_id"], first["source"], first["language"],
                                        first["start_ms"], max(row["end_ms"] for row in current), keys,
                                        centroid.astype("<f4").tobytes()))
        current, total = [], None

    for index, row in enumerate(rows):
        if index % 500 == 0:
            check()
        vector = np.frombuffer(row["vector"], dtype="<f4").astype("float64")
        if current:
            centroid = _normalised(total)
            drifted = centroid is None or float(vector @ centroid) < SEGMENT_DRIFT
            too_long = row["end_ms"] - current[0]["start_ms"] > SEGMENT_SPAN_MS or len(current) >= SEGMENT_MAX_WINDOWS
            if row["track_id"] != current[0]["track_id"] or too_long or (drifted and len(current) >= 2):
                close()
        current.append(row)
        total = vector.copy() if total is None else total + vector
    close()
    return segments


def provenance(segment_count: int, window_count: int) -> dict[str, Any]:
    return {"method": METHOD, "topicsVersion": TOPICS_VERSION, "embeddingModelVersion": semantic.model_version(),
            "segments": segment_count, "windows": window_count, "labels": None,
            "labelsNote": "no topic labels or summaries: no brain authority is wired to media",
            "generatedAt": time.time()}


# ------------------------------------------------------------------ search


def _excluded_clause() -> str:
    return ("NOT (EXISTS (SELECT 1 FROM files f WHERE f.asset_id = s.asset_id) "
            "AND NOT EXISTS (SELECT 1 FROM files f WHERE f.asset_id = s.asset_id "
            "AND (f.error IS NULL OR f.error != 'out_of_scope')))")


def segment_candidates(db: sqlite3.Connection, query, *, limit: int, language: str | None, asset_id: str | None,
                       check: Callable[[], None]) -> tuple[list[dict], dict]:
    """Top segments by centroid cosine, scanning in bounded chunks."""
    import numpy as np

    clauses, params = [_excluded_clause()], []
    if language:
        clauses.append("s.language = ?")
        params.append(language)
    if asset_id:
        clauses.append("s.asset_id = ?")
        params.append(asset_id)
    where = " AND ".join(clauses)
    heap: list[tuple[float, int, dict]] = []
    scanned, offset, counter, truncated = 0, 0, 0, False
    while True:
        check()
        rows = db.execute(
            "SELECT s.asset_id, s.segment_key, s.track_id, s.source, s.language, s.start_ms, s.end_ms, "
            f"s.window_keys, s.vector FROM topic_segments s WHERE {where} ORDER BY s.rowid "
            f"LIMIT {SCAN_CHUNK} OFFSET {offset}", params).fetchall()
        if not rows:
            break
        matrix = np.frombuffer(b"".join(row["vector"] for row in rows), dtype="<f4").reshape(len(rows), -1)
        for row, score in zip(rows, (matrix @ query).tolist()):
            counter += 1
            item = (float(score), -counter, {key: row[key] for key in row.keys() if key != "vector"})
            if len(heap) < limit:
                heapq.heappush(heap, item)
            elif item[0] > heap[0][0]:
                heapq.heapreplace(heap, item)
        scanned += len(rows)
        offset += SCAN_CHUNK
        if scanned >= MAX_SCAN_SEGMENTS:
            truncated = True
            break
    best = sorted(heap, key=lambda entry: (-entry[0], -entry[1]))
    return [dict(row, similarity=score) for score, _, row in best], {"scanned": scanned, "truncated": truncated}


def _evidence_windows(db: sqlite3.Connection, candidate: dict) -> list[sqlite3.Row]:
    keys = json.loads(candidate["window_keys"])
    if not keys:
        return []
    marks = ",".join("?" for _ in keys)
    return db.execute(
        "SELECT w.window_key, w.track_id, w.source, w.language, w.seq_from, w.seq_to, w.start_ms, w.end_ms, w.text, "
        f"c.vector FROM semantic_windows w JOIN semantic_cache c ON c.cache_key = w.cache_key "
        f"WHERE w.asset_id = ? AND w.window_key IN ({marks})", [candidate["asset_id"], *keys]).fetchall()


def _term_overlap(query_words: list[str], text: str) -> float:
    if not query_words:
        return 0.0
    present = set(words(text))
    return sum(1 for word in set(query_words) if word in present) / len(set(query_words))


def hint_terms(texts: list[str], query_words: list[str], limit: int = MAX_HINTS) -> list[str]:
    counts: dict[str, int] = {}
    for text in texts:
        for word in set(words(text)):
            if word not in query_words and len(word) >= 3 and not word.isdigit():
                counts[word] = counts.get(word, 0) + 1
    return [word for word, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def search_topics(db: sqlite3.Connection, query_text: str, query_vector: bytes, *, limit: int,
                  language: str | None, asset_id: str | None, check: Callable[[], None],
                  hint_vectors: list[bytes] | None = None) -> dict[str, Any]:
    """Evidence-backed topic hits (see the module docstring). Never returns a hit without indexed windows."""
    import numpy as np

    query = np.frombuffer(query_vector, dtype="<f4").astype("float64")
    query_words = words(query_text)
    pool = max(limit * 3, 10)
    first, scan = segment_candidates(db, query, limit=min(pool, MAX_RERANK), language=language,
                                     asset_id=asset_id, check=check)
    candidates: dict[str, dict] = {c["segment_key"]: dict(c, firstSimilarity=c["similarity"]) for c in first}
    best_first = max((c["similarity"] for c in first), default=0.0)
    expansion: dict[str, Any] = {"applied": False}
    expanded = None
    if first and (best_first < EXPANSION_TRIGGER or len(query_words) <= EXPANSION_SHORT_QUERY_WORDS):
        sources: list[tuple[float, sqlite3.Row]] = []
        for candidate in first[:EXPANSION_SOURCES]:
            check()
            for window in _evidence_windows(db, candidate):
                vector = np.frombuffer(window["vector"], dtype="<f4").astype("float64")
                sources.append((float(vector @ query), window))
        sources.sort(key=lambda item: -item[0])
        chosen = [window for _, window in sources[:EXPANSION_SOURCES]]
        if chosen:
            feedback = np.mean([np.frombuffer(w["vector"], dtype="<f4").astype("float64") for w in chosen], axis=0)
            expanded = _normalised(query + EXPANSION_WEIGHT * _normalised(feedback))
        if expanded is not None:
            second, _ = segment_candidates(db, expanded, limit=min(pool, MAX_RERANK), language=language,
                                           asset_id=asset_id, check=check)
            for candidate in second:
                held = candidates.setdefault(candidate["segment_key"], dict(candidate, firstSimilarity=None))
                held["expandedSimilarity"] = candidate["similarity"]
            expansion = {"applied": True, "method": "pseudo_relevance_feedback", "weight": EXPANSION_WEIGHT,
                         "trigger": "weak_first_pass" if best_first < EXPANSION_TRIGGER else "short_query",
                         "fromWindows": [{"trackId": w["track_id"], "startMs": w["start_ms"], "endMs": w["end_ms"]}
                                         for w in chosen],
                         "hints": hint_terms([w["text"] for w in chosen], query_words), "note": HINT_NOTE}
    for hint_vector in (hint_vectors or [])[:8]:
        # Generated search hints (qronos_media.brain_labels): more CANDIDATES only; every hit below still needs its
        # own indexed lines, and the hint's similarity only feeds the (fixed-weight) expansion component.
        hinted = np.frombuffer(hint_vector, dtype="<f4").astype("float64")
        found, _ = segment_candidates(db, hinted, limit=min(pool, MAX_RERANK), language=language,
                                      asset_id=asset_id, check=check)
        for candidate in found:
            held = candidates.setdefault(candidate["segment_key"], dict(candidate, firstSimilarity=None))
            held["expandedSimilarity"] = max(float(held.get("expandedSimilarity") or 0.0), candidate["similarity"])
    hits = []
    for candidate in list(candidates.values())[:MAX_RERANK]:
        check()
        windows = _evidence_windows(db, candidate)
        if not windows:
            continue  # a segment without indexed evidence is never a result
        matrix = np.frombuffer(b"".join(w["vector"] for w in windows), dtype="<f4").reshape(len(windows), -1)
        similarities = (matrix @ query).tolist()
        ranked = sorted(zip(similarities, windows), key=lambda item: -item[0])[:EVIDENCE_PER_HIT]
        segment_similarity = candidate["firstSimilarity"]
        if segment_similarity is None:
            centroid = _normalised(np.mean(matrix.astype("float64"), axis=0))
            segment_similarity = float(centroid @ query) if centroid is not None else 0.0
        text = " ".join(w["text"] for w in windows)
        components = {"segment": round(float(segment_similarity), 6), "evidence": round(float(ranked[0][0]), 6),
                      "terms": round(_term_overlap(query_words, text), 6),
                      "expansion": round(float(candidate.get("expandedSimilarity") or 0.0), 6)}
        score = sum(WEIGHTS[name] * value for name, value in components.items())
        hits.append({"assetId": candidate["asset_id"], "segmentKey": candidate["segment_key"],
                     "trackId": candidate["track_id"], "source": candidate["source"], "language": candidate["language"],
                     "startMs": candidate["start_ms"], "endMs": candidate["end_ms"], "score": round(score, 6),
                     "scoreKind": "rank_uncalibrated", "components": components,
                     "evidence": [{"startMs": w["start_ms"], "endMs": w["end_ms"], "text": w["text"],
                                   "seqFrom": w["seq_from"], "seqTo": w["seq_to"], "similarity": round(s, 6),
                                   "script": semantic.script_of(w["text"])} for s, w in ranked],
                     "windows": len(windows)})
    hits.sort(key=lambda hit: (-hit["score"], hit["assetId"], hit["startMs"]))
    return {"results": hits[:limit], "expansion": expansion, "scanned": scan["scanned"],
            "status": "partial" if scan["truncated"] else "complete", "scoreNote": SCORE_NOTE,
            "weights": dict(WEIGHTS), "queryScript": semantic.script_of(query_text)}


def asset_segments(db: sqlite3.Connection, asset_id: str, *, limit: int = 50, lines: int = 2) -> list[dict]:
    """The asset's topic segments with their most central lines (for reports): evidence, never a label."""
    import numpy as np

    out = []
    for row in db.execute("SELECT segment_key, track_id, source, language, start_ms, end_ms, window_keys, vector "
                          "FROM topic_segments WHERE asset_id = ? ORDER BY start_ms, track_id LIMIT ?",
                          (asset_id, limit)):
        candidate = dict(row, asset_id=asset_id)
        windows = _evidence_windows(db, candidate)
        if not windows:
            continue
        centroid = np.frombuffer(row["vector"], dtype="<f4")
        matrix = np.frombuffer(b"".join(w["vector"] for w in windows), dtype="<f4").reshape(len(windows), -1)
        central = sorted(zip((matrix @ centroid).tolist(), windows), key=lambda item: -item[0])[:lines]
        out.append({"segmentKey": row["segment_key"], "trackId": row["track_id"], "source": row["source"],
                    "language": row["language"],
                    "startMs": row["start_ms"], "endMs": row["end_ms"], "windows": len(windows), "label": None,
                    "lines": [{"startMs": w["start_ms"], "endMs": w["end_ms"], "text": w["text"]} for _, w in central]})
    return out
