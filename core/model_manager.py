from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.activity_guard import (
    ActivityMode,
    ResourcePressure,
)
from core.brain_runtime import BrainRuntime
from core.model_registry import (
    ModelProfile,
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
    model: ModelProfile
    decision: ResourceDecision
    keep_loaded: bool


class ModelManager:
    """Choose models while respecting activity and resource pressure."""

    def __init__(
        self,
        runtime: BrainRuntime | None = None,
    ) -> None:
        self.runtime = (
            runtime
            if runtime is not None
            else OllamaController()
        )

        # Temporary compatibility alias.
        # Remove after the MVP migration away from Ollama-specific naming.
        self.ollama = self.runtime

    def select_model(
        self,
        task_class: TaskClass,
        system: SystemStatus,
        gpu: GpuStatus | None,
        activity_mode: ActivityMode = ActivityMode.NORMAL,
        resource_pressure: ResourcePressure = ResourcePressure.NORMAL,
    ) -> ModelSelection:
        model = get_model(
            task_class.value
        )

        decision = evaluate_resources(
            system=system,
            gpu=gpu,
        )

        if self._activity_blocks(
            task_class=task_class,
            activity_mode=activity_mode,
        ):
            decision = ResourceDecision.BLOCK

        # Never load either model while the user's system is already under
        # measured pressure. Qronos waits for safe headroom instead.
        if (
            resource_pressure
            is not ResourcePressure.NORMAL
        ):
            decision = ResourceDecision.BLOCK

        keep_loaded = self._should_keep_loaded(
            task_class=task_class,
            activity_mode=activity_mode,
            resource_pressure=resource_pressure,
        )

        return ModelSelection(
            model=model,
            decision=decision,
            keep_loaded=keep_loaded,
        )

    def can_start(
        self,
        task_class: TaskClass,
        system: SystemStatus,
        gpu: GpuStatus | None,
        activity_mode: ActivityMode = ActivityMode.NORMAL,
        resource_pressure: ResourcePressure = ResourcePressure.NORMAL,
    ) -> bool:
        selection = self.select_model(
            task_class=task_class,
            system=system,
            gpu=gpu,
            activity_mode=activity_mode,
            resource_pressure=resource_pressure,
        )

        return (
            selection.decision
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

    @staticmethod
    def _should_keep_loaded(
        task_class: TaskClass,
        activity_mode: ActivityMode,
        resource_pressure: ResourcePressure,
    ) -> bool:
        if task_class is not TaskClass.FAST:
            return False

        if activity_mode is not ActivityMode.NORMAL:
            return False

        if (
            resource_pressure
            is not ResourcePressure.NORMAL
        ):
            return False

        return True


if __name__ == "__main__":
    print("Qronos Model Manager: ready")