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
CANCEL_REASON_PAUSE = "runtime_paused"

_REASONS = frozenset(
    {CANCEL_REASON_USER_STOP, CANCEL_REASON_SHUTDOWN, CANCEL_REASON_PAUSE}
)


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

    Stop also has to reach work that has not *started* yet: a turn that was
    still routing or waiting on a model when Stop arrived must not launch its
    computer action afterwards. Every ``cancel_all`` therefore advances an
    epoch. A worker thread calls :meth:`mark_turn_start` when its turn begins;
    any action that thread begins after a later Stop/Pause gets a token that
    is already cancelled. A turn that starts after the Stop is unaffected, so
    an idle Stop never poisons the next command.

    After :meth:`close` every new action is born cancelled: nothing may run
    once shutdown has begun.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._active: dict[int, ActiveAction] = {}
        self._ids = itertools.count(1)
        self._epoch = 0
        self._last_reason = ""
        self._closed = False
        self._turn = threading.local()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def mark_turn_start(self) -> None:
        """Record, for the calling thread, the epoch its current turn began in."""
        with self._lock:
            self._turn.epoch = self._epoch

    def stopped_since_turn_start(self) -> str:
        """
        Why the calling thread's turn must not start new work, or "".

        Returns the cancellation reason when the registry is closed or a
        Stop/Pause happened after this thread's last ``mark_turn_start``.
        Threads that never marked a turn are only affected by ``close``.
        """
        with self._lock:
            if self._closed:
                return CANCEL_REASON_SHUTDOWN
            started = getattr(self._turn, "epoch", None)
            if started is not None and started != self._epoch:
                return self._last_reason
            return ""

    @contextmanager
    def begin(self, label: str) -> Iterator[CancellationToken]:
        token = CancellationToken(label=label)
        stopped = self.stopped_since_turn_start()
        if stopped:
            # The action is still registered and unwinds normally, but its
            # first checkpoint (before any side effect) raises.
            token.cancel(stopped)
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

    def close(self) -> tuple[str, ...]:
        """Refuse all future actions and cancel the current ones. Idempotent."""
        with self._lock:
            self._closed = True
        return self.cancel_all(CANCEL_REASON_SHUTDOWN)

    def cancel_all(self, reason: str = CANCEL_REASON_USER_STOP) -> tuple[str, ...]:
        """
        Cancel every currently registered action and advance the epoch so
        turns already in progress cannot begin new actions; return the labels
        of the actions that were running.
        """
        if reason not in _REASONS:
            raise ValueError(f"Unknown cancellation reason: {reason!r}.")
        with self._lock:
            self._epoch += 1
            self._last_reason = reason
            current = tuple(self._active.values())
        cancelled: list[str] = []
        for item in current:
            if item.token.cancel(reason):
                cancelled.append(item.label)
        return tuple(cancelled)
