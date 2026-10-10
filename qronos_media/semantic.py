"""Semantic dialogue retrieval (handoff Level 2) with the fixed bge-m3 profile of Qronos's memory sidecar.

The embedding model and its server are not chosen here. Qronos already owns one
CPU-only Ollama sidecar for bge-m3 (``core/memory_runtime.py``: 127.0.0.1:11435,
``num_gpu 0``, ``keep_alive 1h``, one loaded model, a Windows guard mutex, an
exact process identity). :class:`MediaEmbedder` uses exactly that profile, taken
from those constants, through ``core.local_ipc.request_json`` with the owned
process as ``owner`` (the accepted connection itself must belong to it). It never
starts a server, never takes a URL, port or model from a caller and never unloads
anything. Before embedding it checks that the sidecar serves bge-m3 in F16; every
vector must have exactly 1024 finite values. Responses are byte-bounded.

Windows are consecutive cues of one track (bounded characters and span, one cue
of overlap) with their original text and lineage. Vectors live in
``semantic_cache`` keyed by (model, version, dimension, text), so unchanged text is
never embedded twice; the searchable ``semantic_windows`` rows are written only
when the stage completes.

Scores are cosine similarities between normalised vectors: an uncalibrated
similarity, not a probability, a percentage or an accuracy. bge-m3 is multilingual,
so a query may match dialogue in another language; nothing is translated.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import heapq
import json
import math
import sqlite3
import struct
from typing import Any, Callable, Sequence

from qronos_media.resources import BGE_M3, ResourceRefused

SEMANTIC_VERSION = 1
DIMENSION = BGE_M3.dimension
ACCEPTED_MODEL_NAMES = frozenset({"bge-m3", "bge-m3:latest"})
REQUIRED_QUANTIZATION = "F16"
EMBED_BATCH = 8
MAX_TEXT_CHARS = 1_200
WINDOW_CHARS = 600
WINDOW_SPAN_MS = 30_000
MAX_WINDOWS_PER_ASSET = 20_000
EMBED_TIMEOUT_SECONDS = 30.0
QUERY_TIMEOUT_SECONDS = 5.0
TAGS_TIMEOUT_SECONDS = 2.0
MAX_TAGS_BYTES = 256 * 1024
BYTES_PER_VECTOR_JSON = 40 * DIMENSION  # generous: a float in JSON is far below 40 characters
SCAN_CHUNK = 2_000
MAX_SCAN_WINDOWS = 200_000
SCORE_NOTE = "cosine similarity of bge-m3 vectors: uncalibrated, not a probability, percentage or accuracy"


class EmbeddingInvalid(RuntimeError):
    """The sidecar answered with something that is not a bge-m3 F16 1024-dimension result; nothing is stored."""


def _fixed_profile() -> dict[str, Any]:
    from core.local_ipc import local_base_url
    from core.memory_runtime import MEMORY_SIDECAR_BASE_URL, MEMORY_SIDECAR_KEEP_ALIVE, MEMORY_SIDECAR_PORT
    from core.user_memory import DEFAULT_EMBEDDING_MODEL, DEFAULT_EMBEDDING_VERSION

    return {"base": local_base_url(MEMORY_SIDECAR_BASE_URL, required_port=MEMORY_SIDECAR_PORT),
            "model": DEFAULT_EMBEDDING_MODEL, "version": DEFAULT_EMBEDDING_VERSION,
            "keep_alive": MEMORY_SIDECAR_KEEP_ALIVE}


def model_version() -> str:
    return f"{_fixed_profile()['version']}+media{SEMANTIC_VERSION}"


class MediaEmbedder:
    """bge-m3 through the runtime-owned memory sidecar, with the provider's exact request profile."""

    def __init__(self, owner_check: Callable[[], object], *, transport: Callable[..., Any] | None = None) -> None:
        from core.local_ipc import request_json

        self._owner_check = owner_check
        self._transport = transport or request_json  # tests: a fake HTTP transport only
        self._profile = _fixed_profile()
        self._verified = False
        self._last_owner: object = None  # the owned sidecar process last used (diagnostics only; not a proof)

    def _owner(self) -> object:
        try:
            owner = self._owner_check()
        except Exception:  # noqa: BLE001 - unreadable ownership is no ownership
            owner = None
        if owner is None or owner is False:
            raise ResourceRefused("model_unavailable", "the bge-m3 memory sidecar is not running under Qronos ownership")
        self._last_owner = owner
        return owner

    def unavailable_reason(self) -> str | None:
        """Asked BEFORE a model slot is reserved (no IPC): "model_unavailable" while no owned sidecar process
        exists, so nothing is reserved for a model that cannot be asked; None when a request may be tried."""
        try:
            self._owner()
        except ResourceRefused as refused:
            return refused.reason
        return None

    def residency(self) -> str:
        """Whether the sidecar still lists bge-m3: "loaded", "unloaded" or "unknown" (never a guess).

        "unloaded": the owned sidecar's own /api/ps no longer lists bge-m3 (its runner was let go). That frees a
        RESIDENT slot only; it never proves that a failed or timed-out request has drained (a queued request may
        not be listed yet). The server process merely exiting is not reported as proof either: its model runner
        may outlive it. No current owner is "unknown", which keeps the reservation counted.
        """
        try:
            owner = self._owner_check()
        except Exception:  # noqa: BLE001
            owner = None
        if owner is None or owner is False:
            return "unknown"
        try:
            listed = self._transport("GET", self._profile["base"] + "/api/ps", timeout=TAGS_TIMEOUT_SECONDS,
                                     json=None, owner=owner, max_bytes=MAX_TAGS_BYTES)
        except Exception:  # noqa: BLE001
            return "unknown"
        models = listed.get("models") if isinstance(listed, dict) else None
        if not isinstance(models, list):
            return "unknown"
        for item in models:
            if isinstance(item, dict) and {str(item.get("name", "")).casefold(),
                                           str(item.get("model", "")).casefold()} & ACCEPTED_MODEL_NAMES:
                return "loaded"
        return "unloaded"

    def _call(self, method: str, path: str, *, timeout: float, max_bytes: int, payload: dict | None = None,
              failure: str = "embedding_request_failed", on_dispatch: Callable[[], None] | None = None) -> Any:
        owner = self._owner()  # no owner: refused before anything is sent
        if on_dispatch is not None:
            on_dispatch()  # from here on the request may reach the server
        try:
            return self._transport(method, self._profile["base"] + path, timeout=timeout, json=payload,
                                   owner=owner, max_bytes=max_bytes)
        except ResourceRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - IPC failures are typed, retried later
            raise ResourceRefused(failure, type(exc).__name__) from None

    def verify(self) -> None:
        """The sidecar must serve bge-m3 in F16 (checked once per embedder).

        /api/tags starts no inference, so its failure is a capability refusal ("model_unavailable"), never an
        unresolved request."""
        if self._verified:
            return
        tags = self._call("GET", "/api/tags", timeout=TAGS_TIMEOUT_SECONDS, max_bytes=MAX_TAGS_BYTES,
                          failure="model_unavailable")
        models = tags.get("models") if isinstance(tags, dict) else None
        for item in models if isinstance(models, list) else []:
            if not isinstance(item, dict):
                continue
            names = {str(item.get("name", "")).casefold(), str(item.get("model", "")).casefold()}
            details = item.get("details") if isinstance(item.get("details"), dict) else {}
            if names & ACCEPTED_MODEL_NAMES and details.get("quantization_level") == REQUIRED_QUANTIZATION:
                self._verified = True
                return
        raise ResourceRefused("model_unavailable", "the sidecar does not serve bge-m3 in F16")

    def embed(self, texts: Sequence[str], *, timeout: float = EMBED_TIMEOUT_SECONDS,
              on_dispatch: Callable[[], None] | None = None) -> list[bytes]:
        """Normalised float32 vectors (little-endian bytes) for at most EMBED_BATCH bounded texts.

        ``on_dispatch`` is called once the owner is resolved and the model verified, immediately before the POST
        that may make the sidecar load bge-m3 (the lease's evidence that a request was possibly dispatched)."""
        cleaned = [str(text).strip() for text in texts]
        if not cleaned or len(cleaned) > EMBED_BATCH or any(not t or len(t) > MAX_TEXT_CHARS for t in cleaned):
            raise ValueError("embedding input must be 1..8 non-empty texts of at most 1200 characters")
        self.verify()
        payload = {"model": self._profile["model"], "input": cleaned, "truncate": False,
                   "keep_alive": self._profile["keep_alive"], "options": {"num_gpu": 0}}
        answer = self._call("POST", "/api/embed", timeout=timeout,
                            max_bytes=BYTES_PER_VECTOR_JSON * len(cleaned) + 4096, payload=payload,
                            on_dispatch=on_dispatch)
        if not isinstance(answer, dict):
            raise EmbeddingInvalid("the embedding answer is not an object")
        echoed = answer.get("model")
        if echoed is not None and str(echoed).casefold() not in ACCEPTED_MODEL_NAMES:
            raise EmbeddingInvalid(f"the sidecar answered for another model: {str(echoed)[:60]!r}")
        vectors = answer.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(cleaned):
            raise EmbeddingInvalid("the embedding answer has the wrong number of vectors")
        out = []
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != DIMENSION:
                raise EmbeddingInvalid("a vector does not have exactly 1024 values")
            values = []
            for value in vector:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise EmbeddingInvalid("a vector holds a non-finite or non-numeric value")
                values.append(float(value))
            norm = math.sqrt(sum(v * v for v in values))
            if not norm > 1e-12:
                raise EmbeddingInvalid("a vector has zero length")
            out.append(encode([v / norm for v in values]))
        return out


def encode(values: Sequence[float]) -> bytes:
    return struct.pack(f"<{DIMENSION}f", *values)


def decode_many(blobs: Sequence[bytes]):
    import numpy as np

    return np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), DIMENSION)


# ------------------------------------------------------------------ windows

@dataclass(frozen=True)
class Window:
    track_id: str
    source: str
    language: str | None
    seq_from: int
    seq_to: int
    start_ms: int
    end_ms: int
    text: str
    cache_key: str
    window_key: str


def cache_key(text: str) -> str:
    profile = _fixed_profile()
    material = json.dumps([profile["model"], REQUIRED_QUANTIZATION, model_version(), DIMENSION, text],
                          ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_windows(db: sqlite3.Connection, asset_id: str) -> list[Window]:
    """Consecutive indexed cues of each track, bounded in characters and span, with one cue of overlap."""
    rows = db.execute(
        "SELECT d.track_id, d.seq, d.start_ms, d.end_ms, d.text, t.source, t.language FROM dialogue d "
        "JOIN subtitle_tracks t ON t.track_id = d.track_id WHERE d.asset_id = ? AND d.kind = 'cue' "
        "AND t.status = 'indexed' ORDER BY d.track_id, d.seq", (asset_id,)).fetchall()
    by_track: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_track.setdefault(row["track_id"], []).append(row)
    windows: list[Window] = []
    for track_id, cues in by_track.items():
        i = 0
        while i < len(cues) and len(windows) < MAX_WINDOWS_PER_ASSET:
            j = i
            chars = len(cues[i]["text"])
            while j + 1 < len(cues):
                nxt = cues[j + 1]
                if chars + 1 + len(nxt["text"]) > WINDOW_CHARS or nxt["end_ms"] - cues[i]["start_ms"] > WINDOW_SPAN_MS:
                    break
                j += 1
                chars += 1 + len(nxt["text"])
            text = " ".join(c["text"].strip() for c in cues[i:j + 1]).strip()[:MAX_TEXT_CHARS]
            if text:
                key = cache_key(text)
                window_key = hashlib.sha256(json.dumps([track_id, cues[i]["seq"], cues[j]["seq"], key]).encode()
                                            ).hexdigest()
                windows.append(Window(track_id, cues[i]["source"], cues[i]["language"], cues[i]["seq"],
                                      cues[j]["seq"], cues[i]["start_ms"], cues[j]["end_ms"], text, key, window_key))
            if j + 1 >= len(cues):
                break
            i = j if j > i else i + 1  # one cue of overlap with the next window
    return windows


def script_of(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "none"
    arabic = sum(1 for c in letters if "؀" <= c <= "ۿ" or "ݐ" <= c <= "ݿ")
    return "arabic" if arabic * 2 >= len(letters) else "latin_or_other"


# ------------------------------------------------------------------ search

def semantic_candidates(db: sqlite3.Connection, query_vector: bytes, *, limit: int, language: str | None,
                        asset_id: str | None, check: Callable[[], None]) -> tuple[list[dict], dict]:
    """Top windows by cosine, scanning stored vectors in bounded chunks (``check`` raises when the budget ends)."""
    import numpy as np

    query = np.frombuffer(query_vector, dtype="<f4")
    clauses = ["NOT (EXISTS (SELECT 1 FROM files f WHERE f.asset_id = w.asset_id) "
               "AND NOT EXISTS (SELECT 1 FROM files f WHERE f.asset_id = w.asset_id "
               "AND (f.error IS NULL OR f.error != 'out_of_scope')))"]
    params: list[Any] = []
    if language:
        clauses.append("w.language = ?")
        params.append(language)
    if asset_id:
        clauses.append("w.asset_id = ?")
        params.append(asset_id)
    where = " AND ".join(clauses)
    heap: list[tuple[float, int, dict]] = []
    scanned, offset, counter = 0, 0, 0
    truncated = False
    while True:
        check()
        rows = db.execute(
            "SELECT w.asset_id, w.track_id, w.source, w.language, w.seq_from, w.seq_to, w.start_ms, w.end_ms, "
            f"w.text, c.vector FROM semantic_windows w JOIN semantic_cache c ON c.cache_key = w.cache_key WHERE {where} "
            f"ORDER BY w.rowid LIMIT {SCAN_CHUNK} OFFSET {offset}", params).fetchall()
        if not rows:
            break
        matrix = decode_many([row["vector"] for row in rows])
        scores = matrix @ query
        for row, score in zip(rows, scores.tolist()):
            counter += 1
            item = (float(score), -counter, {key: row[key] for key in row.keys() if key != "vector"})
            if len(heap) < limit:
                heapq.heappush(heap, item)
            elif item[0] > heap[0][0]:
                heapq.heapreplace(heap, item)
        scanned += len(rows)
        offset += SCAN_CHUNK
        if scanned >= MAX_SCAN_WINDOWS:
            truncated = True
            break
    best = sorted(heap, key=lambda entry: (-entry[0], -entry[1]))
    return [dict(row, score=round(score, 6)) for score, _, row in best], {"scanned": scanned, "truncated": truncated}
