from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Protocol, Sequence
import uuid

import numpy as np
import requests

from core.storage_locations import ensure_storage_root


MEMORY_SCHEMA_VERSION = 1
DEFAULT_EMBEDDING_MODEL = "bge-m3"
DEFAULT_EMBEDDING_VERSION = "provisional-v1"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"

TRUSTED_SOURCE_KINDS = {"user", "user_correction", "verified_behavior"}
EVIDENCE_TYPES = {"explicit", "repeated", "inferred"}
SENSITIVITY_LEVELS = {"normal", "personal", "sensitive", "secret"}
CATEGORIES = {
    "preference",
    "interaction_style",
    "persistent_fact",
    "routine",
    "project_context",
    "learned_behavior",
}
SCOPE_TYPES = {"global", "project", "task_type", "output_format"}

STATUS_PENDING_REVIEW = "PENDING_REVIEW"
STATUS_PENDING_EMBEDDING = "PENDING_EMBEDDING"
STATUS_ACTIVE = "ACTIVE"
STATUS_SUPERSEDED = "SUPERSEDED"

_MIN_INFERRED_CONFIDENCE = 0.75


class MemoryPolicyError(ValueError):
    pass


class EmbeddingUnavailable(RuntimeError):
    pass


class ReindexRequired(RuntimeError):
    pass


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str:
        ...

    @property
    def model_version(self) -> str:
        ...

    def available(self) -> tuple[bool, str]:
        ...

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        ...


class OllamaEmbeddingProvider:
    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        *,
        model_version: str = DEFAULT_EMBEDDING_VERSION,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        timeout_seconds: float = 12.0,
    ) -> None:
        cleaned = base_url.rstrip("/")
        if cleaned not in {"http://127.0.0.1:11434", "http://localhost:11434"}:
            raise ValueError("Memory embedding endpoint must remain local in V1.")
        self._model_name = model_name.strip()
        self._model_version = model_version.strip() or DEFAULT_EMBEDDING_VERSION
        self._base_url = cleaned
        self._timeout = max(1.0, float(timeout_seconds))

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_version(self) -> str:
        return self._model_version

    def available(self) -> tuple[bool, str]:
        if not self._model_name:
            return False, "embedding_model_not_configured"
        try:
            response = requests.get(
                f"{self._base_url}/api/tags",
                timeout=min(self._timeout, 4.0),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return False, f"ollama_unavailable:{type(exc).__name__}"
        models = payload.get("models", []) if isinstance(payload, dict) else []
        wanted = self._model_name.lower()
        for item in models:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).lower()
            model = str(item.get("model", "")).lower()
            if (
                name == wanted
                or model == wanted
                or name.split(":", 1)[0] == wanted.split(":", 1)[0]
                or model.split(":", 1)[0] == wanted.split(":", 1)[0]
            ):
                return True, "ready"
        return False, "embedding_model_not_installed"

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        cleaned = [str(text).strip() for text in texts]
        if not cleaned or any(not text for text in cleaned):
            raise ValueError("Embedding input must contain non-empty text.")
        ready, reason = self.available()
        if not ready:
            raise EmbeddingUnavailable(reason)
        response = requests.post(
            f"{self._base_url}/api/embed",
            json={
                "model": self._model_name,
                "input": cleaned,
                "truncate": False,
                "keep_alive": "0s",
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()
        vectors = payload.get("embeddings") if isinstance(payload, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(cleaned):
            raise RuntimeError("Ollama embedding response has an invalid shape.")
        matrix = np.asarray(vectors, dtype=np.float32)
        return _normalize_matrix(matrix)


class DeterministicEmbeddingProvider:
    """Tests only. Never selected by the production MemoryStore factory."""

    def __init__(self, dimensions: int = 32, *, model_name: str = "deterministic-test") -> None:
        self._dimensions = max(8, int(dimensions))
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_version(self) -> str:
        return "test-v1"

    def available(self) -> tuple[bool, str]:
        return True, "ready"

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        rows: list[np.ndarray] = []
        for text in texts:
            digest = hashlib.sha256(str(text).encode("utf-8")).digest()
            raw = bytearray()
            counter = 0
            while len(raw) < self._dimensions * 4:
                raw.extend(hashlib.sha256(digest + counter.to_bytes(4, "little")).digest())
                counter += 1
            values = np.frombuffer(bytes(raw[: self._dimensions * 4]), dtype=np.uint32)
            row = (values.astype(np.float64) / np.iinfo(np.uint32).max) * 2.0 - 1.0
            rows.append(row.astype(np.float32))
        return _normalize_matrix(np.vstack(rows))


class UnavailableEmbeddingProvider:
    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        *,
        reason: str = "embedding_provider_unavailable",
    ) -> None:
        self._model_name = model_name
        self._reason = reason

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_version(self) -> str:
        return DEFAULT_EMBEDDING_VERSION

    def available(self) -> tuple[bool, str]:
        return False, self._reason

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        raise EmbeddingUnavailable(self._reason)


def _normalize_matrix(matrix: np.ndarray) -> np.ndarray:
    value = np.asarray(matrix, dtype=np.float32)
    if value.ndim != 2 or value.shape[0] == 0 or value.shape[1] == 0:
        raise ValueError("Embedding matrix must be two-dimensional and non-empty.")
    if not np.isfinite(value).all():
        raise ValueError("Embedding matrix contains non-finite values.")
    norms = np.linalg.norm(value, axis=1, keepdims=True)
    if np.any(norms <= 1e-12):
        raise ValueError("Embedding vector must not be zero length.")
    return (value / norms).astype(np.float32, copy=False)


def _utc_now() -> float:
    return time.time()


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_required(value: str, label: str, *, max_chars: int = 8000) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError(f"{label} must not be empty.")
    if len(cleaned) > max_chars:
        raise ValueError(f"{label} is too long.")
    return cleaned


def _scope_score(memory_scope: str, memory_key: str, requested: dict[str, str]) -> float:
    if memory_scope == "global":
        return 1.0
    requested_key = requested.get(memory_scope, "").strip()
    if not requested_key:
        return 0.0
    return 1.0 if requested_key.casefold() == memory_key.casefold() else 0.0


def _evidence_score(value: str) -> float:
    return {"explicit": 1.0, "repeated": 0.8, "inferred": 0.5}.get(value, 0.0)


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    memory_key: str
    value: str
    category: str
    scope_type: str
    scope_key: str
    confidence: float
    evidence_type: str
    sensitivity: str
    status: str
    source_kind: str
    source_conversation_id: str
    source_message_id: str
    source_request_id: str
    created_at: float
    updated_at: float
    last_confirmed_at: float
    last_used_at: float
    embedding_model: str
    embedding_version: str
    embedding_dimensions: int
    supersedes_id: str

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "memoryKey": self.memory_key,
            "value": self.value,
            "category": self.category,
            "scopeType": self.scope_type,
            "scopeKey": self.scope_key,
            "confidence": self.confidence,
            "evidenceType": self.evidence_type,
            "sensitivity": self.sensitivity,
            "status": self.status,
            "sourceKind": self.source_kind,
            "sourceConversationId": self.source_conversation_id,
            "sourceMessageId": self.source_message_id,
            "sourceRequestId": self.source_request_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "lastConfirmedAt": self.last_confirmed_at,
            "lastUsedAt": self.last_used_at,
            "embeddingModel": self.embedding_model,
            "embeddingVersion": self.embedding_version,
            "embeddingDimensions": self.embedding_dimensions,
            "supersedesId": self.supersedes_id,
        }


class MemoryVectorIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _empty(self) -> tuple[list[str], np.ndarray, str, str]:
        return [], np.empty((0, 0), dtype=np.float32), "", ""

    def load(self) -> tuple[list[str], np.ndarray, str, str]:
        with self._lock:
            if not self.path.is_file():
                return self._empty()
            try:
                with np.load(self.path, allow_pickle=False) as payload:
                    ids = [str(value) for value in payload["ids"].tolist()]
                    vectors = np.asarray(payload["vectors"], dtype=np.float32)
                    model = str(payload["model"].tolist()[0]) if payload["model"].size else ""
                    version = str(payload["version"].tolist()[0]) if payload["version"].size else ""
            except Exception as exc:
                raise RuntimeError(f"Memory vector index is unreadable: {exc}") from exc
            if vectors.ndim != 2:
                raise RuntimeError("Memory vector index has an invalid vector shape.")
            if vectors.shape[0] != len(ids):
                raise RuntimeError("Memory vector index id/vector count mismatch.")
            return ids, vectors, model, version

    def _write(
        self,
        ids: Sequence[str],
        vectors: np.ndarray,
        *,
        model: str,
        version: str,
    ) -> None:
        ids_list = [str(value) for value in ids]
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2:
            raise ValueError("Vector index matrix must be two-dimensional.")
        if matrix.shape[0] != len(ids_list):
            raise ValueError("Vector index id/vector count mismatch.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp.npz")
        max_id = max([1, *(len(value) for value in ids_list)])
        np.savez_compressed(
            tmp,
            ids=np.asarray(ids_list, dtype=f"<U{max_id}"),
            vectors=matrix,
            model=np.asarray([model], dtype=f"<U{max(1, len(model))}"),
            version=np.asarray([version], dtype=f"<U{max(1, len(version))}"),
        )
        os.replace(tmp, self.path)

    def stats(self) -> dict[str, object]:
        ids, vectors, model, version = self.load()
        return {
            "count": len(ids),
            "dimensions": int(vectors.shape[1]) if vectors.ndim == 2 and vectors.shape[0] else 0,
            "model": model,
            "version": version,
            "path": str(self.path),
        }

    def replace_all(
        self,
        ids: Sequence[str],
        vectors: np.ndarray,
        *,
        model: str,
        version: str,
    ) -> None:
        with self._lock:
            matrix = np.asarray(vectors, dtype=np.float32)
            if len(ids):
                matrix = _normalize_matrix(matrix)
            elif matrix.shape != (0, 0):
                matrix = np.empty((0, 0), dtype=np.float32)
            self._write(ids, matrix, model=model, version=version)

    def upsert(
        self,
        memory_id: str,
        vector: np.ndarray,
        *,
        model: str,
        version: str,
    ) -> None:
        row = _normalize_matrix(np.asarray(vector, dtype=np.float32).reshape(1, -1))[0]
        with self._lock:
            ids, vectors, current_model, current_version = self.load()
            if ids and (current_model != model or current_version != version):
                raise ReindexRequired(
                    f"Vector index uses {current_model}/{current_version}; "
                    f"provider uses {model}/{version}."
                )
            if ids and vectors.shape[1] != row.shape[0]:
                raise ReindexRequired("Embedding dimensions changed; reindex is required.")
            if memory_id in ids:
                index = ids.index(memory_id)
                vectors[index] = row
                matrix = vectors
            else:
                ids.append(memory_id)
                matrix = row.reshape(1, -1) if vectors.size == 0 else np.vstack([vectors, row])
            self._write(ids, matrix, model=model, version=version)

    def remove(self, memory_id: str) -> None:
        with self._lock:
            ids, vectors, model, version = self.load()
            if memory_id not in ids:
                return
            index = ids.index(memory_id)
            next_ids = ids[:index] + ids[index + 1 :]
            if next_ids:
                matrix = np.delete(vectors, index, axis=0)
            else:
                matrix = np.empty((0, 0), dtype=np.float32)
            self._write(next_ids, matrix, model=model, version=version)

    def search(
        self,
        query_vector: np.ndarray,
        *,
        limit: int = 20,
        expected_model: str,
        expected_version: str,
    ) -> list[tuple[str, float]]:
        row = _normalize_matrix(np.asarray(query_vector, dtype=np.float32).reshape(1, -1))[0]
        ids, vectors, model, version = self.load()
        if not ids:
            return []
        if model != expected_model or version != expected_version:
            raise ReindexRequired("Vector index model/version differs from the active provider.")
        if vectors.shape[1] != row.shape[0]:
            raise ReindexRequired("Vector index dimensions differ from the active provider.")
        scores = vectors @ row
        order = np.argsort(-scores)[: max(1, int(limit))]
        return [(ids[int(index)], float(scores[int(index)])) for index in order]


class MemoryStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        vector_path: str | Path | None = None,
        *,
        provider: EmbeddingProvider | None = None,
    ) -> None:
        # An explicit database path decides the folder; the locator is read
        # only when no path was given, and only the memory root is created.
        if db_path is not None:
            root = Path(db_path).parent
        else:
            root = ensure_storage_root("memoryRoot")
        self.db_path = Path(db_path) if db_path is not None else root / "user_memory.sqlite3"
        self.vector_path = (
            Path(vector_path)
            if vector_path is not None
            else root / "memory_vectors.npz"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vector_index = MemoryVectorIndex(self.vector_path)
        self._lock = threading.RLock()
        self._ensure_schema()
        self.provider = provider if provider is not None else self._production_provider()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._lock:
            with self._connection() as c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS memory_records (
                        id TEXT PRIMARY KEY,
                        memory_key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        category TEXT NOT NULL,
                        scope_type TEXT NOT NULL,
                        scope_key TEXT NOT NULL DEFAULT '',
                        confidence REAL NOT NULL,
                        evidence_type TEXT NOT NULL,
                        sensitivity TEXT NOT NULL,
                        status TEXT NOT NULL,
                        source_kind TEXT NOT NULL,
                        source_conversation_id TEXT NOT NULL DEFAULT '',
                        source_message_id TEXT NOT NULL DEFAULT '',
                        source_request_id TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        last_confirmed_at REAL NOT NULL DEFAULT 0,
                        last_used_at REAL NOT NULL DEFAULT 0,
                        embedding_model TEXT NOT NULL DEFAULT '',
                        embedding_version TEXT NOT NULL DEFAULT '',
                        embedding_dimensions INTEGER NOT NULL DEFAULT 0,
                        supersedes_id TEXT NOT NULL DEFAULT ''
                    )
                """)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS memory_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                """)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS memory_tombstones (
                        memory_id TEXT PRIMARY KEY,
                        deleted_at REAL NOT NULL,
                        content_hash TEXT NOT NULL
                    )
                """)
                c.execute("""
                    CREATE INDEX IF NOT EXISTS idx_memory_status
                    ON memory_records(status)
                """)
                c.execute("""
                    CREATE INDEX IF NOT EXISTS idx_memory_scope
                    ON memory_records(scope_type, scope_key)
                """)
                c.execute("""
                    CREATE INDEX IF NOT EXISTS idx_memory_key
                    ON memory_records(memory_key)
                """)
                defaults = {
                    "schema_version": str(MEMORY_SCHEMA_VERSION),
                    "memory_enabled": "1",
                    "embedding_model": DEFAULT_EMBEDDING_MODEL,
                    "embedding_version": DEFAULT_EMBEDDING_VERSION,
                    "embedding_base_url": DEFAULT_OLLAMA_BASE_URL,
                }
                for key, value in defaults.items():
                    c.execute(
                        "INSERT OR IGNORE INTO memory_settings(key,value) VALUES (?,?)",
                        (key, value),
                    )

    def _setting(self, key: str, fallback: str = "") -> str:
        with self._connection() as c:
            row = c.execute(
                "SELECT value FROM memory_settings WHERE key=?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row is not None else fallback

    def _production_provider(self) -> EmbeddingProvider:
        model = self._setting("embedding_model", DEFAULT_EMBEDDING_MODEL)
        version = self._setting("embedding_version", DEFAULT_EMBEDDING_VERSION)
        base_url = self._setting("embedding_base_url", DEFAULT_OLLAMA_BASE_URL)
        try:
            return OllamaEmbeddingProvider(
                model,
                model_version=version,
                base_url=base_url,
            )
        except Exception as exc:
            return UnavailableEmbeddingProvider(
                model,
                reason=f"embedding_provider_configuration_error:{type(exc).__name__}",
            )

    def memory_enabled(self) -> bool:
        return self._setting("memory_enabled", "1") == "1"

    def set_memory_enabled(self, enabled: bool) -> None:
        with self._connection() as c:
            c.execute(
                "INSERT INTO memory_settings(key,value) VALUES ('memory_enabled',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if enabled else "0",),
            )

    def _row(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=str(row["id"]),
            memory_key=str(row["memory_key"]),
            value=str(row["value"]),
            category=str(row["category"]),
            scope_type=str(row["scope_type"]),
            scope_key=str(row["scope_key"]),
            confidence=float(row["confidence"]),
            evidence_type=str(row["evidence_type"]),
            sensitivity=str(row["sensitivity"]),
            status=str(row["status"]),
            source_kind=str(row["source_kind"]),
            source_conversation_id=str(row["source_conversation_id"]),
            source_message_id=str(row["source_message_id"]),
            source_request_id=str(row["source_request_id"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_confirmed_at=float(row["last_confirmed_at"]),
            last_used_at=float(row["last_used_at"]),
            embedding_model=str(row["embedding_model"]),
            embedding_version=str(row["embedding_version"]),
            embedding_dimensions=int(row["embedding_dimensions"]),
            supersedes_id=str(row["supersedes_id"]),
        )

    def get(self, memory_id: str) -> MemoryRecord:
        with self._connection() as c:
            row = c.execute(
                "SELECT * FROM memory_records WHERE id=?",
                (memory_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown memory: {memory_id}")
        return self._row(row)

    def list(self, *, include_inactive: bool = True) -> list[MemoryRecord]:
        query = "SELECT * FROM memory_records"
        params: tuple[object, ...] = ()
        if not include_inactive:
            query += " WHERE status='ACTIVE'"
        query += " ORDER BY updated_at DESC, id DESC"
        with self._connection() as c:
            rows = c.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def _validate_policy(
        self,
        *,
        category: str,
        scope_type: str,
        scope_key: str,
        confidence: float,
        evidence_type: str,
        sensitivity: str,
        source_kind: str,
        user_confirmed_sensitive: bool,
    ) -> str:
        if category not in CATEGORIES:
            raise MemoryPolicyError(f"Unsupported memory category: {category}")
        if scope_type not in SCOPE_TYPES:
            raise MemoryPolicyError(f"Unsupported memory scope: {scope_type}")
        if scope_type != "global" and not scope_key:
            raise MemoryPolicyError("Scoped memory requires a scope key.")
        if not (0.0 <= confidence <= 1.0) or not math.isfinite(confidence):
            raise MemoryPolicyError("Memory confidence must be between 0 and 1.")
        if evidence_type not in EVIDENCE_TYPES:
            raise MemoryPolicyError(f"Unsupported evidence type: {evidence_type}")
        if sensitivity not in SENSITIVITY_LEVELS:
            raise MemoryPolicyError(f"Unsupported sensitivity: {sensitivity}")
        if source_kind not in TRUSTED_SOURCE_KINDS:
            raise MemoryPolicyError(
                "Untrusted content cannot create User Memory directly."
            )
        if sensitivity == "secret":
            raise MemoryPolicyError("Secrets must never be stored in User Memory.")
        if sensitivity == "sensitive" and not user_confirmed_sensitive:
            return STATUS_PENDING_REVIEW
        if evidence_type == "inferred" and confidence < _MIN_INFERRED_CONFIDENCE:
            return STATUS_PENDING_REVIEW
        return STATUS_PENDING_EMBEDDING

    def create_candidate(
        self,
        *,
        value: str,
        category: str,
        memory_key: str = "",
        scope_type: str = "global",
        scope_key: str = "",
        confidence: float = 1.0,
        evidence_type: str = "explicit",
        sensitivity: str = "normal",
        source_kind: str = "user",
        source_conversation_id: str = "",
        source_message_id: str = "",
        source_request_id: str = "",
        user_confirmed_sensitive: bool = False,
        supersedes_id: str = "",
    ) -> MemoryRecord:
        cleaned_value = _clean_required(value, "Memory value")
        cleaned_category = category.strip().lower()
        cleaned_scope = scope_type.strip().lower()
        cleaned_scope_key = scope_key.strip()
        cleaned_evidence = evidence_type.strip().lower()
        cleaned_sensitivity = sensitivity.strip().lower()
        cleaned_source = source_kind.strip().lower()
        cleaned_memory_key = memory_key.strip() or (
            f"{cleaned_category}:{cleaned_scope}:{cleaned_scope_key}:"
            f"{_content_hash(cleaned_value)[:16]}"
        )
        planned_status = self._validate_policy(
            category=cleaned_category,
            scope_type=cleaned_scope,
            scope_key=cleaned_scope_key,
            confidence=float(confidence),
            evidence_type=cleaned_evidence,
            sensitivity=cleaned_sensitivity,
            source_kind=cleaned_source,
            user_confirmed_sensitive=bool(user_confirmed_sensitive),
        )
        if supersedes_id:
            old = self.get(supersedes_id)
            if old.status != STATUS_ACTIVE:
                raise MemoryPolicyError("Only an active memory can be superseded.")
        now = _utc_now()
        memory_id = f"mem-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        with self._connection() as c:
            c.execute("""
                INSERT INTO memory_records (
                    id,memory_key,value,category,scope_type,scope_key,confidence,
                    evidence_type,sensitivity,status,source_kind,
                    source_conversation_id,source_message_id,source_request_id,
                    created_at,updated_at,last_confirmed_at,last_used_at,
                    embedding_model,embedding_version,embedding_dimensions,
                    supersedes_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                memory_id,
                cleaned_memory_key,
                cleaned_value,
                cleaned_category,
                cleaned_scope,
                cleaned_scope_key,
                float(confidence),
                cleaned_evidence,
                cleaned_sensitivity,
                planned_status,
                cleaned_source,
                source_conversation_id.strip(),
                source_message_id.strip(),
                source_request_id.strip(),
                now,
                now,
                now if cleaned_evidence == "explicit" else 0.0,
                0.0,
                "",
                "",
                0,
                supersedes_id.strip(),
            ))
        created = self.get(memory_id)
        if planned_status == STATUS_PENDING_EMBEDDING:
            created = self._try_activate(created.id)
        return created

    def _try_activate(self, memory_id: str) -> MemoryRecord:
        record = self.get(memory_id)
        if record.status != STATUS_PENDING_EMBEDDING:
            return record
        ready, _reason = self.provider.available()
        if not ready:
            return record
        matrix = self.provider.embed([record.value])
        vector = matrix[0]
        try:
            self.vector_index.upsert(
                record.id,
                vector,
                model=self.provider.model_name,
                version=self.provider.model_version,
            )
        except ReindexRequired:
            return record
        now = _utc_now()
        with self._connection() as c:
            c.execute(
                """
                UPDATE memory_records
                SET status='ACTIVE',
                    embedding_model=?,
                    embedding_version=?,
                    embedding_dimensions=?,
                    updated_at=?
                WHERE id=? AND status='PENDING_EMBEDDING'
                """,
                (
                    self.provider.model_name,
                    self.provider.model_version,
                    int(vector.shape[0]),
                    now,
                    record.id,
                ),
            )
            if record.supersedes_id:
                c.execute(
                    """
                    UPDATE memory_records
                    SET status='SUPERSEDED', updated_at=?
                    WHERE id=? AND status='ACTIVE'
                    """,
                    (now, record.supersedes_id),
                )
        if record.supersedes_id:
            self.vector_index.remove(record.supersedes_id)
        return self.get(record.id)

    def approve_candidate(self, memory_id: str) -> MemoryRecord:
        record = self.get(memory_id)
        if record.status != STATUS_PENDING_REVIEW:
            return record
        with self._connection() as c:
            c.execute(
                "UPDATE memory_records SET status='PENDING_EMBEDDING', updated_at=? WHERE id=?",
                (_utc_now(), memory_id),
            )
        return self._try_activate(memory_id)

    def supersede(
        self,
        memory_id: str,
        *,
        value: str,
        confidence: float = 1.0,
        evidence_type: str = "explicit",
        sensitivity: str | None = None,
        source_kind: str = "user_correction",
        source_conversation_id: str = "",
        source_message_id: str = "",
        source_request_id: str = "",
        user_confirmed_sensitive: bool = False,
    ) -> MemoryRecord:
        old = self.get(memory_id)
        return self.create_candidate(
            value=value,
            category=old.category,
            memory_key=old.memory_key,
            scope_type=old.scope_type,
            scope_key=old.scope_key,
            confidence=confidence,
            evidence_type=evidence_type,
            sensitivity=sensitivity or old.sensitivity,
            source_kind=source_kind,
            source_conversation_id=source_conversation_id,
            source_message_id=source_message_id,
            source_request_id=source_request_id,
            user_confirmed_sensitive=user_confirmed_sensitive,
            supersedes_id=old.id,
        )

    def delete(self, memory_id: str) -> None:
        record = self.get(memory_id)
        # Remove the retrieval representation first. A DB failure after this point
        # leaves a non-retrievable record, which is safer than the reverse order.
        self.vector_index.remove(memory_id)
        with self._connection() as c:
            c.execute(
                """
                INSERT INTO memory_tombstones(memory_id,deleted_at,content_hash)
                VALUES (?,?,?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    deleted_at=excluded.deleted_at,
                    content_hash=excluded.content_hash
                """,
                (record.id, _utc_now(), _content_hash(record.value)),
            )
            c.execute("DELETE FROM memory_records WHERE id=?", (memory_id,))

    def retry_pending_embeddings(self) -> dict[str, int]:
        rows = [
            item
            for item in self.list(include_inactive=True)
            if item.status == STATUS_PENDING_EMBEDDING
        ]
        activated = 0
        for item in rows:
            if self._try_activate(item.id).status == STATUS_ACTIVE:
                activated += 1
        return {"pending": len(rows), "activated": activated}

    def rebuild_index(self) -> dict[str, object]:
        ready, reason = self.provider.available()
        if not ready:
            raise EmbeddingUnavailable(reason)
        active = [
            item
            for item in self.list(include_inactive=False)
            if item.status == STATUS_ACTIVE
        ]
        pending = [
            item
            for item in self.list(include_inactive=True)
            if item.status == STATUS_PENDING_EMBEDDING
        ]
        superseded_targets = {
            item.supersedes_id
            for item in pending
            if item.supersedes_id
        }
        records = [
            item
            for item in active
            if item.id not in superseded_targets
        ]
        all_records = records + pending
        if not all_records:
            self.vector_index.replace_all(
                [],
                np.empty((0, 0), dtype=np.float32),
                model=self.provider.model_name,
                version=self.provider.model_version,
            )
            return {"count": 0, "dimensions": 0}
        matrix = self.provider.embed([item.value for item in all_records])
        self.vector_index.replace_all(
            [item.id for item in all_records],
            matrix,
            model=self.provider.model_name,
            version=self.provider.model_version,
        )
        now = _utc_now()
        dimensions = int(matrix.shape[1])
        with self._connection() as c:
            for item in all_records:
                c.execute(
                    """
                    UPDATE memory_records
                    SET status='ACTIVE',
                        embedding_model=?,
                        embedding_version=?,
                        embedding_dimensions=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        self.provider.model_name,
                        self.provider.model_version,
                        dimensions,
                        now,
                        item.id,
                    ),
                )
                if item.supersedes_id:
                    c.execute(
                        """
                        UPDATE memory_records
                        SET status='SUPERSEDED', updated_at=?
                        WHERE id=? AND status='ACTIVE'
                        """,
                        (now, item.supersedes_id),
                    )
        return {"count": len(all_records), "dimensions": dimensions}

    def retrieve(
        self,
        query: str,
        *,
        scopes: dict[str, str] | None = None,
        limit: int = 5,
        candidate_limit: int = 20,
        min_semantic_score: float = -1.0,
    ) -> list[dict[str, object]]:
        if not self.memory_enabled():
            return []
        cleaned_query = _clean_required(query, "Memory query", max_chars=12000)
        ready, reason = self.provider.available()
        if not ready:
            raise EmbeddingUnavailable(reason)
        query_vector = self.provider.embed([cleaned_query])[0]
        candidates = self.vector_index.search(
            query_vector,
            limit=max(limit, candidate_limit),
            expected_model=self.provider.model_name,
            expected_version=self.provider.model_version,
        )
        requested_scopes = scopes or {}
        threshold = float(min_semantic_score)
        if not math.isfinite(threshold) or threshold < -1.0 or threshold > 1.0:
            raise ValueError("Memory semantic threshold must be between -1 and 1.")
        now = _utc_now()
        ranked: list[tuple[float, MemoryRecord, float]] = []
        for memory_id, semantic in candidates:
            if semantic < threshold:
                continue
            try:
                record = self.get(memory_id)
            except KeyError:
                continue
            if record.status != STATUS_ACTIVE:
                continue
            scope = _scope_score(record.scope_type, record.scope_key, requested_scopes)
            if record.scope_type != "global" and scope <= 0:
                continue
            age_days = max(0.0, (now - record.updated_at) / 86400.0)
            recency = 1.0 / (1.0 + age_days / 180.0)
            final = (
                0.55 * max(0.0, min(1.0, (semantic + 1.0) / 2.0))
                + 0.20 * scope
                + 0.10 * record.confidence
                + 0.10 * _evidence_score(record.evidence_type)
                + 0.05 * recency
            )
            ranked.append((final, record, semantic))
        ranked.sort(key=lambda item: item[0], reverse=True)
        selected = ranked[: max(0, int(limit))]
        if selected:
            with self._connection() as c:
                c.executemany(
                    "UPDATE memory_records SET last_used_at=? WHERE id=?",
                    [(now, item[1].id) for item in selected],
                )
        return [
            {
                "memory": record.public(),
                "semanticScore": semantic,
                "finalScore": final,
            }
            for final, record, semantic in selected
        ]

    def status(self) -> dict[str, object]:
        records = self.list(include_inactive=True)
        counts = {
            "active": sum(item.status == STATUS_ACTIVE for item in records),
            "pendingEmbedding": sum(
                item.status == STATUS_PENDING_EMBEDDING for item in records
            ),
            "pendingReview": sum(
                item.status == STATUS_PENDING_REVIEW for item in records
            ),
            "superseded": sum(
                item.status == STATUS_SUPERSEDED for item in records
            ),
            "total": len(records),
        }
        provider_ready, provider_reason = self.provider.available()
        try:
            index = self.vector_index.stats()
            index_error = ""
        except Exception as exc:
            index = {
                "count": 0,
                "dimensions": 0,
                "model": "",
                "version": "",
                "path": str(self.vector_path),
            }
            index_error = str(exc)
        index_healthy = (
            not index_error
            and int(index.get("count", 0)) == counts["active"]
            and (
                counts["active"] == 0
                or (
                    index.get("model") == self.provider.model_name
                    and index.get("version") == self.provider.model_version
                )
            )
        )
        return {
            "enabled": self.memory_enabled(),
            "counts": counts,
            "provider": {
                "ready": provider_ready,
                "reason": provider_reason,
                "model": self.provider.model_name,
                "version": self.provider.model_version,
                "localOnly": True,
            },
            "index": {
                **index,
                "healthy": index_healthy,
                "error": index_error,
            },
            "databasePath": str(self.db_path),
            "vectorPath": str(self.vector_path),
        }
