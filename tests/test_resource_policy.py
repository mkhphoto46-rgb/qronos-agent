from __future__ import annotations

import unittest

from core.resource_guard import GpuStatus, SystemStatus
from core.resource_policy import (
    ResourceDecision,
    evaluate_resources,
)


class TestResourcePolicy(unittest.TestCase):
    def make_system(
        self,
        cpu: float,
        ram: float,
    ) -> SystemStatus:
        return SystemStatus(
            cpu_usage_percent=cpu,
            ram_usage_percent=ram,
            ram_used_gb=ram * 0.319,
            ram_total_gb=31.9,
        )

    def make_gpu(
        self,
        temperature: int,
        vram_used: int,
        vram_total: int = 8192,
    ) -> GpuStatus:
        return GpuStatus(
            name="NVIDIA GeForce RTX 3070 Ti",
            temperature_c=temperature,
            gpu_utilization_percent=0,
            vram_used_mb=vram_used,
            vram_total_mb=vram_total,
        )

    def test_normal_system_is_allowed(self) -> None:
        result = evaluate_resources(
            self.make_system(cpu=20.0, ram=40.0),
            self.make_gpu(temperature=50, vram_used=2000),
        )
        self.assertEqual(result, ResourceDecision.ALLOW)

    def test_high_cpu_warns(self) -> None:
        result = evaluate_resources(
            self.make_system(cpu=82.0, ram=40.0),
            self.make_gpu(temperature=50, vram_used=2000),
        )
        self.assertEqual(result, ResourceDecision.WARN)

    def test_critical_cpu_blocks(self) -> None:
        result = evaluate_resources(
            self.make_system(cpu=91.0, ram=40.0),
            self.make_gpu(temperature=50, vram_used=2000),
        )
        self.assertEqual(result, ResourceDecision.BLOCK)

    def test_high_ram_warns(self) -> None:
        result = evaluate_resources(
            self.make_system(cpu=20.0, ram=80.0),
            self.make_gpu(temperature=50, vram_used=2000),
        )
        self.assertEqual(result, ResourceDecision.WARN)

    def test_critical_ram_blocks(self) -> None:
        result = evaluate_resources(
            self.make_system(cpu=20.0, ram=86.0),
            self.make_gpu(temperature=50, vram_used=2000),
        )
        self.assertEqual(result, ResourceDecision.BLOCK)

    def test_high_gpu_temperature_warns(self) -> None:
        result = evaluate_resources(
            self.make_system(cpu=20.0, ram=40.0),
            self.make_gpu(temperature=76, vram_used=2000),
        )
        self.assertEqual(result, ResourceDecision.WARN)

    def test_critical_gpu_temperature_blocks(self) -> None:
        result = evaluate_resources(
            self.make_system(cpu=20.0, ram=40.0),
            self.make_gpu(temperature=83, vram_used=2000),
        )
        self.assertEqual(result, ResourceDecision.BLOCK)

    def test_high_vram_warns(self) -> None:
        result = evaluate_resources(
            self.make_system(cpu=20.0, ram=40.0),
            self.make_gpu(temperature=50, vram_used=6200),
        )
        self.assertEqual(result, ResourceDecision.WARN)

    def test_critical_vram_blocks(self) -> None:
        result = evaluate_resources(
            self.make_system(cpu=20.0, ram=40.0),
            self.make_gpu(temperature=50, vram_used=7400),
        )
        self.assertEqual(result, ResourceDecision.BLOCK)


if __name__ == "__main__":
    unittest.main()