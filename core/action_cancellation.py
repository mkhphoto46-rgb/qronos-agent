"""
Cooperative cancellation for user-visible computer actions.

Native GUI input cannot be pre-empted safely: killing a thread between
mouse-down and mouse-up leaves a synthetic button held in whatever window is
under the cursor. Cancellation is therefore cooperative. Executors check one
token between native steps and inside stroke loops, and every path that has
pressed a button releases it before the cancellation propagates.

A token grants nothing. Cancelling can only make an action do less; no code
path consults a token to decide whether something is *allowed*.

The registry tracks only actions that explicitly registered themselves as
user-visible computer actions. Stop cancels exactly those tokens that exist at
the moment Stop arrives. It never touches queue tasks, file actions, voice
playback, other processes or a token created after the Stop.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import itertools
import math
import threading
from typing import Iterator


#: Why a token was cancelled. Fixed vocabulary so an audit or report never
#: carries caller-supplied free text.
CANCEL_REASON_USER_STOP = "user_stop"
CANCEL_REASON_SHUTDOWN = "runtime_shutdown"

_REASONS = frozenset({CANCEL_REASON_USER_STOP, CANCEL_REASON_SHUTDOWN})


class ActionCancelled(RuntimeError):
    """A cooperative cancellation checkpoint observed a cancelled token."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Action cancelled ({reason}).")


class CancellationToken:
    """One action's cancellation flag. Thread-safe; cancellation is one-way."""

    def __init__(self, *, label: str = "") -> None:
        self.label = str(label or "")
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""

    def cancel(self, reason: str = CANCEL_REASON_USER_STOP) -> bool:
        """Cancel once. Returns False when the token was already cancelled."""
        if reason not in _REASONS:
            raise ValueError(f"Unknown cancellation reason: {reason!r}.")
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason
            self._event.set()
            return True

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise ActionCancelled(self._reason)

    def sleep(self, seconds: float) -> None:
        """Wait up to ``seconds``, waking immediately on cancellation."""
        delay = float(seconds)
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("Cancellation-aware sleep needs a finite non-negative delay.")
        self.raise_if_cancelled()
        if delay and self._event.wait(delay):
            self.raise_if_cancelled()


def checkpoint(token: CancellationToken | None) -> None:
    """Raise ActionCancelled when ``token`` is cancelled; no-op for None."""
    if token is not None:
        token.raise_if_cancelled()


@dataclass(frozen=True)
class ActiveAction:
    action_key: int
    label: str
    token: CancellationToken


class ActiveActionRegistry:
    """
    Process-local set of in-flight user-visible computer actions.

    ``begin`` is a context manager so registration can never outlive the
    action: an exception, cancellation or normal return all unregister.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._active: dict[int, ActiveAction] = {}
        self._ids = itertools.count(1)

    @contextmanager
    def begin(self, label: str) -> Iterator[CancellationToken]:
        token = CancellationToken(label=label)
        with self._lock:
            key = next(self._ids)
            self._active[key] = ActiveAction(key, token.label, token)
        try:
            yield token
        finally:
            with self._lock:
                self._active.pop(key, None)
                if not self._active:
                    self._idle.notify_all()

    def active(self) -> tuple[ActiveAction, ...]:
        with self._lock:
            return tuple(self._active.values())

    def wait_idle(self, timeout: float) -> bool:
        """Wait until no action is registered; False if still busy at timeout."""
        with self._lock:
            return self._idle.wait_for(lambda: not self._active, timeout=max(0.0, float(timeout)))

    def cancel_all(self, reason: str = CANCEL_REASON_USER_STOP) -> tuple[str, ...]:
        """Cancel every currently registered action; return their labels."""
        with self._lock:
            current = tuple(self._active.values())
        cancelled: list[str] = []
        for item in current:
            if item.token.cancel(reason):
                cancelled.append(item.label)
        return tuple(cancelled)
