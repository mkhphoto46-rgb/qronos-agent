"""
The seam the Vision, Computer and Browser workers will attach to.

Today the router recognises five task types and the orchestrator can execute
two. The other three produce a formatted sentence — ``Task type 'vision' is not
implemented yet.`` — sitting in the same ``error`` field a real failure uses. A
caller cannot tell "this feature does not exist" from "this feature broke", and
the only way to try is to match on English prose.

This module replaces that with a registry and a reason code. Nothing here
executes anything either; the difference is that the gap is now typed, and the
day a Vision worker exists it registers itself instead of the orchestrator
growing another branch.

Note what is deliberately *not* here. ``TaskClass`` in
:mod:`core.model_manager` stays FAST and HEAVY, because it answers a different
question: which language model to load. Vision and Browser are not brains, and
widening that enum would ask ``ModelManager`` to find a model profile for a
worker that does not use one. Task type and model class are two axes, and this
module keeps them apart.

Every worker is expected to route its side effects through
:mod:`security.gate`. The registry cannot enforce that — a worker is arbitrary
code — but :class:`TaskWorker` names it, and the gate is the only sanctioned
route.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from core.task_plan import PlanStep
from core.task_router import TaskType


class UnavailableReason(Enum):
    """Why a task type could not be executed. A code, not a sentence."""

    # No worker has been registered for this task type. The Vision, Computer
    # and Browser case today.
    NOT_IMPLEMENTED = "not_implemented"

    # A worker exists but its dependencies are missing on this machine — a
    # model that was never downloaded, a runtime binary that is not installed.
    NOT_INSTALLED = "not_installed"

    # A worker exists and is installed, but the user or policy turned it off.
    DISABLED = "disabled"


@dataclass(frozen=True)
class WorkerOutput:
    """What a worker produced."""

    output: str
    success: bool = True
    error: str | None = None


@dataclass(frozen=True)
class Unavailable:
    """A task type that cannot run, and why, in a form code can branch on."""

    task_type: TaskType
    reason: UnavailableReason
    detail: str = ""

    def message(self) -> str:
        """
        The sentence a person sees.

        Kept as a rendering of the reason rather than the reason itself, so the
        wording can change — or be translated into Persian, which this product
        will need — without breaking anything that reads the code.
        """
        if self.detail:
            return self.detail

        if self.reason is UnavailableReason.NOT_IMPLEMENTED:
            return (
                f"Qronos cannot carry out {self.task_type.value} tasks yet."
            )

        if self.reason is UnavailableReason.NOT_INSTALLED:
            return (
                f"The {self.task_type.value} worker is not installed on this "
                "machine."
            )

        return f"The {self.task_type.value} worker is turned off."


class TaskWorker(ABC):
    """
    One kind of work Qronos can do.

    Modelled on :class:`core.brain_runtime.BrainRuntime`, which is the existing
    interface of this shape in the codebase: a health check that reports
    readiness without side effects, and one method that does the work.
    """

    #: The task type this worker answers for.
    task_type: TaskType

    @abstractmethod
    def health_check(self) -> bool:
        """
        True when this worker could run right now.

        Must not start anything, download anything or prompt anybody. It is
        called to decide whether to offer a capability, including on paths
        where the answer is "no" and nothing should have happened.
        """

    @abstractmethod
    def execute(self, step: PlanStep) -> WorkerOutput:
        """
        Carry out one step.

        Any effect on the machine goes through :mod:`security.gate` first.
        """


class WorkerRegistry:
    """
    Which worker answers for which task type.

    Empty by default, on purpose: an orchestrator with no registry behaves
    exactly as it does today, and a test can register a fake without touching
    global state.
    """

    def __init__(self) -> None:
        self._workers: dict[TaskType, TaskWorker] = {}

    def register(self, worker: TaskWorker) -> None:
        task_type = getattr(worker, "task_type", None)

        if not isinstance(task_type, TaskType):
            raise ValueError(
                "A worker must declare a TaskType in its task_type attribute."
            )

        if task_type in self._workers:
            raise ValueError(
                f"A worker is already registered for {task_type.value}."
            )

        self._workers[task_type] = worker

    def worker_for(self, task_type: TaskType) -> TaskWorker | None:
        return self._workers.get(task_type)

    def registered(self) -> frozenset[TaskType]:
        return frozenset(self._workers)

    def availability(self, task_type: TaskType) -> Unavailable | None:
        """
        None when the task type can run, otherwise the reason it cannot.

        Returning the reason rather than a boolean is the point of the module:
        "not built" and "built but the model is missing" send a user to two
        completely different places.
        """
        worker = self.worker_for(task_type)

        if worker is None:
            return Unavailable(
                task_type=task_type,
                reason=UnavailableReason.NOT_IMPLEMENTED,
            )

        try:
            healthy = worker.health_check()
        except Exception as error:
            # A health check that raises is a worker that is not ready, not a
            # crash to propagate into the caller's step.
            return Unavailable(
                task_type=task_type,
                reason=UnavailableReason.NOT_INSTALLED,
                detail=str(error),
            )

        if not healthy:
            return Unavailable(
                task_type=task_type,
                reason=UnavailableReason.NOT_INSTALLED,
            )

        return None
