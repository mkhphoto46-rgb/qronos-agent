from __future__ import annotations

import unittest

from core.activity_guard import ActivityMode
from core.model_manager import ModelManager, TaskClass
from core.resource_guard import GpuStatus, SystemStatus
from core.resource_policy import ResourceDecision


class TestModelManager(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ModelManager()

    def make_system(
        self,
        cpu: float = 20.0,
        ram: float = 40.0,
    ) -> SystemStatus:
        return SystemStatus(
            cpu_usage_percent=cpu,
            ram_usage_percent=ram,
            ram_used_gb=12.0,
            ram_total_gb=31.9,
        )

    def make_gpu(
        self,
        temperature: int = 50,
        vram_used: int = 2000,
    ) -> GpuStatus:
        return GpuStatus(
            name="NVIDIA GeForce RTX 3070 Ti",
            temperature_c=temperature,
            gpu_utilization_percent=10,
            vram_used_mb=vram_used,
            vram_total_mb=8192,
        )

    def test_fast_model_selection(self) -> None:
        result = self.manager.select_model(
            TaskClass.FAST,
            self.make_system(),
            self.make_gpu(),
        )

        self.assertEqual(result.model.name, "qwen3.5:9b")
        self.assertEqual(result.decision, ResourceDecision.ALLOW)
        self.assertTrue(result.keep_loaded)

    def test_heavy_model_selection(self) -> None:
        result = self.manager.select_model(
            TaskClass.HEAVY,
            self.make_system(),
            self.make_gpu(),
        )

        self.assertEqual(result.model.name, "qwen3.6:27b")
        self.assertEqual(result.decision, ResourceDecision.ALLOW)
        self.assertFalse(result.keep_loaded)

    def test_heavy_model_blocked_when_vram_is_critical(self) -> None:
        result = self.manager.select_model(
            TaskClass.HEAVY,
            self.make_system(),
            self.make_gpu(vram_used=7600),
        )

        self.assertEqual(result.decision, ResourceDecision.BLOCK)

    def test_fast_model_blocked_when_ram_is_critical(self) -> None:
        result = self.manager.select_model(
            TaskClass.FAST,
            self.make_system(ram=86.0),
            self.make_gpu(),
        )

        self.assertEqual(result.decision, ResourceDecision.BLOCK)

    def test_can_start_returns_false_when_blocked(self) -> None:
        allowed = self.manager.can_start(
            TaskClass.HEAVY,
            self.make_system(),
            self.make_gpu(vram_used=7600),
        )

        self.assertFalse(allowed)

    def test_can_start_returns_true_when_resources_are_safe(self) -> None:
        allowed = self.manager.can_start(
            TaskClass.FAST,
            self.make_system(),
            self.make_gpu(vram_used=2000),
        )

        self.assertTrue(allowed)

    def test_gaming_assist_blocks_heavy_model(self) -> None:
        result = self.manager.select_model(
            TaskClass.HEAVY,
            self.make_system(),
            self.make_gpu(),
            ActivityMode.GAMING_ASSIST,
        )

        self.assertEqual(result.decision, ResourceDecision.BLOCK)

    def test_gaming_assist_allows_fast_model_on_demand(self) -> None:
        result = self.manager.select_model(
            TaskClass.FAST,
            self.make_system(),
            self.make_gpu(),
            ActivityMode.GAMING_ASSIST,
        )

        self.assertEqual(result.decision, ResourceDecision.ALLOW)
        self.assertFalse(result.keep_loaded)

    def test_gaming_performance_blocks_heavy_model(self) -> None:
        result = self.manager.select_model(
            TaskClass.HEAVY,
            self.make_system(),
            self.make_gpu(),
            ActivityMode.GAMING_PERFORMANCE,
        )

        self.assertEqual(result.decision, ResourceDecision.BLOCK)

    def test_creator_assist_blocks_heavy_model(self) -> None:
        result = self.manager.select_model(
            TaskClass.HEAVY,
            self.make_system(),
            self.make_gpu(),
            ActivityMode.CREATOR_ASSIST,
        )

        self.assertEqual(result.decision, ResourceDecision.BLOCK)

    def test_creator_performance_blocks_heavy_model(self) -> None:
        result = self.manager.select_model(
            TaskClass.HEAVY,
            self.make_system(),
            self.make_gpu(),
            ActivityMode.CREATOR_PERFORMANCE,
        )

        self.assertEqual(result.decision, ResourceDecision.BLOCK)

    def test_creator_performance_fast_model_is_on_demand(self) -> None:
        result = self.manager.select_model(
            TaskClass.FAST,
            self.make_system(),
            self.make_gpu(),
            ActivityMode.CREATOR_PERFORMANCE,
        )

        self.assertEqual(result.decision, ResourceDecision.ALLOW)
        self.assertFalse(result.keep_loaded)

    def test_idle_mode_does_not_keep_models_loaded(self) -> None:
        fast = self.manager.select_model(
            TaskClass.FAST,
            self.make_system(),
            self.make_gpu(),
            ActivityMode.IDLE,
        )

        heavy = self.manager.select_model(
            TaskClass.HEAVY,
            self.make_system(),
            self.make_gpu(),
            ActivityMode.IDLE,
        )

        self.assertFalse(fast.keep_loaded)
        self.assertFalse(heavy.keep_loaded)


if __name__ == "__main__":
    unittest.main()