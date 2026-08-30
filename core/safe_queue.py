"""
What happens to work that cannot safely run yet.

Today a task refused by the resource governor is simply refused, and the user
is told no. The architecture asks for something better: hold it, run it when
the machine is free, and let the user see and change the queue meanwhile.

This is the state machine for that, and only the state machine. It runs
nothing, spawns nothing and sleeps nowhere — it decides what is next and
records what happened. Two reasons that separation is worth having:

    The invariants are the hard part, not the execution. "A cancelled task
    never runs" and "nothing is admitted while a heavy task holds the machine"
    are properties that have to hold across every interleaving, and they are
    provable here in a way they would not be if they were tangled up with
    threads.

    Execution belongs to whatever owns the workers. A queue that also ran
    things would need to know about brains, workers, the bridge and the event
    stream, and would become the place all of those meet.

The transition table is explicit and total. Every state names what may follow
it, and a move that is not listed is refused rather than allowed to fall
through — the failure mode that turned a permission decision into an accidental
allow elsewhere in this codebase.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from core.resource_governor import Weight


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


class TaskState(Enum):
    """Where a queued task is in its life."""

    # Waiting for the machine to be free.
    QUEUED = "queued"

    # Held by the user. Skipped when choosing what to run next, and stays held
    # until the user says otherwise, however free the machine becomes.
    PAUSED = "paused"

    # Handed out to be executed.
    RUNNING = "running"

    DONE = "done"
    FAILED = "failed"

    # Stopped by the user. Terminal, and specifically not the same as FAILED:
    # nothing went wrong and nothing should be retried.
    CANCELLED = "cancelled"


#: What may follow what. Total by construction: a state missing from this table
#: allows nothing, which is the safe direction.
_ALLOWED: dict[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset(
        {TaskState.RUNNING, TaskState.PAUSED, TaskState.CANCELLED}
    ),
    TaskState.PAUSED: frozenset(
        {TaskState.QUEUED, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.DONE,
            TaskState.FAILED,
            TaskState.CANCELLED,
            # Requeued: the governor pulled the machine out from under it, or
            # the user paused mid-run. The task goes back to waiting rather
            # than failing, because nothing was wrong with the task.
            TaskState.QUEUED,
        }
    ),
    TaskState.DONE: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

_TERMINAL = frozenset(
    {TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED}
)


class IllegalTransition(Exception):
    """A task was asked to move somewhere it cannot go from where it is."""


class UnknownTask(KeyError):
    """No task with that id is in the queue."""


@dataclass
class QueuedTask:
    """One piece of work waiting its turn."""

    task_id: str
    weight: Weight
    summary: str
    state: TaskState = TaskState.QUEUED
    queued_at: float = 0.0

    # Increments every time the task is admitted. A task that has been pulled
    # back and requeued repeatedly is a task something is wrong with, and this
    # is what makes that visible instead of it silently spinning.
    attempts: int = 0

    # Insertion order, so ties break by arrival rather than by dictionary
    # iteration.
    sequence: int = 0

    history: list[TaskState] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.state in _TERMINAL

    @property
    def waiting(self) -> bool:
        return self.state is TaskState.QUEUED

    def describe(self) -> str:
        return f"{self.summary} [{self.weight.value}] {self.state.value}"


class SafeQueue:
    """
    The waiting line, and the rules about moving through it.

    Thread-safe, because the thing that admits tasks and the thing that reports
    their outcome are not the same thread in this application.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock: Clock = clock or time.time

        self._lock = threading.RLock()
        self._tasks: dict[str, QueuedTask] = {}
        self._sequence = itertools.count()

    # ---------------------------------------------------------- admission

    def submit(
        self,
        task_id: str,
        weight: Weight,
        summary: str,
    ) -> QueuedTask:
        """Put a task in the queue."""
        if not task_id.strip():
            raise ValueError("A queued task must have an id.")

        with self._lock:
            if task_id in self._tasks:
                raise ValueError(f"{task_id} is already queued.")

            task = QueuedTask(
                task_id=task_id,
                weight=weight,
                summary=summary,
                queued_at=self.clock(),
                sequence=next(self._sequence),
            )

            self._tasks[task_id] = task

            return task

    def next_ready(self, heavy_allowed: bool = True) -> QueuedTask | None:
        """
        The task that should run next, or None.

        ``heavy_allowed`` is how the governor's single-heavy-consumer rule
        reaches the queue: when a heavy task already holds the machine, heavy
        work is skipped and light work still flows. Skipped, not blocked —
        letting one heavy task at the front stall every light task behind it
        would make the queue worse than no queue.
        """
        with self._lock:
            candidates = [
                task
                for task in self._tasks.values()
                if task.waiting
                and (heavy_allowed or task.weight is not Weight.HEAVY)
            ]

            if not candidates:
                return None

            return min(candidates, key=lambda task: task.sequence)

    # --------------------------------------------------------- transitions

    def transition(
        self,
        task_id: str,
        state: TaskState,
    ) -> QueuedTask:
        """
        Move a task, or refuse.

        Refusing rather than ignoring is deliberate. A silently dropped
        transition leaves the caller believing something happened, and the
        queue is exactly the component where that produces a task nobody is
        watching.
        """
        with self._lock:
            task = self._require(task_id)

            if state not in _ALLOWED[task.state]:
                raise IllegalTransition(
                    f"{task_id} cannot go from {task.state.value} to "
                    f"{state.value}."
                )

            task.history.append(task.state)
            task.state = state

            if state is TaskState.RUNNING:
                task.attempts += 1

            return task

    def start(self, task_id: str) -> QueuedTask:
        return self.transition(task_id, TaskState.RUNNING)

    def finish(self, task_id: str, success: bool = True) -> QueuedTask:
        return self.transition(
            task_id,
            TaskState.DONE if success else TaskState.FAILED,
        )

    def pause(self, task_id: str) -> QueuedTask:
        return self.transition(task_id, TaskState.PAUSED)

    def resume(self, task_id: str) -> QueuedTask:
        return self.transition(task_id, TaskState.QUEUED)

    def requeue(self, task_id: str) -> QueuedTask:
        """
        Put a running task back in the line.

        For when the machine was taken away, not when the task failed. It keeps
        its place: re-queuing to the back would let a busy machine starve the
        one task that keeps getting interrupted.
        """
        return self.transition(task_id, TaskState.QUEUED)

    def cancel(self, task_id: str) -> QueuedTask:
        """
        Stop a task for good.

        Reachable from every non-terminal state, including RUNNING, because a
        user who has asked to stop should not have to wait for a state they
        cannot see.
        """
        return self.transition(task_id, TaskState.CANCELLED)

    # ------------------------------------------------------------ reading

    def get(self, task_id: str) -> QueuedTask:
        with self._lock:
            return self._require(task_id)

    def waiting(self) -> tuple[QueuedTask, ...]:
        """Everything still in line, in the order it will be taken."""
        with self._lock:
            return tuple(
                sorted(
                    (task for task in self._tasks.values() if task.waiting),
                    key=lambda task: task.sequence,
                )
            )

    def running(self) -> tuple[QueuedTask, ...]:
        with self._lock:
            return tuple(
                task
                for task in self._tasks.values()
                if task.state is TaskState.RUNNING
            )

    def all_tasks(self) -> tuple[QueuedTask, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._tasks.values(),
                    key=lambda task: task.sequence,
                )
            )

    def forget_finished(self) -> int:
        """
        Drop terminal tasks. Returns how many went.

        The queue is not a history — :mod:`core.action_audit` is. Keeping
        finished tasks here forever would turn an in-memory structure into an
        unbounded one.
        """
        with self._lock:
            done = [
                task_id
                for task_id, task in self._tasks.items()
                if task.finished
            ]

            for task_id in done:
                del self._tasks[task_id]

            return len(done)

    def describe(self) -> str:
        return "\n".join(task.describe() for task in self.all_tasks())

    def _require(self, task_id: str) -> QueuedTask:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise UnknownTask(task_id) from None
