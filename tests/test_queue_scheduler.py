"""
Holding work, and the exact circumstances under which it stops being held.

Almost every test here drives ``tick()`` directly with a hand-moved clock and
hand-built readings. No thread is started and nothing sleeps, so the rules are
provable rather than merely likely. The two tests that do start a thread assert
only that starting and stopping is clean.

The one to read first, if you only read one, is
``TestOverrideBuysPolitenessNotSafety``. It is the promise the button makes.
"""

from __future__ import annotations

import threading
import unittest

from core.hard_floor import FloorBreach
from core.load_signal import LoadLevel, LoadSample
from core.queue_scheduler import (
    HoldReason,
    QueueEvent,
    QueueEventType,
    QueueScheduler,
    SchedulerConfig,
)
from core.resource_governor import ResourceGovernor, Weight
from core.resource_guard import GpuStatus, SystemStatus
from core.resource_policy import ResourceDecision
from core.safe_queue import SafeQueue, TaskState
from tests.fixtures.clock import FakeClock


CARD_MB = 16_303
FAST_MB = 3_482
HEAVY_MB = 10_240


class Work:
    """A runnable that records that it ran, and nothing else."""

    def __init__(self, succeeds: bool = True) -> None:
        self.runs = 0
        self.succeeds = succeeds

    def describe(self) -> str:
        return "a piece of work"

    def run(self) -> bool:
        self.runs += 1
        return self.succeeds


class ExplodingWork(Work):
    def run(self) -> bool:
        self.runs += 1
        raise RuntimeError("the work fell over")


class Machine:
    """
    A machine whose state a test sets directly.

    Stands in for the load monitor so a test can say "busy" or "free" without
    feeding twenty readings through the real dwell logic — that is what
    ``tests/test_load_signal.py`` is for.
    """

    def __init__(
        self,
        clock: FakeClock,
        level: LoadLevel = LoadLevel.FREE,
        free_vram_mb: int = 14_000,
        gpu_temperature_c: int = 45,
    ) -> None:
        self.clock = clock
        self.level = level
        self.free_vram_mb = free_vram_mb
        self.gpu_temperature_c = gpu_temperature_c
        self.observations = 0

        # When this state began, so that "how long has it been like this"
        # actually advances. It used to be a constant, which quietly made the
        # fake unable to express the very thing one of these tests is about.
        self.since_time = clock()

    def _sample(self) -> LoadSample:
        return LoadSample(
            at=self.clock(),
            decision=(
                ResourceDecision.ALLOW
                if self.level is LoadLevel.FREE
                else ResourceDecision.BLOCK
            ),
            cpu_percent=15.0,
            ram_percent=43.0,
            vram_free_mb=self.free_vram_mb,
            gpu_temperature_c=self.gpu_temperature_c,
        )

    def observe(self) -> LoadSample:
        self.observations += 1
        return self._sample()

    def snapshot(self):
        from core.load_signal import LoadSnapshot

        return LoadSnapshot(
            level=self.level,
            since=self.clock() - self.since_time,
            latest=self._sample(),
            sample_count=30,
            loaded_fraction=0.0 if self.level is LoadLevel.FREE else 1.0,
        )


def a_scheduler(
    machine: Machine,
    clock: FakeClock,
    config: SchedulerConfig = SchedulerConfig(),
    activity_mode=None,
) -> tuple[QueueScheduler, list[QueueEvent]]:
    events: list[QueueEvent] = []

    scheduler = QueueScheduler(
        queue=SafeQueue(clock=clock),
        governor=ResourceGovernor(
            read_system=lambda: SystemStatus(
                cpu_usage_percent=10.0,
                ram_usage_percent=30.0,
                ram_used_gb=5.0,
                ram_total_gb=16.0,
            ),
            read_gpu=lambda: GpuStatus(
                name="RTX 5080",
                temperature_c=45,
                gpu_utilization_percent=5,
                vram_used_mb=1_000,
                vram_total_mb=CARD_MB,
            ),
            clock=clock,
        ),
        monitor=machine,  # type: ignore[arg-type]
        notify=events.append,
        clock=clock,
        config=config,
        activity_mode=activity_mode,
    )

    return scheduler, events


class SchedulerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.machine = Machine(self.clock)
        self.scheduler, self.events = a_scheduler(self.machine, self.clock)

    def submit(self, work: Work, weight: Weight = Weight.LIGHT) -> str:
        return self.scheduler.submit(
            work=work,
            weight=weight,
            summary="summarise the meeting notes",
            required_vram_mb=FAST_MB,
        ).task_id

    def kinds(self) -> list[QueueEventType]:
        return [event.event for event in self.events]


class TestABusyMachineHoldsWork(SchedulerTestCase):
    def test_nothing_runs_while_the_machine_is_busy(self) -> None:
        self.machine.level = LoadLevel.BUSY
        work = Work()
        self.submit(work)

        for _ in range(5):
            self.scheduler.tick()

        self.assertEqual(work.runs, 0)

    def test_the_task_is_still_waiting_rather_than_failed(self) -> None:
        self.machine.level = LoadLevel.BUSY
        task_id = self.submit(Work())

        self.scheduler.tick()

        self.assertIs(
            self.scheduler.queue.get(task_id).state,
            TaskState.QUEUED,
        )

    def test_the_reason_is_recorded_for_the_user(self) -> None:
        self.machine.level = LoadLevel.BUSY
        task_id = self.submit(Work())

        self.scheduler.tick()
        held = self.scheduler.view().tasks[0]

        self.assertEqual(held["heldReason"], HoldReason.SUSTAINED_LOAD.value)
        self.assertEqual(held["taskId"], task_id)

    def test_a_held_task_is_marked_overridable(self) -> None:
        self.machine.level = LoadLevel.BUSY
        self.submit(Work())

        self.scheduler.tick()

        self.assertTrue(self.scheduler.view().tasks[0]["overridable"])

    def test_an_unknown_machine_holds_too(self) -> None:
        # Not knowing must never be treated as permission.
        self.machine.level = LoadLevel.UNKNOWN
        work = Work()
        self.submit(work)

        self.scheduler.tick()

        self.assertEqual(work.runs, 0)


class TestAFreeMachineRunsIt(SchedulerTestCase):
    def test_it_runs_on_the_first_tick(self) -> None:
        work = Work()
        self.submit(work)

        self.scheduler.tick()

        self.assertEqual(work.runs, 1)

    def test_it_runs_once_the_machine_frees_up(self) -> None:
        self.machine.level = LoadLevel.BUSY
        work = Work()
        self.submit(work)

        self.scheduler.tick()
        self.assertEqual(work.runs, 0)

        self.machine.level = LoadLevel.FREE
        self.scheduler.tick()

        self.assertEqual(work.runs, 1)

    def test_the_outcome_is_recorded(self) -> None:
        task_id = self.submit(Work())

        self.scheduler.tick()

        self.assertIs(
            self.scheduler.queue.get(task_id).state,
            TaskState.DONE,
        )

    def test_a_failure_is_recorded_as_a_failure(self) -> None:
        task_id = self.submit(Work(succeeds=False))

        self.scheduler.tick()

        self.assertIs(
            self.scheduler.queue.get(task_id).state,
            TaskState.FAILED,
        )

    def test_work_that_raises_does_not_take_the_queue_with_it(self) -> None:
        self.submit(ExplodingWork())

        self.scheduler.tick()

        # And the next task still runs.
        second = Work()
        self.submit(second)
        self.scheduler.tick()

        self.assertEqual(second.runs, 1)


class TestOverrideBuysPolitenessNotSafety(SchedulerTestCase):
    """
    The promise the "run anyway" button makes.

    It will be rude on the user's behalf. It will not do the thing that takes
    down the application they were being polite about.
    """

    def setUp(self) -> None:
        super().setUp()
        self.machine.level = LoadLevel.BUSY

    def test_it_runs_a_task_held_only_by_politeness(self) -> None:
        work = Work()
        task_id = self.submit(work)
        self.scheduler.tick()

        result = self.scheduler.override(task_id)
        self.scheduler.tick()

        self.assertTrue(result.accepted)
        self.assertEqual(work.runs, 1)

    def test_it_does_not_run_a_task_the_model_will_not_fit_for(self) -> None:
        # The measured state of the development machine: 1,906 MiB free while
        # the user's own work held the card.
        self.machine.free_vram_mb = 1_906
        work = Work()
        task_id = self.submit(work)
        self.scheduler.tick()

        result = self.scheduler.override(task_id)
        self.scheduler.tick()

        self.assertFalse(result.accepted)
        self.assertEqual(work.runs, 0)

    def test_and_says_why_in_numbers(self) -> None:
        self.machine.free_vram_mb = 1_906
        task_id = self.submit(Work())
        self.scheduler.tick()

        message = self.scheduler.override(task_id).message()

        self.assertIn("3482", message.replace(",", ""))
        self.assertIn("1906", message.replace(",", ""))
        self.assertIn("cannot be overridden", message)

    def test_a_refused_override_leaves_the_task_queued(self) -> None:
        self.machine.free_vram_mb = 1_906
        task_id = self.submit(Work())
        self.scheduler.tick()

        self.scheduler.override(task_id)

        self.assertIs(
            self.scheduler.queue.get(task_id).state,
            TaskState.QUEUED,
        )

    def test_a_refused_override_does_not_leave_a_flag_that_fires_later(
        self,
    ) -> None:
        # The dangerous version of this bug: the refusal is reported, the flag
        # is set anyway, and the work starts unannounced on the next tick.
        self.machine.free_vram_mb = 1_906
        work = Work()
        task_id = self.submit(work)
        self.scheduler.tick()
        self.scheduler.override(task_id)

        for _ in range(5):
            self.scheduler.tick()

        self.assertEqual(work.runs, 0)

    def test_it_starts_by_itself_once_the_card_frees(self) -> None:
        # A refusal must not require the user to press the button again.
        self.machine.free_vram_mb = 1_906
        work = Work()
        task_id = self.submit(work)
        self.scheduler.tick()
        self.scheduler.override(task_id)

        self.machine.free_vram_mb = 14_000
        self.machine.level = LoadLevel.FREE
        self.scheduler.tick()

        self.assertEqual(work.runs, 1)

    def test_the_floor_is_rechecked_between_pressing_it_and_starting(
        self,
    ) -> None:
        """
        The case the override's own check cannot cover.

        The user presses the button when the card has room, and by the time
        the scheduler comes round to start the work another application has
        taken that room. Checking only at the moment of pressing would run it
        anyway. This is why the floor is re-checked at admission and not
        trusted from a moment ago.
        """
        work = Work()
        task_id = self.submit(work)
        self.scheduler.tick()

        self.assertTrue(self.scheduler.override(task_id).accepted)

        # Somebody else takes the card in the meantime.
        self.machine.free_vram_mb = 900
        self.scheduler.tick()

        self.assertEqual(work.runs, 0)

    def test_and_says_the_floor_is_what_stopped_it(self) -> None:
        task_id = self.submit(Work())
        self.scheduler.tick()
        self.scheduler.override(task_id)

        self.machine.free_vram_mb = 900
        self.scheduler.tick()

        self.assertEqual(
            self.scheduler.view().tasks[0]["heldReason"],
            HoldReason.SAFETY_FLOOR.value,
        )

    def test_a_task_held_by_the_floor_is_not_offered_as_overridable(
        self,
    ) -> None:
        self.machine.free_vram_mb = 1_906
        self.submit(Work())

        self.scheduler.tick()

        self.assertFalse(self.scheduler.view().tasks[0]["overridable"])

    def test_the_refusal_is_announced_exactly_once(self) -> None:
        self.machine.free_vram_mb = 1_906
        task_id = self.submit(Work())
        self.scheduler.tick()

        self.scheduler.override(task_id)

        self.assertEqual(
            self.kinds().count(QueueEventType.OVERRIDE_REFUSED),
            1,
        )

    def test_a_hot_card_refuses_too(self) -> None:
        self.machine.gpu_temperature_c = 90
        work = Work()
        task_id = self.submit(work)
        self.scheduler.tick()

        result = self.scheduler.override(task_id)

        self.assertFalse(result.accepted)
        self.assertIs(result.floor.breach, FloorBreach.GPU_TEMPERATURE)

    def test_overriding_something_that_does_not_exist_is_not_a_crash(
        self,
    ) -> None:
        result = self.scheduler.override("q-nothing")

        self.assertFalse(result.accepted)


class TestCancelAndPause(SchedulerTestCase):
    def test_a_cancelled_task_never_runs(self) -> None:
        self.machine.level = LoadLevel.BUSY
        work = Work()
        task_id = self.submit(work)
        self.scheduler.tick()

        self.scheduler.cancel(task_id)
        self.machine.level = LoadLevel.FREE

        for _ in range(5):
            self.scheduler.tick()

        self.assertEqual(work.runs, 0)

    def test_pausing_holds_work_however_free_the_machine_is(self) -> None:
        work = Work()
        self.submit(work)

        self.scheduler.set_paused(True)
        self.scheduler.tick()

        self.assertEqual(work.runs, 0)

    def test_and_the_reason_says_so(self) -> None:
        self.submit(Work())
        self.scheduler.set_paused(True)

        self.scheduler.tick()

        self.assertEqual(
            self.scheduler.view().tasks[0]["heldReason"],
            HoldReason.PAUSED.value,
        )

    def test_resuming_releases_it(self) -> None:
        work = Work()
        self.submit(work)
        self.scheduler.set_paused(True)
        self.scheduler.tick()

        self.scheduler.set_paused(False)
        self.scheduler.tick()

        self.assertEqual(work.runs, 1)


class TestOneAtATime(SchedulerTestCase):
    def test_two_tasks_run_in_the_order_they_arrived(self) -> None:
        first, second = Work(), Work()
        self.submit(first)
        self.submit(second)

        self.scheduler.tick()
        self.assertEqual((first.runs, second.runs), (1, 0))

        self.scheduler.tick()
        self.assertEqual((first.runs, second.runs), (1, 1))

    def test_a_heavy_task_does_not_block_light_work_behind_it(self) -> None:
        # The governor allows one heavy owner. A heavy task that cannot start
        # must be skipped rather than stall the whole line.
        heavy = Work()
        light = Work()

        self.scheduler.submit(
            work=heavy,
            weight=Weight.HEAVY,
            summary="a long piece of reasoning",
            required_vram_mb=HEAVY_MB,
        )
        self.scheduler.submit(
            work=light,
            weight=Weight.LIGHT,
            summary="a quick lookup",
            required_vram_mb=FAST_MB,
        )

        self.scheduler.governor.request("someone-else", Weight.HEAVY)
        self.scheduler.tick()

        self.assertEqual((heavy.runs, light.runs), (0, 1))


class TestOneStuckTaskDoesNotStallTheRest(SchedulerTestCase):
    """
    Found end to end, and the reason it was worth running end to end.

    A task the safety floor refuses sits at the front of the queue forever,
    because the floor will go on refusing it. If the scheduler gives up for
    that tick, everything behind it waits for a condition that will never
    change. safe_queue.py already argues this case for heavy work — "letting
    one heavy task at the front stall every light task behind it would make
    the queue worse than no queue" — and it is just as true here.
    """

    def test_work_behind_a_floor_blocked_task_still_runs(self) -> None:
        blocked = Work()
        behind = Work()

        self.scheduler.submit(
            work=blocked,
            weight=Weight.LIGHT,
            summary="needs a card nobody has",
            required_vram_mb=900_000,
        )
        self.scheduler.submit(
            work=behind,
            weight=Weight.LIGHT,
            summary="needs nothing at all",
            required_vram_mb=0,
        )

        self.scheduler.tick()

        self.assertEqual(behind.runs, 1)
        self.assertEqual(blocked.runs, 0)

    def test_the_blocked_one_is_still_reported_as_blocked(self) -> None:
        # Skipping it must not mean forgetting to say why it is waiting.
        self.scheduler.submit(
            work=Work(),
            weight=Weight.LIGHT,
            summary="needs a card nobody has",
            required_vram_mb=900_000,
        )
        self.scheduler.submit(
            work=Work(),
            weight=Weight.LIGHT,
            summary="needs nothing at all",
            required_vram_mb=0,
        )

        self.scheduler.tick()

        stuck = self.scheduler.view().tasks[0]

        self.assertEqual(stuck["heldReason"], HoldReason.SAFETY_FLOOR.value)

    def test_several_blocked_tasks_do_not_stall_it_either(self) -> None:
        for index in range(4):
            self.scheduler.submit(
                work=Work(),
                weight=Weight.LIGHT,
                summary=f"impossible {index}",
                required_vram_mb=900_000,
            )

        behind = Work()
        self.scheduler.submit(
            work=behind,
            weight=Weight.LIGHT,
            summary="the one that can run",
            required_vram_mb=0,
        )

        self.scheduler.tick()

        self.assertEqual(behind.runs, 1)


class TestTheGovernorDoesNotOverruleTheQueue(unittest.TestCase):
    """
    The livelock, pinned down.

    The queue judges the machine over a window, because one reading of a
    working machine is close to meaningless. The governor used to take its
    own instantaneous reading when asked for a reservation, and on a loaded
    machine it refused every time — so the queue admitted a task, the governor
    refused it, the task went back, and around it went. Observed end to end:
    nothing could ever run.

    What the governor still decides is its own business — one heavy owner at a
    time — and no caller may waive that.
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.machine = Machine(self.clock, level=LoadLevel.FREE)

        events: list[QueueEvent] = []

        # A governor whose own readings say the machine is hammered.
        self.scheduler = QueueScheduler(
            queue=SafeQueue(clock=self.clock),
            governor=ResourceGovernor(
                read_system=lambda: SystemStatus(
                    cpu_usage_percent=99.0,
                    ram_usage_percent=97.0,
                    ram_used_gb=15.0,
                    ram_total_gb=16.0,
                ),
                read_gpu=lambda: GpuStatus(
                    name="RTX 5080",
                    temperature_c=84,
                    gpu_utilization_percent=100,
                    vram_used_mb=16_000,
                    vram_total_mb=CARD_MB,
                ),
                clock=self.clock,
            ),
            monitor=self.machine,  # type: ignore[arg-type]
            notify=events.append,
            clock=self.clock,
        )

    def test_the_queues_verdict_is_the_one_that_counts(self) -> None:
        work = Work()
        self.scheduler.submit(
            work=work,
            weight=Weight.LIGHT,
            summary="something",
            required_vram_mb=0,
        )

        self.scheduler.tick()

        self.assertEqual(work.runs, 1)

    def test_it_does_not_spin(self) -> None:
        work = Work()
        task_id = self.scheduler.submit(
            work=work,
            weight=Weight.LIGHT,
            summary="something",
            required_vram_mb=0,
        ).task_id

        for _ in range(10):
            self.scheduler.tick()

        self.assertEqual(work.runs, 1)
        self.assertLessEqual(
            self.scheduler.queue.get(task_id).attempts,
            1,
        )

    def test_but_one_heavy_task_at_a_time_still_holds(self) -> None:
        # The rule the governor exists for. A caller may tell it what it
        # thinks of the machine; it may not tell it to hand out two heavy
        # reservations.
        # The verdict has to be supplied here too, or this governor refuses
        # the setup for the same reason it would refuse everything else, and
        # the test would be asserting against a reservation that was never
        # made — which is exactly what it did on the first run.
        grant = self.scheduler.governor.request(
            "somebody-else",
            Weight.HEAVY,
            decision=ResourceDecision.ALLOW,
        )
        self.assertTrue(grant.granted, "the setup did not take the lease")

        heavy = Work()
        self.scheduler.submit(
            work=heavy,
            weight=Weight.HEAVY,
            summary="a long think",
            required_vram_mb=0,
        )

        self.scheduler.tick()

        self.assertEqual(heavy.runs, 0)


class TestItSaysWhatItIsDoing(SchedulerTestCase):
    def test_submitting_announces_the_task(self) -> None:
        self.submit(Work())

        self.assertIn(QueueEventType.QUEUED, self.kinds())

    def test_running_announces_a_start_and_a_finish(self) -> None:
        self.submit(Work())
        self.scheduler.tick()

        self.assertIn(QueueEventType.STARTED, self.kinds())
        self.assertIn(QueueEventType.FINISHED, self.kinds())

    def test_a_quiet_tick_says_nothing(self) -> None:
        # The pump runs every couple of seconds for as long as work waits.
        # Emitting per tick would flood a pipe the voice spectrum already
        # keeps busy, and would strobe the interface.
        self.machine.level = LoadLevel.BUSY
        self.submit(Work())
        self.scheduler.tick()

        before = len(self.events)
        for _ in range(5):
            self.scheduler.tick()

        self.assertEqual(len(self.events), before)

    def test_a_held_task_does_not_change_just_by_waiting(self) -> None:
        """
        The bug an end-to-end run found, pinned down where it is provable.

        The reason a task is held used to include how long it had been held,
        so the wording differed every tick, so the queue looked different
        every tick, so the interface was told twenty times in five seconds
        that nothing had happened. How long it has been waiting belongs in a
        field of its own, not inside the sentence.
        """
        self.machine.level = LoadLevel.BUSY
        self.submit(Work())

        self.scheduler.tick()
        first = self.scheduler.view().tasks[0]["detail"]

        for _ in range(10):
            self.clock.advance(2.0)
            self.scheduler.tick()

        self.assertEqual(
            self.scheduler.view().tasks[0]["detail"],
            first,
        )

    def test_and_so_the_revision_does_not_move_either(self) -> None:
        self.machine.level = LoadLevel.BUSY
        self.submit(Work())
        self.scheduler.tick()

        before = self.scheduler.view().revision

        for _ in range(10):
            self.clock.advance(2.0)
            self.scheduler.tick()

        self.assertEqual(self.scheduler.view().revision, before)

    def test_the_level_is_announced_when_it_changes(self) -> None:
        self.machine.level = LoadLevel.BUSY
        self.submit(Work())
        self.scheduler.tick()

        self.machine.level = LoadLevel.FREE
        self.scheduler.tick()

        levels = [
            event.level
            for event in self.events
            if event.event is QueueEventType.HOLD_STATE
        ]

        self.assertIn(LoadLevel.BUSY, levels)
        self.assertIn(LoadLevel.FREE, levels)

    def test_a_listener_that_throws_does_not_stop_the_queue(self) -> None:
        scheduler, _ = a_scheduler(self.machine, self.clock)
        scheduler._notify = lambda event: (_ for _ in ()).throw(ValueError())
        work = Work()

        scheduler.submit(
            work=work,
            weight=Weight.LIGHT,
            summary="something",
            required_vram_mb=FAST_MB,
        )
        scheduler.tick()

        self.assertEqual(work.runs, 1)

    def test_every_change_moves_the_revision_forward(self) -> None:
        # The interface drops any snapshot older than the one it has. A
        # revision that ever went backwards would freeze the display.
        seen = [self.scheduler.view().revision]

        self.submit(Work())
        seen.append(self.scheduler.view().revision)

        self.scheduler.tick()
        seen.append(self.scheduler.view().revision)

        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(set(seen)), len(seen))


class TestTheSpinBackstop(SchedulerTestCase):
    def test_a_task_that_keeps_being_pulled_back_is_paused(self) -> None:
        # If the scheduler and something downstream disagree about whether the
        # machine is usable, the task would be admitted and pulled back
        # forever, loading a model each pass. A visible stall beats that.
        task_id = self.submit(Work())

        for _ in range(4):
            task = self.scheduler.queue.get(task_id)

            if task.state is TaskState.RUNNING:
                self.scheduler.queue.requeue(task_id)

            self.scheduler.tick()

        self.assertIn(
            self.scheduler.queue.get(task_id).state,
            (TaskState.PAUSED, TaskState.DONE),
        )


class TestNotSchedulingWhenThereIsNothingToSchedule(SchedulerTestCase):
    def test_an_empty_queue_is_idle(self) -> None:
        # This is what stops an idle Qronos launching nvidia-smi forever.
        self.assertTrue(self.scheduler._idle())

    def test_a_waiting_task_is_not_idle(self) -> None:
        self.machine.level = LoadLevel.BUSY
        self.submit(Work())

        self.assertFalse(self.scheduler._idle())


class TestTheThread(unittest.TestCase):
    """The only tests here that start anything."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.machine = Machine(self.clock)
        self.scheduler, _ = a_scheduler(
            self.machine,
            self.clock,
            config=SchedulerConfig(tick_seconds=0.05),
        )

    def tearDown(self) -> None:
        self.scheduler.stop()

    def test_starting_and_stopping_is_clean(self) -> None:
        self.scheduler.start()
        self.assertTrue(self.scheduler.running)

        self.scheduler.stop()

        self.assertFalse(self.scheduler.running)

    def test_starting_twice_does_not_make_two_threads(self) -> None:
        before = threading.active_count()

        self.scheduler.start()
        self.scheduler.start()

        self.assertEqual(threading.active_count(), before + 1)

    def test_stopping_without_starting_is_harmless(self) -> None:
        self.scheduler.stop()

    def test_it_actually_runs_queued_work(self) -> None:
        done = threading.Event()

        class Signalling(Work):
            def run(self) -> bool:
                done.set()
                return True

        self.scheduler.start()
        self.scheduler.submit(
            work=Signalling(),
            weight=Weight.LIGHT,
            summary="prove the thread works",
            required_vram_mb=FAST_MB,
        )

        self.assertTrue(done.wait(timeout=10.0))


if __name__ == "__main__":
    unittest.main()
