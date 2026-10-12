"""Which paths the media index may touch at all.

Only local files on a drive the user explicitly approved. Anything that could
make a decoder reach the network or another machine — URLs, UNC shares, device
namespaces, links that point elsewhere — is refused here, before a path is ever
handed to a native decoder.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

VIDEO_EXTENSIONS = frozenset({
    ".mkv", ".webm", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".mts",
    ".mpg", ".mpeg", ".vob", ".flv", ".wmv", ".asf", ".ogv",
})
SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt", ".ass", ".ssa"})

_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+:")


_RESERVED_DEVICE = re.compile(
    r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$|clock\$)$",
    re.IGNORECASE,
)


class UnsafeMediaPath(ValueError):
    """A path the media index must not read."""


def _is_link(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    # Windows junctions and other reparse points report as directories, not links.
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def local_path(raw: object) -> Path:
    """Return an absolute local path, or refuse.

    Refused: empty or non-string input, NUL bytes, UNC shares and device
    namespaces (``\\\\server``, ``\\\\?\\``, ``\\\\.\\``), URL-like values
    (``http:``, ``file:``, ``concat:``), relative paths, ``..`` segments, and
    on Windows alternate data streams (``video.mp4:other``), reserved device
    names in any segment (``NUL``, ``COM1.mkv``, ``con .srt``) and segments
    ending in a dot or space, which Windows silently rewrites.
    """
    if not isinstance(raw, (str, os.PathLike)):
        raise UnsafeMediaPath("path must be a string")
    text = os.fspath(raw)
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise UnsafeMediaPath("path is empty or contains NUL")
    if text.startswith(("\\\\", "//")):
        raise UnsafeMediaPath("network shares and device paths are not indexed")
    if os.name == "nt":
        if not _DRIVE_PATH.match(text):
            raise UnsafeMediaPath("path must be an absolute drive path")
        if ":" in text[2:]:
            raise UnsafeMediaPath("alternate data streams are not indexed")
        for segment in re.split(r"[\\/]", text[3:]):
            if not segment:
                continue
            if segment[-1] in ". ":
                raise UnsafeMediaPath("path segments must not end in a dot or space")
            if _RESERVED_DEVICE.match(segment.split(".", 1)[0].rstrip(" ")):
                raise UnsafeMediaPath("reserved device names are not indexed")
    else:
        if _SCHEME.match(text) or not text.startswith("/"):
            raise UnsafeMediaPath("path must be absolute")
    if any(part == ".." for part in re.split(r"[\\/]", text)):
        raise UnsafeMediaPath("path must not contain '..'")
    return Path(os.path.normpath(text))


def has_link_component(path: Path, stop_at: Path | None = None) -> bool:
    """True if ``path`` or any parent below ``stop_at`` is a link or junction.

    Callers that admit a file pass no ``stop_at``: a junction *above* the
    approved root redirects the whole root just as surely as one below it.
    """
    current = path
    while True:
        if _is_link(current):
            return True
        if stop_at is not None and current == stop_at:
            return False
        parent = current.parent
        if parent == current:
            return False
        current = parent


def readable_media_file(raw: object, *, root: Path | None = None) -> Path:
    """Validate a file before a decoder opens it."""
    path = local_path(raw)
    if root is not None:
        try:
            path.relative_to(root)
        except ValueError:
            raise UnsafeMediaPath("file is outside its approved root") from None
    if has_link_component(path):
        raise UnsafeMediaPath("links and junctions are not followed")
    if not path.is_file():
        raise UnsafeMediaPath("not a regular file")
    return path


def decoder_input(path: Path) -> str:
    """The only form in which a path is given to ffmpeg/ffprobe.

    The explicit ``file:`` protocol stops a name such as ``concat:a|b`` or
    ``http:...`` from being read as a protocol, and the caller also pins
    ``-protocol_whitelist file``.
    """
    checked = local_path(path)
    return "file:" + str(checked)


_OWNED_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,79}$")


def owned_child(base: Path, *parts: str) -> Path:
    """A path strictly inside ``base`` that the media index may write or delete.

    Every part must be a plain name (no separators, no ``..``), ``base`` must
    have no link or junction on its path, the result must resolve inside the
    resolved ``base``, and an existing target must not be a link either. Used
    before every recursive delete and every cache or report write.
    """
    for part in parts:
        if not isinstance(part, str) or not _OWNED_PART.match(part) or part in {".", ".."}:
            raise UnsafeMediaPath("cache names must be plain names")
    if has_link_component(base):
        raise UnsafeMediaPath("the media data folder must not be behind a link or junction")
    target = base.joinpath(*parts)
    try:
        target.resolve(strict=False).relative_to(base.resolve(strict=False))
    except ValueError:
        raise UnsafeMediaPath("cache path escapes the media data folder") from None
    if os.path.lexists(target) and has_link_component(target, stop_at=base):
        raise UnsafeMediaPath("cache path is a link or junction")
    return target
