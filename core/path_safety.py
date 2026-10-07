"""
Small, shared answers to "is this path what it claims to be?".

Two questions keep coming up wherever Qronos decides where user data lives or
which file it may touch, and answering them slightly differently in each place
is how one caller ends up stricter, and another looser, than the policy:

    Is this a link? Symlinks, junctions and mount points redirect a path to
    somewhere else; those are refused where redirection matters. Cloud-sync
    placeholders are reparse points too, but they do not
    redirect — the file is still at the path the user chose — so they are not
    links. Treating every reparse point as a link would make a cloud-synced
    Documents folder unusable.

    Is this a local path at all? UNC shares and Win32 device namespaces
    (``\\\\server\\share``, ``\\\\?\\``, ``\\\\.\\``) are refused as storage
    roots: a share sends data off the machine, and device paths bypass the
    normalisation every other check here relies on.
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import stat


# Microsoft's "name surrogate" bit: set on every reparse tag that makes a path
# stand for another name (symlink, junction/mount point, and their kin).
_NAME_SURROGATE_BIT = 0x20000000
# App execution aliases are not name surrogates but behave like links to an
# executable; never treat one as an ordinary file.
_IO_REPARSE_TAG_APPEXECLINK = 0x8000001B


def is_link_or_reparse_point(path: str | Path) -> bool:
    """
    True for symlinks, junctions, mount points and app-execution aliases.

    False for missing paths and for non-redirecting reparse points such as
    cloud-sync placeholders.
    """
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        # Exists but cannot be inspected: callers use this to refuse, so an
        # unanswerable question is answered the safe way.
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    if not getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    tag = int(getattr(st, "st_reparse_tag", 0) or 0)
    if not tag:
        # Attribute set but tag unavailable: cannot prove it is harmless.
        return True
    return bool(tag & _NAME_SURROGATE_BIT) or tag == _IO_REPARSE_TAG_APPEXECLINK


def same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(
        os.path.normpath(str(b))
    )


def is_network_or_device_path(value: str) -> bool:
    """UNC shares and Win32 device/extended namespaces, in either slash style."""
    text = str(value).strip().replace("/", "\\")
    return text.startswith("\\\\")


def has_control_characters(value: str) -> bool:
    return any(ord(ch) < 32 or ch == "\x7f" for ch in value)


def is_within(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or inside it (case-insensitive on Windows)."""
    return path == root or path.is_relative_to(root)


def is_absolute_local(value: str) -> bool:
    """
    An absolute path on a local drive letter (Windows) or rooted path (POSIX).

    ``C:folder`` (drive-relative) and ``\\folder`` (root-relative) are not
    absolute: both resolve against process state that changes between runs.
    """
    if os.name == "nt":
        pure = PureWindowsPath(value)
        return bool(pure.drive) and bool(pure.root) and len(pure.drive) == 2
    return Path(value).is_absolute()
