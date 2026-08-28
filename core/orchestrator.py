from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from core.activity_guard import (
    ActivityGuard,
    ActivityMode,
    ResourcePressure,
)
from core.brain_runtime import (
    BrainMessage,
    BrainMessageRole,
    BrainRuntime,
)
from core.model_manager import (
    ModelManager,
    TaskClass,
)
from core.ollama_controller import OllamaController
from core.resource_guard import (
    read_gpu_status,
    read_system_status,
    read_system_status_since_last_call,
)
from core.resource_policy import ResourceDecision
from core.telemetry_cache import TelemetryCache
from core.task_plan import PlanStep, TaskPlan
from core.workers import Unavailable, WorkerRegistry


QRONOS_SYSTEM_PROMPT = """
You are Qronos, the user's personal AI assistant.

Identity rules:
- Your name and product identity are Qronos.
- Always present yourself as Qronos.
- If asked what AI you are, what model you are, who your underlying
  model provider is, what LLM powers you, or similar questions, identify
  yourself only as Qronos, the user's personal AI assistant.
- Do not claim to be Qwen or any other underlying model.
- Do not reveal, speculate about, or volunteer internal model names,
  provider names, runtime implementation details, model routing details,
  or hidden infrastructure.
- Fast Brain and Heavy Brain are internal implementation concepts and
  must not alter your identity. You are Qronos in every mode.

Behavior rules:
- Answer in the language used by the user unless they request another
  language.
- Be accurate and helpful.
- Do not invent facts when information is uncertain.
- Preserve conversational context from the supplied message history.
""".strip()


@dataclass(frozen=True)
class StepResult:
    """Result of one executed task step."""

    order: int
    success: bool
    output: str
    error: str | None = None

    # Set when the step could not run because no worker answers for its
    # task type. Carries a reason code, so a caller can tell a missing
    # capability from a failed one without reading the error text.
    unavailable: Unavailable | None = None


class Orchestrator:
    """
    Execute Qronos plans with resource and activity awareness.

    The Orchestrator also owns the product-level Qronos identity message.
    This keeps identity consistent regardless of which BrainRuntime or
    model is selected.
    """

    def __init__(
        self,
        runtime: BrainRuntime | None = None,
        model_manager: ModelManager | None = None,
        activity_guard: ActivityGuard | None = None,
        workers: WorkerRegistry | None = None,
        telemetry: TelemetryCache | None = None,
    ) -> None:
        self.runtime = (
            runtime
            if runtime is not None
            else OllamaController()
        )

        # Temporary compatibility alias for the existing tests and
        # development code. Higher-level Qronos code should use runtime.
        self.ollama = self.runtime

        self.model_manager = (
            model_manager
            if model_manager is not None
            else ModelManager(
                runtime=self.runtime,
            )
        )

        self.telemetry = telemetry or TelemetryCache(
            read_system=lambda: read_system_status_since_last_call(),
            read_gpu=lambda: read_gpu_status(),
            first_read_system=lambda: read_system_status(),
        )

        self.activity_guard = (
            activity_guard
            if activity_guard is not None
            else ActivityGuard(snapshot_reader=self.telemetry.current)
        )

        # Empty unless something registers a worker, so an orchestrator
        # built the old way behaves exactly as it did before.
        self.workers = (
            workers
            if workers is not None
            else WorkerRegistry()
        )

    def execute_plan(
        self,
        plan: TaskPlan,
        conversation_messages: (
            Sequence[BrainMessage] | None
        ) = None,
    ) -> list[StepResult]:
        """
        Execute plan steps in order.

        conversation_messages contains conversation turns that happened
        before the current plan request.

        The current PlanStep description is appended as the newest user
        message by the Orchestrator, preventing the current turn from being
        duplicated in the model context.
        """

        results: list[StepResult] = []

        for step in plan.steps:
            activity_state = (
                self.activity_guard.detect()
            )

            result = self._execute_step(
                step=step,
                previous_results=results,
                activity_mode=(
                    activity_state.mode
                ),
                resource_pressure=(
                    activity_state.resource_pressure
                ),
                conversation_messages=(
                    conversation_messages
                ),
            )

            results.append(
                result
            )

            if not result.success:
                break

        return results

    def _execute_step(
        self,
        step: PlanStep,
        previous_results: list[StepResult],
        activity_mode: ActivityMode,
        resource_pressure: ResourcePressure,
        conversation_messages: (
            Sequence[BrainMessage] | None
        ),
    ) -> StepResult:
        task_class = self._get_task_class(
            step
        )

        if task_class is None:
            return self._execute_with_worker(
                step
            )

        try:
            selection = (
                self._prepare_resources(
                    task_class=task_class,
                    activity_mode=activity_mode,
                    resource_pressure=(
                        resource_pressure
                    ),
                )
            )

            if (
                selection.decision
                is not ResourceDecision.ALLOW
            ):
                return StepResult(
                    order=step.order,
                    success=False,
                    output="",
                    error=self._resource_error(
                        selection.decision,
                        resource_pressure,
                    ),
                )

            # The earlier resource snapshot may already be stale after
            # cleanup or plan preparation. Re-read immediately before
            # allowing the runtime to load or execute a brain.
            live_state = (
                self.activity_guard.detect()
            )

            selection = (
                self._prepare_resources(
                    task_class=task_class,
                    activity_mode=(
                        live_state.mode
                    ),
                    resource_pressure=(
                        live_state.resource_pressure
                    ),
                )
            )

            if (
                selection.decision
                is not ResourceDecision.ALLOW
            ):
                return StepResult(
                    order=step.order,
                    success=False,
                    output="",
                    error=self._resource_error(
                        selection.decision,
                        live_state.resource_pressure,
                    ),
                )

            messages = (
                self._build_brain_messages(
                    description=(
                        step.description
                    ),
                    previous_results=(
                        previous_results
                    ),
                    conversation_messages=(
                        conversation_messages
                    ),
                )
            )

            response = self.runtime.chat(
                model_name=(
                    selection.model.name
                ),
                messages=messages,
                think=(
                    task_class
                    is TaskClass.HEAVY
                ),
                num_predict=(
                    512
                    if task_class
                    is TaskClass.HEAVY
                    else 256
                ),
                keep_alive=(
                    "10m"
                    if selection.keep_loaded
                    else "0"
                ),
            )

            if not selection.keep_loaded:
                self.runtime.stop_model(
                    selection.model.name
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

    def answer_web_prompt(self, prompt: str) -> str:
        """Run Web Research's evidence prompt through the guarded Fast Brain."""
        activity = self.activity_guard.detect()
        selection = self._prepare_resources(
            TaskClass.FAST,
            activity.mode,
            activity.resource_pressure,
        )

        if selection.decision is not ResourceDecision.ALLOW:
            raise RuntimeError(
                self._resource_error(
                    selection.decision,
                    activity.resource_pressure,
                )
            )

        return self.runtime.chat(
            model_name=selection.model.name,
            prompt=prompt,
            think=False,
            num_predict=768,
            # Web answers are occasional and can fill VRAM. Unload after the
            # request so the next voice turn is not blocked by our own model.
            keep_alive="0",
        )

    def _prepare_resources(
        self,
        task_class: TaskClass,
        activity_mode: ActivityMode,
        resource_pressure: ResourcePressure,
    ):
        """
        Prepare resources and return the selected model.
        """

        snapshot = self.telemetry.current()
        system = snapshot.system
        gpu = snapshot.gpu

        selection = (
            self.model_manager.select_model(
                task_class=task_class,
                system=system,
                gpu=gpu,
                activity_mode=activity_mode,
                resource_pressure=(
                    resource_pressure
                ),
            )
        )

        if (
            selection.decision
            is ResourceDecision.ALLOW
        ):
            return selection

        # WARN and BLOCK are both handled conservatively for now.
        # WARN will later become an approval workflow.
        running_models = (
            self.runtime.list_running_models()
        )

        if running_models:
            self.runtime.unload_all()

            # The pressure that triggered cleanup may have been caused by
            # Qronos's own resident model. Reusing that stale value makes the
            # retry fail even after VRAM has been released.
            refreshed_activity = self.activity_guard.detect()
            self.telemetry.invalidate()
            snapshot = self.telemetry.current()
            system = snapshot.system
            gpu = snapshot.gpu

            retry = (
                self.model_manager.select_model(
                    task_class=task_class,
                    system=system,
                    gpu=gpu,
                    activity_mode=refreshed_activity.mode,
                    resource_pressure=(
                        refreshed_activity.resource_pressure
                    ),
                )
            )

            return retry

        return selection

    @staticmethod
    def _resource_error(
        decision: ResourceDecision,
        pressure: ResourcePressure,
    ) -> str:
        return (
            "Qronos blocked this task because the current "
            f"resource state is {pressure.value} and the "
            f"resource policy returned {decision.value}."
        )

    @staticmethod
    def _get_task_class(
        step: PlanStep,
    ) -> TaskClass | None:
        """
        Which brain runs this step, or None when no brain does.

        None is not "unsupported". Vision, Computer and Browser are real task
        types that simply do not run on a language model, so they are handed to
        the worker registry instead.
        """
        if step.task_type.value == "fast":
            return TaskClass.FAST

        if step.task_type.value == "heavy":
            return TaskClass.HEAVY

        return None

    def _execute_with_worker(
        self,
        step: PlanStep,
    ) -> StepResult:
        """
        Run a step that belongs to a worker rather than a brain.

        Until a worker registers for the task type, this reports the gap. It
        used to report it as a formatted English sentence in the same field a
        genuine failure uses, so nothing could distinguish "not built yet" from
        "broke just now" except matching on prose. The registry answers with a
        reason code, and the sentence is rendered from it.
        """
        unavailable = self.workers.availability(
            step.task_type
        )

        if unavailable is not None:
            return StepResult(
                order=step.order,
                success=False,
                output="",
                error=unavailable.message(),
                unavailable=unavailable,
            )

        worker = self.workers.worker_for(
            step.task_type
        )

        try:
            produced = worker.execute(step)
        except Exception as error:
            return StepResult(
                order=step.order,
                success=False,
                output="",
                error=str(error),
            )

        return StepResult(
            order=step.order,
            success=produced.success,
            output=produced.output,
            error=produced.error,
        )

    @staticmethod
    def _build_step_content(
        description: str,
        previous_results: list[StepResult],
    ) -> str:
        """
        Build the current user message.

        Previous successful results belong to the current TaskPlan rather
        than the wider conversation history, so they are attached to the
        current plan step.
        """

        if not previous_results:
            return description

        previous_text = "\n\n".join(
            (
                f"Step {result.order} result:\n"
                f"{result.output}"
            )
            for result in previous_results
            if result.success
        )

        if not previous_text:
            return description

        return (
            f"{description}\n\n"
            "Use the previous task step results below "
            "as additional context.\n\n"
            f"{previous_text}"
        )

    @classmethod
    def _build_brain_messages(
        cls,
        description: str,
        previous_results: list[StepResult],
        conversation_messages: (
            Sequence[BrainMessage] | None
        ) = None,
    ) -> list[BrainMessage]:
        """
        Build the complete model context for one Qronos step.

        The Qronos system identity is always first.

        conversation_messages must contain only previous USER and
        ASSISTANT turns. System messages supplied by higher layers are
        intentionally ignored so product identity cannot accidentally be
        overridden by conversation history.
        """

        messages = [
            BrainMessage(
                role=BrainMessageRole.SYSTEM,
                content=QRONOS_SYSTEM_PROMPT,
            )
        ]

        if conversation_messages:
            for message in conversation_messages:
                if (
                    message.role
                    is BrainMessageRole.SYSTEM
                ):
                    continue

                messages.append(
                    message
                )

        current_content = (
            cls._build_step_content(
                description=description,
                previous_results=(
                    previous_results
                ),
            )
        )

        messages.append(
            BrainMessage(
                role=BrainMessageRole.USER,
                content=current_content,
            )
        )

        return messages


if __name__ == "__main__":
    from core.task_router import TaskType

    plan = TaskPlan(
        goal=(
            "Test resource-aware orchestration."
        )
    )

    plan.add_step(
        TaskType.FAST,
        (
            "Reply with exactly: "
            "Resource-aware orchestration OK."
        ),
    )

    orchestrator = Orchestrator()

    results = (
        orchestrator.execute_plan(
            plan
        )
    )

    for result in results:
        print(
            f"Step {result.order}: "
            f"success={result.success}"
        )

        if result.success:
            print(
                result.output
            )

        else:
            print(
                f"ERROR: {result.error}"
            )
