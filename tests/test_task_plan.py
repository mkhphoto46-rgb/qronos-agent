from __future__ import annotations

import unittest

from core.task_plan import PlanStep, TaskPlan
from core.task_router import TaskType


class TestTaskPlan(unittest.TestCase):
    def test_new_plan_is_empty(self) -> None:
        plan = TaskPlan(goal="Test goal")

        self.assertTrue(plan.is_empty)
        self.assertEqual(plan.steps, [])

    def test_add_step(self) -> None:
        plan = TaskPlan(goal="Test goal")

        plan.add_step(
            TaskType.FAST,
            "Handle the request quickly.",
        )

        self.assertFalse(plan.is_empty)
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].order, 1)
        self.assertEqual(plan.steps[0].task_type, TaskType.FAST)

    def test_steps_are_ordered(self) -> None:
        plan = TaskPlan(goal="Test goal")

        plan.add_step(TaskType.BROWSER, "Use browser.")
        plan.add_step(TaskType.VISION, "Analyze image.")
        plan.add_step(TaskType.HEAVY, "Reason about results.")

        self.assertEqual(
            [step.order for step in plan.steps],
            [1, 2, 3],
        )

        self.assertEqual(
            [step.task_type for step in plan.steps],
            [
                TaskType.BROWSER,
                TaskType.VISION,
                TaskType.HEAVY,
            ],
        )

    def test_plan_step_fields(self) -> None:
        step = PlanStep(
            order=1,
            task_type=TaskType.COMPUTER,
            description="Open Premiere.",
        )

        self.assertEqual(step.order, 1)
        self.assertEqual(step.task_type, TaskType.COMPUTER)
        self.assertEqual(step.description, "Open Premiere.")


if __name__ == "__main__":
    unittest.main()