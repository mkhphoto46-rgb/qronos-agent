"""
Holding work until the machine is free, and running it when it is.

``core/safe_queue.py`` is the waiting line and its rules; it deliberately "runs
nothing, spawns nothing and sleeps nowhere". ``core/resource_governor.py``
decides who may hold the machine. ``core/load_signal.py`` says whether the
machine has been busy for long enough to matter, and ``core/hard_floor.py``
says whether the work is safe at all. All four already exist. This is the thing
that drives them.

The shape worth understanding before reading any of it:

    ``tick()`` is public, synchronous, and sleeps nowhere. It is one pass of
    the whole decision: take a reading, see what finished, decide what may
    start, start it. Every rule in this module is tested by calling ``tick()``
    with a hand-moved clock and hand-built readings, with no thread anywhere in
    the test suite.

    The thread does nothing else. ``while not stopping: tick(); wait()``. That
    is the entire body, which is why the concurrency here is small enough to
    reason about.

Two rules that are easy to get wrong and are therefore stated here:

    **Override buys politeness, not safety.** It skips the "you look busy"
    hold. It does not skip the hard floor, and it does not skip the governor's
    rule that only one heavy task may hold the machine at a time — that one is
    correctness, not manners.

    **An override is not remembered.** It applies to the attempt it was pressed
    for. A remembered override is a landmine that fires twenty minutes later,
    when the user has long forgotten pressing it and is in the middle of
    something else.

The queue holds no payload of its own. ``QueuedTask`` carries an identifier, a
weight and a human summary, and this module keeps the runnable thing beside it
in a table keyed by that identifier. That keeps an untyped blob out of the one
module whose whole value is that its invariants are provable in isolation.
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Protocol

from core.activity_guard import ActivityMode, ResourcePressure
from core.hard_floor import (
    DEFAULT_HARD_FLOOR,
    FloorVerdict,
    HardFloorConfig,
)
from core.hard_floor import check as check_floor
from core.load_signal import (
    LoadLevel,
    LoadSample,
    SustainedLoadMonitor,
)
from core.resource_governor import Refusal, ResourceGovernor, Weight
from core.resource_policy import ResourceDecision
from core.safe_queue import QueuedTask, SafeQueue, TaskState


class Clock(Protocol):
    def __call__(self) -> float:  # pragma: no cover - protocol
        ...


class Runnable(Protocol):
    """
    Something the scheduler can run later.

    A protocol rather than a bare callable because a closure cannot be shown to
    the user, listed in the queue, or written to an audit record — and being
    visible is half of what was asked for.

    Two optional methods are looked up with ``getattr`` rather than required,
    so the simplest work stays a two-method object:

        ``cancel() -> bool`` is asked before a *running* task is cancelled.
        False means the work has already committed its result and cancelling
        now would misreport what happened, so the cancel is refused.

        ``public_fields() -> dict`` adds work-specific fields — which
        conversation request it answers, say — to the task as the interface
        sees it.
    """

    def describe(self) -> str:  # pragma: no cover - protocol
        ...

    def run(self) -> "bool | WorkResult":  # pragma: no cover - protocol
        ...


Notifier = Callable[["QueueEvent"], None]


class HoldReason(Enum):
    """
    Why a task is not running.

    Codes rather than sentences, following ``core/workers.py``: the wording can
    change, or be translated into Persian for the interface, without breaking
    anything that reads the value.
    """

    SUSTAINED_LOAD = "sustained_load"
    WARMING_UP = "warming_up"
    HEAVY_TASK_IN_PROGRESS = "heavy_task_in_progress"
    PAUSED = "paused"
    SAFETY_FLOOR = "safety_floor"
    ACTIVITY_MODE = "activity_mode"

    #: The user is talking to Qronos right now. Their turn owns the model
    #: phase and the GPU; queued work waits for it rather than competing.
    FOREGROUND_ACTIVE = "foreground_active"


class WorkOutcome(Enum):
    """How a piece of queued work ended, as the work itself reports it."""

    DONE = "done"
    FAILED = "failed"

    #: The work found, at the moment it ran, that it should not run yet — the
    #: machine was under pressure, the user's own turn took priority, Qronos
    #: was paused. It goes back in line rather than failing, because nothing
    #: is wrong with it.
    DEFERRED = "deferred"

    #: The work noticed it had been cancelled and produced nothing.
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class WorkResult:
    """What a piece of work says about how it went."""

    outcome: WorkOutcome
    detail: str = ""

    #: For a deferral: why it is waiting again. Defaults to the machine being
    #: busy, which is the usual reason.
    hold: Optional[HoldReason] = None

    #: For a deferral: whether this attempt counts towards the limit that
    #: pauses a task which keeps not completing. An interruption caused by the
    #: user's own turn is not the task's fault and does not count.
    count_attempt: bool = True


#: What the governor's refusals mean to somebody looking at the queue.
#: Written out rather than derived, so a refusal added to the governor later
#: shows up as a missing entry rather than silently becoming "a heavy task is
#: already running" — which is the mistake the permission gate documents at
#: some length.
_HOLD_FOR_REFUSAL: dict[Refusal, HoldReason] = {
    Refusal.HEAVY_TASK_IN_PROGRESS: HoldReason.HEAVY_TASK_IN_PROGRESS,
    Refusal.ACTIVITY_MODE: HoldReason.ACTIVITY_MODE,
    Refusal.RESOURCE_PRESSURE: HoldReason.SUSTAINED_LOAD,
}


#: Holds an override can lift. Anything else is a safety limit or a
#: correctness rule, and pressing the button does not change it.
_POLITENESS_HOLDS = frozenset(
    {
        HoldReason.SUSTAINED_LOAD,
        HoldReason.WARMING_UP,
    }
)


class QueueEventType(Enum):
    #: Something about the queue is different from a moment ago. Carries no
    #: detail of its own: the listener re-reads ``view()``, which is whole
    #: state rather than a delta and therefore cannot drift out of step.
    CHANGED = "changed"

    QUEUED = "queued"
    STARTED = "started"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEFERRED = "deferred"
    OVERRIDE_REFUSED = "override_refused"
    HOLD_STATE = "hold_state"


@dataclass(frozen=True)
class QueueEvent:
    """Something the user should be able to see happening."""

    event: QueueEventType
    at: float
    task_id: str = ""
    summary: str = ""
    reason: Optional[HoldReason] = None
    detail: str = ""
    floor: Optional[FloorVerdict] = None
    level: Optional[LoadLevel] = None


@dataclass
class _Entry:
    """The scheduler's own record, beside the queue's."""

    task_id: str
    work: Runnable
    required_vram_mb: int
    hold: HoldReason
    detail: str = ""
    override: bool = False
    floor_refusal: Optional[FloorVerdict] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    #: After a deferral, the task is not offered again before this moment, so
    #: a machine the work keeps finding too busy is not asked every tick.
    not_before: Optional[float] = None


@dataclass(frozen=True)
class SchedulerConfig:
    tick_seconds: float = 2.0
    stop_timeout_seconds: float = 5.0

    #: How long a finished task stays visible before it is forgotten, so the
    #: user sees that something completed rather than watching it vanish.
    history_seconds: float = 300.0

    #: A task admitted this many times without ever completing is paused
    #: rather than left to spin. ``QueuedTask.attempts`` exists for exactly
    #: this: "a task that has been pulled back and requeued repeatedly is a
    #: task something is wrong with".
    max_attempts: int = 3

    #: How long a task that deferred itself waits before it is offered again.
    retry_after_deferral_seconds: float = 30.0


DEFAULT_SCHEDULER_CONFIG = SchedulerConfig()


@dataclass(frozen=True)
class OverrideResult:
    """What pressing "run anyway" achieved."""

    accepted: bool
    floor: Optional[FloorVerdict] = None
    detail: str = ""

    def message(self) -> str:
        if self.accepted:
            return "Qronos will run this now."

        if self.floor is not None:
            return self.floor.message()

        return self.detail or "That task cannot be overridden."


@dataclass(frozen=True)
class QueueView:
    """Everything the interface needs, in one piece."""

    revision: int
    paused: bool
    level: LoadLevel
    holding_since: Optional[float]
    tasks: tuple[dict, ...]


class QueueScheduler:
    """
    The pump. Owns nothing it could have borrowed.

    Thread-safe: ``tick()`` runs on the pump thread while the bridge may call
    ``submit``, ``override`` or ``view`` from the thread reading stdin.
    """

    def __init__(
        self,
        queue: SafeQueue,
        governor: ResourceGovernor,
        monitor: SustainedLoadMonitor,
        runner: Callable[[_Entry], Optional[bool]] | None = None,
        notify: Notifier | None = None,
        clock: Clock | None = None,
        config: SchedulerConfig = DEFAULT_SCHEDULER_CONFIG,
        floor: HardFloorConfig = DEFAULT_HARD_FLOOR,
        activity_mode: Callable[[], ActivityMode] | None = None,
        foreground_busy: Callable[[], bool] | None = None,
    ) -> None:
        self.queue = queue
        self.governor = governor
        self.monitor = monitor
        self.config = config
        self.floor = floor
        self.clock: Clock = clock or time.time

        self._runner = runner or _run_in_this_thread
        self._notify = notify
        self._activity_mode = activity_mode or (lambda: ActivityMode.NORMAL)
        self._foreground_busy = foreground_busy or (lambda: False)

        self._entries: dict[str, _Entry] = {}

        # Work that has been handed out and has not yet reported back. Wider
        # than "tasks in the RUNNING state": a task cancelled mid-run leaves
        # RUNNING at once, but its model call is still on the GPU until it
        # returns, and nothing else may start until it has.
        self._inflight: set[str] = set()
        self._paused = False
        self._revision = itertools.count(1)
        self._current_revision = 0

        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle = threading.Lock()

        self._last_level: LoadLevel | None = None

    # ------------------------------------------------------------ admission

    def submit(
        self,
        work: Runnable,
        weight: Weight,
        summary: str,
        required_vram_mb: int,
        task_id: str | None = None,
    ) -> QueuedTask:
        """Put work in the queue. It starts when the machine allows it."""
        if not summary.strip():
            raise ValueError("A queued task must say what it is.")

        identifier = task_id or f"q-{uuid.uuid4().hex[:8]}"

        with self._lock:
            task = self.queue.submit(
                task_id=identifier,
                weight=weight,
                summary=summary.strip(),
            )
            self._entries[identifier] = _Entry(
                task_id=identifier,
                work=work,
                required_vram_mb=required_vram_mb,
                hold=HoldReason.WARMING_UP,
                detail="Qronos is still working out how busy the machine is.",
            )
            self._bump()

        self._emit(
            QueueEventType.QUEUED,
            task_id=identifier,
            summary=summary.strip(),
            reason=HoldReason.WARMING_UP,
        )
        self._wake.set()

        return task

    def override(self, task_id: str) -> OverrideResult:
        """
        Run this now, if it is only politeness standing in the way.

        Deliberately not a flag that survives the attempt. The floor is checked
        against a reading taken right now, and if it refuses, the task stays
        queued and will start by itself when the machine allows — the user does
        not have to press anything a second time.
        """
        with self._lock:
            entry = self._entries.get(task_id)

            if entry is None:
                return OverrideResult(
                    accepted=False,
                    detail=f"There is no queued task called {task_id}.",
                )

            task = self.queue.get(task_id)

            if task.state is not TaskState.QUEUED:
                return OverrideResult(
                    accepted=False,
                    detail=(
                        f"That task is already {task.state.value} and cannot "
                        "be started again."
                    ),
                )

        sample = self.monitor.snapshot().latest or self.monitor.observe()
        verdict = check_floor(
            sample,
            required_vram_mb=entry.required_vram_mb,
            config=self.floor,
            currently_refused=entry.floor_refusal is not None,
        )

        if not verdict.passed:
            with self._lock:
                entry.floor_refusal = verdict
                entry.hold = HoldReason.SAFETY_FLOOR
                entry.detail = verdict.message()
                self._bump()

            self._emit(
                QueueEventType.OVERRIDE_REFUSED,
                task_id=task_id,
                summary=self.queue.get(task_id).summary,
                reason=HoldReason.SAFETY_FLOOR,
                detail=verdict.message(),
                floor=verdict,
            )

            return OverrideResult(accepted=False, floor=verdict)

        with self._lock:
            entry.override = True
            entry.floor_refusal = None
            self._bump()

        self._wake.set()

        return OverrideResult(accepted=True)

    def cancel(self, task_id: str) -> bool:
        """
        Stop a task for good. False when it is too late to say so honestly.

        A running task is asked first. Work that has already committed its
        result refuses, because reporting "cancelled" for an answer the user
        is already reading would be false. Work that accepts is marked
        cancelled at once, but keeps its place in flight — and keeps the
        machine — until it actually returns, so nothing else starts on a GPU
        that is still busy with it.
        """
        with self._lock:
            entry = self._entries.get(task_id)
            task = self.queue.get(task_id)
            was_running = task.state is TaskState.RUNNING

            if was_running and entry is not None:
                ask = getattr(entry.work, "cancel", None)

                if callable(ask) and ask() is False:
                    return False

            self.queue.cancel(task_id)

            if entry is not None and task_id not in self._inflight:
                entry.finished_at = self.clock()

            self._bump()
            summary = task.summary

        self._emit(
            QueueEventType.CANCELLED,
            task_id=task_id,
            summary=summary,
            detail=(
                "Cancelled while running. Any output it produces from here "
                "on is discarded."
                if was_running
                else ""
            ),
        )

        return True

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = paused
            self._bump()

        if not paused:
            self._wake.set()

    def wake(self) -> None:
        """Look again now rather than at the next tick."""
        self._wake.set()

    # -------------------------------------------------------------- reading

    def running_work(self) -> tuple[Runnable, ...]:
        """The work currently handed out, including work being cancelled."""
        with self._lock:
            return tuple(
                self._entries[task_id].work
                for task_id in self._inflight
                if task_id in self._entries
            )

    def work_for(self, task_id: str) -> Optional[Runnable]:
        """The work behind a task, if the scheduler still remembers it."""
        with self._lock:
            entry = self._entries.get(task_id)

            return None if entry is None else entry.work

    def view(self) -> QueueView:
        with self._lock:
            snapshot = self.monitor.snapshot()

            return QueueView(
                revision=self._current_revision,
                paused=self._paused,
                level=snapshot.level,
                holding_since=snapshot.since,
                tasks=tuple(
                    self._describe(task) for task in self.queue.all_tasks()
                ),
            )

    def _describe(self, task: QueuedTask) -> dict:
        entry = self._entries.get(task.task_id)
        extra: dict = {}

        if entry is not None:
            public = getattr(entry.work, "public_fields", None)

            if callable(public):
                try:
                    extra = dict(public())
                except Exception:
                    extra = {}

        return {
            **extra,
            "taskId": task.task_id,
            "summary": task.summary,
            "weight": task.weight.value,
            "state": task.state.value,
            "attempts": task.attempts,
            "queuedAt": task.queued_at,
            "heldReason": (
                None
                if entry is None or not task.waiting
                else entry.hold.value
            ),
            "detail": "" if entry is None else entry.detail,
            "overridable": (
                entry is not None
                and task.waiting
                and entry.hold in _POLITENESS_HOLDS
            ),
            "override": entry is not None and entry.override,
            "startedAt": None if entry is None else entry.started_at,
            # Cancelled, but its work has not returned yet: the interface
            # says "stopping" rather than pretending the GPU is already free.
            "inFlight": task.task_id in self._inflight,
            "cancellable": not task.finished,
        }

    # ------------------------------------------------------------- the pass

    def tick(self) -> None:
        """
        One pass of the whole decision. Sleeps nowhere.

        Everything the scheduler does is here, so a test that wants to know
        what happens in some state builds that state, calls this once, and
        looks.
        """
        before = self._current_revision

        sample = self.monitor.observe()
        level = self.monitor.snapshot().level

        self._announce_level_changes(level)
        self._forget_old_entries()
        self._renew_inflight_leases()

        if self._paused:
            self._hold_everything(HoldReason.PAUSED, "The queue is paused.")
        elif self._foreground_is_busy():
            # The user's own turn owns the model phase. Not overridable: two
            # model phases at once on one card is a correctness problem, not
            # a matter of manners.
            self._hold_everything(
                HoldReason.FOREGROUND_ACTIVE,
                "Qronos is answering you right now; queued work waits "
                "until that turn is finished.",
            )
        else:
            candidate = self._next_admissible(level, sample)

            if candidate is not None:
                self._start(candidate, level)

        # Only when something actually moved. A hold whose reason changed —
        # "working out how busy you are" becoming "your machine is busy" — is
        # a change the interface has to hear about, and it emits no event of
        # its own.
        if self._current_revision != before:
            self._emit(QueueEventType.CHANGED)

    def _next_admissible(
        self,
        level: LoadLevel,
        sample: LoadSample,
    ) -> Optional[_Entry]:
        """
        The first task that may start right now, if any.

        Every waiting task is considered in turn rather than only the one at
        the front. ``SafeQueue.next_ready`` already skips heavy work when
        heavy work cannot run, on the grounds that "letting one heavy task at
        the front stall every light task behind it would make the queue worse
        than no queue" — but the same is true of a task the safety floor
        refuses, and that one used to stall the whole line. An end-to-end run
        found it: a task needing a model that would never fit sat at the front
        and nothing behind it was ever looked at again.

        The safety floor is asked before the politeness hold, and that order
        is deliberate. Both would stop the same task, but only one of them can
        be lifted by the user — so a task the floor refuses must be reported
        as refused by the floor, or the interface offers a "run anyway" button
        that cannot possibly work and the user presses it twice.
        """
        with self._lock:
            if self.queue.running() or self._inflight:
                # One at a time. Qronos loads a model per task and two would
                # not fit on any card it currently targets.
                return None

            heavy_allowed = self.governor.heavy_owner() is None
            candidates = [
                task
                for task in self.queue.waiting()
                if heavy_allowed or task.weight is not Weight.HEAVY
            ]

        for task in candidates:
            with self._lock:
                entry = self._entries.get(task.task_id)

            if entry is None:
                continue

            if (
                entry.not_before is not None
                and self.clock() < entry.not_before
            ):
                # Deferred itself recently. Its hold already says why.
                continue

            verdict = check_floor(
                sample,
                required_vram_mb=entry.required_vram_mb,
                config=self.floor,
                currently_refused=entry.floor_refusal is not None,
            )

            with self._lock:
                if not verdict.passed:
                    entry.floor_refusal = verdict
                    entry.override = False
                    self._hold(
                        entry,
                        HoldReason.SAFETY_FLOOR,
                        verdict.message(),
                    )
                    continue

                entry.floor_refusal = None

                if level is not LoadLevel.FREE and not entry.override:
                    self._hold(
                        entry,
                        HoldReason.SUSTAINED_LOAD
                        if level is LoadLevel.BUSY
                        else HoldReason.WARMING_UP,
                        self.monitor.snapshot().reason,
                    )
                    continue

                if task.attempts >= self.config.max_attempts:
                    # Something is wrong with this task rather than with the
                    # machine. A visible stall beats an invisible spin.
                    self.queue.pause(task.task_id)
                    self._hold(
                        entry,
                        HoldReason.PAUSED,
                        (
                            f"Qronos tried this {task.attempts} times "
                            "without it completing, so it is paused for you "
                            "to look at."
                        ),
                    )
                    self._bump()
                    continue

                return entry

        return None

    def _start(self, entry: _Entry, level: LoadLevel) -> None:
        task = self.queue.get(entry.task_id)

        # The machine has already been judged, over a window, by the monitor —
        # and the safety floor has already been asked, against a reading taken
        # moments ago. Letting the governor take a third, instantaneous reading
        # here would let one sample overrule both, which is precisely the spin
        # that stopped anything ever running.
        grant = self.governor.request(
            task_id=entry.task_id,
            weight=task.weight,
            activity_mode=self._activity_mode(),
            resource_pressure=ResourcePressure.NORMAL,
            decision=ResourceDecision.ALLOW,
        )

        if not grant.granted:
            # What is left for the governor to refuse is its own business: the
            # single-heavy-owner rule and performance mode. Those are
            # correctness rather than manners, so an override does not skip
            # them.
            with self._lock:
                self._hold(
                    entry,
                    _HOLD_FOR_REFUSAL.get(
                        grant.refusal,
                        HoldReason.HEAVY_TASK_IN_PROGRESS,
                    ),
                    grant.detail,
                )
            return

        with self._lock:
            self.queue.start(entry.task_id)
            entry.started_at = self.clock()
            entry.override = False
            entry.not_before = None
            self._inflight.add(entry.task_id)
            self._bump()

        self._emit(
            QueueEventType.STARTED,
            task_id=entry.task_id,
            summary=task.summary,
            level=level,
        )

        # A runner that returns a verdict ran the work here and now, so the
        # outcome is recorded for it. One that returns None has taken the work
        # elsewhere and will call report_outcome itself when it is done.
        try:
            outcome = self._runner(entry)
        except Exception as exc:
            outcome = WorkResult(WorkOutcome.FAILED, f"{exc}")

        if outcome is not None:
            self.report_outcome(entry.task_id, outcome)

    def report_finished(self, task_id: str, success: bool) -> None:
        """Record that work finished, well or badly."""
        self.report_outcome(
            task_id,
            WorkResult(WorkOutcome.DONE if success else WorkOutcome.FAILED),
        )

    def report_outcome(
        self,
        task_id: str,
        result: "WorkResult | bool",
    ) -> None:
        """
        Record how handed-out work ended, and give the machine back.

        Separate from the runner so that a runner which hands the work to
        another thread can report back later without this module needing to
        know how it did that.

        Whatever the work says, a task the user cancelled while it ran stays
        cancelled: its result is not recorded as done, and nothing it
        produced is announced. Its claim on the machine is released only now,
        when the work has genuinely returned.
        """
        result = _as_work_result(result)
        event: QueueEventType | None = None
        hold_reason: HoldReason | None = None

        with self._lock:
            self._inflight.discard(task_id)
            entry = self._entries.get(task_id)

            try:
                task = self.queue.get(task_id)
            except KeyError:
                task = None

            if entry is None or task is None:
                summary = ""
            elif task.state is not TaskState.RUNNING:
                # Cancelled while it ran. The cancellation was already
                # announced; what is new is only that the machine is free.
                entry.finished_at = self.clock()
                summary = task.summary
            elif result.outcome is WorkOutcome.DEFERRED:
                self.queue.requeue(
                    task_id,
                    count_attempt=result.count_attempt,
                )
                hold_reason = result.hold or HoldReason.SUSTAINED_LOAD
                entry.started_at = None
                entry.not_before = (
                    self.clock() + self.config.retry_after_deferral_seconds
                )
                entry.hold = hold_reason
                entry.detail = result.detail or (
                    "Qronos found the machine busy when it came to run this "
                    "and will try again."
                )
                summary = task.summary
                event = QueueEventType.DEFERRED
            elif result.outcome is WorkOutcome.CANCELLED:
                self.queue.cancel(task_id)
                entry.finished_at = self.clock()
                summary = task.summary
                event = QueueEventType.CANCELLED
            else:
                success = result.outcome is WorkOutcome.DONE
                self.queue.finish(task_id, success=success)
                entry.finished_at = self.clock()
                entry.detail = result.detail
                summary = task.summary
                event = (
                    QueueEventType.FINISHED
                    if success
                    else QueueEventType.FAILED
                )

            self._bump()

        self.governor.release(task_id)

        if event is not None:
            self._emit(
                event,
                task_id=task_id,
                summary=summary,
                reason=hold_reason,
                detail=result.detail,
            )
        else:
            self._emit(QueueEventType.CHANGED)

        self._wake.set()

    # ------------------------------------------------------------ internals

    def _hold(self, entry: _Entry, reason: HoldReason, detail: str) -> None:
        """Record why something is waiting. Called with the lock held."""
        if entry.hold is reason and entry.detail == detail:
            return

        entry.hold = reason
        entry.detail = detail
        self._bump()

    def _hold_everything(self, reason: HoldReason, detail: str) -> None:
        with self._lock:
            for task in self.queue.waiting():
                entry = self._entries.get(task.task_id)

                if entry is not None:
                    self._hold(entry, reason, detail)

    def _announce_level_changes(self, level: LoadLevel) -> None:
        """
        Say something only when the answer changed.

        The pump runs every couple of seconds for as long as there is work
        waiting. Emitting per tick would flood a pipe that the voice spectrum
        already keeps busy, and would strobe the interface.
        """
        if level is self._last_level:
            return

        self._last_level = level
        self._emit(QueueEventType.HOLD_STATE, level=level)

    def _forget_old_entries(self) -> None:
        with self._lock:
            cutoff = self.clock() - self.config.history_seconds
            stale = [
                task_id
                for task_id, entry in self._entries.items()
                if entry.finished_at is not None
                and entry.finished_at <= cutoff
                and task_id not in self._inflight
            ]

            for task_id in stale:
                self._entries.pop(task_id, None)

            if stale:
                self.queue.forget_finished(keep=frozenset(self._inflight))
                self._bump()

    def _renew_inflight_leases(self) -> None:
        """
        Keep the governor's claim alive for work that is still running.

        A lease that expires under a long generation would let a second heavy
        task be admitted while the first is still on the GPU. Renewing only
        from the pump means a pump that dies stops renewing, and the lease
        then frees the machine by itself, which is what it is for.
        """
        with self._lock:
            inflight = tuple(self._inflight)

        for task_id in inflight:
            self.governor.renew(task_id)

    def _foreground_is_busy(self) -> bool:
        try:
            return bool(self._foreground_busy())
        except Exception:
            # Unknown is treated as busy: waiting a tick is harmless, and
            # competing with the user's turn for the GPU is not.
            return True

    def _bump(self) -> None:
        """A new revision. Called with the lock held."""
        self._current_revision = next(self._revision)

    def _emit(self, event: QueueEventType, **fields) -> None:
        if self._notify is None:
            return

        try:
            self._notify(QueueEvent(event=event, at=self.clock(), **fields))
        except Exception:
            # A listener that throws must not take the pump with it. There is
            # nowhere useful to report this: stderr is watched by the desktop
            # and a traceback there looks like a crash.
            pass

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Begin pumping. Idempotent."""
        with self._lifecycle:
            if self._thread is not None:
                return

            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._pump,
                name="qronos-queue-scheduler",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Stop pumping and wait for the thread. Idempotent."""
        with self._lifecycle:
            thread, self._thread = self._thread, None

        if thread is None:
            return

        self._stopping.set()
        self._wake.set()
        thread.join(timeout=timeout or self.config.stop_timeout_seconds)

    def __enter__(self) -> "QueueScheduler":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _pump(self) -> None:
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception as exc:
                # An unhandled exception here would reach threading.excepthook
                # and print a traceback to stderr, which the desktop reads as
                # a crash. Report it as an event and keep pumping.
                self._emit(
                    QueueEventType.FAILED,
                    detail=f"The queue scheduler hit an error: {exc}",
                )

            if self._idle():
                # Nothing to schedule, so nothing to watch. This is what keeps
                # an idle Qronos from launching nvidia-smi forever, which
                # core/telemetry_cache.py argues for at some length.
                self._wake.wait()
                self._wake.clear()
            else:
                self._stopping.wait(self.config.tick_seconds)

    def _idle(self) -> bool:
        with self._lock:
            return (
                not self.queue.waiting()
                and not self.queue.running()
                and not self._inflight
            )


def _as_work_result(value: "WorkResult | bool | None") -> WorkResult:
    """A plain verdict from simple work, in the shape the scheduler records."""
    if isinstance(value, WorkResult):
        return value

    return WorkResult(WorkOutcome.DONE if value else WorkOutcome.FAILED)


def _run_in_this_thread(entry: _Entry) -> "WorkResult | bool":
    """
    The default runner: do the work here and now, and say how it went.

    Fine for tests and for work measured in milliseconds. The bridge supplies
    one that hands the work to its own thread, so a two-minute generation does
    not stop the pump from noticing that the machine got busy.

    Work that raises is a failure, not a crash of the queue. Letting it
    propagate would reach the pump thread and take the scheduler with it.
    """
    try:
        outcome = entry.work.run()
    except Exception as exc:
        return WorkResult(WorkOutcome.FAILED, f"{exc}")

    return outcome if isinstance(outcome, WorkResult) else bool(outcome)
