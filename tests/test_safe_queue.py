from __future__ import annotations

import unittest

from core.resource_governor import Weight
from core.safe_queue import (
    IllegalTransition,
    SafeQueue,
    TaskState,
    UnknownTask,
)


class FakeClock:
    def __init__(self, initial_time: float = 1000.0) -> None:
        self.current_time = initial_time

    def __call__(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


class TestSubmission(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = SafeQueue(clock=FakeClock())

    def test_a_submitted_task_waits(self) -> None:
        task = self.queue.submit("t1", Weight.LIGHT, "answer a question")

        self.assertIs(task.state, TaskState.QUEUED)
        self.assertTrue(task.waiting)

    def test_a_task_needs_an_id(self) -> None:
        with self.assertRaises(ValueError):
            self.queue.submit("  ", Weight.LIGHT, "x")

    def test_the_same_id_cannot_be_submitted_twice(self) -> None:
        # Two tasks with one id would make cancel ambiguous, which is the worst
        # place for ambiguity in this component.
        self.queue.submit("t1", Weight.LIGHT, "x")

        with self.assertRaises(ValueError):
            self.queue.submit("t1", Weight.LIGHT, "y")

    def test_an_unknown_task_is_reported(self) -> None:
        with self.assertRaises(UnknownTask):
            self.queue.get("nope")


class TestOrdering(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = SafeQueue(clock=FakeClock())
        self.queue.submit("first", Weight.LIGHT, "1")
        self.queue.submit("second", Weight.LIGHT, "2")
        self.queue.submit("third", Weight.LIGHT, "3")

    def test_the_line_is_first_in_first_out(self) -> None:
        self.assertEqual(self.queue.next_ready().task_id, "first")

    def test_waiting_lists_in_order(self) -> None:
        self.assertEqual(
            [task.task_id for task in self.queue.waiting()],
            ["first", "second", "third"],
        )

    def test_taking_one_advances_the_line(self) -> None:
        self.queue.start("first")

        self.assertEqual(self.queue.next_ready().task_id, "second")

    def test_an_empty_line_offers_nothing(self) -> None:
        self.assertIsNone(SafeQueue().next_ready())


class TestHeavyWorkIsSkippedNotBlocking(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = SafeQueue(clock=FakeClock())
        self.queue.submit("render", Weight.HEAVY, "render the video")
        self.queue.submit("answer", Weight.LIGHT, "answer a question")

    def test_heavy_work_is_taken_when_allowed(self) -> None:
        self.assertEqual(self.queue.next_ready().task_id, "render")

    def test_light_work_flows_past_blocked_heavy_work(self) -> None:
        # Letting one heavy task at the front stall every light task behind it
        # would make the queue worse than no queue at all.
        self.assertEqual(
            self.queue.next_ready(heavy_allowed=False).task_id,
            "answer",
        )

    def test_heavy_work_keeps_its_place_while_skipped(self) -> None:
        self.queue.start(
            self.queue.next_ready(heavy_allowed=False).task_id
        )

        self.assertEqual(self.queue.next_ready().task_id, "render")


class TestCancellation(unittest.TestCase):
    """
    The invariant that matters most: a cancelled task never runs.

    Every other property of a queue is a convenience. This one is a promise to
    the user, made at the moment they press stop.
    """

    def setUp(self) -> None:
        self.queue = SafeQueue(clock=FakeClock())

    def test_a_cancelled_task_is_never_offered(self) -> None:
        self.queue.submit("t1", Weight.LIGHT, "x")
        self.queue.cancel("t1")

        self.assertIsNone(self.queue.next_ready())

    def test_a_cancelled_task_cannot_be_started(self) -> None:
        self.queue.submit("t1", Weight.LIGHT, "x")
        self.queue.cancel("t1")

        with self.assertRaises(IllegalTransition):
            self.queue.start("t1")

    def test_a_running_task_can_be_cancelled(self) -> None:
        # A user who asked to stop should not have to wait for a state they
        # cannot see.
        self.queue.submit("t1", Weight.LIGHT, "x")
        self.queue.start("t1")

        self.assertIs(
            self.queue.cancel("t1").state,
            TaskState.CANCELLED,
        )

    def test_a_paused_task_can_be_cancelled(self) -> None:
        self.queue.submit("t1", Weight.LIGHT, "x")
        self.queue.pause("t1")

        self.assertIs(
            self.queue.cancel("t1").state,
            TaskState.CANCELLED,
        )

    def test_cancellation_is_final(self) -> None:
        self.queue.submit("t1", Weight.LIGHT, "x")
        self.queue.cancel("t1")

        for move in (
            self.queue.resume,
            self.queue.start,
            self.queue.pause,
            self.queue.cancel,
        ):
            with self.subTest(move=move.__name__):
                with self.assertRaises(IllegalTransition):
                    move("t1")

    def test_cancelling_is_not_failing(self) -> None:
        # Nothing went wrong and nothing should be retried. Conflating the two
        # is how a user's stop turns into a retry loop.
        self.queue.submit("t1", Weight.LIGHT, "x")
        self.queue.cancel("t1")

        self.assertIsNot(self.queue.get("t1").state, TaskState.FAILED)


class TestPausing(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = SafeQueue(clock=FakeClock())
        self.queue.submit("t1", Weight.LIGHT, "x")

    def test_a_paused_task_is_not_offered(self) -> None:
        self.queue.pause("t1")

        self.assertIsNone(self.queue.next_ready())

    def test_a_paused_task_stays_paused_however_free_the_machine(
        self,
    ) -> None:
        # Held by the user, not by the machine. Only the user lifts it.
        self.queue.pause("t1")

        self.assertIsNone(self.queue.next_ready(heavy_allowed=True))

    def test_resuming_puts_it_back_in_line(self) -> None:
        self.queue.pause("t1")
        self.queue.resume("t1")

        self.assertEqual(self.queue.next_ready().task_id, "t1")

    def test_a_running_task_cannot_be_paused_directly(self) -> None:
        # It is requeued instead: pausing something mid-execution would leave
        # the queue believing it is held while it is still running.
        self.queue.start("t1")

        with self.assertRaises(IllegalTransition):
            self.queue.pause("t1")


class TestRequeueing(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = SafeQueue(clock=FakeClock())

    def test_a_requeued_task_waits_again(self) -> None:
        self.queue.submit("t1", Weight.HEAVY, "x")
        self.queue.start("t1")
        self.queue.requeue("t1")

        self.assertTrue(self.queue.get("t1").waiting)

    def test_a_requeued_task_keeps_its_place(self) -> None:
        # Sending it to the back would let a busy machine starve the one task
        # that keeps getting interrupted.
        self.queue.submit("first", Weight.HEAVY, "1")
        self.queue.submit("second", Weight.LIGHT, "2")

        self.queue.start("first")
        self.queue.requeue("first")

        self.assertEqual(self.queue.next_ready().task_id, "first")

    def test_attempts_are_counted(self) -> None:
        # A task pulled back repeatedly is a task something is wrong with, and
        # this is what makes that visible rather than letting it spin.
        self.queue.submit("t1", Weight.HEAVY, "x")

        for _ in range(3):
            self.queue.start("t1")
            self.queue.requeue("t1")

        self.assertEqual(self.queue.get("t1").attempts, 3)


class TestTransitionsAreTotal(unittest.TestCase):
    def test_every_state_declares_what_may_follow_it(self) -> None:
        from core.safe_queue import _ALLOWED

        for state in TaskState:
            with self.subTest(state=state):
                self.assertIn(state, _ALLOWED)

    def test_terminal_states_allow_nothing(self) -> None:
        from core.safe_queue import _ALLOWED

        for state in (
            TaskState.DONE,
            TaskState.FAILED,
            TaskState.CANCELLED,
        ):
            with self.subTest(state=state):
                self.assertEqual(_ALLOWED[state], frozenset())

    def test_an_illegal_move_is_refused_not_ignored(self) -> None:
        # A silently dropped transition leaves the caller believing something
        # happened, and this is exactly the component where that produces a
        # task nobody is watching.
        queue = SafeQueue()
        queue.submit("t1", Weight.LIGHT, "x")
        queue.start("t1")
        queue.finish("t1")

        with self.assertRaises(IllegalTransition):
            queue.start("t1")

    def test_history_records_where_a_task_has_been(self) -> None:
        queue = SafeQueue()
        queue.submit("t1", Weight.LIGHT, "x")
        queue.start("t1")
        queue.finish("t1")

        self.assertEqual(
            queue.get("t1").history,
            [TaskState.QUEUED, TaskState.RUNNING],
        )


class TestFinishing(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = SafeQueue(clock=FakeClock())
        self.queue.submit("t1", Weight.LIGHT, "x")
        self.queue.start("t1")

    def test_success_and_failure_are_distinct(self) -> None:
        self.queue.finish("t1", success=False)

        self.assertIs(self.queue.get("t1").state, TaskState.FAILED)

    def test_a_finished_task_leaves_the_line(self) -> None:
        self.queue.finish("t1")

        self.assertEqual(self.queue.waiting(), ())
        self.assertEqual(self.queue.running(), ())

    def test_finished_tasks_can_be_forgotten(self) -> None:
        # The queue is not a history; the audit trail is. Keeping finished
        # tasks forever turns an in-memory structure into an unbounded one.
        self.queue.finish("t1")
        self.queue.submit("t2", Weight.LIGHT, "y")

        self.assertEqual(self.queue.forget_finished(), 1)
        self.assertEqual(len(self.queue.all_tasks()), 1)


if __name__ == "__main__":
    unittest.main()
