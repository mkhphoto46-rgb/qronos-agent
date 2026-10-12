"""
Protective preemption: the scheduler stops Qronos's own running work when the
safety floor has breached for consecutive readings, and never anything else.

The monitor is a stand-in whose sample each test sets; the work records the
pauses it was asked for. Nothing loads a model or touches another application.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.hard_floor import FloorBreach
from core.load_signal import LoadLevel, LoadSample, LoadSnapshot
from core.queue_scheduler import (
    HoldReason,
    QueueEventType,
    QueueScheduler,
    SchedulerConfig,
    WorkOutcome,
    WorkResult,
)
from core.resource_governor import ResourceGovernor, Weight
from core.resource_guard import GpuStatus, SystemStatus
from core.resource_policy import ResourceDecision
from core.safe_queue import SafeQueue, TaskState
from tests.fixtures.clock import FakeClock

QUIET = SystemStatus(cpu_usage_percent=10.0, ram_usage_percent=40.0, ram_used_gb=6.0, ram_total_gb=16.0)
CARD = GpuStatus(name="test card", temperature_c=45, gpu_utilization_percent=5, vram_used_mb=1_000, vram_total_mb=8_192)


def reading(**fields) -> LoadSample:
    base = dict(at=0.0, decision=ResourceDecision.ALLOW, cpu_percent=10.0, ram_percent=40.0,
                vram_free_mb=7_000, gpu_temperature_c=45)
    base.update(fields)
    return LoadSample(**base)




class Machine:
    """Stands in for the monitor with a sample a test edits."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.sample = reading()
        self.level = LoadLevel.FREE

    def observe(self) -> LoadSample:
        self.sample = replace(self.sample, at=self.clock())
        return self.sample

    def snapshot(self) -> LoadSnapshot:
        return LoadSnapshot(level=self.level, since=0.0, latest=self.sample, sample_count=30, loaded_fraction=0.0)


class PausableWork:
    """Running work that stops when Qronos pauses it, as QueuedConversation does."""

    def __init__(self, committed: bool = False):
        self.pauses = 0
        self.committed = committed

    def describe(self) -> str:
        return "summarise the notes"

    def run(self):  # pragma: no cover - the runner below never calls it
        raise AssertionError

    def pause(self) -> bool:
        self.pauses += 1
        return not self.committed


class UnstoppableWork(PausableWork):
    pause = None  # type: ignore[assignment]


def a_scheduler(clock: FakeClock, machine: Machine, config: SchedulerConfig = SchedulerConfig()):
    events = []
    scheduler = QueueScheduler(
        queue=SafeQueue(clock=clock),
        governor=ResourceGovernor(read_system=lambda: QUIET, read_gpu=lambda: CARD, clock=clock),
        monitor=machine,  # type: ignore[arg-type]
        runner=lambda entry: None,  # hand the work elsewhere; it reports back later
        notify=events.append,
        clock=clock,
        config=config,
    )
    return scheduler, events


def started(scheduler: QueueScheduler, work) -> str:
    task_id = scheduler.submit(work=work, weight=Weight.LIGHT, summary="summarise", required_vram_mb=0).task_id
    scheduler.tick()
    assert scheduler.queue.get(task_id).state is TaskState.RUNNING
    return task_id


def test_a_dying_battery_stops_running_qronos_work_after_two_readings():
    clock = FakeClock()
    machine = Machine(clock)
    scheduler, events = a_scheduler(clock, machine)
    work = PausableWork()
    task_id = started(scheduler, work)

    machine.sample = reading(on_battery=True, battery_percent=8.0)
    scheduler.tick()
    assert work.pauses == 0  # one reading is a spike
    scheduler.tick()
    assert work.pauses == 1
    held = [e for e in events if e.event is QueueEventType.HOLD_STATE and e.task_id == task_id]
    assert held and held[-1].floor.breach is FloorBreach.BATTERY_CRITICAL

    scheduler.tick()
    assert work.pauses == 1  # asked once, then waited for

    scheduler.report_outcome(task_id, WorkResult(WorkOutcome.DEFERRED, "paused", hold=HoldReason.PAUSED))
    task = scheduler.queue.get(task_id)
    assert task.state is TaskState.QUEUED and task.attempts == 0
    view = scheduler.view().tasks[0]
    assert view["heldReason"] == HoldReason.SAFETY_FLOOR.value and "battery" in view["detail"]
    assert view["overridable"] is False

    machine.sample = reading(on_battery=True, battery_percent=14.0)
    scheduler.tick()
    assert scheduler.queue.get(task_id).state is TaskState.QUEUED  # below the resume line
    machine.sample = reading(on_battery=False, battery_percent=14.0)
    scheduler.tick()
    assert scheduler.queue.get(task_id).state is TaskState.RUNNING


def test_a_spike_that_clears_does_not_stop_work():
    clock = FakeClock()
    machine = Machine(clock)
    scheduler, _ = a_scheduler(clock, machine)
    work = PausableWork()
    started(scheduler, work)
    for sample in (reading(gpu_temperature_c=90), reading(), reading(gpu_temperature_c=90), reading()):
        machine.sample = sample
        scheduler.tick()
    assert work.pauses == 0


def test_safety_stops_work_even_while_the_queue_is_paused():
    clock = FakeClock()
    machine = Machine(clock)
    scheduler, _ = a_scheduler(clock, machine)
    work = PausableWork()
    started(scheduler, work)
    scheduler.set_paused(True)
    machine.sample = reading(disk_free_mb=500)
    scheduler.tick()
    scheduler.tick()
    assert work.pauses == 1


def test_squeezed_card_stops_running_work():
    clock = FakeClock()
    machine = Machine(clock)
    scheduler, events = a_scheduler(clock, machine)
    work = PausableWork()
    started(scheduler, work)
    machine.sample = reading(vram_free_mb=6_500, own_vram_mb=6_400, external_vram_growth_mb=292)
    scheduler.tick()
    scheduler.tick()
    assert work.pauses == 1
    holds = [e for e in events if e.event is QueueEventType.HOLD_STATE and e.floor is not None]
    assert holds[-1].floor.breach is FloorBreach.VRAM_SQUEEZED


def test_work_that_cannot_be_paused_is_left_running_and_the_reason_is_shown():
    clock = FakeClock()
    machine = Machine(clock)
    scheduler, events = a_scheduler(clock, machine)
    task_id = started(scheduler, UnstoppableWork())
    machine.sample = reading(ram_percent=97.0)
    scheduler.tick()
    scheduler.tick()
    assert scheduler.queue.get(task_id).state is TaskState.RUNNING
    assert "cannot be stopped safely" in scheduler.view().tasks[0]["detail"]
    notices = [e for e in events if e.event is QueueEventType.HOLD_STATE and e.task_id == task_id]
    assert len(notices) == 1
    for _ in range(5):
        scheduler.tick()
    assert len([e for e in events if e.event is QueueEventType.HOLD_STATE and e.task_id == task_id]) == 1


def test_work_that_already_committed_its_answer_finishes():
    clock = FakeClock()
    machine = Machine(clock)
    scheduler, _ = a_scheduler(clock, machine)
    work = PausableWork(committed=True)
    task_id = started(scheduler, work)
    machine.sample = reading(on_battery=True, battery_percent=5.0)
    scheduler.tick()
    scheduler.tick()
    assert work.pauses == 1
    scheduler.report_outcome(task_id, WorkResult(WorkOutcome.DONE))
    assert scheduler.queue.get(task_id).state is TaskState.DONE


def test_preemption_needs_at_least_one_reading():
    with pytest.raises(ValueError):
        SchedulerConfig(preempt_after_samples=0)


# ------------------------------------- B3-1 / B3-2: pause races and failures


class InlineReportingWork(PausableWork):
    """pause() closes the run and reports DEFERRED before returning, as a synchronous worker can."""

    def __init__(self, outcome=WorkOutcome.DEFERRED, answer=True):
        super().__init__()
        self.scheduler = None
        self.task_id = None
        self.outcome = outcome
        self.answer = answer

    def pause(self):
        self.pauses += 1
        self.scheduler.report_outcome(self.task_id, WorkResult(self.outcome, "paused", hold=HoldReason.PAUSED))
        return self.answer


def inline_started(scheduler, work):
    work.scheduler = scheduler
    task_id = started(scheduler, work)
    work.task_id = task_id
    return task_id


def breach(scheduler, machine):
    machine.sample = reading(on_battery=True, battery_percent=8.0)
    scheduler.tick()
    scheduler.tick()


def test_an_inline_reported_pause_is_attributed_to_the_floor_exactly_once():
    clock = FakeClock()
    machine = Machine(clock)
    scheduler, _ = a_scheduler(clock, machine)
    work = InlineReportingWork()
    task_id = inline_started(scheduler, work)
    breach(scheduler, machine)

    task = scheduler.queue.get(task_id)
    assert work.pauses == 1 and task.state is TaskState.QUEUED and task.attempts == 0
    assert scheduler.view().tasks[0]["heldReason"] == HoldReason.SAFETY_FLOOR.value
    assert scheduler._entries[task_id].preempted is None  # consumed, nothing stale

    machine.sample = reading(on_battery=True, battery_percent=12.0)
    scheduler.tick()
    assert scheduler.queue.get(task_id).state is TaskState.QUEUED  # resume line (15%) applies


def test_after_an_inline_stop_an_ordinary_deferral_is_not_misattributed():
    clock = FakeClock()
    machine = Machine(clock)
    scheduler, _ = a_scheduler(clock, machine)
    work = InlineReportingWork()
    task_id = inline_started(scheduler, work)
    breach(scheduler, machine)

    machine.sample = reading()
    clock.advance(60)
    scheduler.tick()
    assert scheduler.queue.get(task_id).state is TaskState.RUNNING
    scheduler.report_outcome(task_id, WorkResult(WorkOutcome.DEFERRED, "busy", hold=HoldReason.SUSTAINED_LOAD))
    task = scheduler.queue.get(task_id)
    assert task.attempts == 1
    assert scheduler.view().tasks[0]["heldReason"] == HoldReason.SUSTAINED_LOAD.value


def test_an_ordinary_deferral_without_a_breach_still_counts():
    clock = FakeClock()
    machine = Machine(clock)
    scheduler, _ = a_scheduler(clock, machine)
    task_id = started(scheduler, PausableWork())
    scheduler.report_outcome(task_id, WorkResult(WorkOutcome.DEFERRED, "busy", hold=HoldReason.SUSTAINED_LOAD))
    assert scheduler.queue.get(task_id).attempts == 1
    assert scheduler.view().tasks[0]["heldReason"] == HoldReason.SUSTAINED_LOAD.value


class RaisingPauseWork(PausableWork):
    def pause(self) -> bool:
        self.pauses += 1
        raise RuntimeError("pause failed inside the work")


def test_a_raising_pause_is_contained_reported_once_and_leaves_nothing_pending():
    clock = FakeClock()
    machine = Machine(clock)
    scheduler, events = a_scheduler(clock, machine)
    other = PausableWork()
    bad = RaisingPauseWork()
    bad_id = started(scheduler, bad)
    machine.sample = reading(on_battery=True, battery_percent=8.0)
    for _ in range(4):
        scheduler.tick()  # must not raise
    assert bad.pauses >= 1
    assert scheduler.queue.get(bad_id).state is TaskState.RUNNING
    assert scheduler._entries[bad_id].preempted is None
    notices = [e for e in events if e.event is QueueEventType.HOLD_STATE and e.task_id == bad_id]
    assert len(notices) == 1 and "cannot be stopped safely" in notices[0].detail
    scheduler.report_outcome(bad_id, WorkResult(WorkOutcome.DONE))
    assert scheduler.queue.get(bad_id).state is TaskState.DONE
    assert other.pauses == 0


def test_a_pause_refused_after_an_inline_finish_keeps_the_finish():
    clock = FakeClock()
    machine = Machine(clock)
    scheduler, _ = a_scheduler(clock, machine)
    work = InlineReportingWork(outcome=WorkOutcome.DONE, answer=False)
    task_id = inline_started(scheduler, work)
    breach(scheduler, machine)
    assert scheduler.queue.get(task_id).state is TaskState.DONE
    assert scheduler._entries[task_id].preempted is None


def test_a_cancel_while_the_stop_is_pending_stays_cancelled_and_clean():
    clock = FakeClock()
    machine = Machine(clock)
    scheduler, _ = a_scheduler(clock, machine)
    work = PausableWork()
    task_id = started(scheduler, work)
    breach(scheduler, machine)
    assert scheduler._entries[task_id].preempted is not None  # pending, awaiting the work
    assert scheduler.cancel(task_id)
    scheduler.report_outcome(task_id, WorkResult(WorkOutcome.DEFERRED, "paused", hold=HoldReason.PAUSED))
    assert scheduler.queue.get(task_id).state is TaskState.CANCELLED
    assert scheduler._entries[task_id].preempted is None


class NoneReturningPause(PausableWork):
    def pause(self):
        self.pauses += 1
        return None  # no answer is not a refusal


def test_a_pause_without_an_answer_is_treated_as_accepted():
    clock = FakeClock()
    machine = Machine(clock)
    scheduler, _ = a_scheduler(clock, machine)
    work = NoneReturningPause()
    task_id = started(scheduler, work)
    breach(scheduler, machine)
    assert scheduler._entries[task_id].preempted is not None
    scheduler.report_outcome(task_id, WorkResult(WorkOutcome.DEFERRED, "paused"))
    assert scheduler.queue.get(task_id).attempts == 0
