from __future__ import annotations

import unittest
from unittest.mock import patch

from core.activity_guard import (
    ActivityGuard,
    ResourcePressure,
)
from core.resource_guard import (
    GpuStatus,
    SystemStatus,
)


class TestLiveResourcePressure(unittest.TestCase):
    def make_system(
        self,
        cpu: float,
        ram: float,
    ) -> SystemStatus:
        return SystemStatus(
            cpu_usage_percent=cpu,
            ram_usage_percent=ram,
            ram_used_gb=16.0,
            ram_total_gb=31.9,
        )

    def make_gpu(
        self,
        temperature: int,
        vram_used: int,
        utilization: int = 10,
    ) -> GpuStatus:
        return GpuStatus(
            name="NVIDIA GeForce RTX 3070 Ti",
            temperature_c=temperature,
            gpu_utilization_percent=utilization,
            vram_used_mb=vram_used,
            vram_total_mb=8192,
        )

    def test_normal_pressure(self) -> None:
        guard = ActivityGuard()

        system = self.make_system(
            cpu=20.0,
            ram=40.0,
        )

        gpu = self.make_gpu(
            temperature=50,
            vram_used=2000,
        )

        with patch(
            "core.activity_guard.read_system_status",
            return_value=system,
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=gpu,
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.NORMAL,
        )

    def test_high_vram_pressure(self) -> None:
        guard = ActivityGuard()

        system = self.make_system(
            cpu=20.0,
            ram=40.0,
        )

        gpu = self.make_gpu(
            temperature=50,
            vram_used=7000,
        )

        with patch(
            "core.activity_guard.read_system_status",
            return_value=system,
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=gpu,
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.HIGH,
        )

    def test_high_gpu_utilization_pressure(self) -> None:
        guard = ActivityGuard()

        with patch(
            "core.activity_guard.read_system_status",
            return_value=self.make_system(20.0, 40.0),
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=self.make_gpu(50, 2000, utilization=80),
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.HIGH,
        )

    def test_critical_gpu_utilization_pressure(self) -> None:
        guard = ActivityGuard()

        with patch(
            "core.activity_guard.read_system_status",
            return_value=self.make_system(20.0, 40.0),
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=self.make_gpu(50, 2000, utilization=95),
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.CRITICAL,
        )

    def test_critical_vram_pressure(self) -> None:
        guard = ActivityGuard()

        system = self.make_system(
            cpu=20.0,
            ram=40.0,
        )

        gpu = self.make_gpu(
            temperature=50,
            vram_used=7600,
        )

        with patch(
            "core.activity_guard.read_system_status",
            return_value=system,
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=gpu,
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.CRITICAL,
        )

    def test_high_cpu_pressure(self) -> None:
        guard = ActivityGuard()

        system = self.make_system(
            cpu=80.0,
            ram=40.0,
        )

        gpu = self.make_gpu(
            temperature=50,
            vram_used=2000,
        )

        with patch(
            "core.activity_guard.read_system_status",
            return_value=system,
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=gpu,
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.HIGH,
        )

    def test_critical_cpu_pressure(self) -> None:
        guard = ActivityGuard()

        system = self.make_system(
            cpu=95.0,
            ram=40.0,
        )

        gpu = self.make_gpu(
            temperature=50,
            vram_used=2000,
        )

        with patch(
            "core.activity_guard.read_system_status",
            return_value=system,
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=gpu,
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.CRITICAL,
        )

    def test_critical_gpu_temperature(self) -> None:
        guard = ActivityGuard()

        system = self.make_system(
            cpu=20.0,
            ram=40.0,
        )

        gpu = self.make_gpu(
            temperature=88,
            vram_used=2000,
        )

        with patch(
            "core.activity_guard.read_system_status",
            return_value=system,
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=gpu,
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.CRITICAL,
        )


if __name__ == "__main__":
    unittest.main()
