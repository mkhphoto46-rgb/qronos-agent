"""Order one foreground worker's inference and publication against Stop."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading

from core.inference_cancellation import (
    LocalInferenceCancellation, LocalInferenceCancelled, inference_cancellation_scope,
)


class ForegroundBusy(RuntimeError):
    pass


@dataclass
class ForegroundTurn:
    kind: str
    token: LocalInferenceCancellation = field(default_factory=LocalInferenceCancellation)
    terminal: bool = False
    cancellation_reported: bool = False


class ForegroundLifecycle:
    """Per-runtime gate; it neither authorizes nor owns any native action.

    Stop holds transition() through its acknowledgement. New scopes cannot
    enter that transition, and publication either precedes Stop completely or
    observes its cancelled token. A fresh later worker has a fresh token.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._active = None
        self._closed = False
        self._current = ContextVar("foreground_turn", default=None)

    @property
    def current(self) -> ForegroundTurn | None:
        return self._current.get()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active is not None

    @contextmanager
    def transition(self):
        with self._lock:
            yield

    def cancel_current(self, reason: str) -> bool:
        with self._lock:
            return bool(self._active and self._active.token.cancel(reason))

    def close(self):
        with self._lock:
            self._closed = True
            return self.cancel_current("runtime_shutdown")

    @contextmanager
    def begin(self, kind, *, state, on_cancel=None):
        with self._lock:
            if self._closed:
                raise LocalInferenceCancelled("runtime_shutdown")
            paused, busy = state()
            if paused:
                raise LocalInferenceCancelled("runtime_paused")
            if self._active is not None or busy:
                raise ForegroundBusy("Qronos is already processing another request.")
            turn = ForegroundTurn(kind)
            self._active = turn
        handle = self._current.set(turn)
        try:
            with inference_cancellation_scope(turn.token):
                yield turn
        except LocalInferenceCancelled as exc:
            if on_cancel is not None:
                on_cancel(turn, exc)
            raise
        finally:
            self._current.reset(handle)
            with self._lock:
                if self._active is turn:
                    self._active = None

    @contextmanager
    def publication(self, *, action_outcome=False, terminal=False):
        # The caller binds the scope inside its actual worker. Queue work and
        # legacy helper calls outside a foreground scope keep their own gate.
        with self._lock:
            turn = self.current
            if turn is not None and not action_outcome:
                turn.token.raise_if_cancelled()
            yield
            if turn is not None and not action_outcome:
                turn.token.raise_if_cancelled()
            if turn is not None and terminal:
                turn.terminal = True

    def claim_cancellation_report(self, turn):
        with self._lock:
            if turn is not None:
                if turn.terminal or turn.cancellation_reported:
                    return False
                turn.cancellation_reported = True
            return True
