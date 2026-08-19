from __future__ import annotations

import unittest
from unittest.mock import patch

from core.activity_guard import ActivityMode
from core.model_manager import TaskClass
from core.orchestrator import Orchestrator
from core.resource_guard import GpuStatus, SystemStatus
from core.resource_policy import ResourceDecision
from core.task_plan import TaskPlan
from core.task_router import TaskType


class TestOrchestrator(unittest.TestCase):
    def setUp(self) -> None:
        self.orchestrator = Orchestrator()

    def test_empty_plan_returns_no_results(self) -> None:
        plan = TaskPlan(goal="Empty test")

        results = self.orchestrator.execute_plan(plan)

        self.assertEqual(results, [])

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

        results = self.orchestrator.execute_plan(plan)

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].success)
        self.assertTrue(results[1].success)

        self.assertIn("TEST_STEP_ONE", results[0].output)
        self.assertIn("TEST_STEP_TWO", results[1].output)

    def test_unsupported_task_fails_cleanly(self) -> None:
        plan = TaskPlan(goal="Unsupported task test")

        plan.add_step(
            TaskType.VISION,
            "Analyze this image.",
        )

        results = self.orchestrator.execute_plan(plan)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)
        self.assertIn("not implemented", results[0].error or "")

    def test_blocked_task_does_not_run_model(self) -> None:
        system = SystemStatus(
            cpu_usage_percent=95.0,
            ram_usage_percent=90.0,
            ram_used_gb=28.0,
            ram_total_gb=31.9,
        )

        gpu = GpuStatus(
            name="NVIDIA GeForce RTX 3070 Ti",
            temperature_c=85,
            gpu_utilization_percent=98,
            vram_used_mb=8100,
            vram_total_mb=8192,
        )

        with patch(
            "core.orchestrator.read_system_status",
            return_value=system,
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=gpu,
        ), patch.object(
            self.orchestrator.ollama,
            "chat",
        ) as mock_chat:
            plan = TaskPlan(goal="Blocked test")

            plan.add_step(
                TaskType.HEAVY,
                "This should not run.",
            )

            results = self.orchestrator.execute_plan(plan)

            self.assertEqual(len(results), 1)
            self.assertFalse(results[0].success)
            self.assertIn("blocked", results[0].error or "")
            mock_chat.assert_not_called()

    def test_running_model_is_unloaded_when_resources_require_retry(self) -> None:
        high_load_system = SystemStatus(
            cpu_usage_percent=85.0,
            ram_usage_percent=80.0,
            ram_used_gb=25.0,
            ram_total_gb=31.9,
        )

        safe_system = SystemStatus(
            cpu_usage_percent=20.0,
            ram_usage_percent=40.0,
            ram_used_gb=12.0,
            ram_total_gb=31.9,
        )

        high_load_gpu = GpuStatus(
            name="NVIDIA GeForce RTX 3070 Ti",
            temperature_c=50,
            gpu_utilization_percent=80,
            vram_used_mb=7000,
            vram_total_mb=8192,
        )

        safe_gpu = GpuStatus(
            name="NVIDIA GeForce RTX 3070 Ti",
            temperature_c=50,
            gpu_utilization_percent=10,
            vram_used_mb=2000,
            vram_total_mb=8192,
        )

        with patch(
            "core.orchestrator.read_system_status",
            side_effect=[high_load_system, safe_system],
        ), patch(
            "core.orchestrator.read_gpu_status",
            side_effect=[high_load_gpu, safe_gpu],
        ), patch.object(
            self.orchestrator.ollama,
            "list_running_models",
            return_value=[object()],
        ), patch.object(
            self.orchestrator.ollama,
            "unload_all",
        ) as mock_unload:

            decision = self.orchestrator._prepare_resources(
                TaskClass.HEAVY,
                ActivityMode.NORMAL,
            )

            self.assertEqual(
                decision.decision,
                ResourceDecision.ALLOW,
            )

            mock_unload.assert_called_once()

    def test_no_unload_when_resources_are_safe(self) -> None:
        safe_system = SystemStatus(
            cpu_usage_percent=20.0,
            ram_usage_percent=40.0,
            ram_used_gb=12.0,
            ram_total_gb=31.9,
        )

        safe_gpu = GpuStatus(
            name="NVIDIA GeForce RTX 3070 Ti",
            temperature_c=50,
            gpu_utilization_percent=10,
            vram_used_mb=2000,
            vram_total_mb=8192,
        )

        with patch(
            "core.orchestrator.read_system_status",
            return_value=safe_system,
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=safe_gpu,
        ), patch.object(
            self.orchestrator.ollama,
            "unload_all",
        ) as mock_unload:

            decision = self.orchestrator._prepare_resources(
                TaskClass.FAST,
                ActivityMode.NORMAL,
            )

            self.assertEqual(
                decision.decision,
                ResourceDecision.ALLOW,
            )

            mock_unload.assert_not_called()

    def test_gaming_mode_blocks_heavy_model(self) -> None:
        safe_system = SystemStatus(
            cpu_usage_percent=20.0,
            ram_usage_percent=40.0,
            ram_used_gb=12.0,
            ram_total_gb=31.9,
        )

        safe_gpu = GpuStatus(
            name="NVIDIA GeForce RTX 3070 Ti",
            temperature_c=50,
            gpu_utilization_percent=10,
            vram_used_mb=2000,
            vram_total_mb=8192,
        )

        with patch(
            "core.orchestrator.read_system_status",
            return_value=safe_system,
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=safe_gpu,
        ), patch.object(
            self.orchestrator.ollama,
            "chat",
        ) as mock_chat:

            plan = TaskPlan(goal="Gaming protection test")

            plan.add_step(
                TaskType.HEAVY,
                "This must not run during gaming.",
            )

            with patch.object(
                self.orchestrator.activity_guard,
                "detect",
            ) as mock_detect:
                mock_detect.return_value.mode = (
                    ActivityMode.GAMING_PERFORMANCE
                )

                results = self.orchestrator.execute_plan(plan)

            self.assertEqual(len(results), 1)
            self.assertFalse(results[0].success)
            mock_chat.assert_not_called()

    def test_normal_fast_model_uses_warm_lifecycle(self) -> None:
        safe_system = SystemStatus(
            cpu_usage_percent=20.0,
            ram_usage_percent=40.0,
            ram_used_gb=12.0,
            ram_total_gb=31.9,
        )

        safe_gpu = GpuStatus(
            name="NVIDIA GeForce RTX 3070 Ti",
            temperature_c=50,
            gpu_utilization_percent=10,
            vram_used_mb=2000,
            vram_total_mb=8192,
        )

        with patch(
            "core.orchestrator.read_system_status",
            return_value=safe_system,
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=safe_gpu,
        ), patch.object(
            self.orchestrator.activity_guard,
            "detect",
        ) as mock_detect, patch.object(
            self.orchestrator.ollama,
            "chat",
            return_value="OK",
        ) as mock_chat, patch.object(
            self.orchestrator.ollama,
            "stop_model",
        ) as mock_stop:

            mock_detect.return_value.mode = ActivityMode.NORMAL

            plan = TaskPlan(goal="Warm lifecycle test")

            plan.add_step(
                TaskType.FAST,
                "Reply with OK",
            )

            results = self.orchestrator.execute_plan(plan)

            self.assertTrue(results[0].success)

            mock_chat.assert_called_once()

            call_kwargs = mock_chat.call_args.kwargs

            self.assertEqual(
                call_kwargs["keep_alive"],
                "10m",
            )

            mock_stop.assert_not_called()

    def test_gaming_fast_model_uses_on_demand_lifecycle(self) -> None:
        safe_system = SystemStatus(
            cpu_usage_percent=20.0,
            ram_usage_percent=40.0,
            ram_used_gb=12.0,
            ram_total_gb=31.9,
        )

        safe_gpu = GpuStatus(
            name="NVIDIA GeForce RTX 3070 Ti",
            temperature_c=50,
            gpu_utilization_percent=10,
            vram_used_mb=2000,
            vram_total_mb=8192,
        )

        with patch(
            "core.orchestrator.read_system_status",
            return_value=safe_system,
        ), patch(
            "core.orchestrator.read_gpu_status",
            return_value=safe_gpu,
        ), patch.object(
            self.orchestrator.activity_guard,
            "detect",
        ) as mock_detect, patch.object(
            self.orchestrator.ollama,
            "chat",
            return_value="OK",
        ) as mock_chat, patch.object(
            self.orchestrator.ollama,
            "stop_model",
        ) as mock_stop:

            mock_detect.return_value.mode = ActivityMode.GAMING_ASSIST

            plan = TaskPlan(goal="Gaming lifecycle test")

            plan.add_step(
                TaskType.FAST,
                "Reply with OK",
            )

            results = self.orchestrator.execute_plan(plan)

            self.assertTrue(results[0].success)

            call_kwargs = mock_chat.call_args.kwargs

            self.assertEqual(
                call_kwargs["keep_alive"],
                "0",
            )

            mock_stop.assert_called_once_with(
                "qwen3.5:9b",
            )


if __name__ == "__main__":
    unittest.main()