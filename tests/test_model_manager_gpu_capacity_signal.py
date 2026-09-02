from __future__ import annotations

import unittest

from core.activity_guard import (
    ActivityMode,
    ResourcePressure,
)
from core.model_manager import (
    ModelManager,
    TaskClass,
)
from core.resource_guard import (
    GpuStatus,
    SystemStatus,
)
from core.resource_policy import ResourceDecision


class TestModelManagerGpuCapacitySignal(unittest.TestCase):

    def setUp(self) -> None:
        self.manager = ModelManager()

    @staticmethod
    def system(
        *,
        cpu: float = 41.8,
        ram: float = 37.3,
    ) -> SystemStatus:
        return SystemStatus(
            cpu_usage_percent=cpu,
            ram_usage_percent=ram,
            ram_used_gb=12.0,
            ram_total_gb=32.0,
        )

    @staticmethod
    def gpu(
        *,
        utilization: int = 100,
        temperature: int = 45,
        used_mb: int = 4665,
        total_mb: int = 8192,
    ) -> GpuStatus:
        return GpuStatus(
            name="NVIDIA GeForce RTX 3070 Ti",
            temperature_c=temperature,
            gpu_utilization_percent=utilization,
            vram_used_mb=used_mb,
            vram_total_mb=total_mb,
        )

    def test_global_gpu_utilization_spike_does_not_block_normal_pressure(
        self,
    ) -> None:

        result = self.manager.select_model(
            task_class=TaskClass.FAST,
            system=self.system(),
            gpu=self.gpu(
                utilization=100,
            ),
            activity_mode=ActivityMode.NORMAL,
            resource_pressure=ResourcePressure.NORMAL,
        )

        self.assertIs(
            result.decision,
            ResourceDecision.ALLOW,
        )

    def test_high_external_pressure_still_blocks(
        self,
    ) -> None:

        result = self.manager.select_model(
            task_class=TaskClass.FAST,
            system=self.system(),
            gpu=self.gpu(
                utilization=100,
            ),
            activity_mode=ActivityMode.NORMAL,
            resource_pressure=ResourcePressure.HIGH,
        )

        self.assertIs(
            result.decision,
            ResourceDecision.BLOCK,
        )

    def test_critical_external_pressure_still_blocks(
        self,
    ) -> None:

        result = self.manager.select_model(
            task_class=TaskClass.FAST,
            system=self.system(),
            gpu=self.gpu(
                utilization=10,
            ),
            activity_mode=ActivityMode.NORMAL,
            resource_pressure=ResourcePressure.CRITICAL,
        )

        self.assertIs(
            result.decision,
            ResourceDecision.BLOCK,
        )

    def test_vram_hard_limit_still_blocks(
        self,
    ) -> None:

        result = self.manager.select_model(
            task_class=TaskClass.FAST,
            system=self.system(),
            gpu=self.gpu(
                utilization=10,
                used_mb=7600,
                total_mb=8192,
            ),
            activity_mode=ActivityMode.NORMAL,
            resource_pressure=ResourcePressure.NORMAL,
        )

        self.assertIs(
            result.decision,
            ResourceDecision.BLOCK,
        )

    def test_gpu_temperature_hard_limit_still_blocks(
        self,
    ) -> None:

        result = self.manager.select_model(
            task_class=TaskClass.FAST,
            system=self.system(),
            gpu=self.gpu(
                utilization=10,
                temperature=83,
            ),
            activity_mode=ActivityMode.NORMAL,
            resource_pressure=ResourcePressure.NORMAL,
        )

        self.assertIs(
            result.decision,
            ResourceDecision.BLOCK,
        )

    def test_cpu_hard_limit_still_blocks(
        self,
    ) -> None:

        result = self.manager.select_model(
            task_class=TaskClass.FAST,
            system=self.system(
                cpu=91.0,
            ),
            gpu=self.gpu(
                utilization=10,
            ),
            activity_mode=ActivityMode.NORMAL,
            resource_pressure=ResourcePressure.NORMAL,
        )

        self.assertIs(
            result.decision,
            ResourceDecision.BLOCK,
        )

    def test_ram_hard_limit_still_blocks(
        self,
    ) -> None:

        result = self.manager.select_model(
            task_class=TaskClass.FAST,
            system=self.system(
                ram=86.0,
            ),
            gpu=self.gpu(
                utilization=10,
            ),
            activity_mode=ActivityMode.NORMAL,
            resource_pressure=ResourcePressure.NORMAL,
        )

        self.assertIs(
            result.decision,
            ResourceDecision.BLOCK,
        )


if __name__ == "__main__":
    unittest.main()
