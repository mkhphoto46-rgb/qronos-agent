from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.activity_guard import ActivityMode
from core.model_registry import ModelProfile, get_model
from core.ollama_controller import OllamaController
from core.resource_guard import GpuStatus, SystemStatus
from core.resource_policy import ResourceDecision, evaluate_resources


class TaskClass(Enum):
    FAST = "fast"
    HEAVY = "heavy"


@dataclass(frozen=True)
class ModelSelection:
    model: ModelProfile
    decision: ResourceDecision
    keep_loaded: bool


class ModelManager:
    """Choose models while respecting resources and activity mode."""

    def __init__(
        self,
        ollama: OllamaController | None = None,
    ) -> None:
        self.ollama = ollama or OllamaController()

    def select_model(
        self,
        task_class: TaskClass,
        system: SystemStatus,
        gpu: GpuStatus | None,
        activity_mode: ActivityMode = ActivityMode.NORMAL,
    ) -> ModelSelection:
        model = get_model(task_class.value)

        decision = evaluate_resources(
            system=system,
            gpu=gpu,
        )

        if (
            task_class is TaskClass.HEAVY
            and activity_mode
            in {
                ActivityMode.GAMING_ASSIST,
                ActivityMode.GAMING_PERFORMANCE,
                ActivityMode.CREATOR_ASSIST,
                ActivityMode.CREATOR_PERFORMANCE,
            }
        ):
            decision = ResourceDecision.BLOCK

        keep_loaded = self._should_keep_loaded(
            task_class=task_class,
            activity_mode=activity_mode,
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
    ) -> bool:
        selection = self.select_model(
            task_class=task_class,
            system=system,
            gpu=gpu,
            activity_mode=activity_mode,
        )

        return selection.decision is ResourceDecision.ALLOW

    def unload_all(self) -> None:
        self.ollama.unload_all()

    @staticmethod
    def _should_keep_loaded(
        task_class: TaskClass,
        activity_mode: ActivityMode,
    ) -> bool:
        if activity_mode is ActivityMode.NORMAL:
            return task_class is TaskClass.FAST

        return False


if __name__ == "__main__":
    print("Qronos Model Manager: ready")