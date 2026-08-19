from __future__ import annotations

from dataclasses import dataclass

from core.model_manager import ModelManager, TaskClass
from core.ollama_controller import OllamaController
from core.resource_guard import read_gpu_status, read_system_status
from core.resource_policy import ResourceDecision
from core.task_plan import PlanStep, TaskPlan


@dataclass(frozen=True)
class StepResult:
    """Result of one executed task step."""

    order: int
    success: bool
    output: str
    error: str | None = None


class Orchestrator:
    """Execute Qronos plans while respecting resource limits."""

    def __init__(
        self,
        ollama: OllamaController | None = None,
        model_manager: ModelManager | None = None,
    ) -> None:
        self.ollama = ollama or OllamaController()
        self.model_manager = model_manager or ModelManager(
            ollama=self.ollama,
        )

    def execute_plan(
        self,
        plan: TaskPlan,
    ) -> list[StepResult]:
        """Execute plan steps in order."""

        results: list[StepResult] = []

        for step in plan.steps:
            result = self._execute_step(
                step=step,
                previous_results=results,
            )

            results.append(result)

            if not result.success:
                break

        return results

    def _execute_step(
        self,
        step: PlanStep,
        previous_results: list[StepResult],
    ) -> StepResult:
        task_class = self._get_task_class(step)

        if task_class is None:
            return StepResult(
                order=step.order,
                success=False,
                output="",
                error=(
                    f"Task type '{step.task_type.value}' "
                    "is not implemented yet."
                ),
            )

        try:
            decision = self._prepare_resources(task_class)

            if decision is not ResourceDecision.ALLOW:
                return StepResult(
                    order=step.order,
                    success=False,
                    output="",
                    error=(
                        f"Resource policy blocked task: "
                        f"{decision.value}"
                    ),
                )

            model_name = (
                "qwen3.5:9b"
                if task_class is TaskClass.FAST
                else "qwen3.6:27b"
            )

            prompt = self._build_prompt(
                description=step.description,
                previous_results=previous_results,
            )

            response = self.ollama.chat(
                model_name=model_name,
                prompt=prompt,
                think=False,
                num_predict=256,
                keep_alive="0",
            )

            return StepResult(
                order=step.order,
                success=True,
                output=response,
            )

        except Exception as exc:
            return StepResult(
                order=step.order,
                success=False,
                output="",
                error=str(exc),
            )

    def _prepare_resources(
        self,
        task_class: TaskClass,
    ) -> ResourceDecision:
        """
        Check resources before starting a task.

        When switching models, unload currently loaded models first.
        This prevents Qronos from keeping multiple large models in VRAM.
        """

        system = read_system_status()
        gpu = read_gpu_status()

        selection = self.model_manager.select_model(
            task_class=task_class,
            system=system,
            gpu=gpu,
        )

        if selection.decision is ResourceDecision.ALLOW:
            return ResourceDecision.ALLOW

        running_models = self.ollama.list_running_models()

        if not running_models:
            return selection.decision

        self.ollama.unload_all()

        system = read_system_status()
        gpu = read_gpu_status()

        selection = self.model_manager.select_model(
            task_class=task_class,
            system=system,
            gpu=gpu,
        )

        return selection.decision

    @staticmethod
    def _get_task_class(
        step: PlanStep,
    ) -> TaskClass | None:
        if step.task_type.value == "fast":
            return TaskClass.FAST

        if step.task_type.value == "heavy":
            return TaskClass.HEAVY

        return None

    @staticmethod
    def _build_prompt(
        description: str,
        previous_results: list[StepResult],
    ) -> str:
        """Build the prompt using previous successful results."""

        if not previous_results:
            return description

        previous_text = "\n\n".join(
            f"Step {result.order} result:\n{result.output}"
            for result in previous_results
            if result.success
        )

        return (
            f"{description}\n\n"
            "Use the previous step results below as context.\n\n"
            f"{previous_text}"
        )


if __name__ == "__main__":
    from core.task_router import TaskType

    plan = TaskPlan(
        goal="Test the resource-aware Qronos orchestrator."
    )

    plan.add_step(
        TaskType.FAST,
        "Reply with exactly: Resource-aware step one complete.",
    )

    plan.add_step(
        TaskType.FAST,
        "Reply with exactly: Resource-aware step two complete.",
    )

    orchestrator = Orchestrator()
    results = orchestrator.execute_plan(plan)

    for result in results:
        print(
            f"Step {result.order}: "
            f"success={result.success}"
        )

        if result.success:
            print(result.output)
        else:
            print(f"ERROR: {result.error}")