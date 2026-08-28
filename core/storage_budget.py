from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from core.config import CONFIG
from core.storage_guard import (
    bytes_to_gb,
    gb_to_bytes,
    is_reparse_point,
)


# Qronos-managed data lives under directories derived from the paths already
# declared in core.config. They are derived rather than added to QronosPaths so
# that the storage subsystem can be reviewed without touching shared config.
VISION_TEMP_DIR = CONFIG.paths.temp / "vision"
IMAGE_TEMP_DIR = CONFIG.paths.temp / "image"
FILESYSTEM_INDEX_DIR = CONFIG.paths.data / "index"


class BudgetComponent(Enum):
    """A category of Qronos-managed data with its own quota."""

    MEMORY = "memory"
    VISION_TEMP = "vision_temp"
    IMAGE_TEMP = "image_temp"
    FILESYSTEM_METADATA = "filesystem_metadata"
    LOGS_AND_TEMP = "logs_and_temp"


class CapLevel(Enum):
    """
    Where measured usage sits on the two-cap ladder.

    NORMAL   below the soft cap; nothing to do
    SOFT     normal cleanup starts; oldest disposable data is reclaimed first
    HARD     growth is blocked until safe reclamation succeeds

    Two caps, not three. The soft cap is where routine reclamation begins and
    the hard cap is a boundary a component may not grow past. There is
    deliberately no middle level: an intermediate band would need its own
    distinct action to justify existing, and there isn't one.
    """

    NORMAL = "normal"
    SOFT = "soft"
    HARD = "hard"


class CleanupTrigger(Enum):
    """Why a cleanup is being planned."""

    NONE = "none"
    AGE = "age"
    SIZE = "size"
    AGE_AND_SIZE = "age_and_size"


SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True)
class ComponentBudget:
    """
    Quota for one category of Qronos-managed data.

    Size and age are independent triggers combined with OR: whichever limit is
    reached first causes cleanup. They are deliberately not an AND, because an
    AND would let a component sit just under its size cap indefinitely while
    accumulating stale data.

    Both caps are absolute rather than a fraction of one another. Their ratio
    varies enormously per component — memory allows 2 GB normally against a
    15 GB emergency ceiling, while vision temp allows 500 MB against 1.5 GB —
    and a shared percentage could not express both.
    """

    component: BudgetComponent
    root: Path
    soft_cap_bytes: int
    hard_cap_bytes: int
    max_age_seconds: float | None = None

    # Deleting data is only appropriate for disposable, Qronos-owned artifacts.
    # A component holding meaningful state (memory) must be consolidated and
    # compacted by the component that understands it. For such a component the
    # soft cap is an alarm rather than a cleanup trigger.
    disposable: bool = True

    def __post_init__(self) -> None:
        if self.soft_cap_bytes <= 0:
            raise ValueError(
                "soft_cap_bytes must be positive: "
                f"{self.soft_cap_bytes}"
            )

        if self.hard_cap_bytes <= self.soft_cap_bytes:
            raise ValueError(
                "hard_cap_bytes must exceed soft_cap_bytes, got "
                f"soft={self.soft_cap_bytes} "
                f"hard={self.hard_cap_bytes}"
            )

        if (
            self.max_age_seconds is not None
            and self.max_age_seconds <= 0
        ):
            raise ValueError(
                "max_age_seconds must be positive when set: "
                f"{self.max_age_seconds}"
            )

    @property
    def soft_cap_gb(self) -> float:
        return bytes_to_gb(self.soft_cap_bytes)

    @property
    def hard_cap_gb(self) -> float:
        return bytes_to_gb(self.hard_cap_bytes)

    @property
    def max_age_days(self) -> float | None:
        if self.max_age_seconds is None:
            return None

        return self.max_age_seconds / SECONDS_PER_DAY


# Approved budgets. The soft caps keep the normal footprint low while the hard
# caps preserve a large emergency envelope, which is the point of the two-cap
# model: routine operation stays small, and a component still has headroom
# before it is refused outright.
#
# Memory is the case the two caps were introduced for. A single 15 GB limit
# could never be reached by distilled memory, making it decoration rather than
# a safety limit; a single 2 GB limit would throw away headroom. Split, the
# 2 GB soft cap becomes a working defect detector and 15 GB stays an emergency
# ceiling.
DEFAULT_BUDGETS: tuple[ComponentBudget, ...] = (
    ComponentBudget(
        component=BudgetComponent.MEMORY,
        root=CONFIG.paths.memory,
        soft_cap_bytes=gb_to_bytes(2.0),
        hard_cap_bytes=gb_to_bytes(15.0),
        max_age_seconds=None,
        disposable=False,
    ),
    ComponentBudget(
        component=BudgetComponent.VISION_TEMP,
        root=VISION_TEMP_DIR,
        soft_cap_bytes=gb_to_bytes(0.5),
        hard_cap_bytes=gb_to_bytes(1.5),
        max_age_seconds=7 * SECONDS_PER_DAY,
    ),
    ComponentBudget(
        component=BudgetComponent.IMAGE_TEMP,
        root=IMAGE_TEMP_DIR,
        soft_cap_bytes=gb_to_bytes(3.0),
        hard_cap_bytes=gb_to_bytes(10.0),
        max_age_seconds=30 * SECONDS_PER_DAY,
    ),
    # Sourced from the filesystem index design envelope: a target of hundreds
    # of megabytes with a 1-2 GB ceiling.
    ComponentBudget(
        component=BudgetComponent.FILESYSTEM_METADATA,
        root=FILESYSTEM_INDEX_DIR,
        soft_cap_bytes=gb_to_bytes(0.5),
        hard_cap_bytes=gb_to_bytes(2.0),
        max_age_seconds=None,
    ),
    # PENDING: no approved figures exist for logs. These are chosen here and
    # must be confirmed rather than assumed correct.
    ComponentBudget(
        component=BudgetComponent.LOGS_AND_TEMP,
        root=CONFIG.paths.logs,
        soft_cap_bytes=gb_to_bytes(0.25),
        hard_cap_bytes=gb_to_bytes(1.0),
        max_age_seconds=30 * SECONDS_PER_DAY,
    ),
)


@dataclass(frozen=True)
class FileEntry:
    """One file found beneath a component root."""

    path: Path
    size_bytes: int
    modified_at: float

    def age_seconds(self, now: float) -> float:
        """
        Age relative to ``now``, never negative.

        A file with a modification time in the future (clock skew, a restored
        archive) is treated as brand new rather than as impossibly old, so it is
        never selected for deletion by the age rule.
        """
        return max(0.0, now - self.modified_at)


@dataclass(frozen=True)
class ComponentUsage:
    """
    Measured usage for one component.

    ``partial`` is True when at least one entry could not be read. A partial
    measurement understates usage, so it must never be used to justify allowing
    a write; see :mod:`core.storage_policy`, which treats it as unsafe.
    """

    component: BudgetComponent
    root: Path
    total_bytes: int
    entries: tuple[FileEntry, ...] = ()
    partial: bool = False
    root_exists: bool = True

    @property
    def file_count(self) -> int:
        return len(self.entries)

    @property
    def total_gb(self) -> float:
        return bytes_to_gb(self.total_bytes)

    def oldest_modified_at(self) -> float | None:
        if not self.entries:
            return None

        return min(entry.modified_at for entry in self.entries)

    def entries_oldest_first(self) -> tuple[FileEntry, ...]:
        """
        Entries sorted oldest first.

        The path is used as a secondary key so the order is deterministic when
        several files share a modification time, which matters because cleanup
        plans are compared in tests and shown to the user.
        """
        return tuple(
            sorted(
                self.entries,
                key=lambda entry: (entry.modified_at, str(entry.path)),
            )
        )

    def expired_entries(
        self,
        max_age_seconds: float | None,
        now: float,
    ) -> tuple[FileEntry, ...]:
        """Entries older than ``max_age_seconds``, oldest first."""
        if max_age_seconds is None:
            return ()

        return tuple(
            entry
            for entry in self.entries_oldest_first()
            if entry.age_seconds(now) >= max_age_seconds
        )


def measure_component(
    budget: ComponentBudget,
    follow_symlinks: bool = False,
) -> ComponentUsage:
    """
    Walk a component root and total the size of the files beneath it.

    Symlinks are never followed and never counted. Following them would allow a
    link inside a Qronos directory to make user data appear to belong to a
    Qronos quota, and later to be selected for deletion.

    Unreadable entries are skipped and flagged rather than raising, so one
    locked file cannot stop the whole measurement. Directories themselves are
    not counted; only their files.
    """
    root = budget.root

    if not root.is_dir():
        # A directory that has not been created yet holds nothing. This is the
        # normal state on a fresh installation, not an error.
        return ComponentUsage(
            component=budget.component,
            root=root,
            total_bytes=0,
            entries=(),
            partial=False,
            root_exists=False,
        )

    entries: list[FileEntry] = []
    total = 0
    partial = False

    for directory, subdirectories, filenames in os.walk(
        root,
        followlinks=follow_symlinks,
    ):
        if not follow_symlinks:
            # Prune reparse points from the walk itself, in place, which
            # is the only thing os.walk honours. followlinks=False covers
            # symlinks and nothing else, so a Windows junction is still
            # descended into — and the files behind it are ordinary files,
            # so checking each one individually would not catch them.
            # Measured: a junction pointing at a documents folder added
            # 1.2 MB of the user's files to a 250 KB Qronos quota.
            subdirectories[:] = [
                name
                for name in subdirectories
                if not is_reparse_point(Path(directory) / name)
            ]

        for filename in filenames:
            candidate = Path(directory) / filename

            try:
                # Reparse points rather than symlinks: a Windows junction
                # is not a symlink, needs no privileges to create, and
                # would otherwise make a folder of the user's documents
                # count toward a Qronos quota.
                if is_reparse_point(candidate):
                    continue

                stat_result = candidate.stat()
            except OSError:
                # Deleted mid-walk, permission denied, or an unreadable mount.
                partial = True
                continue

            entries.append(
                FileEntry(
                    path=candidate,
                    size_bytes=stat_result.st_size,
                    modified_at=stat_result.st_mtime,
                )
            )

            total += stat_result.st_size

    return ComponentUsage(
        component=budget.component,
        root=root,
        total_bytes=total,
        entries=tuple(entries),
        partial=partial,
        root_exists=True,
    )


def classify_usage(
    budget: ComponentBudget,
    usage: ComponentUsage,
) -> CapLevel:
    """
    Place measured usage on the two-cap ladder.

    Compared against the hard cap first so that a component far over its limit
    is never reported as merely SOFT.
    """
    total = usage.total_bytes

    if total >= budget.hard_cap_bytes:
        return CapLevel.HARD

    if total >= budget.soft_cap_bytes:
        return CapLevel.SOFT

    return CapLevel.NORMAL


def resolve_trigger(
    budget: ComponentBudget,
    usage: ComponentUsage,
    now: float,
) -> CleanupTrigger:
    """
    Decide why a cleanup is warranted, if at all.

    Size and age are evaluated independently and combined with OR, matching the
    approved rule: whichever limit is reached first causes cleanup.
    """
    by_size = (
        classify_usage(budget, usage) is not CapLevel.NORMAL
    )

    by_age = bool(
        usage.expired_entries(budget.max_age_seconds, now)
    )

    if by_size and by_age:
        return CleanupTrigger.AGE_AND_SIZE

    if by_age:
        return CleanupTrigger.AGE

    if by_size:
        return CleanupTrigger.SIZE

    return CleanupTrigger.NONE


def budget_for(
    component: BudgetComponent,
    budgets: tuple[ComponentBudget, ...] = DEFAULT_BUDGETS,
) -> ComponentBudget:
    """Look up a budget by component."""
    for budget in budgets:
        if budget.component is component:
            return budget

    raise ValueError(
        f"No budget defined for component: {component.value}"
    )


def total_soft_cap_bytes(
    budgets: tuple[ComponentBudget, ...] = DEFAULT_BUDGETS,
) -> int:
    """
    Sum of every component's soft cap: the expected normal footprint.

    This is the number that describes Qronos in ordinary operation.
    """
    return sum(budget.soft_cap_bytes for budget in budgets)


def total_hard_cap_bytes(
    budgets: tuple[ComponentBudget, ...] = DEFAULT_BUDGETS,
) -> int:
    """
    Sum of every component's hard cap: the emergency envelope.

    Reaching this total would mean every component simultaneously sat at its
    ceiling, which should never happen in practice. It bounds the worst case
    rather than describing the expected one.
    """
    return sum(budget.hard_cap_bytes for budget in budgets)


def main() -> None:
    """Show the configured budgets and current usage."""
    import time

    now = time.time()

    print("=== Qronos Managed-Storage Budgets ===")
    print(
        f"Normal footprint (soft): "
        f"{bytes_to_gb(total_soft_cap_bytes()):.2f} GB"
    )
    print(
        f"Emergency envelope (hard): "
        f"{bytes_to_gb(total_hard_cap_bytes()):.2f} GB"
    )
    print()

    for budget in DEFAULT_BUDGETS:
        usage = measure_component(budget)
        level = classify_usage(budget, usage)
        trigger = resolve_trigger(budget, usage, now)

        age = (
            f"{budget.max_age_days:.0f} days"
            if budget.max_age_days is not None
            else "no age limit"
        )

        print(
            f"{budget.component.value}: "
            f"{usage.total_gb:.3f} GB "
            f"(soft {budget.soft_cap_gb:.2f} / hard "
            f"{budget.hard_cap_gb:.2f} GB, "
            f"{usage.file_count} files, {age})"
        )
        print(
            f"  level={level.value} "
            f"trigger={trigger.value} "
            f"exists={usage.root_exists} "
            f"partial={usage.partial}"
        )


if __name__ == "__main__":
    main()
