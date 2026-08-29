from __future__ import annotations

import unittest
from unittest.mock import patch

from core.activity_guard import ActivityMode, ResourcePressure
from core.model_manager import TaskClass
from core.model_registry import get_model
from core.orchestrator import Orchestrator
from core.resource_guard import GpuStatus, SystemStatus
from core.resource_policy import ResourceDecision
from core.task_plan import TaskPlan
from core.task_router import TaskType
from core.workers import (
    TaskWorker,
    UnavailableReason,
    WorkerOutput,
    WorkerRegistry,
)


class TestOrchestrator(unittest.TestCase):
    def setUp(self) -> None:
        self.orchestrator = Orchestrator()

    def _safe_system(self) -> SystemStatus:
        return SystemStatus(
            cpu_usage_percent=20.0,
            ram_usage_percent=40.0,
            ram_used_gb=12.0,
            ram_total_gb=31.9,
        )

    def _safe_gpu(self) -> GpuStatus:
        return GpuStatus(
            name="NVIDIA GeForce RTX 3070 Ti",
            temperature_c=50,
            gpu_utilization_percent=10,
            vram_used_mb=2000,
            vram_total_mb=8192,
        )

    def _make_state(
        self,
        mode: ActivityMode,
        pressure: ResourcePressure,
    ):
        return type(
            "State",
            (),
            {
                "mode": mode,
                "resource_pressure": pressure,
            },
        )

    def test_empty_plan_returns_no_results(self) -> None:
        plan = TaskPlan(goal="Empty test")

        results = self.orchestrator.execute_plan(plan)

        self.assertEqual(results, [])

    def test_unsupported_task_fails_cleanly(self) -> None:
        plan = TaskPlan(goal="Unsupported task test")

        plan.add_step(
            TaskType.VISION,
            "Analyze this image.",
        )

        results = self.orchestrator.execute_plan(plan)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)

        # Asserted on the reason code rather than the sentence. This test used
        # to match the substring "not implemented", which is why the message
        # could not be reworded or translated into Persian without breaking
        # it — and why nothing else could distinguish a missing capability
        # from a broken one either.
        self.assertIsNotNone(results[0].unavailable)
        self.assertIs(
            results[0].unavailable.reason,
            UnavailableReason.NOT_IMPLEMENTED,
        )
        self.assertIs(
            results[0].unavailable.task_type,
            TaskType.VISION,
        )

    def test_every_unbuilt_task_type_reports_itself(self) -> None:
        for task_type in (
            TaskType.VISION,
            TaskType.COMPUTER,
            TaskType.BROWSER,
        ):
            with self.subTest(task_type=task_type):
                plan = TaskPlan(goal="Unbuilt task test")
                plan.add_step(task_type, "Do the thing.")

                result = self.orchestrator.execute_plan(plan)[0]

                self.assertIs(
                    result.unavailable.reason,
                    UnavailableReason.NOT_IMPLEMENTED,
                )

    def test_a_registered_worker_runs_the_step(self) -> None:
        class FakeVisionWorker(TaskWorker):
            task_type = TaskType.VISION

            def health_check(self) -> bool:
                return True

            def execute(self, step) -> WorkerOutput:
                return WorkerOutput(output=f"saw: {step.description}")

        registry = WorkerRegistry()
        registry.register(FakeVisionWorker())

        plan = TaskPlan(goal="Worker test")
        plan.add_step(TaskType.VISION, "a cat")

        result = Orchestrator(workers=registry).execute_plan(plan)[0]

        self.assertTrue(result.success)
        self.assertEqual(result.output, "saw: a cat")
        self.assertIsNone(result.unavailable)

    def test_an_unhealthy_worker_reports_not_installed(self) -> None:
        # A worker that exists but cannot run sends the user somewhere
        # completely different from one that was never built: install the
        # missing piece, rather than wait for the feature.
        class UninstalledWorker(TaskWorker):
            task_type = TaskType.VISION

            def health_check(self) -> bool:
                return False

            def execute(self, step) -> WorkerOutput:
                raise AssertionError("must not be reached")

        registry = WorkerRegistry()
        registry.register(UninstalledWorker())

        plan = TaskPlan(goal="Worker test")
        plan.add_step(TaskType.VISION, "a cat")

        result = Orchestrator(workers=registry).execute_plan(plan)[0]

        self.assertIs(
            result.unavailable.reason,
            UnavailableReason.NOT_INSTALLED,
        )

    def test_a_raising_worker_does_not_escape_the_step(self) -> None:
        class BrokenWorker(TaskWorker):
            task_type = TaskType.BROWSER

            def health_check(self) -> bool:
                return True

            def execute(self, step) -> WorkerOutput:
                raise OSError("the browser vanished")

        registry = WorkerRegistry()
        registry.register(BrokenWorker())

        plan = TaskPlan(goal="Worker test")
        plan.add_step(TaskType.BROWSER, "open a page")

        result = Orchestrator(workers=registry).execute_plan(plan)[0]

        self.assertFalse(result.success)
        self.assertIn("the browser vanished", result.error or "")
        # A worker that broke is not a worker that is missing.
        self.assertIsNone(result.unavailable)

    def test_fast_two_step_plan_succeeds(self) -> None:
        plan = TaskPlan(goal="Two step test")

        plan.add_step(
            TaskType.FAST,
            "Reply with exactly: TEST_STEP_ONE",
        )

        plan.add_step(
            TaskType.FAST,
            "Reply with exactly: TEST_STEP_TWO",
        )

        with patch(
            "core.orchestrator.read_system_status",
            return_value=self._safe_system(),
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=self._safe_gpu(),
        ), patch.object(
            self.orchestrator.activity_guard,
            "detect",
        ) as mock_detect, patch.object(
            self.orchestrator.ollama,
            "chat",
            side_effect=[
                "TEST_STEP_ONE",
                "TEST_STEP_TWO",
            ],
        ) as mock_chat:

            mock_detect.return_value = self._make_state(
                ActivityMode.NORMAL,
                ResourcePressure.NORMAL,
            )

            results = self.orchestrator.execute_plan(plan)

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].success)
        self.assertTrue(results[1].success)

        self.assertIn(
            "TEST_STEP_ONE",
            results[0].output,
        )

        self.assertIn(
            "TEST_STEP_TWO",
            results[1].output,
        )

        self.assertEqual(
            mock_chat.call_count,
            2,
        )

    def test_critical_pressure_blocks_heavy_model(self) -> None:
        plan = TaskPlan(goal="Critical resource test")

        plan.add_step(
            TaskType.HEAVY,
            "This must not run.",
        )

        with patch(
            "core.orchestrator.read_system_status",
            return_value=self._safe_system(),
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=self._safe_gpu(),
        ), patch.object(
            self.orchestrator.activity_guard,
            "detect",
        ) as mock_detect, patch.object(
            self.orchestrator.ollama,
            "chat",
        ) as mock_chat, patch.object(
            self.orchestrator.ollama,
            "list_running_models",
            return_value=[],
        ) as mock_list_running:

            mock_detect.return_value = self._make_state(
                ActivityMode.NORMAL,
                ResourcePressure.CRITICAL,
            )

            results = self.orchestrator.execute_plan(plan)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)

        self.assertIn(
            "blocked",
            results[0].error.lower(),
        )

        mock_chat.assert_not_called()
        mock_list_running.assert_called_once()

    def test_high_pressure_blocks_and_does_not_run_heavy_model(self) -> None:
        plan = TaskPlan(goal="High pressure test")

        plan.add_step(
            TaskType.HEAVY,
            "This should wait for approval.",
        )

        with patch(
            "core.orchestrator.read_system_status",
            return_value=self._safe_system(),
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=self._safe_gpu(),
        ), patch.object(
            self.orchestrator.activity_guard,
            "detect",
        ) as mock_detect, patch.object(
            self.orchestrator.ollama,
            "chat",
        ) as mock_chat, patch.object(
            self.orchestrator.ollama,
            "list_running_models",
            return_value=[],
        ) as mock_list_running:

            mock_detect.return_value = self._make_state(
                ActivityMode.NORMAL,
                ResourcePressure.HIGH,
            )

            results = self.orchestrator.execute_plan(plan)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)

        self.assertIn(
            "block",
            results[0].error.lower(),
        )

        mock_chat.assert_not_called()
        mock_list_running.assert_called_once()

    def test_fresh_second_check_blocks_model_load(self) -> None:
        plan = TaskPlan(goal="Fresh resource check test")
        plan.add_step(TaskType.FAST, "This must not load.")

        with patch(
            "core.orchestrator.read_system_status",
            return_value=self._safe_system(),
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=self._safe_gpu(),
        ), patch.object(
            self.orchestrator.activity_guard,
            "detect",
        ) as mock_detect, patch.object(
            self.orchestrator.ollama,
            "chat",
        ) as mock_chat, patch.object(
            self.orchestrator.ollama,
            "list_running_models",
            return_value=[],
        ):
            mock_detect.side_effect = [
                self._make_state(
                    ActivityMode.NORMAL,
                    ResourcePressure.NORMAL,
                ),
                self._make_state(
                    ActivityMode.NORMAL,
                    ResourcePressure.HIGH,
                ),
            ]

            results = self.orchestrator.execute_plan(plan)

        self.assertFalse(results[0].success)
        self.assertIn("block", results[0].error.lower())
        mock_chat.assert_not_called()

    def test_the_fast_brain_is_unloaded_even_when_nothing_is_wrong(self) -> None:
        """
        The one case that used to keep a model in VRAM, and no longer does.

        Normal activity, normal pressure, a short Fast turn: the conditions
        the old ten-minute keep-alive was written for. Qronos now gives the
        card back after every answer without exception, so this asserts the
        absence of a special case rather than the presence of one.
        """
        plan = TaskPlan(goal="Fast brain unload test")

        plan.add_step(
            TaskType.FAST,
            "Reply with exactly: WARM",
        )

        with patch(
            "core.orchestrator.read_system_status",
            return_value=self._safe_system(),
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=self._safe_gpu(),
        ), patch.object(
            self.orchestrator.activity_guard,
            "detect",
        ) as mock_detect, patch.object(
            self.orchestrator.ollama,
            "chat",
            return_value="WARM",
        ) as mock_chat, patch.object(
            self.orchestrator.ollama,
            "stop_model",
        ) as mock_stop:

            mock_detect.return_value = self._make_state(
                ActivityMode.NORMAL,
                ResourcePressure.NORMAL,
            )

            results = self.orchestrator.execute_plan(plan)

        self.assertTrue(results[0].success)

        self.assertEqual(
            mock_chat.call_args.kwargs["keep_alive"],
            "0",
        )
        self.assertFalse(
            mock_chat.call_args.kwargs["think"],
        )
        self.assertEqual(
            mock_chat.call_args.kwargs["num_predict"],
            256,
        )

        mock_stop.assert_called_once_with(
            "qwen3:4b-instruct",
        )

    def test_normal_heavy_brain_thinks_and_unloads(self) -> None:
        plan = TaskPlan(goal="Heavy reasoning test")

        plan.add_step(
            TaskType.HEAVY,
            "Analyze this request deeply.",
        )

        with patch(
            "core.orchestrator.read_system_status",
            return_value=self._safe_system(),
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=self._safe_gpu(),
        ), patch.object(
            self.orchestrator.activity_guard,
            "detect",
        ) as mock_detect, patch.object(
            self.orchestrator.ollama,
            "chat",
            return_value="HEAVY_RESULT",
        ) as mock_chat, patch.object(
            self.orchestrator.ollama,
            "stop_model",
        ) as mock_stop:

            mock_detect.return_value = self._make_state(
                ActivityMode.NORMAL,
                ResourcePressure.NORMAL,
            )

            results = self.orchestrator.execute_plan(plan)

        self.assertTrue(results[0].success)
        self.assertEqual(
            mock_chat.call_args.kwargs["model_name"],
            "qwen3:14b",
        )
        self.assertTrue(
            mock_chat.call_args.kwargs["think"],
        )
        self.assertEqual(
            mock_chat.call_args.kwargs["num_predict"],
            512,
        )
        self.assertEqual(
            mock_chat.call_args.kwargs["keep_alive"],
            "0",
        )
        mock_stop.assert_called_once_with(
            "qwen3:14b",
        )

    def test_gaming_fast_brain_is_on_demand(self) -> None:
        plan = TaskPlan(goal="Gaming test")

        plan.add_step(
            TaskType.FAST,
            "Reply with exactly: GAMING",
        )

        with patch(
            "core.orchestrator.read_system_status",
            return_value=self._safe_system(),
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=self._safe_gpu(),
        ), patch.object(
            self.orchestrator.activity_guard,
            "detect",
        ) as mock_detect, patch.object(
            self.orchestrator.ollama,
            "chat",
            return_value="GAMING",
        ) as mock_chat, patch.object(
            self.orchestrator.ollama,
            "stop_model",
        ) as mock_stop:

            mock_detect.return_value = self._make_state(
                ActivityMode.GAMING_ASSIST,
                ResourcePressure.NORMAL,
            )

            results = self.orchestrator.execute_plan(plan)

        self.assertTrue(results[0].success)

        self.assertEqual(
            mock_chat.call_args.kwargs["keep_alive"],
            "0",
        )

        mock_stop.assert_called_once_with(
            "qwen3:4b-instruct",
        )

    def test_prepare_resources_retries_after_unloading_models(self) -> None:
        with patch(
            "core.orchestrator.read_system_status",
            return_value=self._safe_system(),
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=self._safe_gpu(),
        ), patch.object(
            self.orchestrator.ollama,
            "list_running_models",
            return_value=[object()],
        ), patch.object(
            self.orchestrator.ollama,
            "unload_all",
        ) as mock_unload, patch.object(
            self.orchestrator.activity_guard,
            "detect",
            return_value=self._make_state(
                ActivityMode.NORMAL,
                ResourcePressure.NORMAL,
            ),
        ), patch.object(
            self.orchestrator.model_manager,
            "select_model",
        ) as mock_select:

            first = type(
                "Selection",
                (),
                {
                    "decision": ResourceDecision.WARN,
                    "model": get_model("fast"),
                },
            )

            second = type(
                "Selection",
                (),
                {
                    "decision": ResourceDecision.ALLOW,
                    "model": get_model("fast"),
                },
            )

            mock_select.side_effect = [
                first,
                second,
            ]

            result = self.orchestrator._prepare_resources(
                TaskClass.FAST,
                ActivityMode.NORMAL,
                ResourcePressure.NORMAL,
            )

        self.assertEqual(
            result.decision,
            ResourceDecision.ALLOW,
        )

        mock_unload.assert_called_once()

        self.assertEqual(
            mock_select.call_count,
            2,
        )

        self.assertIs(
            mock_select.call_args_list[1].kwargs["resource_pressure"],
            ResourcePressure.NORMAL,
        )

    def test_retry_remeasures_pressure_after_unloading_own_model(self) -> None:
        with patch(
            "core.orchestrator.read_system_status",
            return_value=self._safe_system(),
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=self._safe_gpu(),
        ), patch.object(
            self.orchestrator.ollama,
            "list_running_models",
            return_value=[object()],
        ), patch.object(
            self.orchestrator.ollama,
            "unload_all",
        ), patch.object(
            self.orchestrator.activity_guard,
            "detect",
            return_value=self._make_state(
                ActivityMode.NORMAL,
                ResourcePressure.NORMAL,
            ),
        ), patch.object(
            self.orchestrator.model_manager,
            "select_model",
        ) as mock_select:
            blocked = type(
                "Selection",
                (),
                {
                    "decision": ResourceDecision.BLOCK,
                    "model": get_model("fast"),
                    "keep_loaded": False,
                },
            )
            allowed = type(
                "Selection",
                (),
                {
                    "decision": ResourceDecision.ALLOW,
                    "model": get_model("fast"),
                    "keep_loaded": True,
                },
            )
            mock_select.side_effect = [blocked, allowed]

            result = self.orchestrator._prepare_resources(
                TaskClass.FAST,
                ActivityMode.NORMAL,
                ResourcePressure.CRITICAL,
            )

        self.assertIs(result, allowed)
        self.assertIs(
            mock_select.call_args_list[1].kwargs["resource_pressure"],
            ResourcePressure.NORMAL,
        )

    def test_gaming_mode_blocks_heavy_model(self) -> None:
        plan = TaskPlan(goal="Gaming protection test")

        plan.add_step(
            TaskType.HEAVY,
            "This must not run during gaming.",
        )

        with patch(
            "core.orchestrator.read_system_status",
            return_value=self._safe_system(),
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=self._safe_gpu(),
        ), patch.object(
            self.orchestrator.activity_guard,
            "detect",
        ) as mock_detect, patch.object(
            self.orchestrator.ollama,
            "chat",
        ) as mock_chat:

            mock_detect.return_value = self._make_state(
                ActivityMode.GAMING_PERFORMANCE,
                ResourcePressure.NORMAL,
            )

            results = self.orchestrator.execute_plan(plan)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)

        mock_chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
