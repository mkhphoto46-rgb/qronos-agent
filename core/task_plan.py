from __future__ import annotations

from dataclasses import dataclass, field

from core.task_router import TaskType


@dataclass(frozen=True)
class PlanStep:
    """One step in a Qronos task plan."""

    order: int
    task_type: TaskType
    description: str


@dataclass
class TaskPlan:
    """Ordered execution plan for a Qronos request."""

    goal: str
    steps: list[PlanStep] = field(default_factory=list)

    def add_step(
        self,
        task_type: TaskType,
        description: str,
    ) -> None:
        self.steps.append(
            PlanStep(
                order=len(self.steps) + 1,
                task_type=task_type,
                description=description,
            )
        )

    @property
    def is_empty(self) -> bool:
        return not self.steps


if __name__ == "__main__":
    plan = TaskPlan(
        goal="Analyze an image with an external AI and compare the result."
    )

    plan.add_step(
        TaskType.BROWSER,
        "Open the selected external AI website and submit the request.",
    )

    plan.add_step(
        TaskType.VISION,
        "Analyze the image locally.",
    )

    plan.add_step(
        TaskType.HEAVY,
        "Compare both results and reason about disagreements.",
    )

    for step in plan.steps:
        print(
            f"{step.order}. "
            f"{step.task_type.value} -> "
            f"{step.description}"
        )