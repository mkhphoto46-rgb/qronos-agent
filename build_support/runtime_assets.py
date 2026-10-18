"""Check a runtime asset folder against the reviewed asset registry (build_support/runtime_assets.json).

Read only: it stats and hashes files, opens nothing else, follows no link, starts nothing and downloads nothing.
Registry paths are plain relative Windows paths (no drive, root, UNC/device prefix, '..', stream or reserved
name). A root that is a link, or any folder between the root and an asset that is one, refuses the asset; a
verified file is hashed only through a handle that does not follow a link and whose final path is still the
intended file under the root (core.path_safety and the handle checks below).

    python -m build_support.runtime_assets --root "<Local AppData>\\Qronos\\RuntimeAssets"
    python -m build_support.runtime_assets --root ... --require-verified   # release gate: exit 1 unless all verified

Each registry entry is reported as one of:
- ``verified``: present, and its size and SHA-256 equal the reviewed official identity;
- ``mismatch``: present, but not the reviewed file (never used as if it were);
- ``unverified``: the registry has no official identity for it (present or not, nothing is vouched for);
- ``missing``: configured but absent;
- ``refused``: a link or reparse point, or not a regular file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import re
import stat
import sys
from pathlib import Path, PureWindowsPath
from typing import Any

from core.path_safety import has_control_characters, is_link_or_reparse_point, is_network_or_device_path

REGISTRY = Path(__file__).resolve().parent / "runtime_assets.json"
STATUSES = ("verified", "unverified", "missing")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class RegistryError(ValueError):
    """The registry itself is not well formed; nothing is checked."""


def _plain_relative(relative: object) -> bool:
    """A relative Windows path that can only name something under the root (Windows path rules, any OS)."""
    if not isinstance(relative, str) or not relative or has_control_characters(relative) \
            or is_network_or_device_path(relative):
        return False
    pure = PureWindowsPath(relative)
    return not (pure.drive or pure.root) and not {".", ".."} & set(pure.parts) and not ntpath.isreserved(relative)


def load_registry(path: Path = REGISTRY) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not isinstance(data.get("assets"), list):
        raise RegistryError("unsupported runtime asset registry")
    if not data["assets"]:
        raise RegistryError("the runtime asset registry lists no asset; nothing can be verified")
    seen: set[str] = set()
    for entry in data["assets"]:
        relative = entry.get("path")
        if not _plain_relative(relative) or relative in seen:
            raise RegistryError(f"bad or duplicate asset path: {relative!r}")
        seen.add(relative)
        status = entry.get("status")
        if status not in STATUSES:
            raise RegistryError(f"{relative}: unknown status {status!r}")
        if status == "verified":
            if not (isinstance(entry.get("bytes"), int) and entry["bytes"] > 0
                    and isinstance(entry.get("sha256"), str) and _SHA256.fullmatch(entry["sha256"])
                    and str(entry.get("source", "")).startswith("https://") and entry.get("evidence")):
                raise RegistryError(f"{relative}: a verified entry needs bytes, sha256, an https source and evidence")
        elif "sha256" in entry or "bytes" in entry:
            raise RegistryError(f"{relative}: only a verified entry may carry an identity")
    return data["assets"]


def _open_unfollowed(path: Path) -> int | None:
    """A read handle on ``path`` that follows no link and whose final path is still ``path``, else None."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0x00200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        return None
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)  # FILE_NAME_NORMALIZED, VOLUME_NAME_DOS
    final = buffer.value if 0 < length < len(buffer) else ""
    if final.startswith("\\\\?\\UNC\\"):
        final = "\\\\" + final[8:]
    elif final.startswith("\\\\?\\"):
        final = final[4:]
    if not final or os.path.normcase(os.path.normpath(final)) != os.path.normcase(
            os.path.normpath(os.path.abspath(path))):
        kernel.CloseHandle(handle)  # a folder on the way changed: the opened file is not the intended one
        return None
    return int(handle)


def _sha256(path: Path) -> str | None:
    """SHA-256 read through a handle that follows no link and is still ``path`` (no writer meanwhile), else None."""
    handle = _open_unfollowed(path)
    if handle is None:
        return None
    import msvcrt

    digest = hashlib.sha256()
    with os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redirected(root: Path, parts: tuple[str, ...]) -> bool:
    """The root, or a folder between it and the asset, is a link (a missing folder is not)."""
    folders = [root]
    for part in parts[:-1]:
        folders.append(folders[-1] / part)
    return any(is_link_or_reparse_point(folder) for folder in folders)


def check(root: Path, assets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The state of every registry entry under ``root``."""
    assets = load_registry() if assets is None else assets
    root = Path(os.path.abspath(root))
    results = []
    for entry in assets:
        parts = PureWindowsPath(entry["path"]).parts
        path = root.joinpath(*parts)
        if is_network_or_device_path(str(root)) or _redirected(root, parts):
            results.append({"path": entry["path"], "state": "refused"})
            continue
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            results.append({"path": entry["path"], "state": "missing"})
            continue
        except OSError:
            results.append({"path": entry["path"], "state": "refused"})
            continue
        attributes = getattr(info, "st_file_attributes", 0)
        if not stat.S_ISREG(info.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            results.append({"path": entry["path"], "state": "refused"})
            continue
        if entry["status"] != "verified":
            results.append({"path": entry["path"], "state": "unverified", "bytes": info.st_size})
            continue
        digest = _sha256(path) if info.st_size == entry["bytes"] else ""
        if digest is None:
            results.append({"path": entry["path"], "state": "refused"})
            continue
        same = digest == entry["sha256"]
        results.append({"path": entry["path"], "state": "verified" if same else "mismatch", "bytes": info.st_size})
    counts = {state: sum(1 for item in results if item["state"] == state)
              for state in ("verified", "mismatch", "unverified", "missing", "refused")}
    return {"schemaVersion": 1, "root": str(root), "assets": results, "counts": counts,
            "allVerified": bool(results) and counts["verified"] == len(results)}  # zero entries verify nothing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="The runtime asset folder to check.")
    parser.add_argument("--require-verified", action="store_true",
                        help="Exit 1 unless every registry entry is present and verified (release gate). "
                             "An unusable or empty registry exits 2.")
    arguments = parser.parse_args(argv)
    try:
        report = check(arguments.root)
    except RegistryError as error:  # an unusable or empty registry is a refusal, never a pass
        sys.stdout.write(json.dumps({"schemaVersion": 1, "error": str(error), "allVerified": False}) + "\n")
        return 2
    sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    if any(report["counts"][state] for state in ("mismatch", "refused")):
        return 2
    return 1 if arguments.require_verified and not report["allVerified"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
