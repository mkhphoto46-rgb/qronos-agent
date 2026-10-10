"""
The runtime bridge's singleton: ``Local\\QronosAgentRuntimeBridge``, OWNED for the bridge's life.

Ownership is a wait-acquisition by a holder thread (``core.instance_ownership``),
never "the mutex was created" or "it already existed". After acquiring, the
update fence is checked; while an update or its installer is alive, the mutex
is released again and the bridge refuses (``UpdateInProgress``). The updater's
quiescence takes this same name by create-then-wait, so a live bridge makes it
refuse, and a bridge cannot start while the updater holds it.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from core.instance_ownership import AlreadyOwned, MutexApi, OwnedMutex, acquire_unless_updating


class RuntimeAlreadyRunning(RuntimeError):
    pass


class RuntimeInstanceGuard:
    NAME = "Local\\QronosAgentRuntimeBridge"

    def __init__(self, owned: Optional[OwnedMutex]) -> None:
        self._owned = owned

    @classmethod
    def acquire(
        cls,
        *,
        retry_seconds: float = 0.0,
        api: Optional[MutexApi] = None,
        fence_exists: Optional[Callable[[], bool]] = None,
        name: Optional[str] = None,
    ) -> "RuntimeInstanceGuard":
        """Own the bridge singleton, then refuse while an update runs (see the module doc)."""
        if os.name != "nt" and api is None:
            return cls(None)
        options = {"retry_seconds": retry_seconds, "api": api}
        if fence_exists is not None:
            options["fence_exists"] = fence_exists
        try:
            return cls(acquire_unless_updating(name or cls.NAME, **options))
        except AlreadyOwned as error:
            raise RuntimeAlreadyRunning("Another Qronos runtime instance is already active.") from error

    @property
    def owned(self) -> bool:
        return self._owned is not None and self._owned.owned

    def close(self) -> bool:
        """Release the singleton on its holder thread, once; True only if Windows confirmed it."""
        owned, self._owned = self._owned, None
        return owned.release() if owned is not None else True

    release = close
