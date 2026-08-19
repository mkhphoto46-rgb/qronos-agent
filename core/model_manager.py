from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.model_registry import ModelProfile, get_model
from core.ollama_controller import OllamaController
from core.resource_guard import GpuStatus, SystemStatus
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


class ModelManager:
    """Choose and prepare the appropriate local model."""

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
    ) -> ModelSelection:
        """Select a model and verify whether the current resources allow it."""

        model_role = task_class.value
        model = get_model(model_role)

        decision = evaluate_resources(
            system=system,
            gpu=gpu,
        )

        return ModelSelection(
            model=model,
            decision=decision,
        )

    def unload_all(self) -> None:
        """Unload all currently running local models."""
        self.ollama.unload_all()

    def can_start(
        self,
        task_class: TaskClass,
        system: SystemStatus,
        gpu: GpuStatus | None,
    ) -> bool:
        """Return True when the selected workload is allowed to start."""

        selection = self.select_model(
            task_class=task_class,
            system=system,
            gpu=gpu,
        )

        return selection.decision is ResourceDecision.ALLOW


if __name__ == "__main__":
    print("Qronos Model Manager: ready")