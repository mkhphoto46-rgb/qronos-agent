from __future__ import annotations

import unittest
import subprocess
from unittest.mock import patch

from core.resource_guard import (
    GpuStatusReadError,
    read_gpu_status,
    read_system_status,
)


class TestResourceGuard(unittest.TestCase):
    @patch("core.resource_guard.subprocess.run", side_effect=FileNotFoundError)
    def test_missing_nvidia_sensor_is_optional(self, _run) -> None:
        self.assertIsNone(read_gpu_status())

    @patch(
        "core.resource_guard.subprocess.run",
        side_effect=subprocess.TimeoutExpired("nvidia-smi", 5),
    )
    def test_broken_nvidia_sensor_is_not_treated_as_no_gpu(self, _run) -> None:
        with self.assertRaises(GpuStatusReadError):
            read_gpu_status()

    def test_system_status_has_valid_cpu_usage(self) -> None:
        status = read_system_status()

        self.assertGreaterEqual(status.cpu_usage_percent, 0.0)
        self.assertLessEqual(status.cpu_usage_percent, 100.0)

    def test_system_status_has_valid_ram_usage(self) -> None:
        status = read_system_status()

        self.assertGreaterEqual(status.ram_usage_percent, 0.0)
        self.assertLessEqual(status.ram_usage_percent, 100.0)
        self.assertGreater(status.ram_total_gb, 0.0)
        self.assertGreaterEqual(status.ram_used_gb, 0.0)

    def test_gpu_status(self) -> None:
        status = read_gpu_status()

        if status is None:
            self.skipTest("NVIDIA GPU status is unavailable.")

        self.assertTrue(status.name)
        self.assertIsNotNone(status.temperature_c)
        self.assertIsNotNone(status.gpu_utilization_percent)
        self.assertIsNotNone(status.vram_used_mb)
        self.assertIsNotNone(status.vram_total_mb)

        assert status.temperature_c is not None
        assert status.gpu_utilization_percent is not None
        assert status.vram_used_mb is not None
        assert status.vram_total_mb is not None

        self.assertGreaterEqual(status.temperature_c, 0)
        self.assertLessEqual(status.temperature_c, 120)

        self.assertGreaterEqual(status.gpu_utilization_percent, 0)
        self.assertLessEqual(status.gpu_utilization_percent, 100)

        self.assertGreaterEqual(status.vram_used_mb, 0)
        self.assertGreater(status.vram_total_mb, 0)
        self.assertLessEqual(
            status.vram_used_mb,
            status.vram_total_mb,
        )


if __name__ == "__main__":
    unittest.main()
