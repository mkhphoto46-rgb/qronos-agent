"""
One owner for the question "may this run right now, and may it keep running".

The pieces were all here. :mod:`core.resource_guard` reads the machine,
:mod:`core.resource_policy` turns a reading into a decision, and
:mod:`core.activity_guard` says whether the user is gaming or editing video.
What was missing is anything that *owns* the answer: the orchestrator asked the
question inline, per step, and nothing else could ask it at all. Two
consequences the architecture already names as problems:

    Check and start are separate moments. Two heavy tasks can both be told yes
    before either has started, because nothing records that the first one is
    about to consume what the second was just promised. A reservation closes
    that window.

    "One heavy consumer of the GPU" cannot be enforced by a function that
    returns a decision and forgets. Somebody has to hold the fact that a heavy
    task is running. That is what this does.

Everything is injectable — the readings, the clock, the thresholds — so the
whole module is testable with no GPU and no waiting. That matters beyond
convenience: the interesting cases are a machine under load and a lease that
expired, and neither is something a test should have to produce for real.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Protocol

from core.activity_guard import ActivityMode, ResourcePressure
from core.resource_guard import (
    GpuStatus,
    SystemStatus,
    read_gpu_status,
    read_system_status,
)
from core.resource_policy import (
    DEFAULT_THRESHOLDS,
    ResourceDecision,
    ResourceThresholds,
    evaluate_resources,
)


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


SystemReader = Callable[[], SystemStatus]
GpuReader = Callable[[], Optional[GpuStatus]]


# How long a reservation stays valid without being renewed or released. A task
# that crashes between reserving and running must not hold the GPU shut
# forever; the lease expiring is the recovery path, not a supervisor.
DEFAULT_LEASE_SECONDS = 300.0


class Weight(Enum):
    """How much of the machine a task expects to need."""

    # Reads, small models, anything that can run beside a game.
    LIGHT = "light"

    # Wants the GPU largely to itself. Only one of these runs at a time.
    HEAVY = "heavy"


class Refusal(Enum):
    """Why a reservation was not granted."""

    RESOURCE_PRESSURE = "resource_pressure"
    ACTIVITY_MODE = "activity_mode"
    HEAVY_TASK_IN_PROGRESS = "heavy_task_in_progress"


# Activity modes in which the user has effectively said "stay out of the way".
# A light task is still fine; a heavy one is not, whatever the current readings
# say, because the readings describe this instant and a render does not.
_PERFORMANCE_MODES = frozenset(
    {
        ActivityMode.GAMING_PERFORMANCE,
        ActivityMode.CREATOR_PERFORMANCE,
    }
)


@dataclass(frozen=True)
class Reservation:
    """A granted claim on the machine."""

    task_id: str
    weight: Weight
    granted_at: float
    expires_at: float

    def expired_at(self, now: float) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True)
class Grant:
    """The answer to one request to run."""

    granted: bool
    decision: ResourceDecision
    reservation: Reservation | None = None
    refusal: Refusal | None = None
    detail: str = ""

    def describe(self) -> str:
        if self.granted:
            return f"granted ({self.decision.value})"

        reason = self.refusal.value if self.refusal else "unknown"

        return f"refused ({reason}): {self.detail}"


class ResourceGovernor:
    """
    Decides what may run, and remembers what is running.

    Thread-safe because the bridge runs a turn on its own thread while the
    reader loop keeps handling commands, so two requests genuinely can arrive
    at once. That is precisely the race a reservation exists to close, and it
    would be strange to close it with a data structure that has the same
    problem.
    """

    def __init__(
        self,
        read_system: SystemReader | None = None,
        read_gpu: GpuReader | None = None,
        thresholds: ResourceThresholds = DEFAULT_THRESHOLDS,
        clock: Clock | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.read_system: SystemReader = read_system or read_system_status
        self.read_gpu: GpuReader = read_gpu or read_gpu_status
        self.thresholds = thresholds
        self.clock: Clock = clock or time.time
        self.lease_seconds = lease_seconds

        self._lock = threading.Lock()
        self._reservations: dict[str, Reservation] = {}

    # ----------------------------------------------------------- reading

    def evaluate(self) -> ResourceDecision:
        """
        What the machine says right now, with no reservation taken.

        For a caller that wants to display the state or decide whether to
        offer something, rather than to run it.
        """
        return evaluate_resources(
            self.read_system(),
            self.read_gpu(),
            self.thresholds,
        )

    # ------------------------------------------------------- reservations

    def request(
        self,
        task_id: str,
        weight: Weight,
        activity_mode: ActivityMode = ActivityMode.NORMAL,
        resource_pressure: ResourcePressure = ResourcePressure.NORMAL,
        decision: ResourceDecision | None = None,
    ) -> Grant:
        """
        Check and reserve in one step.

        The two halves are not separable. Deciding and then reserving leaves a
        window in which a second heavy task is told yes on readings taken
        before the first one started, which is the failure the architecture
        calls out as mandatory to prevent.

        ``decision`` lets a caller that has already judged the machine say so,
        instead of having its judgement quietly overruled by one instantaneous
        sample taken here.

        That is not a convenience. The queue judges the machine over a window,
        because a single reading of a working machine is close to meaningless —
        on the development card the GPU signal crossed its own threshold
        twenty-nine times in forty-five seconds. When the queue decided a task
        could start and this method then took its own reading and refused, the
        task went back to the queue, was admitted again, was refused again, and
        span. That is not hypothetical: it was observed end to end, and nothing
        could ever run.

        The single-heavy-owner rule and the lease still apply either way. Those
        are correctness, and no caller may waive them.
        """
        if not task_id.strip():
            raise ValueError("A reservation must name a task.")

        with self._lock:
            now = self.clock()
            self._expire(now)

            if weight is Weight.HEAVY:
                refusal = self._heavy_is_blocked(
                    activity_mode,
                    resource_pressure,
                )

                if refusal is not None:
                    return refusal

            if decision is None:
                decision = evaluate_resources(
                    self.read_system(),
                    self.read_gpu(),
                    self.thresholds,
                )

            if decision is ResourceDecision.BLOCK:
                return Grant(
                    granted=False,
                    decision=decision,
                    refusal=Refusal.RESOURCE_PRESSURE,
                    detail=(
                        "The machine is under too much load to start "
                        "another task."
                    ),
                )

            # WARN grants. It means "this is getting tight", not "stop", and
            # turning a warning into a refusal here would make Qronos unusable
            # on a busy machine — which is most machines.
            reservation = Reservation(
                task_id=task_id,
                weight=weight,
                granted_at=now,
                expires_at=now + self.lease_seconds,
            )

            self._reservations[task_id] = reservation

            return Grant(
                granted=True,
                decision=decision,
                reservation=reservation,
            )

    def release(self, task_id: str) -> bool:
        """Give back a reservation. True if there was one to give back."""
        with self._lock:
            return self._reservations.pop(task_id, None) is not None

    def renew(self, task_id: str) -> Reservation | None:
        """
        Push a lease out. None when there is nothing holding it.

        A long task renews rather than taking a long lease, so that a task
        which dies mid-run releases the GPU on its own within one lease period.
        """
        with self._lock:
            now = self.clock()
            self._expire(now)

            existing = self._reservations.get(task_id)

            if existing is None:
                return None

            renewed = Reservation(
                task_id=existing.task_id,
                weight=existing.weight,
                granted_at=existing.granted_at,
                expires_at=now + self.lease_seconds,
            )

            self._reservations[task_id] = renewed

            return renewed

    def active(self) -> tuple[Reservation, ...]:
        """Everything currently holding a claim, oldest first."""
        with self._lock:
            self._expire(self.clock())

            return tuple(
                sorted(
                    self._reservations.values(),
                    key=lambda item: item.granted_at,
                )
            )

    def heavy_owner(self) -> str | None:
        """
        Who holds the heavy slot, if anyone.

        The single-heavy-consumer rule, in one place, answerable. Previously it
        was a policy in a document with no code able to state it.
        """
        for reservation in self.active():
            if reservation.weight is Weight.HEAVY:
                return reservation.task_id

        return None

    # ------------------------------------------------------------ internals

    def _expire(self, now: float) -> None:
        """Drop leases nobody renewed. Called with the lock held."""
        dead = [
            task_id
            for task_id, reservation in self._reservations.items()
            if reservation.expired_at(now)
        ]

        for task_id in dead:
            del self._reservations[task_id]

    def _heavy_is_blocked(
        self,
        activity_mode: ActivityMode,
        resource_pressure: ResourcePressure,
    ) -> Grant | None:
        """The rules that apply to heavy work only. Lock held."""
        for reservation in self._reservations.values():
            if reservation.weight is Weight.HEAVY:
                return Grant(
                    granted=False,
                    decision=ResourceDecision.BLOCK,
                    refusal=Refusal.HEAVY_TASK_IN_PROGRESS,
                    detail=(
                        f"{reservation.task_id} is already running as the "
                        "heavy task."
                    ),
                )

        if activity_mode in _PERFORMANCE_MODES:
            return Grant(
                granted=False,
                decision=ResourceDecision.BLOCK,
                refusal=Refusal.ACTIVITY_MODE,
                detail=(
                    f"Qronos stays out of the way in {activity_mode.value}."
                ),
            )

        if resource_pressure is ResourcePressure.CRITICAL:
            return Grant(
                granted=False,
                decision=ResourceDecision.BLOCK,
                refusal=Refusal.RESOURCE_PRESSURE,
                detail="The machine is under critical pressure.",
            )

        return None
