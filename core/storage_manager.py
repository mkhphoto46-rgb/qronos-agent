from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.artifact_ownership import ArtifactOwnershipRegistry
from core.storage_budget import (
    DEFAULT_BUDGETS,
    BudgetComponent,
    CapLevel,
    ComponentBudget,
    ComponentUsage,
    classify_usage,
    measure_component,
    total_hard_cap_bytes,
    total_soft_cap_bytes,
)
from core.storage_guard import (
    StorageStatus,
    VolumeStatus,
    bytes_to_gb,
    read_storage_status,
)
from core.storage_janitor import CleanupOutcome, CleanupPlan, StorageJanitor
from core.storage_policy import (
    DEFAULT_STORAGE_THRESHOLDS,
    StorageDecision,
    StorageEvaluation,
    StorageThresholds,
    evaluate_component_write,
    evaluate_download,
    evaluate_storage,
    worst_decision,
)


class EmergencyAction(Enum):
    """
    Steps in the critical disk-pressure sequence, in order.

    The Storage Manager does not own model downloads or the task queue, so it
    reports the sequence rather than performing it. Returning an ordered plan
    keeps the decision here and the authority where it belongs, and makes the
    sequence testable without side effects.
    """

    STOP_MODEL_DOWNLOADS = "stop_model_downloads"
    CLEAR_DISPOSABLE_CACHE = "clear_disposable_cache"
    CLEAR_TEMP = "clear_temp"
    PAUSE_DEFERRED_TASKS = "pause_deferred_tasks"
    NOTIFY_USER = "notify_user"


EMERGENCY_SEQUENCE: tuple[EmergencyAction, ...] = (
    EmergencyAction.STOP_MODEL_DOWNLOADS,
    EmergencyAction.CLEAR_DISPOSABLE_CACHE,
    EmergencyAction.CLEAR_TEMP,
    EmergencyAction.PAUSE_DEFERRED_TASKS,
    EmergencyAction.NOTIFY_USER,
)


@dataclass(frozen=True)
class ComponentReport:
    """Everything known about one component in a single pass."""

    budget: ComponentBudget
    usage: ComponentUsage
    level: CapLevel
    evaluation: StorageEvaluation

    @property
    def component(self) -> BudgetComponent:
        return self.budget.component

    @property
    def needs_cleanup(self) -> bool:
        return self.level is not CapLevel.NORMAL

    @property
    def is_breached(self) -> bool:
        """Usage has reached the hard cap; growth must stop."""
        return self.level is CapLevel.HARD

    @property
    def is_alarming(self) -> bool:
        """
        A non-disposable component has crossed its soft cap.

        For memory the soft cap is a defect detector rather than a cleanup
        trigger, so crossing it is worth reporting immediately instead of
        waiting for the emergency ceiling.
        """
        return (
            not self.budget.disposable
            and self.level is not CapLevel.NORMAL
        )

    def describe(self) -> str:
        return (
            f"{self.component.value}: "
            f"{self.usage.total_gb:.3f} GB "
            f"(soft {self.budget.soft_cap_gb:.2f} / hard "
            f"{self.budget.hard_cap_gb:.2f} GB, "
            f"{self.usage.file_count} files) "
            f"level={self.level.value} "
            f"decision={self.evaluation.decision.value}"
        )


@dataclass(frozen=True)
class StorageReport:
    """A complete storage picture: volumes, components and the verdict."""

    status: StorageStatus
    volume_evaluation: StorageEvaluation
    components: tuple[ComponentReport, ...]
    overall: StorageEvaluation

    @property
    def managed_bytes(self) -> int:
        return sum(
            report.usage.total_bytes
            for report in self.components
        )

    @property
    def breached_components(self) -> tuple[BudgetComponent, ...]:
        return tuple(
            report.component
            for report in self.components
            if report.is_breached
        )

    @property
    def alarming_components(self) -> tuple[BudgetComponent, ...]:
        return tuple(
            report.component
            for report in self.components
            if report.is_alarming
        )

    @property
    def components_needing_cleanup(self) -> tuple[BudgetComponent, ...]:
        return tuple(
            report.component
            for report in self.components
            if report.needs_cleanup
        )

    @property
    def is_critical(self) -> bool:
        return self.volume_evaluation.decision is StorageDecision.BLOCK


class StorageManager:
    """
    The Storage Manager component, sitting under the Supervisor.

    Owns disk quota, cache eviction, temporary-file cleanup, retention, model
    storage awareness, free-space emergency handling, orphan cleanup and the
    artifact ownership registry.

    Its governing rule is one sentence: **under disk pressure Qronos cleans
    Qronos-owned disposable data, and never user-owned files.** Every method
    here either reports, or delegates deletion to
    :class:`core.storage_janitor.StorageJanitor`, which enforces that rule on
    every individual path.

    Nothing in this class writes to disk except through the janitor and the
    ownership registry. Telemetry-style measurements are returned to the caller
    rather than persisted, so a status poll costs no writes.

    Deliberately not wired into the Resource Governor here. The Governor's own
    fail-closed correction is a separate change, and coupling the two would make
    both harder to review.
    """

    def __init__(
        self,
        budgets: tuple[ComponentBudget, ...] = DEFAULT_BUDGETS,
        registry: ArtifactOwnershipRegistry | None = None,
        janitor: StorageJanitor | None = None,
        thresholds: StorageThresholds = DEFAULT_STORAGE_THRESHOLDS,
    ) -> None:
        self.budgets = budgets
        self.thresholds = thresholds

        self.registry = (
            registry
            if registry is not None
            else ArtifactOwnershipRegistry()
        )

        self.janitor = (
            janitor
            if janitor is not None
            else StorageJanitor(registry=self.registry)
        )

    # ---------------------------------------------------------------- reading

    def read_status(self) -> StorageStatus:
        """Read free space for every managed directory."""
        return read_storage_status(
            tuple(budget.root for budget in self.budgets)
        )

    def measure_all(self) -> tuple[ComponentUsage, ...]:
        """Measure usage for every component."""
        return tuple(
            measure_component(budget)
            for budget in self.budgets
        )

    def report(
        self,
        status: StorageStatus | None = None,
    ) -> StorageReport:
        """
        Produce the complete storage picture in one pass.

        The overall verdict is the most restrictive of the volume verdict and
        every component verdict, so a single breached component or a single full
        disk governs the outcome.
        """
        resolved_status = (
            status
            if status is not None
            else self.read_status()
        )

        volume_evaluation = evaluate_storage(
            resolved_status,
            self.thresholds,
        )

        components: list[ComponentReport] = []

        for budget in self.budgets:
            usage = measure_component(budget)

            components.append(
                ComponentReport(
                    budget=budget,
                    usage=usage,
                    level=classify_usage(budget, usage),
                    evaluation=evaluate_component_write(budget, usage),
                )
            )

        overall = worst_decision(
            (volume_evaluation,)
            + tuple(report.evaluation for report in components)
        )

        return StorageReport(
            status=resolved_status,
            volume_evaluation=volume_evaluation,
            components=tuple(components),
            overall=overall,
        )

    # -------------------------------------------------------------- decisions

    def evaluate(self) -> StorageEvaluation:
        """The overall storage verdict."""
        return self.report().overall

    def evaluate_write(
        self,
        component: BudgetComponent,
        additional_bytes: int = 0,
    ) -> StorageEvaluation:
        """Decide whether one component may accept more data."""
        budget = self._budget_for(component)
        usage = measure_component(budget)

        return evaluate_component_write(budget, usage, additional_bytes)

    def evaluate_download(
        self,
        required_bytes: int,
        volume: VolumeStatus | None = None,
    ) -> StorageEvaluation:
        """
        Preflight a download of ``required_bytes``.

        When no volume is supplied, the volume holding the model directory is
        measured, because that is where a download lands. A missing reading
        blocks, in line with the fail-closed rule.
        """
        resolved = volume

        if resolved is None:
            from core.config import CONFIG
            from core.storage_guard import read_volume_status

            resolved = read_volume_status(CONFIG.paths.models)

        return evaluate_download(
            volume=resolved,
            required_bytes=required_bytes,
            thresholds=self.thresholds,
        )

    # --------------------------------------------------------------- cleanup

    def plan_cleanup(
        self,
        now: float,
        component: BudgetComponent | None = None,
    ) -> tuple[CleanupPlan, ...]:
        """
        Build cleanup plans without deleting anything.

        Returns one plan per component considered, including empty plans, so a
        caller can see that a component was examined and found to need nothing.
        """
        budgets = (
            (self._budget_for(component),)
            if component is not None
            else self.budgets
        )

        plans: list[CleanupPlan] = []

        for budget in budgets:
            usage = measure_component(budget)
            plans.append(self.janitor.plan(budget, usage, now))

        return tuple(plans)

    def execute_cleanup(
        self,
        plans: tuple[CleanupPlan, ...],
        dry_run: bool = False,
    ) -> tuple[CleanupOutcome, ...]:
        """Execute cleanup plans. Empty plans are skipped."""
        return tuple(
            self.janitor.execute(plan, dry_run=dry_run)
            for plan in plans
            if not plan.is_empty
        )

    def run_cleanup(
        self,
        now: float,
        component: BudgetComponent | None = None,
        dry_run: bool = False,
    ) -> tuple[CleanupOutcome, ...]:
        """Plan and execute in one call."""
        return self.execute_cleanup(
            self.plan_cleanup(now, component),
            dry_run=dry_run,
        )

    # -------------------------------------------------------------- emergency

    def emergency_sequence(
        self,
        report: StorageReport | None = None,
    ) -> tuple[EmergencyAction, ...]:
        """
        The ordered response to critical disk pressure.

        Empty when pressure is not critical. The Storage Manager cannot stop a
        download or pause a task itself, so this is a set of instructions for
        the Supervisor rather than an action.
        """
        resolved = report if report is not None else self.report()

        if not resolved.is_critical:
            return ()

        return EMERGENCY_SEQUENCE

    # ----------------------------------------------------------- maintenance

    def prune_ownership_records(self) -> int:
        """
        Drop Qronos-owned records whose files have vanished.

        Keeps quota accounting truthful after an external deletion. User-owned
        records are never pruned.
        """
        return len(self.registry.prune_missing_qronos_records())

    def alarms(
        self,
        report: StorageReport | None = None,
    ) -> tuple[str, ...]:
        """
        Conditions that indicate a defect rather than a routine state.

        A non-disposable component crossing its **soft** cap is the important
        one. Memory's 2 GB soft cap is sized so that reaching it means
        consolidation has stopped working — cleanup cannot fix that, and
        quietly deleting meaningful memory would hide the fault. Waiting for
        the 15 GB emergency ceiling would mean noticing the fault long after
        it started.
        """
        resolved = report if report is not None else self.report()

        messages: list[str] = []

        for entry in resolved.components:
            if entry.is_alarming:
                ceiling = (
                    " The emergency ceiling has been reached."
                    if entry.is_breached
                    else ""
                )

                messages.append(
                    f"{entry.component.value} is at "
                    f"{entry.usage.total_gb:.3f} GB, above its "
                    f"{entry.budget.soft_cap_gb:.2f} GB alarm threshold. "
                    "This component is not disposable, so cleanup cannot "
                    "reduce it. Investigate consolidation rather than "
                    f"deleting.{ceiling}"
                )

            if entry.usage.partial:
                messages.append(
                    f"{entry.component.value} could not be measured "
                    "completely, so its headroom is unknown."
                )

        if resolved.status.is_empty:
            messages.append(
                "No volume reading was available, so free space is unknown."
            )

        return tuple(messages)

    # ---------------------------------------------------------------- helpers

    def _budget_for(
        self,
        component: BudgetComponent,
    ) -> ComponentBudget:
        for budget in self.budgets:
            if budget.component is component:
                return budget

        raise ValueError(
            f"No budget configured for component: {component.value}"
        )


def main() -> None:
    """Print a full storage report."""
    import time

    manager = StorageManager()
    report = manager.report()

    print("=== Qronos Storage Manager ===")
    print(
        f"Managed data: {bytes_to_gb(report.managed_bytes):.3f} GB "
        f"(normal footprint "
        f"{bytes_to_gb(total_soft_cap_bytes(manager.budgets)):.2f} GB, "
        f"emergency envelope "
        f"{bytes_to_gb(total_hard_cap_bytes(manager.budgets)):.2f} GB)"
    )
    print(f"Volume: {report.volume_evaluation.decision.value}")
    print(f"  {report.volume_evaluation.reason}")
    print()

    for entry in report.components:
        print(entry.describe())

    print()
    print(f"Overall: {report.overall.decision.value}")
    print(f"  {report.overall.reason}")

    for message in manager.alarms(report):
        print(f"ALARM: {message}")

    plans = manager.plan_cleanup(time.time())

    print()
    print("Cleanup preview:")

    for plan in plans:
        print(f"  {plan.describe()}")

    sequence = manager.emergency_sequence(report)

    if sequence:
        print()
        print("Critical disk pressure. Required sequence:")

        for step, action in enumerate(sequence, start=1):
            print(f"  {step}. {action.value}")


if __name__ == "__main__":
    main()
