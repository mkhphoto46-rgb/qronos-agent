"""
Shared resource-attribution context for Qronos.

This module connects the ResourceLedger to the existing sustained-load monitor.

Important invariant:
    Resource usage created by an admitted Qronos workload must not be
    interpreted as new external/user pressure.

Windows/NVIDIA telemetry does not provide trustworthy per-process GPU
utilization for this architecture, so while Qronos has an admitted GPU
reservation we do two conservative things:

    1. subtract Qronos's known reserved VRAM from the external-pressure view;
    2. ignore the global GPU-utilization percentage for external-pressure
       classification, because that percentage contains Qronos's own work.

Temperature is never masked. RAM/CPU remain visible. Foreground/activity-mode
policy remains responsible for protecting gaming/creator workloads when GPU
utilization cannot be attributed safely.
"""

from __future__ import annotations

from core.load_signal import SustainedLoadMonitor
from core.resource_guard import GpuStatus, read_gpu_status
from core.resource_ownership import (
    GLOBAL_RESOURCE_LEDGER,
    ResourceLedger,
    ResourceOwner,
)


def qronos_reserved_vram_mb(
    ledger: ResourceLedger = GLOBAL_RESOURCE_LEDGER,
) -> int:
    """How much VRAM Qronos has intentionally admitted."""
    return max(
        0,
        ledger.reserved_vram_mb(ResourceOwner.QRONOS),
    )


def qronos_gpu_work_active(
    ledger: ResourceLedger = GLOBAL_RESOURCE_LEDGER,
) -> bool:
    """
    Whether Qronos currently owns an active GPU reservation.

    A positive VRAM reservation is the current marker for a GPU workload.
    """
    return qronos_reserved_vram_mb(ledger) > 0


def read_gpu_for_external_pressure(
    ledger: ResourceLedger = GLOBAL_RESOURCE_LEDGER,
) -> GpuStatus | None:
    """
    Read GPU telemetry as external-pressure telemetry.

    When no Qronos GPU workload is active, return the raw reading.

    When Qronos owns GPU work, global GPU utilization cannot be attributed
    safely between Qronos and the user, so utilization is suppressed. VRAM is
    left raw here because SustainedLoadMonitor already subtracts the known
    Qronos reservation through its own_vram_mb seam.
    """
    gpu = read_gpu_status()

    if gpu is None:
        return None

    if not qronos_gpu_work_active(ledger):
        return gpu

    return GpuStatus(
        name=gpu.name,
        temperature_c=gpu.temperature_c,
        gpu_utilization_percent=None,
        vram_used_mb=gpu.vram_used_mb,
        vram_total_mb=gpu.vram_total_mb,
    )


def build_sustained_load_monitor(
    *,
    ledger: ResourceLedger = GLOBAL_RESOURCE_LEDGER,
) -> SustainedLoadMonitor:
    """
    Build the production sustained-load monitor with Qronos attribution wired.

    The monitor subtracts Qronos-owned VRAM and uses the attribution-aware GPU
    reader above, preventing Qronos from reading its own admitted GPU workload
    as fresh user pressure.
    """
    return SustainedLoadMonitor(
        read_gpu=lambda: read_gpu_for_external_pressure(ledger),
        own_vram_mb=lambda: qronos_reserved_vram_mb(ledger),
    )
