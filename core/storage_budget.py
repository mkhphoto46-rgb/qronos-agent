from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from core.config import CONFIG
from core.storage_guard import bytes_to_gb, gb_to_bytes


# Qronos-managed data lives under directories derived from the paths already
# declared in core.config. They are derived rather than added to QronosPaths so
# that the storage subsystem can be reviewed without touching shared config.
VISION_TEMP_DIR = CONFIG.paths.temp / "vision"
FILESYSTEM_INDEX_DIR = CONFIG.paths.data / "index"


class BudgetComponent(Enum):
    """A category of Qronos-managed data with its own quota."""

    MEMORY = "memory"
    VISION_TEMP = "vision_temp"
    FILESYSTEM_METADATA = "filesystem_metadata"
    LOGS_AND_TEMP = "logs_and_temp"


class CapLevel(Enum):
    """
    How close a component is to its hard cap.

    NORMAL   below the soft cap; nothing to do
    SOFT     start removing the oldest disposable items quietly
    MEDIUM   remove aggressively, tell the user, refuse low-priority writes
    HARD     refuse all new writes for this component until it is back under
    """

    NORMAL = "normal"
    SOFT = "soft"
    MEDIUM = "medium"
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

    ``hard_cap_bytes`` and ``max_age_seconds`` come from the approved budgets in
    the master context. ``soft_fraction`` and ``medium_fraction`` are policy
    defaults chosen here, not measurements, and are marked PENDING in the master
    context until signed off.
    """

    component: BudgetComponent
    root: Path
    hard_cap_bytes: int
    max_age_seconds: float | None = None
    soft_fraction: float = 0.60
    medium_fraction: float = 0.80

    # Deleting data is only appropriate for disposable, Qronos-owned artifacts.
    # A component holding meaningful state (memory) must be consolidated and
    # compacted first, and a breach is evidence of a defect rather than a
    # routine condition.
    disposable: bool = True

    def __post_init__(self) -> None:
        if self.hard_cap_bytes <= 0:
            raise ValueError(
                "hard_cap_bytes must be positive: "
                f"{self.hard_cap_bytes}"
            )

        if not 0.0 < self.soft_fraction < self.medium_fraction < 1.0:
            raise ValueError(
                "Expected 0 < soft_fraction < medium_fraction < 1, got "
                f"soft={self.soft_fraction} "
                f"medium={self.medium_fraction}"
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
    def soft_cap_bytes(self) -> int:
        return int(self.hard_cap_bytes * self.soft_fraction)

    @property
    def medium_cap_bytes(self) -> int:
        return int(self.hard_cap_bytes * self.medium_fraction)

    @property
    def hard_cap_gb(self) -> float:
        return bytes_to_gb(self.hard_cap_bytes)

    @property
    def max_age_days(self) -> float | None:
        if self.max_age_seconds is None:
            return None

        return self.max_age_seconds / SECONDS_PER_DAY


# Approved budgets. Total managed data is roughly 3.5 GB, which replaces the
# earlier 30 GB envelope. A cap that cannot be reached is not a safety limit:
# the memory cap in particular is sized so that breaching it is a meaningful
# signal that consolidation has stopped working.
DEFAULT_BUDGETS: tuple[ComponentBudget, ...] = (
    ComponentBudget(
        component=BudgetComponent.MEMORY,
        root=CONFIG.paths.memory,
        hard_cap_bytes=gb_to_bytes(2.0),
        max_age_seconds=None,
        disposable=False,
    ),
    ComponentBudget(
        component=BudgetComponent.VISION_TEMP,
        root=VISION_TEMP_DIR,
        hard_cap_bytes=gb_to_bytes(0.5),
        max_age_seconds=7 * SECONDS_PER_DAY,
    ),
    ComponentBudget(
        component=BudgetComponent.FILESYSTEM_METADATA,
        root=FILESYSTEM_INDEX_DIR,
        hard_cap_bytes=gb_to_bytes(0.5),
        max_age_seconds=None,
    ),
    ComponentBudget(
        component=BudgetComponent.LOGS_AND_TEMP,
        root=CONFIG.paths.logs,
        hard_cap_bytes=gb_to_bytes(0.5),
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

    for directory, _subdirectories, filenames in os.walk(
        root,
        followlinks=follow_symlinks,
    ):
        for filename in filenames:
            candidate = Path(directory) / filename

            try:
                if candidate.is_symlink():
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
    Place measured usage on the soft/medium/hard ladder.

    Compared against the hard cap first so that a component far over its limit
    is never reported as merely SOFT.
    """
    total = usage.total_bytes

    if total >= budget.hard_cap_bytes:
        return CapLevel.HARD

    if total >= budget.medium_cap_bytes:
        return CapLevel.MEDIUM

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


def total_managed_cap_bytes(
    budgets: tuple[ComponentBudget, ...] = DEFAULT_BUDGETS,
) -> int:
    """Sum of every component's hard cap."""
    return sum(budget.hard_cap_bytes for budget in budgets)


def main() -> None:
    """Show the configured budgets and current usage."""
    import time

    now = time.time()

    print("=== Qronos Managed-Storage Budgets ===")
    print(
        f"Total cap: "
        f"{bytes_to_gb(total_managed_cap_bytes()):.2f} GB"
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
            f"{usage.total_gb:.3f} / {budget.hard_cap_gb:.2f} GB "
            f"({usage.file_count} files, {age})"
        )
        print(
            f"  level={level.value} "
            f"trigger={trigger.value} "
            f"exists={usage.root_exists} "
            f"partial={usage.partial}"
        )


if __name__ == "__main__":
    main()
