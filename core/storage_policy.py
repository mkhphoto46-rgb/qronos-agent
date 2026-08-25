from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.storage_budget import (
    BudgetComponent,
    CapLevel,
    ComponentBudget,
    ComponentUsage,
    classify_usage,
)
from core.storage_guard import (
    StorageStatus,
    VolumeStatus,
    bytes_to_gb,
    gb_to_bytes,
)


class StorageDecision(Enum):
    """Decision for an operation that consumes disk space."""

    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True)
class StorageThresholds:
    """
    Volume-level limits for Qronos.

    Absolute and percentage limits are both checked. The absolute limit protects
    small volumes, where 10% free is still not enough for a model download. The
    percentage limit protects large volumes, where 20 GB free on a 4 TB disk
    means the disk is nearly full.

    These are policy defaults, not benchmarks. Qronos needs roughly 18 GB in
    total, so a block limit of 8 GB free plus a 5 GB reserve leaves the
    operating system room to work.
    """

    free_warn_gb: float = 20.0
    free_block_gb: float = 8.0

    used_warn_percent: float = 85.0
    used_block_percent: float = 93.0

    # Headroom Qronos refuses to consume even when an operation would
    # technically fit, so the operating system and the user's own work never
    # fail because Qronos filled the disk to the last byte.
    reserve_gb: float = 5.0

    @property
    def reserve_bytes(self) -> int:
        return gb_to_bytes(self.reserve_gb)


DEFAULT_STORAGE_THRESHOLDS = StorageThresholds()


@dataclass(frozen=True)
class StorageEvaluation:
    """
    Outcome of a storage check, with the reason retained for the audit trail.

    Every decision carries a human-readable reason because the Resource
    Governor has to be able to tell the user why an operation was refused.
    """

    decision: StorageDecision
    reason: str
    volume: VolumeStatus | None = None
    component: BudgetComponent | None = None

    @property
    def is_allowed(self) -> bool:
        return self.decision is StorageDecision.ALLOW

    @property
    def is_blocked(self) -> bool:
        return self.decision is StorageDecision.BLOCK


def _blocked(
    reason: str,
    volume: VolumeStatus | None = None,
    component: BudgetComponent | None = None,
) -> StorageEvaluation:
    return StorageEvaluation(
        decision=StorageDecision.BLOCK,
        reason=reason,
        volume=volume,
        component=component,
    )


def evaluate_volume(
    volume: VolumeStatus,
    thresholds: StorageThresholds = DEFAULT_STORAGE_THRESHOLDS,
) -> StorageEvaluation:
    """
    Decide whether one volume has room for further Qronos growth.

    Reports only. Never deletes anything, never stops a running task.
    """
    if volume.free_gb <= thresholds.free_block_gb:
        return _blocked(
            f"Only {volume.free_gb:.1f} GB free on "
            f"{volume.measured_path}, at or below the "
            f"{thresholds.free_block_gb:.1f} GB block limit.",
            volume,
        )

    if volume.used_percent >= thresholds.used_block_percent:
        return _blocked(
            f"{volume.measured_path} is {volume.used_percent:.1f}% full, "
            f"at or above the {thresholds.used_block_percent:.1f}% "
            "block limit.",
            volume,
        )

    if volume.free_gb <= thresholds.free_warn_gb:
        return StorageEvaluation(
            decision=StorageDecision.WARN,
            reason=(
                f"Only {volume.free_gb:.1f} GB free on "
                f"{volume.measured_path}, at or below the "
                f"{thresholds.free_warn_gb:.1f} GB warning limit."
            ),
            volume=volume,
        )

    if volume.used_percent >= thresholds.used_warn_percent:
        return StorageEvaluation(
            decision=StorageDecision.WARN,
            reason=(
                f"{volume.measured_path} is {volume.used_percent:.1f}% full, "
                f"at or above the {thresholds.used_warn_percent:.1f}% "
                "warning limit."
            ),
            volume=volume,
        )

    return StorageEvaluation(
        decision=StorageDecision.ALLOW,
        reason=f"{volume.free_gb:.1f} GB free on {volume.measured_path}.",
        volume=volume,
    )


def evaluate_storage(
    status: StorageStatus,
    thresholds: StorageThresholds = DEFAULT_STORAGE_THRESHOLDS,
) -> StorageEvaluation:
    """
    Decide the overall storage state across every managed volume.

    Fail-closed. An empty reading means the disk could not be measured, and an
    unmeasurable disk is treated as unsafe rather than as evidence of health.
    Qronos must never conclude "all clear" from a missing sensor: a failed
    reading and a healthy reading are different things, and only one of them
    justifies consuming space.

    The worst volume wins, so one full disk is never hidden by a roomy one.
    """
    volumes = status.distinct_volumes()

    if not volumes:
        return _blocked(
            "No storage reading was available, so free space could not be "
            "verified. Treated as unsafe."
        )

    evaluations = [
        evaluate_volume(volume, thresholds)
        for volume in volumes
    ]

    for decision in (StorageDecision.BLOCK, StorageDecision.WARN):
        for evaluation in evaluations:
            if evaluation.decision is decision:
                return evaluation

    # Every volume is fine. Report the tightest one so the reason names the
    # volume that will run out first.
    return min(
        evaluations,
        key=lambda evaluation: (
            evaluation.volume.free_bytes
            if evaluation.volume is not None
            else 0
        ),
    )


def evaluate_download(
    volume: VolumeStatus | None,
    required_bytes: int,
    thresholds: StorageThresholds = DEFAULT_STORAGE_THRESHOLDS,
) -> StorageEvaluation:
    """
    Decide whether a download of ``required_bytes`` may start.

    This is the preflight that stops Qronos pulling a multi-gigabyte model onto
    a disk that cannot hold it. The reserve is applied on top of the download
    size, so a download that would exactly fill the disk is refused.

    Fail-closed in two ways: an unknown volume is refused, and an unknown
    download size is refused. A model whose size is not known cannot be
    admitted, which mirrors the rule that an unbenchmarked resource requirement
    forbids loading.
    """
    if volume is None:
        return _blocked(
            "No storage reading was available, so "
            f"{bytes_to_gb(max(0, required_bytes)):.1f} GB could not be "
            "verified as available. Treated as unsafe."
        )

    if required_bytes < 0:
        return _blocked(
            f"Invalid download size: {required_bytes} bytes.",
            volume,
        )

    if required_bytes == 0:
        return _blocked(
            "The download size is unknown, so it cannot be verified against "
            "free space. Treated as unsafe.",
            volume,
        )

    if not volume.can_fit(required_bytes, thresholds.reserve_bytes):
        shortfall = (
            required_bytes
            + thresholds.reserve_bytes
            - volume.free_bytes
        )

        return _blocked(
            f"{bytes_to_gb(required_bytes):.1f} GB is required plus a "
            f"{thresholds.reserve_gb:.1f} GB reserve, but only "
            f"{volume.free_gb:.1f} GB is free on "
            f"{volume.measured_path}. Short by "
            f"{bytes_to_gb(shortfall):.1f} GB.",
            volume,
        )

    # The download fits, but the volume may already be in a warning state.
    # Reporting that lets the caller tell the user this is the last download
    # that will comfortably fit.
    current = evaluate_volume(volume, thresholds)

    if current.decision is StorageDecision.BLOCK:
        return current

    if current.decision is StorageDecision.WARN:
        return StorageEvaluation(
            decision=StorageDecision.WARN,
            reason=(
                f"{bytes_to_gb(required_bytes):.1f} GB fits, but "
                f"{current.reason}"
            ),
            volume=volume,
        )

    return StorageEvaluation(
        decision=StorageDecision.ALLOW,
        reason=(
            f"{bytes_to_gb(required_bytes):.1f} GB fits in the "
            f"{volume.free_gb:.1f} GB free on {volume.measured_path}."
        ),
        volume=volume,
    )


def evaluate_component_write(
    budget: ComponentBudget,
    usage: ComponentUsage,
    additional_bytes: int = 0,
) -> StorageEvaluation:
    """
    Decide whether a component may accept more data.

    Maps the soft/medium/hard ladder onto a decision:

    ``NORMAL``  allow
    ``SOFT``    allow, cleanup should run in the background
    ``MEDIUM``  warn, refuse low-priority writes
    ``HARD``    block until the component is back under its cap

    Fail-closed on a partial measurement. A partial walk understates usage, so
    treating it as proof of headroom could let a component grow past its cap
    unnoticed.
    """
    if usage.partial:
        return _blocked(
            f"Usage for {budget.component.value} could not be measured "
            "completely, so remaining headroom is unknown. Treated as unsafe.",
            component=budget.component,
        )

    projected = usage.total_bytes + max(0, additional_bytes)

    if projected >= budget.hard_cap_bytes:
        return _blocked(
            f"{budget.component.value} would reach "
            f"{bytes_to_gb(projected):.3f} GB against a hard cap of "
            f"{budget.hard_cap_gb:.2f} GB.",
            component=budget.component,
        )

    level = classify_usage(budget, usage)

    if level is CapLevel.MEDIUM:
        return StorageEvaluation(
            decision=StorageDecision.WARN,
            reason=(
                f"{budget.component.value} is at "
                f"{bytes_to_gb(usage.total_bytes):.3f} GB, above its medium "
                f"cap of {bytes_to_gb(budget.medium_cap_bytes):.3f} GB. "
                "Cleanup should run and low-priority writes should be "
                "refused."
            ),
            component=budget.component,
        )

    if level is CapLevel.SOFT:
        return StorageEvaluation(
            decision=StorageDecision.ALLOW,
            reason=(
                f"{budget.component.value} is at "
                f"{bytes_to_gb(usage.total_bytes):.3f} GB, above its soft cap "
                f"of {bytes_to_gb(budget.soft_cap_bytes):.3f} GB. Background "
                "cleanup should run."
            ),
            component=budget.component,
        )

    return StorageEvaluation(
        decision=StorageDecision.ALLOW,
        reason=(
            f"{budget.component.value} is at "
            f"{bytes_to_gb(usage.total_bytes):.3f} GB of "
            f"{budget.hard_cap_gb:.2f} GB."
        ),
        component=budget.component,
    )


def worst_decision(
    evaluations: tuple[StorageEvaluation, ...],
) -> StorageEvaluation:
    """
    Return the most restrictive evaluation.

    Used when several components or volumes are checked together, so that any
    single blocking condition governs the outcome.
    """
    if not evaluations:
        return _blocked(
            "No storage evaluation was produced. Treated as unsafe."
        )

    for decision in (StorageDecision.BLOCK, StorageDecision.WARN):
        for evaluation in evaluations:
            if evaluation.decision is decision:
                return evaluation

    return evaluations[0]


def main() -> None:
    """Show the current storage decision."""
    from core.storage_guard import read_storage_status

    evaluation = evaluate_storage(read_storage_status())

    print(f"Qronos storage decision: {evaluation.decision.value}")
    print(evaluation.reason)


if __name__ == "__main__":
    main()
