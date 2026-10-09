"""Content identity: what makes two files the same asset (handoff s7, s39, s40).

The asset is its full SHA-256. Two paths with the same hash are one asset
indexed once (a duplicate); a different cut or release has a different hash
and stays a separate asset, so its timestamps are never borrowed.

Evidence stays bound to the hash only while the file keeps the size,
modification time and file id it had when hashed (:class:`FileState`); any
change sends it back for a full hash.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
from pathlib import Path
from typing import Callable

CHUNK = 1024 * 1024


class HashCancelled(RuntimeError):
    pass


class HashLimitExceeded(ValueError):
    """The file holds more than the byte limit; no digest is returned."""


def full_sha256(path: Path, *, cancelled: Callable[[], bool] | None = None, limit: int | None = None) -> str:
    """Stream the whole file. Checks ``cancelled`` between chunks.

    hashlib state cannot be persisted, so an interrupted hash restarts; that is
    I/O cost only and is accepted for this stage.

    With ``limit`` (small files such as subtitles), the read itself is bounded:
    no more than ``limit + 1`` bytes are ever requested, and a file holding more
    than ``limit`` bytes (for example one that grew after its size was checked)
    raises HashLimitExceeded. Video identity passes no limit.
    """
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as handle:
        while True:
            if cancelled is not None and cancelled():
                raise HashCancelled(str(path))
            size = CHUNK * 4 if limit is None else min(CHUNK * 4, limit + 1 - total)
            chunk = handle.read(size)
            if not chunk:
                break
            total += len(chunk)
            if limit is not None and total > limit:
                raise HashLimitExceeded(f"{path}: more than {limit} bytes")
            digest.update(chunk)
    return digest.hexdigest()


def asset_id_for(sha256: str) -> str:
    return "a_" + sha256[:32]


@dataclass(frozen=True)
class FileState:
    """What must still hold for evidence hashed earlier to describe this file.

    ``file_id`` is the volume's file index (NTFS file ID on Windows, inode
    elsewhere), so a swap of a different file into the same name is caught
    even when size and mtime were copied across. This is a stat check, not a
    re-hash: an edit that preserves size, mtime and file id is not detected.
    """

    size: int
    mtime_ns: int
    file_id: int

    @classmethod
    def of(cls, path: Path) -> "FileState":
        info = os.stat(path, follow_symlinks=False)
        return cls(int(info.st_size), int(info.st_mtime_ns), int(info.st_ino))


class IdentityChanged(RuntimeError):
    """The file no longer matches the state its hash was taken from."""


ASSET_ID_PATTERN = re.compile(r"^a_[0-9a-f]{32}$")


def checked_asset_id(value: object) -> str:
    """An asset id read back from storage, refused unless it has the exact form."""
    if not isinstance(value, str) or not ASSET_ID_PATTERN.match(value):
        raise ValueError("malformed asset id")
    return value
