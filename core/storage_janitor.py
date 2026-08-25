from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from core.artifact_ownership import ArtifactOwnershipRegistry
from core.storage_budget import (
    BudgetComponent,
    CapLevel,
    CleanupTrigger,
    ComponentBudget,
    ComponentUsage,
    FileEntry,
    classify_usage,
    resolve_trigger,
)
from core.storage_guard import bytes_to_gb


class DeleteReason(Enum):
    """Why a file was selected for deletion."""

    EXPIRED = "expired"
    OVER_SIZE_CAP = "over_size_cap"


class SkipReason(Enum):
    """Why a file was not selected, or was rejected at execution time."""

    USER_OWNED = "user_owned"
    OUTSIDE_ROOT = "outside_root"
    SYMLINK = "symlink"
    NOT_A_FILE = "not_a_file"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    ORPHAN_NOT_ELIGIBLE = "orphan_not_eligible"
    COMPONENT_NOT_DISPOSABLE = "component_not_disposable"
    CHANGED_SINCE_PLAN = "changed_since_plan"


@dataclass(frozen=True)
class CleanupCandidate:
    """One file the janitor proposes to delete."""

    path: Path
    size_bytes: int
    age_seconds: float
    reason: DeleteReason


@dataclass(frozen=True)
class SkippedItem:
    """One file the janitor deliberately left alone, and why."""

    path: Path
    reason: SkipReason


@dataclass(frozen=True)
class CleanupPlan:
    """
    A proposal. Building one has no side effects whatsoever.

    Planning and executing are separated so that a plan can be inspected,
    logged, shown to the user or discarded without anything having been
    deleted. :meth:`StorageJanitor.execute` is the only code in the storage
    subsystem that removes a file.
    """

    component: BudgetComponent
    root: Path
    trigger: CleanupTrigger
    candidates: tuple[CleanupCandidate, ...] = ()
    skipped: tuple[SkippedItem, ...] = ()
    usage_before_bytes: int = 0
    target_bytes: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.candidates

    @property
    def reclaimable_bytes(self) -> int:
        return sum(candidate.size_bytes for candidate in self.candidates)

    @property
    def projected_usage_bytes(self) -> int:
        return max(0, self.usage_before_bytes - self.reclaimable_bytes)

    @property
    def meets_target(self) -> bool:
        """
        True when executing the plan would bring usage to the target.

        False means cleanup alone cannot fix the component: everything eligible
        has been selected and it is still over target. That is a condition worth
        surfacing rather than retrying, because the remaining data is either
        user-owned or not disposable.
        """
        return self.projected_usage_bytes <= self.target_bytes

    def describe(self) -> str:
        return (
            f"{self.component.value}: {len(self.candidates)} files, "
            f"{bytes_to_gb(self.reclaimable_bytes):.3f} GB reclaimable, "
            f"trigger={self.trigger.value}"
        )


@dataclass(frozen=True)
class DeletionFailure:
    """A candidate that could not be deleted."""

    path: Path
    error: str


@dataclass(frozen=True)
class CleanupOutcome:
    """
    Result of executing a plan.

    This is the audit record for the deletion. It is returned rather than
    logged so the caller decides where it belongs, which keeps the janitor
    free of side effects other than the deletions themselves.
    """

    component: BudgetComponent
    deleted: tuple[Path, ...] = ()
    rejected: tuple[SkippedItem, ...] = ()
    failed: tuple[DeletionFailure, ...] = ()
    reclaimed_bytes: int = 0

    @property
    def deleted_count(self) -> int:
        return len(self.deleted)

    @property
    def fully_successful(self) -> bool:
        return not self.failed and not self.rejected


class StorageJanitor:
    """
    Removes Qronos-owned disposable data, and nothing else.

    Every safety rule the janitor enforces exists because breaking it would
    destroy something the user cares about:

    * **Never a user-owned file.** Ownership transfer is permanent; a
      transferred artifact leaves Qronos's reach entirely.
    * **Never outside the component root.** A path that resolves outside its
      declared root is refused, so a stray absolute path or a ``..`` cannot
      reach into the user's documents.
    * **Never a symlink.** Deleting a link is confusing; following one is
      dangerous. Both are avoided by refusing them.
    * **Never a directory.** Only regular files are removed, so an unexpected
      tree cannot disappear in one call.
    * **Never a non-disposable component.** Memory holds meaningful state and
      must be consolidated and compacted by the memory manager, not deleted by
      a storage sweep.
    * **Re-checked at execution.** A plan may be minutes old. Every rule is
      applied again immediately before each deletion, because the filesystem
      may have changed in between.
    """

    def __init__(
        self,
        registry: ArtifactOwnershipRegistry | None = None,
    ) -> None:
        self.registry = (
            registry
            if registry is not None
            else ArtifactOwnershipRegistry()
        )

    # ------------------------------------------------------------------ plan

    def plan(
        self,
        budget: ComponentBudget,
        usage: ComponentUsage,
        now: float,
        include_orphans: bool | None = None,
    ) -> CleanupPlan:
        """
        Decide what could be deleted, without deleting anything.

        ``include_orphans`` controls unregistered files found beneath a
        component root. Orphan cleanup is an explicit Storage Manager
        responsibility — crashed workers leave files behind — but only for
        disposable components, and never for anything the registry attributes
        to the user. It defaults to the component's own ``disposable`` flag.

        Selection is oldest-first for size-driven cleanup, so the most recently
        produced data survives longest. Age-driven cleanup takes everything past
        the age limit regardless of how full the component is.
        """
        eligible_orphans = (
            budget.disposable
            if include_orphans is None
            else include_orphans
        )

        trigger = resolve_trigger(budget, usage, now)
        target = self._target_bytes(budget, usage)

        if not budget.disposable:
            # Memory and any other stateful component are never swept. They are
            # reported so the caller can raise an alarm and hand the problem to
            # the component that understands the data.
            return CleanupPlan(
                component=budget.component,
                root=budget.root,
                trigger=trigger,
                candidates=(),
                skipped=tuple(
                    SkippedItem(
                        path=entry.path,
                        reason=SkipReason.COMPONENT_NOT_DISPOSABLE,
                    )
                    for entry in usage.entries_oldest_first()
                ),
                usage_before_bytes=usage.total_bytes,
                target_bytes=target,
            )

        if trigger is CleanupTrigger.NONE:
            return CleanupPlan(
                component=budget.component,
                root=budget.root,
                trigger=trigger,
                usage_before_bytes=usage.total_bytes,
                target_bytes=target,
            )

        skipped: list[SkippedItem] = []
        selected: dict[Path, CleanupCandidate] = {}

        # Files that are safe to consider at all.
        allowed: list[FileEntry] = []

        for entry in usage.entries_oldest_first():
            reason = self._screen(entry.path, budget.root, eligible_orphans)

            if reason is not None:
                skipped.append(
                    SkippedItem(path=entry.path, reason=reason)
                )
                continue

            allowed.append(entry)

        # Age rule: everything past the limit goes, regardless of size.
        expired_paths: set[Path] = set()

        if budget.max_age_seconds is not None:
            for entry in allowed:
                if entry.age_seconds(now) >= budget.max_age_seconds:
                    selected[entry.path] = CleanupCandidate(
                        path=entry.path,
                        size_bytes=entry.size_bytes,
                        age_seconds=entry.age_seconds(now),
                        reason=DeleteReason.EXPIRED,
                    )
                    expired_paths.add(entry.path)

        # Size rule: keep removing the oldest remaining file until projected
        # usage reaches the target.
        projected = usage.total_bytes - sum(
            candidate.size_bytes for candidate in selected.values()
        )

        if classify_usage(budget, usage) is not CapLevel.NORMAL:
            for entry in allowed:
                if projected <= target:
                    break

                if entry.path in expired_paths:
                    continue

                selected[entry.path] = CleanupCandidate(
                    path=entry.path,
                    size_bytes=entry.size_bytes,
                    age_seconds=entry.age_seconds(now),
                    reason=DeleteReason.OVER_SIZE_CAP,
                )

                projected -= entry.size_bytes

        # Preserve oldest-first order in the plan for predictable execution and
        # readable output.
        ordered = tuple(
            selected[entry.path]
            for entry in allowed
            if entry.path in selected
        )

        return CleanupPlan(
            component=budget.component,
            root=budget.root,
            trigger=trigger,
            candidates=ordered,
            skipped=tuple(skipped),
            usage_before_bytes=usage.total_bytes,
            target_bytes=target,
        )

    # --------------------------------------------------------------- execute

    def execute(
        self,
        plan: CleanupPlan,
        dry_run: bool = False,
    ) -> CleanupOutcome:
        """
        Delete the files in a plan.

        The only code in the storage subsystem that removes anything. Every
        safety rule from :meth:`plan` is applied again here, because the plan
        may have been built some time ago and the filesystem may have changed:
        a file may have been moved, replaced by a symlink, or handed to the user
        in the meantime.

        ``dry_run`` reports exactly what would happen and touches nothing,
        which makes the whole path testable and lets a caller show the user a
        truthful preview.
        """
        deleted: list[Path] = []
        rejected: list[SkippedItem] = []
        failed: list[DeletionFailure] = []
        reclaimed = 0

        for candidate in plan.candidates:
            reason = self._screen(
                candidate.path,
                plan.root,
                # Orphan eligibility was already decided during planning; at
                # execution the question is only whether this specific path is
                # still safe to remove.
                include_orphans=True,
            )

            if reason is not None:
                rejected.append(
                    SkippedItem(path=candidate.path, reason=reason)
                )
                continue

            try:
                size_now = candidate.path.stat().st_size
            except OSError:
                rejected.append(
                    SkippedItem(
                        path=candidate.path,
                        reason=SkipReason.MISSING,
                    )
                )
                continue

            if size_now != candidate.size_bytes:
                # The file was rewritten after the plan was built. It is no
                # longer the file that was approved for deletion.
                rejected.append(
                    SkippedItem(
                        path=candidate.path,
                        reason=SkipReason.CHANGED_SINCE_PLAN,
                    )
                )
                continue

            if dry_run:
                deleted.append(candidate.path)
                reclaimed += size_now
                continue

            try:
                candidate.path.unlink()
            except OSError as exc:
                failed.append(
                    DeletionFailure(
                        path=candidate.path,
                        error=str(exc),
                    )
                )
                continue

            deleted.append(candidate.path)
            reclaimed += size_now

            self._forget_if_registered(candidate.path)

        return CleanupOutcome(
            component=plan.component,
            deleted=tuple(deleted),
            rejected=tuple(rejected),
            failed=tuple(failed),
            reclaimed_bytes=reclaimed,
        )

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _target_bytes(
        budget: ComponentBudget,
        usage: ComponentUsage,
    ) -> int:
        """
        How far cleanup should reduce a component.

        Cleaning down to the soft cap rather than just under the hard cap gives
        the component room to work before the next sweep, instead of
        oscillating around its limit and sweeping on every write.
        """
        return min(budget.soft_cap_bytes, usage.total_bytes)

    def _screen(
        self,
        path: Path,
        root: Path,
        include_orphans: bool,
    ) -> SkipReason | None:
        """
        Apply every safety rule to one path.

        Returns the reason to skip, or None when the path may be deleted. The
        order matters: ownership is checked before anything else, so a
        user-owned file is refused even if it is also missing or malformed.

        The whole body is guarded. Any error naming, resolving or inspecting a
        path yields ``UNREADABLE``, which means "do not delete". A path Qronos
        cannot reason about is never a path it removes, so the failure mode of
        this function is always inaction.
        """
        try:
            if self.registry.is_user_owned(path):
                return SkipReason.USER_OWNED

            # Symlinks are rejected before anything else is computed about the
            # path. Checked on the unresolved path, because resolve() follows
            # the link and would answer for the target instead. Refusing here
            # also means the janitor never reasons about a link's target at
            # all, whether that target is inside the root or outside it.
            if path.is_symlink():
                return SkipReason.SYMLINK

            # Containment is checked on the resolved path so that a traversal
            # segment, or a parent directory that is itself a link, cannot
            # smuggle a path out of the root.
            resolved_root = root.expanduser().resolve()
            resolved_path = path.expanduser().resolve()

            if not resolved_path.is_relative_to(resolved_root):
                return SkipReason.OUTSIDE_ROOT

            if not path.exists():
                return SkipReason.MISSING

            if not path.is_file():
                return SkipReason.NOT_A_FILE

            if not include_orphans and self.registry.for_path(path) is None:
                return SkipReason.ORPHAN_NOT_ELIGIBLE
        except (OSError, ValueError):
            # ValueError covers malformed paths, which Path.resolve raises
            # rather than OSError.
            return SkipReason.UNREADABLE

        return None

    def _forget_if_registered(self, path: Path) -> None:
        """Drop the registry entry for a file that has just been deleted."""
        record = self.registry.for_path(path)

        if record is None:
            return

        self.registry.forget(record.artifact_id)


def main() -> None:
    """Show what a cleanup pass would do, without deleting anything."""
    import time

    from core.storage_budget import DEFAULT_BUDGETS, measure_component

    now = time.time()
    janitor = StorageJanitor()

    print("=== Qronos Storage Cleanup Preview ===")

    for budget in DEFAULT_BUDGETS:
        usage = measure_component(budget)
        plan = janitor.plan(budget, usage, now)

        print(plan.describe())

        if plan.candidates:
            outcome = janitor.execute(plan, dry_run=True)
            print(
                f"  would delete {outcome.deleted_count} files, "
                f"{bytes_to_gb(outcome.reclaimed_bytes):.3f} GB"
            )


if __name__ == "__main__":
    main()
