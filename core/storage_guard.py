from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from core.config import CONFIG


BYTES_PER_GB = 1024 ** 3


def bytes_to_gb(value: int) -> float:
    """Convert bytes to binary gigabytes."""
    return value / BYTES_PER_GB


def gb_to_bytes(value: float) -> int:
    """Convert binary gigabytes to whole bytes."""
    return int(value * BYTES_PER_GB)


@dataclass(frozen=True)
class VolumeStatus:
    """
    Free-space reading for one directory Qronos writes into.

    ``requested_path`` is the directory Qronos cares about. ``measured_path``
    is the nearest existing ancestor that could actually be measured, because
    a Qronos directory may not exist yet on a fresh installation.
    """

    requested_path: Path
    measured_path: Path
    total_bytes: int
    used_bytes: int
    free_bytes: int

    @property
    def total_gb(self) -> float:
        return bytes_to_gb(self.total_bytes)

    @property
    def used_gb(self) -> float:
        return bytes_to_gb(self.used_bytes)

    @property
    def free_gb(self) -> float:
        return bytes_to_gb(self.free_bytes)

    @property
    def used_percent(self) -> float:
        """Percentage of the volume already in use."""
        if self.total_bytes <= 0:
            return 0.0

        return (self.used_bytes / self.total_bytes) * 100.0

    @property
    def free_percent(self) -> float:
        """Percentage of the volume still available."""
        if self.total_bytes <= 0:
            return 0.0

        return (self.free_bytes / self.total_bytes) * 100.0

    def can_fit(
        self,
        required_bytes: int,
        reserve_bytes: int = 0,
    ) -> bool:
        """
        Return True when ``required_bytes`` fits and still leaves the reserve.

        The reserve exists so Qronos never fills a disk to the point where the
        operating system or the user's own work starts failing.
        """
        return self.free_bytes - required_bytes >= reserve_bytes


@dataclass(frozen=True)
class StorageStatus:
    """All volume readings taken in one pass."""

    volumes: tuple[VolumeStatus, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.volumes

    def for_path(self, path: str | Path) -> VolumeStatus | None:
        """
        Return the reading taken for ``path``, if one was taken.

        Comparison is done on the resolved requested path so callers can pass
        the same value they passed to :func:`read_storage_status`.
        """
        target = Path(path).expanduser()

        try:
            target = target.resolve()
        except (OSError, ValueError):
            return None

        for volume in self.volumes:
            if volume.requested_path == target:
                return volume

        return None

    @property
    def tightest(self) -> VolumeStatus | None:
        """
        Return the reading with the least free space.

        Storage decisions are made against the worst volume, so a single full
        disk is never hidden by a roomy one.
        """
        if not self.volumes:
            return None

        return min(
            self.volumes,
            key=lambda volume: volume.free_bytes,
        )

    def distinct_volumes(self) -> tuple[VolumeStatus, ...]:
        """
        Return one reading per physical volume.

        Several Qronos directories usually live on the same disk. Reporting
        each of them separately would count the same free space repeatedly, so
        the first reading per measured path wins.
        """
        seen: set[Path] = set()
        unique: list[VolumeStatus] = []

        for volume in self.volumes:
            if volume.measured_path in seen:
                continue

            seen.add(volume.measured_path)
            unique.append(volume)

        return tuple(unique)


def is_reparse_point(path: str | Path) -> bool:
    """
    True for a symlink, a junction, or any other reparse point.

    ``Path.is_symlink`` is not enough on Windows, which is the only platform
    Qronos ships on. A *junction* is a reparse point but not a symlink, so
    ``is_symlink`` answers False for one, and ``stat`` follows it. Junctions
    also need no privileges: any program running as the user can create one
    with ``mklink /J``, while a symlink needs Developer Mode or an
    administrator.

    Measured before this existed: a junction placed inside a Qronos directory
    and pointing at a folder of documents made 1.2 MB of the user's files count
    toward a 250 KB Qronos quota. Nothing was deleted — containment held — but
    the component then looked permanently over its cap, so cleanup would keep
    removing real scratch data trying to reach a limit it could never reach.

    A path that is not there answers False. It is not a reparse point, it is
    absent, and saying otherwise makes a caller report a missing file as a
    link — which is what the janitor's own tests caught the first time this
    was written.

    Any other unreadable path answers True. Not being able to tell what
    something is, is a reason to leave it alone rather than to walk into it.
    """
    try:
        if os.path.islink(path):
            return True

        if os.path.isjunction(path):
            return True

        attributes = getattr(os.lstat(path), "st_file_attributes", 0)

        return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except FileNotFoundError:
        return False
    except (OSError, ValueError, AttributeError):
        return True


def is_nameable(path: str | Path) -> bool:
    """
    False when the text cannot be a path on any platform.

    A control character, the null byte included, is not a legal part of a file
    name anywhere. Checking the text directly is deliberate: the platforms do
    not agree on what happens otherwise. POSIX raises ``ValueError`` from
    ``Path.resolve``, which the caller can catch. Windows resolves the string
    without complaint, produces a path that simply does not exist, and leaves
    the caller to discover that on its own.
    """
    return not any(ch < " " for ch in str(path))


def resolve_measurable_path(path: str | Path) -> Path | None:
    """
    Return the nearest existing directory at or above ``path``.

    ``shutil.disk_usage`` needs a path that exists. Qronos declares its
    directories in :mod:`core.config` before they are created, so the reading
    walks upwards until it finds something real. Returns None when nothing in
    the chain exists or the path cannot be resolved at all.

    A path that cannot be named is rejected before the walk begins. Without
    that, a malformed path on Windows resolves against the working directory
    and the walk climbs out of the nonsense into a real ancestor, so a caller
    asking about garbage would be handed a genuine reading for an unrelated
    volume.
    """
    if not is_nameable(path):
        return None

    try:
        candidate = Path(path).expanduser().resolve()
    except (OSError, ValueError):
        # A path Qronos cannot even name is not a path it can measure.
        return None

    # Path.parents stops at the anchor, so this terminates on every platform.
    for current in (candidate, *candidate.parents):
        if current.is_dir():
            return current

    return None


def read_volume_status(path: str | Path) -> VolumeStatus | None:
    """
    Read free space for one directory.

    Returns None instead of raising when the volume cannot be measured, which
    matches how :func:`core.resource_guard.read_gpu_status` treats missing
    hardware. A storage reading is diagnostic input, never a reason to crash.
    """
    try:
        requested = Path(path).expanduser().resolve()
    except (OSError, ValueError):
        return None

    measured = resolve_measurable_path(requested)

    if measured is None:
        return None

    try:
        usage = shutil.disk_usage(measured)
    except OSError:
        return None

    return VolumeStatus(
        requested_path=requested,
        measured_path=measured,
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
    )


def default_managed_paths() -> tuple[Path, ...]:
    """
    Return the directories Qronos is responsible for filling.

    These are the locations whose growth Qronos controls: downloaded models,
    scratch audio, logs and stored memory. The user's own disk usage elsewhere
    is observed, never managed.
    """
    paths = CONFIG.paths

    return (
        paths.models,
        paths.temp,
        paths.logs,
        paths.data,
        paths.memory,
    )


def read_storage_status(
    paths: tuple[Path, ...] | None = None,
) -> StorageStatus:
    """
    Read free space for every managed directory.

    Unmeasurable directories are omitted rather than reported as zero, so a
    caller can distinguish "no space" from "no reading".
    """
    targets = (
        paths
        if paths is not None
        else default_managed_paths()
    )

    readings: list[VolumeStatus] = []

    for target in targets:
        volume = read_volume_status(target)

        if volume is not None:
            readings.append(volume)

    return StorageStatus(volumes=tuple(readings))


def main() -> None:
    """Display the current Qronos storage status."""
    status = read_storage_status()

    print("=== Qronos Storage Status ===")

    if status.is_empty:
        print("Storage status: unavailable")
        return

    for volume in status.distinct_volumes():
        print(
            f"{volume.measured_path}: "
            f"{volume.free_gb:.1f} GB free of "
            f"{volume.total_gb:.1f} GB "
            f"({volume.used_percent:.1f}% used)"
        )

    tightest = status.tightest

    if tightest is not None:
        print(
            f"Tightest volume: {tightest.measured_path} "
            f"({tightest.free_gb:.1f} GB free)"
        )


if __name__ == "__main__":
    main()
