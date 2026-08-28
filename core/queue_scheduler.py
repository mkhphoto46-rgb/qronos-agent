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
from core.resource_governor import ResourceGovernor, Weight
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
    """

    def describe(self) -> str:  # pragma: no cover - protocol
        ...

    def run(self) -> bool:  # pragma: no cover - protocol
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


#: Holds an override can lift. Anything else is a safety limit or a
#: correctness rule, and pressing the button does not change it.
_POLITENESS_HOLDS = frozenset(
    {
        HoldReason.SUSTAINED_LOAD,
        HoldReason.WARMING_UP,
    }
)


class QueueEventType(Enum):
    QUEUED = "queued"
    STARTED = "started"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"
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

        self._entries: dict[str, _Entry] = {}
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

    def cancel(self, task_id: str) -> None:
        with self._lock:
            self.queue.cancel(task_id)
            entry = self._entries.get(task_id)

            if entry is not None:
                entry.finished_at = self.clock()

            self._bump()
            summary = self.queue.get(task_id).summary

        self._emit(
            QueueEventType.CANCELLED,
            task_id=task_id,
            summary=summary,
        )

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = paused
            self._bump()

        if not paused:
            self._wake.set()

    # -------------------------------------------------------------- reading

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

        return {
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
        }

    # ------------------------------------------------------------- the pass

    def tick(self) -> None:
        """
        One pass of the whole decision. Sleeps nowhere.

        Everything the scheduler does is here, so a test that wants to know
        what happens in some state builds that state, calls this once, and
        looks.
        """
        sample = self.monitor.observe()
        level = self.monitor.snapshot().level

        self._announce_level_changes(level)
        self._forget_old_entries()

        if self._paused:
            self._hold_everything(HoldReason.PAUSED, "The queue is paused.")
            return

        candidate = self._next_admissible(level, sample)

        if candidate is None:
            return

        self._start(candidate, level)

    def _next_admissible(
        self,
        level: LoadLevel,
        sample: LoadSample,
    ) -> Optional[_Entry]:
        """
        The task that may start right now, if any.

        The safety floor is checked before the politeness hold, and that order
        is deliberate. Both would stop the same task, but only one of them can
        be lifted by the user — so a task the floor refuses must be reported as
        refused by the floor, or the interface offers a "run anyway" button
        that cannot possibly work and the user presses it twice.
        """
        with self._lock:
            if self.queue.running():
                # One at a time. Qronos loads a model per task and two would
                # not fit on any card it currently targets.
                return None

            heavy_allowed = self.governor.heavy_owner() is None
            task = self.queue.next_ready(heavy_allowed=heavy_allowed)

            if task is None:
                return None

            entry = self._entries.get(task.task_id)

            if entry is None:
                return None

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
                self._hold(entry, HoldReason.SAFETY_FLOOR, verdict.message())

                return None

            entry.floor_refusal = None

            if level is not LoadLevel.FREE and not entry.override:
                self._hold(
                    entry,
                    HoldReason.SUSTAINED_LOAD
                    if level is LoadLevel.BUSY
                    else HoldReason.WARMING_UP,
                    self.monitor.snapshot().describe(),
                )

                return None

            if task.attempts >= self.config.max_attempts:
                # Something is wrong with this task rather than with the
                # machine. A visible stall beats an invisible spin.
                self.queue.pause(task.task_id)
                self._hold(
                    entry,
                    HoldReason.PAUSED,
                    (
                        f"Qronos tried this {task.attempts} times without it "
                        "completing, so it is paused for you to look at."
                    ),
                )
                self._bump()

                return None

            return entry

    def _start(self, entry: _Entry, level: LoadLevel) -> None:
        task = self.queue.get(entry.task_id)
        grant = self.governor.request(
            task_id=entry.task_id,
            weight=task.weight,
            activity_mode=self._activity_mode(),
            resource_pressure=ResourcePressure.NORMAL,
        )

        if not grant.granted:
            # The governor's own rules — one heavy owner, performance mode —
            # are correctness, not politeness, so an override does not skip
            # them.
            with self._lock:
                self._hold(
                    entry,
                    HoldReason.HEAVY_TASK_IN_PROGRESS,
                    grant.detail,
                )
            return

        with self._lock:
            self.queue.start(entry.task_id)
            entry.started_at = self.clock()
            entry.override = False
            self._bump()

        self._emit(
            QueueEventType.STARTED,
            task_id=entry.task_id,
            summary=task.summary,
            level=level,
        )

        # A runner that returns a verdict ran the work here and now, so the
        # outcome is recorded for it. One that returns None has taken the work
        # elsewhere and will call report_finished itself when it is done.
        outcome = self._runner(entry)

        if outcome is not None:
            self.report_finished(entry.task_id, bool(outcome))

    def report_finished(self, task_id: str, success: bool) -> None:
        """
        Record the outcome of work that has finished.

        Separate from the runner so that a runner which hands the work to
        another thread can report back later without this module needing to
        know how it did that.
        """
        with self._lock:
            entry = self._entries.get(task_id)

            if entry is None:
                return

            entry.finished_at = self.clock()
            self.queue.finish(task_id, success=success)
            self._bump()
            summary = self.queue.get(task_id).summary

        self.governor.release(task_id)
        self._emit(
            QueueEventType.FINISHED if success else QueueEventType.FAILED,
            task_id=task_id,
            summary=summary,
        )
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
            ]

            for task_id in stale:
                self._entries.pop(task_id, None)

            if stale:
                self.queue.forget_finished()
                self._bump()

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
            return not self.queue.waiting() and not self.queue.running()


def _run_in_this_thread(entry: _Entry) -> bool:
    """
    The default runner: do the work here and now, and say how it went.

    Fine for tests and for work measured in milliseconds. The bridge supplies
    one that hands the work to its own thread, so a two-minute generation does
    not stop the pump from noticing that the machine got busy.

    Work that raises is a failure, not a crash of the queue. Letting it
    propagate would reach the pump thread and take the scheduler with it.
    """
    try:
        return bool(entry.work.run())
    except Exception:
        return False
