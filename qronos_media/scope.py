"""Which stored file the index may read *now*: one admission rule for every read.

A ``files`` row is a record of what a scan saw, never permission to read.
Before any open of a video or a subtitle (identity hash, decode, sidecar
discovery, strict verification, export, playback plan) the path is admitted
again against its own root row as the root is approved *now*:

* the text is a safe local path (:func:`qronos_media.paths.local_path`: no UNC,
  device namespace, URL-like value, ``..``, alternate data stream or reserved
  device name);
* it equals an approved file root, or lies inside an approved folder root at a
  folder depth no greater than the root's current ``max_depth`` (files in the
  root folder itself are depth 0, which is what the scanner walks);
* it has a video extension (subtitles: a subtitle extension, beside an
  admitted video);
* no link or junction anywhere on its path;
* it is not inside the media index's own data folder (database, keyframe
  cache, reports), so the index never feeds on its own state.

That is the whole read boundary of this slice. It is not a new permission
system: whether a root may be read at all is the Qronos gate's answer
(``READ_NON_SENSITIVE`` under the Files policy, asked again fresh at each read
handoff by :class:`qronos_media.jobs.StageRunner`). The gate is
category-scoped today; :func:`admit` is the single place a path-scoped rule
would apply if the gate ever gains one. An explicitly selected folder anywhere
else (an external drive, a folder under the user's profile or AppData) is an
ordinary root.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3

from qronos_media.paths import SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS, UnsafeMediaPath, has_link_component, local_path

OUT_OF_SCOPE = "out_of_scope"


class OutOfScope(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RootScope:
    root_id: int
    path: Path
    kind: str
    max_depth: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RootScope":
        return cls(int(row["root_id"]), Path(row["path"]), str(row["kind"]), int(row["max_depth"]))


def load_scopes(db: sqlite3.Connection) -> dict[int, RootScope]:
    return {int(row["root_id"]): RootScope.from_row(row)
            for row in db.execute("SELECT * FROM roots WHERE enabled = 1")}


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def inside(path: Path | str, root: Path | str) -> bool:
    child, parent = _key(path), _key(root)
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


def _excluded(path: Path, excluded: tuple[Path, ...]) -> bool:
    return any(inside(path, root) for root in excluded)


def admit(scope: RootScope, raw: object, excluded: tuple[Path, ...]) -> Path:
    """Return the path if the index may read this video now, or raise OutOfScope.

    ``excluded`` is the media index's own data folder (and nothing else).
    """
    try:
        path = local_path(raw)
    except UnsafeMediaPath:
        raise OutOfScope("unsafe_path") from None
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise OutOfScope("not_media")
    if scope.kind == "file":
        if _key(path) != _key(scope.path):
            raise OutOfScope("outside_root")
    else:
        if not inside(path, scope.path) or _key(path) == _key(scope.path):
            raise OutOfScope("outside_root")
        depth = len(Path(_key(path)).relative_to(Path(_key(scope.path))).parts) - 1
        if depth > scope.max_depth:
            raise OutOfScope("beyond_depth")
    if _excluded(path, excluded):
        raise OutOfScope("media_state")
    if has_link_component(path):
        raise OutOfScope("link")
    return path


def admit_sidecar(video: Path, raw: object, excluded: tuple[Path, ...]) -> Path:
    """A subtitle the index may read: beside an already admitted video, same checks."""
    try:
        path = local_path(raw)
    except UnsafeMediaPath:
        raise OutOfScope("unsafe_path") from None
    if path.suffix.lower() not in SUBTITLE_EXTENSIONS:
        raise OutOfScope("not_subtitle")
    if _key(path.parent) != _key(video.parent):
        raise OutOfScope("not_beside_video")
    if _excluded(path, excluded):
        raise OutOfScope("media_state")
    if has_link_component(path):
        raise OutOfScope("link")
    return path


def root_inside_state(root: Path, excluded: tuple[Path, ...]) -> bool:
    """True when a proposed root lies inside the media index's own data folder."""
    return _excluded(root, excluded)


def outside_scope(paths: list[str], scope: RootScope, excluded: tuple[Path, ...]) -> list[str]:
    """The given paths that this scope no longer admits. Filesystem checks only, no database."""
    failing = []
    for path in paths:
        try:
            admit(scope, path, excluded)
        except OutOfScope:
            failing.append(path)
    return failing


def mark(db: sqlite3.Connection, paths: list[str]) -> int:
    """Mark rows out of scope; inside a short transaction. Their asset is kept."""
    for path in paths:
        db.execute("UPDATE files SET status = 'missing', error = ? WHERE path = ?", (OUT_OF_SCOPE, path))
    return len(paths)
