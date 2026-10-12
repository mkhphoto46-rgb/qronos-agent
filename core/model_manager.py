from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from core.activity_guard import (
    ActivityMode,
    ActivityGuard,
    ResourcePressure,
)
from core.brain_runtime import BrainRuntime
from core.hard_floor import DEFAULT_HARD_FLOOR, required_vram_mb
from core.model_registry import (
    ModelProfile,
    configured_model_names,
    get_model,
)
from core.ollama_controller import OllamaController
from core.resource_guard import (
    GpuStatus,
    SystemStatus,
)
from core.resource_policy import (
    ResourceDecision,
    evaluate_resources,
)


class TaskClass(Enum):
    FAST = "fast"
    HEAVY = "heavy"


@dataclass(frozen=True)
class ModelSelection:
    """Which configured brain should run, and whether it is admitted."""

    model: ModelProfile
    decision: ResourceDecision
    already_resident: bool = False

    #: What the projected-fit check was based on: "observed" (the daemon's own
    #: resident size for this model at this context), "installed" (its size
    #: in the local store), "configured" (only the profile estimate, for a
    #: manager built without a store reader), or "unavailable" (a store
    #: reader was supplied but gave no trustworthy size, so a new GPU load
    #: was deferred).
    capacity_evidence: str = "configured"

    #: Set only when the projected-fit check refused the load: what the model
    #: is expected to need (including headroom) and what is free now, in MiB.
    vram_needed_mb: int | None = None
    vram_free_mb: int | None = None


#: Bytes in one MiB, the unit the GPU telemetry reports.
_MIB = 1024 * 1024

#: No model this code could load is a petabyte. A larger "size" is not a
#: measurement; it is treated as malformed rather than as a huge bound.
_MAX_PLAUSIBLE_MODEL_BYTES = 1 << 50


def _valid_model_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and value == value.strip()
        and not any(ord(character) < 32 for character in value)
    )


def _valid_byte_count(value: Any) -> bool:
    """
    A finite, positive, whole number of bytes, given as an integer.

    Exactly ``int``: a bool, a float (including NaN or infinity), a numeric
    string or anything negative is not a byte count, however it might be
    coerced.
    """
    return (
        type(value) is int
        and 0 < value <= _MAX_PLAUSIBLE_MODEL_BYTES
    )


def parse_installed_listing(data: Any) -> dict[str, int] | None:
    """
    Validated installed sizes from a raw local store listing, or None.

    None means the listing as a whole is unusable. Within a usable listing a
    record that is not an object or has no valid name vouches for nothing
    and is skipped; a model whose size is malformed in any of its records is
    left out, so it reads as unknown rather than as a smaller number.
    """
    if not isinstance(data, dict):
        return None

    entries = data.get("models")

    if not isinstance(entries, list):
        return None

    sizes: dict[str, int] = {}
    malformed: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        name = entry.get("name")

        if not _valid_model_name(name):
            continue

        size = entry.get("size")

        if not _valid_byte_count(size):
            malformed.add(name)
            continue

        sizes[name] = max(size, sizes.get(name, 0))

    for name in malformed:
        sizes.pop(name, None)

    return sizes


class InstalledModelSizes:
    """
    Installed model sizes from the local model store, read briefly cached.

    ``read_listing`` returns the store's raw /api/tags document over the
    bounded loopback client; it is validated here, before any value is used.
    Nothing is downloaded or loaded.

    A listing that cannot be read or parsed keeps the sizes already known:
    a refresh failure never weakens a bound that was established. It is
    retried only after a short pause, so a stalled store cannot add a full
    read timeout to every admission check. A model the store does not list,
    or lists with a malformed size, reads as unknown - never as zero.
    """

    def __init__(
        self,
        read_listing: Callable[[], Any],
        ttl_seconds: float = 30.0,
        retry_after_failure_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read_listing = read_listing
        self._ttl = ttl_seconds
        self._retry_after_failure = retry_after_failure_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._sizes: dict[str, int] = {}
        self._read_at: float | None = None
        self._failed_at: float | None = None

    def __call__(self, model_name: str) -> int | None:
        with self._lock:
            now = self._clock()
            due = self._read_at is None or now - self._read_at >= self._ttl
            backing_off = (
                self._failed_at is not None
                and now - self._failed_at < self._retry_after_failure
            )

            if due and not backing_off:
                try:
                    parsed = parse_installed_listing(self._read_listing())
                except Exception:
                    parsed = None

                if parsed is None:
                    self._failed_at = now
                else:
                    self._sizes = parsed
                    self._read_at = now
                    self._failed_at = None

            size = self._sizes.get(model_name)

            return size if _valid_byte_count(size) else None


class ModelManager:
    """
    Select and swap modular Qronos brains without allowing two large brain
    models to accumulate in VRAM.

    Important distinction:
    - configured Qronos models may be unloaded during a role swap;
    - unrelated Ollama models are never touched;
    - a resident target model is evaluated after discounting its own estimated
      VRAM footprint, preventing Qronos from self-throttling on its own warm
      Heavy Brain.
    """

    def __init__(
        self,
        runtime: BrainRuntime | None = None,
        installed_size_bytes: Callable[[str], int | None] | None = None,
    ) -> None:
        self.runtime = runtime if runtime is not None else OllamaController()

        # Temporary compatibility alias for existing code/tests.
        self.ollama = self.runtime

        # Capacity evidence beyond the configured estimate. The estimate is
        # part of the immutable model specification and is never lowered;
        # evidence can only show that a model needs more than it says.
        self._installed_size_bytes = installed_size_bytes
        self._observed_lock = threading.Lock()
        self._observed_bytes: dict[tuple[str, int], int] = {}

    def ensure_runtime_ready(
        self,
        *,
        auto_start: bool = True,
        timeout_seconds: float = 12.0,
    ) -> bool:
        """Ensure the configured local brain runtime is available."""
        ensure_ready = getattr(self.runtime, "ensure_ready", None)
        if callable(ensure_ready):
            return bool(
                ensure_ready(
                    auto_start=auto_start,
                    timeout_seconds=timeout_seconds,
                )
            )
        return bool(self.runtime.health_check())

    def _running_names(self) -> set[str]:
        try:
            running = list(self.runtime.list_running_models())
        except Exception:
            return set()

        self._remember_footprints(running)

        names = {
            getattr(item, "name", "")
            for item in running
        }

        return {
            name
            for name in names
            if isinstance(name, str) and name
        }

    def _remember_footprints(self, running) -> None:
        """
        Remember what the daemon reports a configured brain occupies.

        The daemon's resident size for a model at a given context is the most
        direct evidence of what loading it again at that context will take.
        Only configured Qronos models are recorded, and only from readings
        that carry both a size and a context.
        """
        names = configured_model_names()

        for item in running:
            name = getattr(item, "name", "")

            if name not in names:
                continue

            raw_size = getattr(item, "size", None)
            context = getattr(item, "context", None)

            # The runtime reports the size as decimal digits. Anything else -
            # a sign, a fraction, an exponent, a bool - is not a measurement.
            if isinstance(raw_size, str) and raw_size.isascii() and raw_size.isdigit():
                size = int(raw_size)
            elif type(raw_size) is int:
                size = raw_size
            else:
                continue

            if not _valid_byte_count(size):
                continue

            if type(context) is not int or context <= 0:
                continue

            with self._observed_lock:
                self._observed_bytes[(name, context)] = size

    def footprint_mb(self, model: ModelProfile) -> tuple[int | None, str]:
        """
        The VRAM a fresh load of this profile is expected to need, and why.

        Never below the configured estimate. Raised by the daemon's observed
        resident size at the profile's own context when one is known, or
        otherwise by the model's installed size: weights the store holds for
        it, which for the specified models at their specified contexts
        bounded the measured resident size from above.

        With a store reader supplied and neither available, the need is
        unknown (None, "unavailable"): the configured estimate is already
        known to understate at least one specified model, so it is not
        evidence of fit. Only a manager built without a store reader - an
        injected or legacy backend - falls back to the estimate.
        """
        estimate_mb = required_vram_mb(model.estimated_vram_gb)

        with self._observed_lock:
            observed = self._observed_bytes.get(
                (model.name, model.context_tokens)
            )

        if observed:
            return max(estimate_mb, math.ceil(observed / _MIB)), "observed"

        installed = None

        if self._installed_size_bytes is not None:
            try:
                installed = self._installed_size_bytes(model.name)
            except Exception:
                installed = None

        if _valid_byte_count(installed):
            return max(estimate_mb, math.ceil(installed / _MIB)), "installed"

        if self._installed_size_bytes is not None:
            return None, "unavailable"

        return estimate_mb, "configured"

    def is_model_resident(self, model_name: str) -> bool:
        return model_name in self._running_names()

    def prepare_model_slot(self, task_class: TaskClass) -> bool:
        """
        Make room for the requested brain by unloading only other models that
        belong to Qronos's configured profile set.

        Returns True when at least one model was unloaded.
        """

        target = get_model(task_class.value)
        qronos_names = configured_model_names()
        changed = False

        for running_name in self._running_names():
            if running_name == target.name:
                continue

            if running_name not in qronos_names:
                continue

            self.runtime.stop_model(running_name)
            changed = True

        return changed

    def unload_qronos_models(self, except_name: str | None = None) -> bool:
        qronos_names = configured_model_names()
        changed = False

        for running_name in self._running_names():
            if except_name and running_name == except_name:
                continue

            if running_name not in qronos_names:
                continue

            self.runtime.stop_model(running_name)
            changed = True

        return changed

    @staticmethod
    def _gpu_for_capacity_admission(
        gpu: GpuStatus | None,
        model: ModelProfile,
        already_resident: bool,
    ) -> GpuStatus | None:
        """
        Build capacity telemetry without counting an already-resident target
        model against itself.

        GPU utilization is intentionally omitted here. ActivityGuard handles
        contention separately; this admission snapshot is about capacity,
        temperature, RAM and VRAM.
        """

        if gpu is None:
            return None

        used_mb = gpu.vram_used_mb

        if already_resident and used_mb is not None:
            own_estimate_mb = int(round(model.estimated_vram_gb * 1024.0))
            used_mb = max(0, used_mb - own_estimate_mb)

        return GpuStatus(
            name=gpu.name,
            temperature_c=gpu.temperature_c,
            gpu_utilization_percent=None,
            vram_used_mb=used_mb,
            vram_total_mb=gpu.vram_total_mb,
        )

    def _new_model_has_vram_capacity(
        self,
        gpu: GpuStatus | None,
        model: ModelProfile,
        already_resident: bool,
    ) -> tuple[bool, str, int | None, int | None]:
        """
        Check projected capacity before admitting a model that is not resident.

        Percentage thresholds answer "is the card busy/full now?". They do
        not answer "will this model fit after it loads?". On an 8 GB card a
        57% current reading can still leave less free VRAM than Fast Brain
        requires. Reuse of an already-resident target needs no second copy and
        therefore bypasses this load-capacity test.

        The projected size is the profile's footprint (see ``footprint_mb``).
        When it would fit this card once other GPU users release memory, but
        does not fit now, the load is deferred: starting it would make the
        model runtime take memory other applications are using, which is
        avoidable by waiting. When it could never fit this card entirely, no
        amount of waiting helps; the model runtime has to keep part of it in
        system memory, so the check falls back to the configured estimate as
        before rather than making the model unusable on this hardware.
        """
        if already_resident or gpu is None:
            return True, "configured", None, None

        if (
            gpu.vram_used_mb is None
            or gpu.vram_total_mb in (None, 0)
        ):
            return True, "configured", None, None

        total_mb = int(gpu.vram_total_mb)
        free_vram_mb = max(
            0,
            total_mb
            - int(gpu.vram_used_mb),
        )
        headroom_mb = int(DEFAULT_HARD_FLOOR.vram_headroom_mb)
        needed_mb, evidence = self.footprint_mb(model)

        if needed_mb is None:
            # A new GPU load of unknown size is not started on a card other
            # applications are using. Waiting costs a retry; loading on the
            # disproven estimate could displace their memory.
            return False, evidence, None, free_vram_mb

        if free_vram_mb >= needed_mb + headroom_mb:
            return True, evidence, None, None

        if needed_mb + headroom_mb > total_mb:
            estimate_mb = required_vram_mb(model.estimated_vram_gb)
            fits = free_vram_mb >= estimate_mb + headroom_mb
            return (
                fits,
                f"{evidence}-exceeds-card",
                None if fits else estimate_mb + headroom_mb,
                None if fits else free_vram_mb,
            )

        return False, evidence, needed_mb + headroom_mb, free_vram_mb

    @staticmethod
    def _resident_pressure_is_only_self_load(
        resource_pressure: ResourcePressure,
        base_decision: ResourceDecision,
        gpu: GpuStatus | None,
    ) -> bool:
        """
        Permit reuse of an already-resident model only when the HIGH pressure
        can plausibly be explained by the model's own VRAM residency.

        CRITICAL pressure is never ignored. A hot/busy GPU is not treated as
        self-only pressure because the active user workload keeps priority.
        """

        if resource_pressure is not ResourcePressure.HIGH:
            return False

        if base_decision is not ResourceDecision.ALLOW:
            return False

        if (
            gpu is not None
            and gpu.gpu_utilization_percent is not None
            and gpu.gpu_utilization_percent
            >= ActivityGuard.HIGH_GPU_UTILIZATION_PERCENT
        ):
            return False

        return True

    def select_model(
        self,
        task_class: TaskClass,
        system: SystemStatus,
        gpu: GpuStatus | None,
        activity_mode: ActivityMode = ActivityMode.NORMAL,
        resource_pressure: ResourcePressure = ResourcePressure.NORMAL,
    ) -> ModelSelection:
        model = get_model(task_class.value)
        already_resident = self.is_model_resident(model.name)

        capacity_gpu = self._gpu_for_capacity_admission(
            gpu=gpu,
            model=model,
            already_resident=already_resident,
        )

        base_decision = evaluate_resources(
            system=system,
            gpu=capacity_gpu,
        )

        (
            fits,
            capacity_evidence,
            vram_needed_mb,
            vram_free_mb,
        ) = self._new_model_has_vram_capacity(
            gpu=gpu,
            model=model,
            already_resident=already_resident,
        )

        if base_decision is not ResourceDecision.BLOCK and not fits:
            base_decision = ResourceDecision.BLOCK

        decision = base_decision

        if self._activity_blocks(
            task_class=task_class,
            activity_mode=activity_mode,
        ):
            decision = ResourceDecision.BLOCK

        if resource_pressure is not ResourcePressure.NORMAL:
            may_reuse_own_resident = (
                already_resident
                and self._resident_pressure_is_only_self_load(
                    resource_pressure=resource_pressure,
                    base_decision=base_decision,
                    gpu=gpu,
                )
            )

            if not may_reuse_own_resident:
                decision = ResourceDecision.BLOCK

        return ModelSelection(
            model=model,
            decision=decision,
            already_resident=already_resident,
            capacity_evidence=capacity_evidence,
            vram_needed_mb=vram_needed_mb,
            vram_free_mb=vram_free_mb,
        )

    def can_start(
        self,
        task_class: TaskClass,
        system: SystemStatus,
        gpu: GpuStatus | None,
        activity_mode: ActivityMode = ActivityMode.NORMAL,
        resource_pressure: ResourcePressure = ResourcePressure.NORMAL,
    ) -> bool:
        return (
            self.select_model(
                task_class=task_class,
                system=system,
                gpu=gpu,
                activity_mode=activity_mode,
                resource_pressure=resource_pressure,
            ).decision
            is ResourceDecision.ALLOW
        )

    def unload_all(self) -> None:
        self.runtime.unload_all()

    @staticmethod
    def _activity_blocks(
        task_class: TaskClass,
        activity_mode: ActivityMode,
    ) -> bool:
        if task_class is not TaskClass.HEAVY:
            return False

        # ASSIST modes may use Heavy on demand when resource pressure is
        # NORMAL. Orchestrator forces keep_alive=0 in user-active modes so
        # the model yields VRAM immediately after answering. PERFORMANCE
        # modes remain hard blocks because the user's active workload wins.
        return activity_mode in {
            ActivityMode.GAMING_PERFORMANCE,
            ActivityMode.CREATOR_PERFORMANCE,
        }


if __name__ == "__main__":
    print("Qronos Model Manager: modular brain slots ready")