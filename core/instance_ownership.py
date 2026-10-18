"""
Owned named mutexes.

A singleton is proved only by OWNING its mutex: one thread waits for it and
Windows grants it (``WAIT_OBJECT_0``, or ``WAIT_ABANDONED`` when the previous
owner died holding it). These do NOT prove ownership and are never used as
proof here:

* ``CreateMutexW`` succeeding, or saying the mutex already exists;
* an initial-owner flag on a handle to an EXISTING mutex (Windows ignores it);
* an abandoned or existing status alone, or any caller-supplied Boolean.

``OwnedMutex`` acquires on a dedicated holder thread, which keeps ownership
until ``release()``: Windows lets only the owning thread release a mutex, so
the release runs on that same thread, exactly once, followed by closing the
handle. If the process ends first, Windows abandons the mutex; the next owner
sees ``WAIT_ABANDONED`` and is told so.
"""

from __future__ import annotations

import ctypes
import os
import threading
from dataclasses import dataclass
from typing import Callable, Optional

WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
#: Bound on how long a caller waits for the holder thread to report.
_REPORT_SECONDS = 10.0


class OwnershipError(RuntimeError):
    """The mutex could not be owned."""


class AlreadyOwned(OwnershipError):
    """Another live process (or thread) owns the mutex."""


class OwnershipUnavailable(OwnershipError):
    """Windows could not create or wait for the mutex."""


@dataclass(frozen=True)
class MutexApi:
    """The four Win32 calls, injectable for tests. Defaults bind kernel32 exactly."""

    create: Callable[[str], int]
    wait: Callable[[int, int], int]
    release: Callable[[int], bool]
    close: Callable[[int], bool]
    last_error: Callable[[], int]


_api_lock = threading.Lock()
_api: Optional[MutexApi] = None


def kernel_api() -> MutexApi:
    """kernel32 CreateMutexW(NULL, FALSE, name) / WaitForSingleObject / ReleaseMutex / CloseHandle."""
    global _api
    if os.name != "nt":
        raise OwnershipUnavailable("Named mutex ownership is a Windows facility.")
    with _api_lock:
        if _api is None:
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel32.CreateMutexW
            create.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            create.restype = wintypes.HANDLE
            wait = kernel32.WaitForSingleObject
            wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            wait.restype = wintypes.DWORD
            release = kernel32.ReleaseMutex
            release.argtypes = [wintypes.HANDLE]
            release.restype = wintypes.BOOL
            close = kernel32.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            _api = MutexApi(
                # bInitialOwner is FALSE on purpose: ownership comes only from the wait.
                create=lambda name: int(create(None, False, name) or 0),
                wait=lambda handle, milliseconds: int(wait(handle, milliseconds)),
                release=lambda handle: bool(release(handle)),
                close=lambda handle: bool(close(handle)),
                last_error=ctypes.get_last_error,
            )
        return _api


class OwnedMutex:
    """One named mutex, owned by its own holder thread until ``release()``."""

    def __init__(self, name: str, api: MutexApi) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("A mutex name is required.")
        self.name = name
        self.abandoned = False
        self._api = api
        self._lock = threading.Lock()
        self._owned = False
        self._released = False
        self._ready = threading.Event()
        self._release = threading.Event()
        self._done = threading.Event()
        self._outcome: tuple[str, int] = ("pending", 0)
        self._thread: Optional[threading.Thread] = None
        #: True only after ReleaseMutex AND CloseHandle both succeeded on the holder thread.
        self.release_confirmed = False

    @classmethod
    def acquire(cls, name: str, *, wait_milliseconds: int = 0, api: Optional[MutexApi] = None) -> "OwnedMutex":
        """Own ``name`` now (or within ``wait_milliseconds``), or raise AlreadyOwned / OwnershipUnavailable."""
        owned = cls(name, api or kernel_api())
        owned._start(max(0, int(wait_milliseconds)))
        return owned

    @property
    def owned(self) -> bool:
        """Still owned: acquired, and no release has finished (a stuck holder thread keeps it owned)."""
        with self._lock:
            return self._owned and (not self._released or not self._done.is_set())

    def _start(self, wait_milliseconds: int) -> None:
        self._thread = threading.Thread(target=self._hold, args=(wait_milliseconds,),
                                        name=f"qronos-mutex-{self.name[-24:]}", daemon=True)
        self._thread.start()
        if not self._ready.wait(wait_milliseconds / 1000 + _REPORT_SECONDS):
            # The holder never reported: ask it to give up whatever it got, and refuse.
            self._release.set()
            raise OwnershipUnavailable(f"Windows did not answer for the mutex {self.name!r}.")
        kind, value = self._outcome
        if kind == "owned":
            return
        if kind == "busy":
            raise AlreadyOwned(f"Another process owns {self.name!r}.")
        raise OwnershipUnavailable(f"The mutex {self.name!r} could not be owned (Windows {value:#x}).")

    def _hold(self, wait_milliseconds: int) -> None:
        api = self._api
        handle = 0
        try:
            handle = api.create(self.name)
            if not handle:
                self._report("error", api.last_error())
                return
            result = api.wait(handle, wait_milliseconds)
            if result not in (WAIT_OBJECT_0, WAIT_ABANDONED):
                api.close(handle)
                handle = 0
                self._report("busy" if result == WAIT_TIMEOUT else "error", result)
                return
            with self._lock:
                self._owned = True
                self.abandoned = result == WAIT_ABANDONED
            self._report("owned", result)
            self._release.wait()
            # Only the owning thread may release it: this one. Then the handle goes.
            released = api.release(handle)
            closed = api.close(handle)
            handle = 0
            # A failed native release is never reported as a clean one: Windows then keeps
            # the ownership until this process exits (when it is released as abandoned).
            self.release_confirmed = bool(released and closed)
        except BaseException:  # noqa: BLE001 - reported to the waiting caller, never silent
            if handle:
                try:
                    api.close(handle)
                except BaseException:  # noqa: BLE001
                    pass
            if not self._ready.is_set():
                self._report("error", 0)
        finally:
            self._done.set()

    def _report(self, kind: str, value: int) -> None:
        self._outcome = (kind, int(value))
        self._ready.set()

    def release(self) -> bool:
        """
        Give the mutex back on its holder thread, once. True only if Windows confirmed it.

        False means unconfirmed, never "released": either ReleaseMutex/CloseHandle
        failed (the holder thread still ends, and Windows abandons whatever it
        owned at that moment), or the holder did not finish in time (then the
        mutex is still owned, and ``owned`` stays True).
        """
        with self._lock:
            if not self._owned or self._released:
                return self.release_confirmed
            self._released = True
        self._release.set()
        if not self._done.wait(_REPORT_SECONDS):
            return False  # the holder did not finish: not a confirmed release
        return self.release_confirmed

    def __enter__(self) -> "OwnedMutex":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
