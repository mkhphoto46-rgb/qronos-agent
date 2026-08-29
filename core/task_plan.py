from __future__ import annotations

from dataclasses import dataclass, field

from core.task_router import TaskType
from core.vision_image import PreparedImage


@dataclass(frozen=True)
class PlanStep:
    """
    One step in a Qronos task plan.

    ``images`` holds the pictures the step is about — a screen capture, a
    camera frame, a file the user pointed at. Until now a step was an order, a
    type and a sentence, with nowhere to put the thing the sentence refers to,
    so "what is in this picture" arrived at a worker with no picture.

    A file is held as a path and a capture as an already-prepared picture, for
    the reasons :class:`core.brain_runtime.BrainMessage` gives. Neither prints
    its contents: a plan is shown in logs and in the desktop's step list.
    """

    order: int
    task_type: TaskType
    description: str
    images: tuple[str | PreparedImage, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.images, str):
            raise TypeError(
                "images is a sequence of pictures, not one picture. A bare "
                "string would be read as one path per character."
            )


@dataclass
class TaskPlan:
    """Ordered execution plan for a Qronos request."""

    goal: str
    steps: list[PlanStep] = field(default_factory=list)

    def add_step(
        self,
        task_type: TaskType,
        description: str,
        images: tuple[str | PreparedImage, ...] = (),
    ) -> None:
        self.steps.append(
            PlanStep(
                order=len(self.steps) + 1,
                task_type=task_type,
                description=description,
                images=images,
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