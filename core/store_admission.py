"""Atomic, cooperative admission to Qronos's stores: shared for writers, exclusive for a sole owner.

Why a lock and not a check: "is someone else using the data?" followed by
opening a store has a gap. Here the question and the answer are one operation
on one byte-range lock (``LockFileEx``) of a fixed file in the data root:

* every process that opens Qronos stores (the runtime bridge, the settings,
  permissions, library, memory and other backend services, standalone jobs)
  takes a SHARED admission before it opens its first store and keeps it for its
  whole life (the OS releases it when the process ends);
* an EXCLUSIVE admission is granted, without waiting, only while no writer
  holds a shared one, and while it is held every new writer is refused at once.

This module is the primitive only; ``core.startup_admission`` takes the shared
admission for every store-opening entry point.
"""

from __future__ import annotations

import os
from pathlib import Path

from core.path_safety import is_link, unsafe_file

ADMISSION_FILE = ".qronos-store-admission.lock"

_LOCKFILE_FAIL_IMMEDIATELY = 0x1
_LOCKFILE_EXCLUSIVE_LOCK = 0x2


class AdmissionError(ValueError):
    """Store admission was refused. ``code`` names the reason for the caller and the start-up refusal."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
        if os.path.lexists(folder) and is_link(folder):
            raise AdmissionError("store", f"{folder.name!r} is a link; store admission is refused.")
    root.mkdir(parents=True, exist_ok=True)
    path = root / ADMISSION_FILE
    problem = unsafe_file(path)
    if problem:
        raise AdmissionError("store", f"The store admission file {problem}; nothing was opened.")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
                         0o600)
    info = os.fstat(descriptor)
    if info.st_nlink != 1:
        os.close(descriptor)
        raise AdmissionError("store", "The store admission file is not this profile's own file.")
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
        raise AdmissionError("unsupported_platform", "Store admission is implemented for Windows only.")
    descriptor = _open(data_root)
    if not _lock(descriptor, exclusive):
        os.close(descriptor)
        if exclusive:
            raise AdmissionError("writers_active", "Another part of Qronos has its data open; it must stop first.")
        raise AdmissionError("busy", "Another part of Qronos holds its data exclusively; it cannot be opened now.")
    return Admission(descriptor, exclusive)


def admit_writer(data_root: Path) -> Admission:
    """SHARED admission for a process that opens stores. Take it before the first store opens; keep it."""
    return _admit(data_root, exclusive=False)


def admit_exclusive(data_root: Path) -> Admission:
    """EXCLUSIVE admission: granted only while no writer holds one, never waited for."""
    return _admit(data_root, exclusive=True)
