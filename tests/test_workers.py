from __future__ import annotations

import unittest

from core.task_plan import PlanStep
from core.task_router import TaskType
from core.workers import (
    TaskWorker,
    Unavailable,
    UnavailableReason,
    WorkerOutput,
    WorkerRegistry,
)


class GoodWorker(TaskWorker):
    task_type = TaskType.VISION

    def health_check(self) -> bool:
        return True

    def execute(self, step: PlanStep) -> WorkerOutput:
        return WorkerOutput(output="done")


class UnhealthyWorker(TaskWorker):
    task_type = TaskType.COMPUTER

    def health_check(self) -> bool:
        return False

    def execute(self, step: PlanStep) -> WorkerOutput:  # pragma: no cover
        raise AssertionError("must not be reached")


class ExplodingHealthCheck(TaskWorker):
    task_type = TaskType.BROWSER

    def health_check(self) -> bool:
        raise OSError("the driver is missing")

    def execute(self, step: PlanStep) -> WorkerOutput:  # pragma: no cover
        raise AssertionError("must not be reached")


class TestRegistration(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = WorkerRegistry()

    def test_a_registry_starts_empty(self) -> None:
        # An orchestrator built without a registry has to behave exactly as it
        # did before this existed.
        self.assertEqual(self.registry.registered(), frozenset())

    def test_a_worker_can_be_found_by_task_type(self) -> None:
        worker = GoodWorker()
        self.registry.register(worker)

        self.assertIs(self.registry.worker_for(TaskType.VISION), worker)

    def test_a_worker_without_a_task_type_is_refused(self) -> None:
        class Nameless(TaskWorker):
            def health_check(self) -> bool:
                return True

            def execute(self, step: PlanStep) -> WorkerOutput:
                return WorkerOutput(output="")

        with self.assertRaises(ValueError):
            self.registry.register(Nameless())

    def test_two_workers_cannot_claim_the_same_task_type(self) -> None:
        # Silently replacing the first would make which worker runs depend on
        # import order.
        self.registry.register(GoodWorker())

        with self.assertRaises(ValueError):
            self.registry.register(GoodWorker())


class TestAvailability(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = WorkerRegistry()

    def test_an_unregistered_type_is_not_implemented(self) -> None:
        unavailable = self.registry.availability(TaskType.VISION)

        self.assertIs(
            unavailable.reason,
            UnavailableReason.NOT_IMPLEMENTED,
        )

    def test_a_healthy_worker_is_available(self) -> None:
        self.registry.register(GoodWorker())

        self.assertIsNone(self.registry.availability(TaskType.VISION))

    def test_an_unhealthy_worker_is_not_installed(self) -> None:
        self.registry.register(UnhealthyWorker())

        self.assertIs(
            self.registry.availability(TaskType.COMPUTER).reason,
            UnavailableReason.NOT_INSTALLED,
        )

    def test_a_raising_health_check_is_not_installed(self) -> None:
        # A health check that raises is a worker that is not ready, not a
        # crash to propagate into the caller's step. It is also the most
        # likely shape of "the model file is missing".
        self.registry.register(ExplodingHealthCheck())

        unavailable = self.registry.availability(TaskType.BROWSER)

        self.assertIs(
            unavailable.reason,
            UnavailableReason.NOT_INSTALLED,
        )
        self.assertIn("driver is missing", unavailable.message())


class TestUnavailableMessages(unittest.TestCase):
    def test_every_reason_renders_a_sentence(self) -> None:
        for reason in UnavailableReason:
            with self.subTest(reason=reason):
                message = Unavailable(
                    task_type=TaskType.VISION,
                    reason=reason,
                ).message()

                self.assertTrue(message)

    def test_the_reason_is_the_code_not_the_sentence(self) -> None:
        # The sentence will be translated into Persian. Nothing may branch on
        # it, which is the whole reason this type exists.
        unavailable = Unavailable(
            task_type=TaskType.VISION,
            reason=UnavailableReason.NOT_IMPLEMENTED,
        )

        self.assertIs(
            unavailable.reason,
            UnavailableReason.NOT_IMPLEMENTED,
        )

    def test_a_detail_overrides_the_generic_sentence(self) -> None:
        unavailable = Unavailable(
            task_type=TaskType.VISION,
            reason=UnavailableReason.NOT_INSTALLED,
            detail="Qwen3-VL was never downloaded.",
        )

        self.assertEqual(
            unavailable.message(),
            "Qwen3-VL was never downloaded.",
        )


class TestTheInterfaceIsAbstract(unittest.TestCase):
    def test_a_worker_must_implement_both_methods(self) -> None:
        class Incomplete(TaskWorker):
            task_type = TaskType.VISION

            def health_check(self) -> bool:
                return True

        with self.assertRaises(TypeError):
            Incomplete()  # type: ignore[abstract]


if __name__ == "__main__":
    unittest.main()
