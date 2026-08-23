from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.resource_guard import GpuStatus, SystemStatus


class ResourceDecision(Enum):
    """Decision for starting a new resource-intensive task."""

    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True)
class ResourceThresholds:
    """Conservative limits for Qronos."""

    cpu_warn_percent: float = 80.0
    cpu_block_percent: float = 90.0

    ram_warn_percent: float = 75.0
    ram_block_percent: float = 85.0

    gpu_temp_warn_c: int = 75
    gpu_temp_block_c: int = 82

    gpu_utilization_warn_percent: int = 75
    gpu_utilization_block_percent: int = 90

    vram_warn_percent: float = 75.0
    vram_block_percent: float = 90.0


DEFAULT_THRESHOLDS = ResourceThresholds()


def evaluate_resources(
    system: SystemStatus,
    gpu: Optional[GpuStatus],
    thresholds: ResourceThresholds = DEFAULT_THRESHOLDS,
) -> ResourceDecision:
    """
    Decide whether a new heavy task should start.

    This function never stops an existing process.
    It only evaluates whether starting another heavy task is advisable.
    """

    if system.cpu_usage_percent >= thresholds.cpu_block_percent:
        return ResourceDecision.BLOCK

    if system.ram_usage_percent >= thresholds.ram_block_percent:
        return ResourceDecision.BLOCK

    if gpu is not None:
        if (
            gpu.gpu_utilization_percent is not None
            and gpu.gpu_utilization_percent
            >= thresholds.gpu_utilization_block_percent
        ):
            return ResourceDecision.BLOCK

        if (
            gpu.temperature_c is not None
            and gpu.temperature_c >= thresholds.gpu_temp_block_c
        ):
            return ResourceDecision.BLOCK

        if gpu.vram_used_percent is not None:
            if gpu.vram_used_percent >= thresholds.vram_block_percent:
                return ResourceDecision.BLOCK

    if system.cpu_usage_percent >= thresholds.cpu_warn_percent:
        return ResourceDecision.WARN

    if system.ram_usage_percent >= thresholds.ram_warn_percent:
        return ResourceDecision.WARN

    if gpu is not None:
        if (
            gpu.gpu_utilization_percent is not None
            and gpu.gpu_utilization_percent
            >= thresholds.gpu_utilization_warn_percent
        ):
            return ResourceDecision.WARN

        if (
            gpu.temperature_c is not None
            and gpu.temperature_c >= thresholds.gpu_temp_warn_c
        ):
            return ResourceDecision.WARN

        if gpu.vram_used_percent is not None:
            if gpu.vram_used_percent >= thresholds.vram_warn_percent:
                return ResourceDecision.WARN

    return ResourceDecision.ALLOW


def main() -> None:
    """Show the current resource decision."""
    from core.resource_guard import read_gpu_status, read_system_status

    system = read_system_status()
    gpu = read_gpu_status()

    decision = evaluate_resources(system, gpu)

    print(f"Qronos resource decision: {decision.value}")


if __name__ == "__main__":
    main()
