from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


DEFAULT_INACTIVITY_TIMEOUT_SECONDS = 60.0


class ConversationRole(Enum):
    USER = "user"
    ASSISTANT = "assistant"


class ConversationState(Enum):
    """
    High-level state of one Qronos conversation session.
    """

    WAITING_FOR_WAKE = "waiting_for_wake"
    LISTENING = "listening"
    PROCESSING = "processing"
    RESPONDING = "responding"


@dataclass(frozen=True)
class ConversationMessage:
    """
    One user or assistant message stored in conversation history.
    """

    role: ConversationRole
    content: str
    timestamp: float


@dataclass(frozen=True)
class ConversationSessionConfig:
    """
    MVP configuration for one Qronos conversation session.
    """

    inactivity_timeout_seconds: float = (
        DEFAULT_INACTIVITY_TIMEOUT_SECONDS
    )

    def __post_init__(self) -> None:
        if self.inactivity_timeout_seconds <= 0:
            raise ValueError(
                "inactivity_timeout_seconds "
                "must be greater than zero."
            )


class ConversationSession:
    """
    Manage one multi-turn Qronos conversation.

    A wake word is required only when no conversation is active.

    Once started:

        Wake Word
            ->
        LISTENING
            ->
        PROCESSING
            ->
        RESPONDING
            ->
        LISTENING
            ->
        follow-up without another wake word

    The inactivity timer runs only while Qronos is waiting for the
    user's next turn. It does not run while Qronos is processing or
    responding.

    Starting a new conversation clears the active context from the
    previous conversation.
    """

    def __init__(
        self,
        config: ConversationSessionConfig | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = (
            config
            if config is not None
            else ConversationSessionConfig()
        )

        self._clock = (
            clock
            if clock is not None
            else time.monotonic
        )

        self._state = (
            ConversationState.WAITING_FOR_WAKE
        )

        self._active = False

        self._started_at: float | None = None
        self._idle_since: float | None = None

        self._messages: list[
            ConversationMessage
        ] = []

    @property
    def state(self) -> ConversationState:
        self._expire_if_needed()

        return self._state

    @property
    def is_active(self) -> bool:
        self._expire_if_needed()

        return self._active

    @property
    def started_at(self) -> float | None:
        return self._started_at

    @property
    def idle_since(self) -> float | None:
        self._expire_if_needed()

        return self._idle_since

    @property
    def messages(
        self,
    ) -> tuple[ConversationMessage, ...]:
        return tuple(
            self._messages
        )

    @property
    def message_count(self) -> int:
        return len(
            self._messages
        )

    def start(self) -> None:
        """
        Start a fresh conversation after a valid wake-word event.

        A new session always starts with fresh active conversation
        context.
        """

        now = self._clock()

        self._messages.clear()

        self._active = True
        self._state = (
            ConversationState.LISTENING
        )

        self._started_at = now
        self._idle_since = now

    def close(self) -> None:
        """
        Close the active conversation.

        Existing messages remain available for diagnostics/history until
        another conversation starts, but they are no longer active context.
        """

        self._active = False

        self._state = (
            ConversationState.WAITING_FOR_WAKE
        )

        self._idle_since = None

    def requires_wake_word(self) -> bool:
        """
        Return True when Qronos must wait for the wake word.
        """

        self._expire_if_needed()

        return not self._active

    def begin_listening(self) -> None:
        """
        Start or restart the follow-up listening window.

        This is the only state in which inactivity timeout advances.
        """

        self._require_active()

        now = self._clock()

        self._state = (
            ConversationState.LISTENING
        )

        self._idle_since = now

    def begin_processing(self) -> None:
        """
        Pause inactivity timeout while STT / routing / Brain work runs.
        """

        self._require_active()

        self._state = (
            ConversationState.PROCESSING
        )

        self._idle_since = None

    def begin_responding(self) -> None:
        """
        Pause inactivity timeout while Qronos is responding.
        """

        self._require_active()

        self._state = (
            ConversationState.RESPONDING
        )

        self._idle_since = None

    def add_user_message(
        self,
        content: str,
    ) -> ConversationMessage:
        self._require_active()

        message = self._add_message(
            role=ConversationRole.USER,
            content=content,
        )

        return message

    def add_assistant_message(
        self,
        content: str,
    ) -> ConversationMessage:
        self._require_active()

        message = self._add_message(
            role=ConversationRole.ASSISTANT,
            content=content,
        )

        return message

    def seconds_until_timeout(
        self,
    ) -> float | None:
        """
        Return remaining follow-up listening time.

        None means timeout is currently paused because the conversation
        is inactive, processing, or responding.
        """

        self._expire_if_needed()

        if (
            not self._active
            or self._state
            is not ConversationState.LISTENING
            or self._idle_since is None
        ):
            return None

        elapsed = (
            self._clock()
            - self._idle_since
        )

        remaining = (
            self.config.inactivity_timeout_seconds
            - elapsed
        )

        return max(
            0.0,
            remaining,
        )

    def _add_message(
        self,
        role: ConversationRole,
        content: str,
    ) -> ConversationMessage:
        cleaned = content.strip()

        if not cleaned:
            raise ValueError(
                "Conversation message "
                "must not be empty."
            )

        message = ConversationMessage(
            role=role,
            content=cleaned,
            timestamp=self._clock(),
        )

        self._messages.append(
            message
        )

        return message

    def _require_active(self) -> None:
        self._expire_if_needed()

        if not self._active:
            raise RuntimeError(
                "Conversation session "
                "is not active."
            )

    def _expire_if_needed(self) -> None:
        if not self._active:
            return

        if (
            self._state
            is not ConversationState.LISTENING
        ):
            return

        if self._idle_since is None:
            return

        elapsed = (
            self._clock()
            - self._idle_since
        )

        if (
            elapsed
            >= self.config.inactivity_timeout_seconds
        ):
            self.close()