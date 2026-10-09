"""Atomic, cooperative admission to Qronos's stores: shared for writers, exclusive for the updater.

Why a lock and not a check: "is an update running?" followed by opening a store
has a gap in which the update can start. Here the question and the answer are
one operation on one byte-range lock (``LockFileEx``) of a fixed file in the
data root:

* every process that opens Qronos stores (the runtime bridge, the settings,
  permissions, library, memory and other backend services, standalone jobs)
  takes a SHARED admission before it opens its first store and keeps it for its
  whole life (the OS releases it when the process ends);
* the updater takes the EXCLUSIVE admission without waiting; it is granted only
  while no writer holds a shared one, and while it is held every new writer is
  refused at once.

So no writer can be inside a store while the updater restores data or the
installer replaces the program, and none can start until the update ends. The
updater keeps the exclusive admission in the process that launched the
installer for the installer's whole lifetime (``core.update_service``).

Wiring every store-opening entry point to ``admit_writer`` is the Runtime and
Control contract (work/opus-production-installer-contract-round5.txt); until it
exists the update launch stays refused. This module is the primitive only.
"""

from __future__ import annotations

import os
from pathlib import Path

from core import local_update
from core.local_update import UpdateError

ADMISSION_FILE = ".qronos-store-admission.lock"

_LOCKFILE_FAIL_IMMEDIATELY = 0x1
_LOCKFILE_EXCLUSIVE_LOCK = 0x2


class Admission:
    """A held admission; ``release`` gives it back (the OS also does at process exit)."""

    def __init__(self, descriptor: int, exclusive: bool) -> None:
        self._descriptor = descriptor
        self.exclusive = exclusive

    def handle(self) -> int:
        import msvcrt

        return msvcrt.get_osfhandle(self._descriptor)

    def release(self) -> None:
        if self._descriptor >= 0:
            try:
                _unlock(self._descriptor)
            finally:
                os.close(self._descriptor)
                self._descriptor = -1

    def __enter__(self) -> "Admission":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _kernel():
    import ctypes
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(Overlapped)]
    kernel.LockFileEx.restype = wintypes.BOOL
    kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.POINTER(Overlapped)]
    kernel.UnlockFileEx.restype = wintypes.BOOL
    return kernel, Overlapped


def _open(data_root: Path) -> int:
    root = Path(os.path.abspath(data_root))
    for folder in (root, *root.parents):
        if os.path.lexists(folder) and local_update._is_link(folder):
            raise UpdateError("store", f"{folder.name!r} is a link; store admission is refused.")
    root.mkdir(parents=True, exist_ok=True)
    path = root / ADMISSION_FILE
    problem = local_update._unsafe_file(path)
    if problem:
        raise UpdateError("store", f"The store admission file {problem}; nothing was opened.")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
                         0o600)
    info = os.fstat(descriptor)
    if info.st_nlink != 1:
        os.close(descriptor)
        raise UpdateError("store", "The store admission file is not this profile's own file.")
    return descriptor


def _lock(descriptor: int, exclusive: bool) -> bool:
    import msvcrt

    kernel, Overlapped = _kernel()
    flags = _LOCKFILE_FAIL_IMMEDIATELY | (_LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0)
    overlapped = Overlapped()
    return bool(kernel.LockFileEx(msvcrt.get_osfhandle(descriptor), flags, 0, 1, 0, overlapped))


def _unlock(descriptor: int) -> None:
    import msvcrt

    kernel, Overlapped = _kernel()
    overlapped = Overlapped()
    kernel.UnlockFileEx(msvcrt.get_osfhandle(descriptor), 0, 1, 0, overlapped)


def _admit(data_root: Path, exclusive: bool) -> Admission:
    if os.name != "nt":
        raise UpdateError("unsupported_platform", "Store admission is implemented for Windows only.")
    descriptor = _open(data_root)
    if not _lock(descriptor, exclusive):
        os.close(descriptor)
        if exclusive:
            raise UpdateError("writers_active", "Another part of Qronos has its data open; it must stop first.")
        raise UpdateError("update_in_progress", "An update is being installed; Qronos's data cannot be opened now.")
    return Admission(descriptor, exclusive)


def admit_writer(data_root: Path) -> Admission:
    """SHARED admission for a process that opens stores. Take it before the first store opens; keep it."""
    return _admit(data_root, exclusive=False)


def admit_exclusive(data_root: Path) -> Admission:
    """EXCLUSIVE admission for the updater: granted only while no writer holds one, never waited for."""
    return _admit(data_root, exclusive=True)
