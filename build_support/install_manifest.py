"""The ownership manifest a Qronos installation carries: every file it put in its folder.

``qronos-install-manifest.txt`` is shipped as a Tauri resource at the root of
the install folder. The installer hook (desktop/src-tauri/installer-hooks/
qronos-install-root.nsh) reads it to:

* recognise a folder as an existing Qronos installation (a non-empty folder
  without it is refused as an install target), and
* before an upgrade, and after an uninstall, delete exactly the listed files and
  then remove listed folders that are empty, so no stale executable or runtime
  file of an older version survives and no unlisted (user) file is touched.

Format (UTF-16LE without BOM, CRLF line endings, read with FileReadUTF16LE):

    QRONOS-INSTALL-MANIFEST 1 com.qronos.agent
    F <file path relative to the install folder, backslash separated>
    ...                       (all files, sorted)
    D <folder path>
    ...                       (all folders, deepest first)

Every entry is a plain relative path: no drive, no leading or doubled
backslash, no ':' or '/', no '..' anywhere, and only names Windows keeps as
written. The hook re-checks these rules before it deletes anything.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath

from build_support.desktop_notices import MANIFEST as NOTICE_MANIFEST

FILE_NAME = "qronos-install-manifest.txt"
HEADER = "QRONOS-INSTALL-MANIFEST 1 com.qronos.agent"
UNINSTALLER = "uninstall.exe"
RUNTIME_FOLDER = "qronos-runtime"
NOTICES_FOLDER = "third-party-notices"

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_DEVICE = re.compile(r"(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])",
                     re.IGNORECASE)


class InstallManifestError(ValueError):
    """The payload cannot be described by an ownership manifest the hook can trust."""


def check_entry(path: str) -> str:
    """A payload path as the hook will see it (backslash separated), or an error."""
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or "\\" in path or ".." in path:
        raise InstallManifestError(f"Payload path {path!r} is not a plain relative path.")
    for part in parts:
        if (_FORBIDDEN.search(part) or part != part.strip(" ") or part.endswith(".")
                or _DEVICE.fullmatch(part.split(".")[0].rstrip(" "))):
            raise InstallManifestError(f"Payload path {path!r} has a name Windows would not keep as written.")
    return "\\".join(parts)


def payload_files(runtime: Path, notices: Path, main_binary: str) -> list[str]:
    """Every file the installer writes into the install folder, from the verified manifests."""
    runtime_manifest = json.loads((runtime / "runtime-manifest.json").read_text(encoding="utf-8"))
    notice_manifest = json.loads((notices / NOTICE_MANIFEST).read_text(encoding="utf-8"))
    files = [f"{main_binary}.exe", UNINSTALLER, FILE_NAME,
             f"{RUNTIME_FOLDER}/runtime-manifest.json", f"{NOTICES_FOLDER}/{NOTICE_MANIFEST}"]
    files += [f"{RUNTIME_FOLDER}/{entry['path']}" for entry in runtime_manifest["files"]]
    files += [f"{NOTICES_FOLDER}/{entry['path']}" for entry in notice_manifest["files"]]
    return files


def render(files: list[str]) -> bytes:
    """The manifest bytes for these payload files (validated, sorted, case-insensitively unique)."""
    checked = sorted({check_entry(path) for path in files}, key=str.casefold)
    folded = [path.casefold() for path in checked]
    if len(set(folded)) != len(folded):
        raise InstallManifestError("Two payload files differ only in letter case.")
    folders = {"\\".join(path.split("\\")[:depth]) for path in checked for depth in range(1, path.count("\\") + 1)}
    if {folder.casefold() for folder in folders} & set(folded):
        raise InstallManifestError("A payload path is both a file and a folder.")
    ordered = sorted(folders, key=lambda folder: (-folder.count("\\"), folder.casefold()))
    lines = [HEADER] + [f"F {path}" for path in checked] + [f"D {folder}" for folder in ordered]
    return ("\r\n".join(lines) + "\r\n").encode("utf-16-le")


def parse(data: bytes) -> tuple[list[str], list[str]]:
    """(files, folders) of a manifest, refusing anything the hook would refuse."""
    lines = data.decode("utf-16-le").split("\r\n")
    if not lines or lines[0] != HEADER or lines[-1] != "":
        raise InstallManifestError("Not a Qronos install manifest.")
    files, folders = [], []
    for line in lines[1:-1]:
        kind, _, path = line.partition(" ")
        if kind not in ("F", "D") or check_entry(path.replace("\\", "/")) != path:
            raise InstallManifestError(f"Invalid install manifest line {line!r}.")
        (files if kind == "F" else folders).append(path)
    return files, folders


def write(directory: Path, files: list[str]) -> Path:
    path = directory / FILE_NAME
    with open(path, "xb") as handle:  # never replaces a manifest
        handle.write(render(files))
    return path


def verify_installed_tree(root: Path, data: bytes, ignore: frozenset[str] = frozenset({"$PLUGINSDIR"})) -> int:
    """An extracted installer holds exactly the files its manifest lists. Returns the file count."""
    files, folders = parse(data)

    def unreadable(error: OSError) -> None:
        # Path.rglob silently skips folders it cannot open (for example past MAX_PATH);
        # a completeness check must not.
        raise InstallManifestError(f"Part of the extracted installer cannot be read: {error}") from error

    present: set[str] = set()
    present_folders: set[str] = set()
    for current, directories, names in os.walk(root, onerror=unreadable):
        relative = os.path.relpath(current, root)
        if relative != "." and relative.split(os.sep)[0] in ignore:
            continue
        for directory in directories:
            path = os.path.normpath(os.path.join(relative, directory))
            if path.split(os.sep)[0] not in ignore:
                present_folders.add(path.replace(os.sep, "\\"))
        for name in names:
            present.add(os.path.normpath(os.path.join(relative, name)).replace(os.sep, "\\"))
    listed = set(files)  # 7-Zip extracts the uninstaller stub as uninstall.exe, so it is compared too
    if present != listed:
        extra, missing = sorted(present - listed), sorted(listed - present)
        raise InstallManifestError(f"The installer's files differ from its install manifest: "
                                   f"unlisted {extra[:5]}, missing {missing[:5]}.")
    if not present_folders <= set(folders):
        raise InstallManifestError("The installer has folders its install manifest does not list.")
    return len(files)
