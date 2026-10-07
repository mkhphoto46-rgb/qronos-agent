"""Request-scoped cancellation for local inference, independent of GUI actions."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import threading
from typing import Callable


_REASONS = frozenset({"user_stop", "runtime_paused", "runtime_shutdown", "preempted"})


class LocalInferenceCancelled(RuntimeError):
    """An intentional cancellation, with no model/prompt/server diagnostics."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Local inference cancelled ({reason}).")


class LocalInferenceCancellation:
    """One-way token. Create a new token for each turn or queued work item.

    cancel() synchronously aborts this token's registered connections. It does
    not stop a daemon, unload models, or prove when the daemon releases its GPU.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reason: str | None = None
        self._aborters: dict[object, Callable[[], None]] = {}

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._reason is not None

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def raise_if_cancelled(self) -> None:
        with self._lock:
            if self._reason is not None:
                raise LocalInferenceCancelled(self._reason) from None

    def cancel(self, reason: str = "user_stop") -> bool:
        if not isinstance(reason, str) or reason not in _REASONS:
            raise ValueError("Unsupported local inference cancellation reason.")
        with self._lock:
            if self._reason is not None:
                return False
            self._reason = reason
            # Keep registration and abort serialized: cancel must not return
            # while an existing connection's shutdown is still pending.
            for abort in tuple(self._aborters.values()):
                abort()
            self._aborters.clear()
            return True

    def _register_abort(self, abort: Callable[[], None]) -> Callable[[], None]:
        identity = object()
        with self._lock:
            if self._reason is not None:
                abort()
                self.raise_if_cancelled()
            self._aborters[identity] = abort

        def release():
            with self._lock:
                self._aborters.pop(identity, None)

        return release


_CURRENT: ContextVar[LocalInferenceCancellation | None] = ContextVar(
    "local_inference_cancellation", default=None,
)


def current_inference_cancellation() -> LocalInferenceCancellation | None:
    return _CURRENT.get()


def inference_checkpoint() -> None:
    token = _CURRENT.get()
    if token is not None:
        token.raise_if_cancelled()


@contextmanager
def inference_cancellation_scope(token: LocalInferenceCancellation | None):
    """Bind inside the worker thread; None inherits an existing outer scope.

    ContextVars do not automatically propagate into newly created threads.
    The caller must still order answer delivery against Stop/commit at its
    existing runtime or queue boundary after this scope returns.
    """
    if token is None:
        inference_checkpoint()
        yield
        return
    if not isinstance(token, LocalInferenceCancellation):
        raise TypeError("Expected a local inference cancellation token.")
    handle = _CURRENT.set(token)
    try:
        token.raise_if_cancelled()
        yield
        token.raise_if_cancelled()
    finally:
        _CURRENT.reset(handle)
