"""Verified bytes for a native decoder, in an owned file nobody else can change (M-P5 F1).

ffmpeg decodes ASS/SSA sidecars by reading a file. Handing it the user's file
by name after hashing would let a file that grew or was replaced in between
reach the decoder. So the decoder gets a private copy instead:

* the bytes are the ones already read with a size cap and checked against the
  sidecar's hash, so the copy is exactly what was hashed;
* the copy is created new (never reused) under the media cache, at
  ``<cache>/subtitle-input/sub-<32 random hex>.<ass|ssa>``, through
  ``owned_child`` (plain name, no link or junction, inside the cache). The name
  never carries the user's file name;
* on Windows the copy is opened with sharing for *read only* and that handle
  is held until the decoder has finished. While it is held no other process can
  open it for writing, truncate, rename or delete it; ffmpeg's read-only open
  is allowed. Windows also refuses to rename or delete a folder above a file
  held open this way, so the path ffmpeg opens by name cannot be rebound to
  another file (tests prove both on this machine). The handle's final path must
  be the intended path (no junction was in the way when it was created), and
  the path is checked for links again before it is handed out;
* before the decoder starts and after it ends the copy is re-read through that
  same handle and must still hash to the expected digest;
* afterwards it is deleted through a new handle whose file identity (volume and
  file index) must be the copy's, so a different file that took its name after
  the handle closed is never deleted. Copies left by a crash are removed by
  :func:`sweep_stale_inputs`; one still held open by a live owner is skipped
  and counted, never forced.

Same-user boundary: another program of the same user cannot change the copy or
rebind its path while the decoder may read it, but it can read the copy, and it
could have changed the user's original before it was read (that is refused by
the digest check, not here).

Elsewhere (not a product target) the copy is a new 0600 file with the same
re-hash before and after the decoder; it is not write-locked and its delete is
identity-checked by stat only.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Iterator

from qronos_media.paths import owned_child

INPUT_DIR = "subtitle-input"
INPUT_NAME = re.compile(r"^sub-[0-9a-f]{32}\.(ass|ssa)$")
MAX_SWEEP_ENTRIES = 256
_SUFFIXES = {".ass", ".ssa"}
_CHUNK = 1024 * 1024


class DecoderInputError(OSError):
    """The verified copy could not be made, kept unchanged or removed; the decoder must not use it."""


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_ALL = 0x00000007
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_TEMPORARY = 0x00000100
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_DISPOSITION_INFO = 4
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_PATH_NOT_FOUND = 3
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    class _ByHandleInfo(ctypes.Structure):
        _fields_ = [("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
                    ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                    ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
                    ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
                    ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD)]

    class _DispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _kernel32.WriteFile.restype = wintypes.BOOL
    _kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong),
                                           wintypes.DWORD]
    _kernel32.SetFilePointerEx.restype = wintypes.BOOL
    _kernel32.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    _kernel32.GetFileSizeEx.restype = wintypes.BOOL
    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _kernel32.FlushFileBuffers.restype = wintypes.BOOL
    _kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleInfo)]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
                                                    wintypes.DWORD]
    _kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                     wintypes.DWORD]
    _kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    def _fail(what: str) -> DecoderInputError:
        return DecoderInputError(ctypes.get_last_error(), f"decoder input: {what} failed")

    def _identity_of(handle) -> tuple[int, int]:
        info = _ByHandleInfo()
        if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise _fail("identity")
        return int(info.dwVolumeSerialNumber), (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)

    class _LockedFile:
        """A new file, written once, then held with read-only sharing until closed."""

        def __init__(self, path: Path) -> None:
            handle = _kernel32.CreateFileW(str(path), _GENERIC_READ | _GENERIC_WRITE, _FILE_SHARE_READ, None,
                                           _CREATE_NEW, _FILE_ATTRIBUTE_TEMPORARY, None)
            if handle is None or handle == _INVALID_HANDLE:
                raise _fail("create")
            self._handle = handle
            self.identity = _identity_of(handle)

        def write_all(self, data: bytes) -> None:
            view = memoryview(data)
            while view:
                piece = view[:_CHUNK]
                written = wintypes.DWORD(0)
                buffer = (ctypes.c_char * len(piece)).from_buffer_copy(piece)
                if not _kernel32.WriteFile(self._handle, buffer, len(piece), ctypes.byref(written), None) \
                        or written.value == 0:
                    raise _fail("write")
                view = view[written.value:]
            if not _kernel32.FlushFileBuffers(self._handle):
                raise _fail("flush")

        def size(self) -> int:
            size = ctypes.c_longlong(0)
            if not _kernel32.GetFileSizeEx(self._handle, ctypes.byref(size)):
                raise _fail("size")
            return int(size.value)

        def final_path(self) -> str:
            buffer = ctypes.create_unicode_buffer(32768)
            length = _kernel32.GetFinalPathNameByHandleW(self._handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                raise _fail("final path")
            return buffer.value

        def digest(self, limit: int) -> str:
            """SHA-256 of the whole copy, read through this handle; more than ``limit`` bytes fails."""
            if not _kernel32.SetFilePointerEx(self._handle, 0, None, 0):
                raise _fail("seek")
            digest, total = hashlib.sha256(), 0
            buffer = ctypes.create_string_buffer(_CHUNK)
            while True:
                count = wintypes.DWORD(0)
                if not _kernel32.ReadFile(self._handle, buffer, _CHUNK, ctypes.byref(count), None):
                    raise _fail("read")
                if count.value == 0:
                    return digest.hexdigest()
                total += count.value
                if total > limit:
                    raise DecoderInputError("decoder input grew")
                digest.update(buffer.raw[:count.value])

        def close(self) -> None:
            if self._handle is not None:
                _kernel32.CloseHandle(self._handle)
                self._handle = None

    def _delete_exact(path: Path, identity: tuple[int, int]) -> bool:
        """Delete ``path`` only if it is still the file with ``identity``. False: it was already gone."""
        handle = _kernel32.CreateFileW(str(path), _DELETE | _FILE_READ_ATTRIBUTES, _FILE_SHARE_ALL, None,
                                       _OPEN_EXISTING, _FILE_FLAG_OPEN_REPARSE_POINT, None)
        if handle is None or handle == _INVALID_HANDLE:
            if ctypes.get_last_error() in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
                return False
            raise _fail("open for delete")
        try:
            if _identity_of(handle) != identity:
                raise DecoderInputError("decoder input: another file has taken the copy's name; it was not deleted")
            disposition = _DispositionInfo(True)
            if not _kernel32.SetFileInformationByHandle(handle, _FILE_DISPOSITION_INFO, ctypes.byref(disposition),
                                                        ctypes.sizeof(disposition)):
                raise _fail("delete")
            return True
        finally:
            _kernel32.CloseHandle(handle)

    def _same_path(final: str, intended: Path) -> bool:
        if final.startswith("\\\\?\\UNC\\"):
            return False
        if final.startswith("\\\\?\\"):
            final = final[4:]
        return os.path.normcase(os.path.normpath(final)) == os.path.normcase(os.path.normpath(str(intended)))

else:
    class _LockedFile:  # pragma: no cover - not a product platform; same checks, no write lock
        def __init__(self, path: Path) -> None:
            self._fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            info = os.fstat(self._fd)
            self.identity = (info.st_dev, info.st_ino)
            self._path = str(path)

        def write_all(self, data: bytes) -> None:
            view = memoryview(data)
            while view:
                view = view[os.write(self._fd, view[:_CHUNK]):]
            os.fsync(self._fd)

        def size(self) -> int:
            return os.fstat(self._fd).st_size

        def final_path(self) -> str:
            return self._path

        def digest(self, limit: int) -> str:
            os.lseek(self._fd, 0, os.SEEK_SET)
            digest, total = hashlib.sha256(), 0
            while chunk := os.read(self._fd, _CHUNK):
                total += len(chunk)
                if total > limit:
                    raise DecoderInputError("decoder input grew")
                digest.update(chunk)
            return digest.hexdigest()

        def close(self) -> None:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def _delete_exact(path: Path, identity: tuple[int, int]) -> bool:  # pragma: no cover
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        if (info.st_dev, info.st_ino) != identity:
            raise DecoderInputError("decoder input: another file has taken the copy's name; it was not deleted")
        os.unlink(path)
        return True

    def _same_path(final: str, intended: Path) -> bool:  # pragma: no cover
        return os.path.realpath(final) == os.path.realpath(str(intended))


def _input_dir(cache_dir: Path) -> Path:
    directory = owned_child(Path(cache_dir), INPUT_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    return owned_child(Path(cache_dir), INPUT_DIR)  # re-checked now that it exists (no link)


@contextmanager
def verified_decoder_input(cache_dir: Path, data: bytes, digest: str, suffix: str) -> Iterator[Path]:
    """
    Yield the path of a new private copy of ``data`` for the decoder.

    ``data`` must hash to ``digest`` (the bytes were read with a cap and checked
    by the caller; this checks again). The copy is unchanged while yielded and
    is checked again on the way out; it is always removed, by identity.
    """
    suffix = suffix.lower()
    if suffix not in _SUFFIXES:
        raise DecoderInputError("decoder input: unsupported subtitle type")
    if hashlib.sha256(data).hexdigest() != digest:
        raise DecoderInputError("decoder input: the bytes do not match the verified digest")
    directory = _input_dir(cache_dir)
    target = owned_child(directory, f"sub-{secrets.token_hex(16)}{suffix}")
    locked = _LockedFile(target)
    identity = locked.identity
    try:
        locked.write_all(data)
        if locked.size() != len(data) or locked.digest(len(data)) != digest:
            raise DecoderInputError("decoder input: the copy does not match the verified bytes")
        if not _same_path(locked.final_path(), target):
            raise DecoderInputError("decoder input: the copy is not where it was created (a link was in the way)")
        owned_child(Path(cache_dir), INPUT_DIR, target.name)  # no link appeared on the path since
        yield target
        if locked.digest(len(data)) != digest:
            raise DecoderInputError("decoder input: the copy changed while it was decoded")
    finally:
        locked.close()
        try:
            _delete_exact(target, identity)
        except OSError as exc:
            if isinstance(exc, DecoderInputError):
                raise
            raise DecoderInputError(f"decoder input: the copy could not be removed ({type(exc).__name__})") from None


def sweep_stale_inputs(cache_dir: Path) -> dict[str, int]:
    """
    Remove copies a crash left behind: only exact ``sub-<hex>.ass|ssa`` plain
    files in ``<cache>/subtitle-input``, at most ``MAX_SWEEP_ENTRIES`` entries
    looked at. A copy still held open by a live owner cannot be deleted
    (Windows refuses it) and is counted as busy, never forced.
    """
    counts = {"removed": 0, "busy": 0}
    directory = owned_child(Path(cache_dir), INPUT_DIR)
    if not os.path.isdir(directory):
        return counts
    with os.scandir(directory) as entries:
        for number, entry in enumerate(entries):
            if number >= MAX_SWEEP_ENTRIES:
                break
            if not INPUT_NAME.match(entry.name):
                continue
            try:
                info = os.lstat(entry.path)
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    continue
                owned_child(directory, entry.name).unlink()
                counts["removed"] += 1
            except OSError:
                counts["busy"] += 1
    return counts
