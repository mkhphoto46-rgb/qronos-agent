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
        self.assertIn(
            "not implemented",
            results[0].error or "",
        )

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

            mock_state = type(
                "State",
                (),
                {
                    "mode": ActivityMode.NORMAL,
                    "resource_pressure": ResourcePressure.NORMAL,
                },
            )

            mock_detect.return_value = mock_state

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
        ) as mock_chat:

            mock_state = type(
                "State",
                (),
                {
                    "mode": ActivityMode.NORMAL,
                    "resource_pressure": ResourcePressure.CRITICAL,
                },
            )

            mock_detect.return_value = mock_state

            results = self.orchestrator.execute_plan(plan)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)

        self.assertIn(
            "blocked",
            results[0].error.lower(),
        )

        mock_chat.assert_not_called()

    def test_high_pressure_warns_and_does_not_run_heavy_model(self) -> None:
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
        ) as mock_chat:

            mock_state = type(
                "State",
                (),
                {
                    "mode": ActivityMode.NORMAL,
                    "resource_pressure": ResourcePressure.HIGH,
                },
            )

            mock_detect.return_value = mock_state

            results = self.orchestrator.execute_plan(plan)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)

        self.assertIn(
            "warn",
            results[0].error.lower(),
        )

        mock_chat.assert_not_called()

    def test_normal_fast_brain_can_stay_warm(self) -> None:
        plan = TaskPlan(goal="Warm fast brain test")

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

            mock_state = type(
                "State",
                (),
                {
                    "mode": ActivityMode.NORMAL,
                    "resource_pressure": ResourcePressure.NORMAL,
                },
            )

            mock_detect.return_value = mock_state

            results = self.orchestrator.execute_plan(plan)

        self.assertTrue(results[0].success)

        self.assertEqual(
            mock_chat.call_args.kwargs["keep_alive"],
            "10m",
        )

        mock_stop.assert_not_called()

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

            mock_state = type(
                "State",
                (),
                {
                    "mode": ActivityMode.GAMING_ASSIST,
                    "resource_pressure": ResourcePressure.NORMAL,
                },
            )

            mock_detect.return_value = mock_state

            results = self.orchestrator.execute_plan(plan)

        self.assertTrue(results[0].success)

        self.assertEqual(
            mock_chat.call_args.kwargs["keep_alive"],
            "0",
        )

        mock_stop.assert_called_once_with(
            "qwen3.5:9b",
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
            self.orchestrator.model_manager,
            "select_model",
        ) as mock_select:

            first = type(
                "Selection",
                (),
                {
                    "decision": ResourceDecision.WARN,
                    "model": get_model("fast"),
                    "keep_loaded": False,
                },
            )

            second = type(
                "Selection",
                (),
                {
                    "decision": ResourceDecision.ALLOW,
                    "model": get_model("fast"),
                    "keep_loaded": True,
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

            mock_state = type(
                "State",
                (),
                {
                    "mode": ActivityMode.GAMING_PERFORMANCE,
                    "resource_pressure": ResourcePressure.NORMAL,
                },
            )

            mock_detect.return_value = mock_state

            results = self.orchestrator.execute_plan(plan)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)
        mock_chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()