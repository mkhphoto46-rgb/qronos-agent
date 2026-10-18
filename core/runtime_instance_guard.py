"""
The runtime bridge's singleton: ``Local\\QronosAgentRuntimeBridge``, OWNED for the bridge's life.

Ownership is a wait-acquisition by a holder thread (``core.instance_ownership``),
never "the mutex was created" or "it already existed".
"""

from __future__ import annotations

import os
from typing import Optional

from core.instance_ownership import AlreadyOwned, MutexApi, OwnedMutex


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
        api: Optional[MutexApi] = None,
        name: Optional[str] = None,
    ) -> "RuntimeInstanceGuard":
        """Own the bridge singleton now, or raise RuntimeAlreadyRunning (see the module doc)."""
        if os.name != "nt" and api is None:
            return cls(None)
        try:
            return cls(OwnedMutex.acquire(name or cls.NAME, api=api))
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
