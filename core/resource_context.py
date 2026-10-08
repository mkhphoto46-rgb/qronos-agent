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

Power and disk (``core/environment_signals.py``) are read fresh by both paths
built here: every sample of the production sustained-load monitor, and every
snapshot prepared for the foreground ActivityGuard. They are never taken from
a cache, so a reading from before a model load never authorizes one.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from typing import Callable, Optional

from core.environment_signals import EnvironmentReader, EnvironmentReading
from core.load_signal import SustainedLoadMonitor
from core.resource_guard import GpuStatus, read_gpu_status
from core.telemetry_cache import Snapshot
from core.resource_ownership import (
    GLOBAL_RESOURCE_LEDGER,
    ResourceLedger,
    ResourceOwner,
)


#: The real local power-and-disk reader. A module attribute rather than a
#: constant so tests replace it instead of reading the machine they run on.
production_environment_reader: Callable[[], EnvironmentReading] = EnvironmentReader()

_PRODUCTION = object()


def _environment(reader: object) -> Optional[EnvironmentReading]:
    """A fresh reading from ``reader``; the production one by default."""
    if reader is None:
        return None

    if reader is _PRODUCTION:
        reader = production_environment_reader

    try:
        return reader()  # type: ignore[operator]
    except Exception:
        # The readers never raise. Anything reaching here leaves the snapshot
        # without an environment, which adds no pressure and claims nothing.
        return None


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


class VramAttribution:
    """
    Qronos's own share of the graphics card, measured rather than assumed.

    The reservation a Qronos workload takes is its *declared* footprint. For
    a model that fits the card that is close to what it uses. For a model
    that can never fit — partial offload, where the model runtime fills
    whatever is free and keeps the rest in system memory — the declaration
    can exceed the whole card. Subtracting it then reads every external
    megabyte as Qronos's own, and another application's growth while Qronos
    holds the card becomes invisible: exactly the pressure the sentinel
    exists to see.

    So the share is measured. The external usage is remembered while Qronos
    holds no reservation (the baseline). Once a reservation appears, growth
    over the baseline during ``settle_seconds`` is the model loading and is
    counted as Qronos's own, never more than the reservation. After that the
    share is fixed, and further growth is somebody else's: reported as
    ``external_growth_mb`` and kept out of Qronos's subtraction.

    Without a baseline (the first reading already had a reservation) Qronos
    cannot tell the two apart; it falls back to the reservation, clamped to
    what the card actually shows used, and reports no growth rather than a
    guess.
    """

    def __init__(
        self,
        ledger: ResourceLedger = GLOBAL_RESOURCE_LEDGER,
        read_gpu: Callable[[], GpuStatus | None] | None = None,
        settle_seconds: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if settle_seconds < 0:
            raise ValueError("settle_seconds must not be negative.")

        self._ledger = ledger
        self._read_raw = read_gpu
        self._settle_seconds = settle_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._baseline_external_mb: int | None = None
        self._reserved_since: float | None = None
        self._own_mb = 0
        self._growth_mb: int | None = None

    def observe(self, gpu: GpuStatus | None) -> None:
        """Fold one raw reading in. Call once per reading, before asking."""
        reserved = qronos_reserved_vram_mb(self._ledger)
        used = None if gpu is None else gpu.vram_used_mb

        with self._lock:
            now = self._clock()

            if reserved <= 0:
                # Qronos holds nothing: everything on the card is external.
                self._baseline_external_mb = used
                self._reserved_since = None
                self._own_mb = 0
                self._growth_mb = None
                return

            if self._reserved_since is None:
                self._reserved_since = now
                self._own_mb = 0

            if used is None:
                self._growth_mb = None
                return

            baseline = self._baseline_external_mb

            if baseline is None:
                self._own_mb = min(reserved, used)
                self._growth_mb = None
                return

            over_baseline = max(0, used - baseline)

            if now - self._reserved_since < self._settle_seconds:
                self._own_mb = max(self._own_mb, min(reserved, over_baseline))

            # Never more than the card shows in use beyond the baseline.
            self._own_mb = min(self._own_mb, over_baseline, reserved)
            self._growth_mb = max(0, over_baseline - self._own_mb)

    def read_gpu(self) -> GpuStatus | None:
        """
        A raw reading, folded in, returned as external-pressure telemetry.

        Global GPU utilisation is suppressed while Qronos holds a reservation,
        because it contains Qronos's own work. VRAM stays raw: the monitor
        subtracts ``own_vram_mb`` itself.
        """
        # Resolved at call time so the real reader is the module's own.
        gpu = (self._read_raw or read_gpu_status)()
        self.observe(gpu)

        if gpu is None or not qronos_gpu_work_active(self._ledger):
            return gpu

        return GpuStatus(
            name=gpu.name,
            temperature_c=gpu.temperature_c,
            gpu_utilization_percent=None,
            vram_used_mb=gpu.vram_used_mb,
            vram_total_mb=gpu.vram_total_mb,
        )

    def own_vram_mb(self) -> int:
        """Qronos's measured share as of the last reading."""
        with self._lock:
            return self._own_mb

    def external_growth_mb(self) -> int | None:
        """External growth since Qronos took the card; None when unknowable."""
        with self._lock:
            return self._growth_mb


_ATTRIBUTIONS: dict[int, VramAttribution] = {}
_ATTRIBUTIONS_LOCK = threading.Lock()


def attribution_for(ledger: ResourceLedger) -> VramAttribution:
    """One shared attribution per ledger, so every view agrees on the share."""
    with _ATTRIBUTIONS_LOCK:
        attribution = _ATTRIBUTIONS.get(id(ledger))

        if attribution is None or attribution._ledger is not ledger:
            attribution = VramAttribution(ledger)
            _ATTRIBUTIONS[id(ledger)] = attribution

        return attribution


def snapshot_for_external_pressure(
    snapshot: Snapshot,
    ledger: ResourceLedger = GLOBAL_RESOURCE_LEDGER,
    environment: object = _PRODUCTION,
) -> Snapshot:
    """
    Return an ActivityGuard-safe view of one raw telemetry snapshot.

    Capacity/admission decisions must still use the raw snapshot. This view is
    only for classifying external/user pressure. While Qronos owns admitted
    GPU work, global GPU utilization cannot be attributed safely, so it is
    suppressed; known Qronos-reserved VRAM is subtracted from used VRAM.

    CPU, RAM and GPU temperature stay untouched. Those signals can still
    represent real machine pressure and must continue protecting the user.

    A fresh power-and-disk reading is attached for ActivityGuard, from the
    production reader unless ``environment`` names another (or None).
    """
    snapshot = dataclasses.replace(snapshot, environment=_environment(environment))
    gpu = snapshot.gpu

    # The measured share, not the declared reservation: a partially
    # offloaded model's reservation can exceed the whole card and would hide
    # every external megabyte. See VramAttribution.
    attribution = attribution_for(ledger)
    attribution.observe(gpu)
    own_vram_mb = (
        attribution.own_vram_mb()
        if qronos_reserved_vram_mb(ledger) > 0
        else 0
    )

    if (
        gpu is None
        or own_vram_mb <= 0
    ):
        return snapshot

    adjusted_vram_used_mb = (
        None
        if gpu.vram_used_mb is None
        else max(
            0,
            int(gpu.vram_used_mb)
            - own_vram_mb,
        )
    )

    adjusted_gpu = GpuStatus(
        name=gpu.name,
        temperature_c=gpu.temperature_c,
        gpu_utilization_percent=None,
        vram_used_mb=adjusted_vram_used_mb,
        vram_total_mb=gpu.vram_total_mb,
    )

    return Snapshot(
        system=snapshot.system,
        gpu=adjusted_gpu,
        taken_at=snapshot.taken_at,
        stale=snapshot.stale,
        environment=snapshot.environment,
    )


def build_sustained_load_monitor(
    *,
    ledger: ResourceLedger = GLOBAL_RESOURCE_LEDGER,
    environment: object = _PRODUCTION,
) -> SustainedLoadMonitor:
    """
    Build the production sustained-load monitor with Qronos attribution wired.

    The monitor subtracts Qronos's measured VRAM share and uses the
    attribution-aware GPU reader above, preventing Qronos from reading its own
    admitted GPU workload as fresh user pressure — and, because the share is
    measured rather than declared, from hiding other applications' growth
    behind a partially offloaded model's oversized reservation.
    """
    def read_environment() -> EnvironmentReading:
        reading = _environment(environment)

        if reading is None:
            raise RuntimeError("no environment reading")  # recorded as unknown

        return reading

    attribution = attribution_for(ledger)

    return SustainedLoadMonitor(
        read_gpu=attribution.read_gpu,
        own_vram_mb=attribution.own_vram_mb,
        external_vram_growth_mb=attribution.external_growth_mb,
        read_environment=None if environment is None else read_environment,
    )
