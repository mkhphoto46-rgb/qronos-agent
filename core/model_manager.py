from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.activity_guard import (
    ActivityMode,
    ActivityGuard,
    ResourcePressure,
)
from core.brain_runtime import BrainRuntime
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

        return activity_mode in {
            ActivityMode.GAMING_ASSIST,
            ActivityMode.GAMING_PERFORMANCE,
            ActivityMode.CREATOR_ASSIST,
            ActivityMode.CREATOR_PERFORMANCE,
        }


if __name__ == "__main__":
    print("Qronos Model Manager: modular brain slots ready")