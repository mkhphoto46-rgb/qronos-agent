from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

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
    ) -> None:
        self.runtime = runtime if runtime is not None else OllamaController()

        # Temporary compatibility alias for existing code/tests.
        self.ollama = self.runtime

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
            return {
                item.name
                for item in self.runtime.list_running_models()
                if item.name
            }
        except Exception:
            return set()

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

    @staticmethod
    def _new_model_has_vram_capacity(
        gpu: GpuStatus | None,
        model: ModelProfile,
        already_resident: bool,
    ) -> bool:
        """
        Check projected capacity before admitting a model that is not resident.

        Percentage thresholds answer "is the card busy/full now?". They do
        not answer "will this model fit after it loads?". On an 8 GB card a
        57% current reading can still leave less free VRAM than Fast Brain
        requires. Reuse of an already-resident target needs no second copy and
        therefore bypasses this load-capacity test.
        """
        if already_resident or gpu is None:
            return True

        if (
            gpu.vram_used_mb is None
            or gpu.vram_total_mb in (None, 0)
        ):
            return True

        free_vram_mb = max(
            0,
            int(gpu.vram_total_mb)
            - int(gpu.vram_used_mb),
        )
        required_mb = (
            required_vram_mb(
                model.estimated_vram_gb
            )
            + int(
                DEFAULT_HARD_FLOOR.vram_headroom_mb
            )
        )

        return free_vram_mb >= required_mb

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

        if (
            base_decision is not ResourceDecision.BLOCK
            and not self._new_model_has_vram_capacity(
                gpu=gpu,
                model=model,
                already_resident=already_resident,
            )
        ):
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