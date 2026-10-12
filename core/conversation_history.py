from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from core.brain_runtime import BrainMessage, BrainMessageRole
from core.storage_locations import ensure_storage_root


DEFAULT_CONTEXT_MAX_MESSAGES = 10
DEFAULT_CONTEXT_MAX_CHARS = 5_000


def _legacy_history_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / ".qronos"
    return base / "Qronos" / "conversation_history.sqlite3"


def _migrate_legacy_history(source: Path, destination: Path) -> None:
    if destination.exists() or not source.is_file() or source == destination:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(str(source), timeout=5.0)
    destination_connection = sqlite3.connect(str(destination), timeout=5.0)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()


def _default_history_path() -> Path:
    override = os.environ.get(
        "QRONOS_CONVERSATION_HISTORY_PATH",
        "",
    ).strip()

    if override:
        return Path(override)

    # Creates only the validated history root, never every storage root.
    destination = ensure_storage_root("historyRoot") / "conversation_history.sqlite3"
    _migrate_legacy_history(_legacy_history_path(), destination)
    return destination


@dataclass(frozen=True)
class ConversationHistoryMessage:
    id: str
    role: str
    text: str
    source: str
    timestamp: float
    request_id: str = ""

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "role": (
                "qronos"
                if self.role == "assistant"
                else "user"
            ),
            "text": self.text,
            "source": self.source,
            "timestamp": self.timestamp,
            "requestId": self.request_id,
        }


class ConversationHistory:
    """
    Canonical local conversation timeline shared by text and voice.

    ConversationSession remains responsible for wake/follow-up state only.
    This class owns durable user/assistant messages and bounded Brain context.
    """

    def __init__(
        self,
        path: str | Path | None = None,
    ) -> None:
        self.path = (
            Path(path)
            if path is not None
            else _default_history_path()
        )
        self._lock = threading.RLock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        connection = sqlite3.connect(
            str(self.path),
            timeout=5.0,
        )
        connection.execute(
            "PRAGMA journal_mode=WAL"
        )
        connection.execute(
            "PRAGMA synchronous=NORMAL"
        )
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
            with self._connection() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conversation_messages (
                        id TEXT PRIMARY KEY,
                        role TEXT NOT NULL,
                        text TEXT NOT NULL,
                        source TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        request_id TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_conversation_messages_timestamp
                    ON conversation_messages(timestamp)
                    """
                )

    def append(
        self,
        *,
        role: str,
        text: str,
        source: str,
        request_id: str = "",
        timestamp: float | None = None,
    ) -> ConversationHistoryMessage:
        cleaned_role = role.strip().lower()
        cleaned_text = text.strip()
        cleaned_source = source.strip().lower()
        cleaned_request_id = request_id.strip()

        if cleaned_role not in {
            "user",
            "assistant",
        }:
            raise ValueError(
                "Conversation history role must be user or assistant."
            )

        if not cleaned_text:
            raise ValueError(
                "Conversation history message must not be empty."
            )

        if cleaned_source not in {
            "text",
            "voice",
        }:
            raise ValueError(
                "Conversation history source must be text or voice."
            )

        created_at = (
            float(timestamp)
            if timestamp is not None
            else time.time()
        )

        message = ConversationHistoryMessage(
            id=(
                "msg-"
                f"{time.time_ns()}-"
                f"{uuid.uuid4().hex[:8]}"
            ),
            role=cleaned_role,
            text=cleaned_text,
            source=cleaned_source,
            timestamp=created_at,
            request_id=cleaned_request_id,
        )

        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO conversation_messages
                    (
                        id,
                        role,
                        text,
                        source,
                        timestamp,
                        request_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.id,
                        message.role,
                        message.text,
                        message.source,
                        message.timestamp,
                        message.request_id,
                    ),
                )

        return message

    def all_messages(
        self,
    ) -> tuple[ConversationHistoryMessage, ...]:
        with self._lock:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        id,
                        role,
                        text,
                        source,
                        timestamp,
                        request_id
                    FROM conversation_messages
                    ORDER BY timestamp ASC, rowid ASC
                    """
                ).fetchall()

        return tuple(
            ConversationHistoryMessage(
                id=str(row[0]),
                role=str(row[1]),
                text=str(row[2]),
                source=str(row[3]),
                timestamp=float(row[4]),
                request_id=str(row[5] or ""),
            )
            for row in rows
        )

    def recent_messages(
        self,
        limit: int,
    ) -> tuple[ConversationHistoryMessage, ...]:
        if limit <= 0:
            return ()

        with self._lock:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        id,
                        role,
                        text,
                        source,
                        timestamp,
                        request_id
                    FROM conversation_messages
                    ORDER BY timestamp DESC, rowid DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()

        rows.reverse()

        return tuple(
            ConversationHistoryMessage(
                id=str(row[0]),
                role=str(row[1]),
                text=str(row[2]),
                source=str(row[3]),
                timestamp=float(row[4]),
                request_id=str(row[5] or ""),
            )
            for row in rows
        )

    def has_assistant_reply(
        self,
        request_id: str,
    ) -> bool:
        """Whether an answer to this request was already recorded."""
        cleaned = request_id.strip()

        if not cleaned:
            return False

        with self._lock:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT 1
                    FROM conversation_messages
                    WHERE request_id = ? AND role = 'assistant'
                    LIMIT 1
                    """,
                    (cleaned,),
                ).fetchone()

        return row is not None

    def brain_context(
        self,
        *,
        max_messages: int = DEFAULT_CONTEXT_MAX_MESSAGES,
        max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
    ) -> tuple[BrainMessage, ...]:
        if (
            max_messages <= 0
            or max_chars <= 0
        ):
            return ()

        # Fetch a little more than the message cap so the character budget,
        # rather than a too-small SQL slice, decides what survives.
        candidates = self.recent_messages(
            max(
                max_messages * 3,
                max_messages,
            )
        )

        selected: list[
            ConversationHistoryMessage
        ] = []
        used_chars = 0

        for message in reversed(
            candidates
        ):
            message_chars = len(
                message.text
            )

            if (
                selected
                and (
                    len(selected)
                    >= max_messages
                    or used_chars
                    + message_chars
                    > max_chars
                )
            ):
                break

            if (
                not selected
                and message_chars
                > max_chars
            ):
                # A single giant prior turn must not evict the current request
                # from the model context. Skip it rather than truncating user
                # content into a potentially misleading fragment.
                continue

            selected.append(
                message
            )
            used_chars += (
                message_chars
            )

        selected.reverse()

        return tuple(
            BrainMessage(
                role=(
                    BrainMessageRole.USER
                    if message.role == "user"
                    else BrainMessageRole.ASSISTANT
                ),
                content=message.text,
            )
            for message in selected
        )

    def snapshot_json(self) -> str:
        return json.dumps(
            {
                "messages": [
                    message.to_public_dict()
                    for message in self.all_messages()
                ]
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
