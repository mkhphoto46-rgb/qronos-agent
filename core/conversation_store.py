"""
Conversations that survive a restart.

``core/conversation_session.py`` holds a conversation in memory and is correct
at what it does. This is the durable half, kept separate on purpose: the
session is about the shape of a turn — listening, processing, responding,
timing out — and none of that should have to know about files.

**This module stores personal data.** A conversation transcript is a record of
what somebody said in their own home, in their own voice, and it is the most
sensitive thing Qronos will hold. Three consequences are built in rather than
left to the caller:

    Storing is opt-out at the point of writing, not filtered at the point of
    reading. When history is turned off, nothing is written. A design that
    writes everything and hides it in the interface would leave the transcripts
    on disk regardless of what the user was told.

    Deletion is real deletion. ``forget`` removes the conversation from the
    file, and the file is rewritten without it. There is no tombstone and no
    "deleted" flag, because an export of a file full of tombstones still
    contains everything the user asked to be rid of.

    Export produces exactly what is stored, in a form a person can read. It is
    the mechanism behind a data-subject request, so it must not quietly omit
    fields or include ones that are not in the store.

The retention policy itself — how long history is kept by default, whether it
expires, what a backup archive contains — is not decided here. That is a
product and compliance decision, flagged for human review, and this module is
built so any answer can be implemented without changing the storage format.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from core.config import CONFIG
from core.json_store import JsonStore


DEFAULT_CONVERSATION_PATH = CONFIG.paths.data / "conversations.json"

SCHEMA_VERSION = 1

# A conversation is capped so one very long session cannot grow the file
# without limit. Oldest turns go first, which matches what a person expects
# when they scroll back far enough.
DEFAULT_MAX_MESSAGES_PER_CONVERSATION = 500

# How many conversations are kept. Also a privacy control, not only a storage
# one: history that ages out is history that cannot leak later.
DEFAULT_MAX_CONVERSATIONS = 200


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class StoredMessage:
    """One turn, as written to disk."""

    role: str
    text: str
    at: float

    def to_json(self) -> dict[str, Any]:
        return {"role": self.role, "text": self.text, "at": round(self.at, 3)}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "StoredMessage":
        return cls(
            role=str(data.get("role", "")),
            text=str(data.get("text", "")),
            at=float(data.get("at", 0.0)),
        )


@dataclass(frozen=True)
class StoredConversation:
    """One conversation, as written to disk."""

    conversation_id: str
    started_at: float
    messages: tuple[StoredMessage, ...]

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def preview(self) -> str:
        """
        The first thing the user said, for a list.

        Deliberately the user's words rather than a generated title: a summary
        would mean sending the transcript somewhere to be summarised, which is
        the one thing this product promises not to do.
        """
        for message in self.messages:
            if message.role == "user":
                return message.text

        return ""

    def to_json(self) -> dict[str, Any]:
        return {
            "conversationId": self.conversation_id,
            "startedAt": round(self.started_at, 3),
            "messages": [message.to_json() for message in self.messages],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "StoredConversation":
        raw = data.get("messages", [])
        messages = tuple(
            StoredMessage.from_json(item)
            for item in raw
            if isinstance(item, dict)
        )

        return cls(
            conversation_id=str(data.get("conversationId", "")),
            started_at=float(data.get("startedAt", 0.0)),
            messages=messages,
        )


class ConversationStore:
    """
    Durable conversation history.

    ``path=None`` keeps everything in memory, which is what tests use and what
    a user who has turned history off effectively gets.
    """

    def __init__(
        self,
        path: str | Path | None = DEFAULT_CONVERSATION_PATH,
        clock: Clock | None = None,
        enabled: bool = True,
        max_conversations: int = DEFAULT_MAX_CONVERSATIONS,
        max_messages: int = DEFAULT_MAX_MESSAGES_PER_CONVERSATION,
    ) -> None:
        self._store = JsonStore(path, version=SCHEMA_VERSION)
        self.clock: Clock = clock or time.time
        self.enabled = enabled
        self.max_conversations = max_conversations
        self.max_messages = max_messages

        self._lock = threading.RLock()
        self._conversations: dict[str, StoredConversation] = {}
        self._load()

    @property
    def path(self) -> Path | None:
        return self._store.path

    # ------------------------------------------------------------- writing

    def append(
        self,
        conversation_id: str,
        role: str,
        text: str,
    ) -> StoredConversation | None:
        """
        Add one turn. Returns None when history is turned off.

        Nothing is written when disabled — not written-and-hidden. The user was
        told their conversations are not being kept, and that has to be true on
        disk, not only in the interface.
        """
        if not self.enabled:
            return None

        if not conversation_id.strip():
            raise ValueError("A conversation needs an id.")

        with self._lock:
            now = self.clock()
            existing = self._conversations.get(conversation_id)

            message = StoredMessage(role=role, text=text, at=now)

            if existing is None:
                updated = StoredConversation(
                    conversation_id=conversation_id,
                    started_at=now,
                    messages=(message,),
                )
            else:
                messages = (*existing.messages, message)

                updated = StoredConversation(
                    conversation_id=existing.conversation_id,
                    started_at=existing.started_at,
                    messages=messages[-self.max_messages :],
                )

            self._conversations[conversation_id] = updated
            self._trim()
            self._persist()

            return updated

    def forget(self, conversation_id: str) -> bool:
        """
        Delete one conversation. True if there was one.

        A real removal, not a flag. An export of a file full of tombstones
        still contains everything the user asked to be rid of.
        """
        with self._lock:
            if self._conversations.pop(conversation_id, None) is None:
                return False

            self._persist()

            return True

    def forget_all(self) -> int:
        """Delete every conversation. Returns how many went."""
        with self._lock:
            count = len(self._conversations)
            self._conversations.clear()
            self._persist()

            return count

    # ------------------------------------------------------------- reading

    def get(self, conversation_id: str) -> StoredConversation | None:
        with self._lock:
            return self._conversations.get(conversation_id)

    def recent(self, limit: int = 20) -> tuple[StoredConversation, ...]:
        """The most recent conversations, newest first."""
        with self._lock:
            ordered = sorted(
                self._conversations.values(),
                key=lambda item: item.started_at,
                reverse=True,
            )

            return tuple(ordered[:limit])

    def all_conversations(self) -> tuple[StoredConversation, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._conversations.values(),
                    key=lambda item: item.started_at,
                )
            )

    def count(self) -> int:
        with self._lock:
            return len(self._conversations)

    def export(self) -> dict[str, Any]:
        """
        Everything held, in a form a person can read.

        The mechanism behind a data-subject request, so it must contain exactly
        what is stored: no omitted fields, and nothing added that is not
        actually on disk.
        """
        with self._lock:
            return {
                "schemaVersion": SCHEMA_VERSION,
                "exportedAt": round(self.clock(), 3),
                "conversations": [
                    conversation.to_json()
                    for conversation in self.all_conversations()
                ],
            }

    def import_conversations(
        self,
        conversations: Iterable[dict[str, Any]],
    ) -> int:
        """
        Load conversations from an export. Returns how many were taken.

        Used by restore. Existing conversations with the same id are replaced,
        because a restore is meant to reproduce a previous state rather than
        merge into the current one.
        """
        with self._lock:
            taken = 0

            for data in conversations:
                conversation = StoredConversation.from_json(data)

                if not conversation.conversation_id:
                    continue

                self._conversations[conversation.conversation_id] = (
                    conversation
                )
                taken += 1

            self._trim()
            self._persist()

            return taken

    # ------------------------------------------------------------ internals

    def _trim(self) -> None:
        """Drop the oldest conversations past the cap. Lock held."""
        if len(self._conversations) <= self.max_conversations:
            return

        ordered = sorted(
            self._conversations.values(),
            key=lambda item: item.started_at,
        )

        for conversation in ordered[
            : len(self._conversations) - self.max_conversations
        ]:
            del self._conversations[conversation.conversation_id]

    def _load(self) -> None:
        payload = self._store.load_payload()
        raw = payload.get("conversations", [])

        if not isinstance(raw, list):
            return

        for item in raw:
            if not isinstance(item, dict):
                continue

            conversation = StoredConversation.from_json(item)

            if conversation.conversation_id:
                self._conversations[conversation.conversation_id] = (
                    conversation
                )

    def _persist(self) -> None:
        self._store.save(
            {
                "conversations": [
                    conversation.to_json()
                    for conversation in self.all_conversations()
                ]
            }
        )
