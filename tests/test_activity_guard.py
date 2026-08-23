from __future__ import annotations

import unittest
from unittest.mock import patch

from core.activity_guard import (
    ActivityGuard,
    ActivityMode,
    ResourcePressure,
)
from core.resource_guard import GpuStatus, SystemStatus


class TestActivityGuard(unittest.TestCase):
    def test_default_mode_is_normal(self) -> None:
        guard = ActivityGuard(
            game_processes={"game.exe"},
            creator_processes={"premiere.exe"},
        )

        with patch.object(
            ActivityGuard,
            "_get_running_processes",
            return_value={"explorer.exe", "chrome.exe"},
        ), patch(
            "core.activity_guard.read_system_status",
            return_value=SystemStatus(
                cpu_usage_percent=20.0,
                ram_usage_percent=40.0,
                ram_used_gb=12.0,
                ram_total_gb=31.9,
            ),
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=GpuStatus(
                name="RTX 3070 Ti",
                temperature_c=50,
                gpu_utilization_percent=10,
                vram_used_mb=2000,
                vram_total_mb=8192,
            ),
        ):
            state = guard.detect()

        self.assertEqual(
            state.mode,
            ActivityMode.NORMAL,
        )

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.NORMAL,
        )

    def test_game_process_selects_gaming_assist(self) -> None:
        guard = ActivityGuard(
            game_processes={"game.exe"},
            creator_processes={"premiere.exe"},
        )

        with patch.object(
            ActivityGuard,
            "_get_running_processes",
            return_value={"game.exe"},
        ), patch(
            "core.activity_guard.read_system_status",
            return_value=SystemStatus(
                cpu_usage_percent=20.0,
                ram_usage_percent=40.0,
                ram_used_gb=12.0,
                ram_total_gb=31.9,
            ),
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=GpuStatus(
                name="RTX 3070 Ti",
                temperature_c=50,
                gpu_utilization_percent=10,
                vram_used_mb=2000,
                vram_total_mb=8192,
            ),
        ):
            state = guard.detect()

        self.assertEqual(
            state.mode,
            ActivityMode.GAMING_ASSIST,
        )

    def test_creator_process_selects_creator_assist(self) -> None:
        guard = ActivityGuard(
            game_processes={"game.exe"},
            creator_processes={"premiere.exe"},
        )

        with patch.object(
            ActivityGuard,
            "_get_running_processes",
            return_value={"premiere.exe"},
        ), patch(
            "core.activity_guard.read_system_status",
            return_value=SystemStatus(
                cpu_usage_percent=20.0,
                ram_usage_percent=40.0,
                ram_used_gb=12.0,
                ram_total_gb=31.9,
            ),
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=GpuStatus(
                name="RTX 3070 Ti",
                temperature_c=50,
                gpu_utilization_percent=10,
                vram_used_mb=2000,
                vram_total_mb=8192,
            ),
        ):
            state = guard.detect()

        self.assertEqual(
            state.mode,
            ActivityMode.CREATOR_ASSIST,
        )

    def test_manual_mode_overrides_detection(self) -> None:
        guard = ActivityGuard(
            game_processes={"game.exe"},
            creator_processes={"premiere.exe"},
        )

        guard.set_manual_mode(
            ActivityMode.GAMING_PERFORMANCE
        )

        with patch.object(
            ActivityGuard,
            "_get_running_processes",
            return_value={"premiere.exe"},
        ), patch(
            "core.activity_guard.read_system_status",
            return_value=SystemStatus(
                cpu_usage_percent=20.0,
                ram_usage_percent=40.0,
                ram_used_gb=12.0,
                ram_total_gb=31.9,
            ),
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=GpuStatus(
                name="RTX 3070 Ti",
                temperature_c=50,
                gpu_utilization_percent=10,
                vram_used_mb=2000,
                vram_total_mb=8192,
            ),
        ):
            state = guard.detect()

        self.assertEqual(
            state.mode,
            ActivityMode.GAMING_PERFORMANCE,
        )

    def test_high_cpu_creates_high_pressure(self) -> None:
        guard = ActivityGuard()

        with patch(
            "core.activity_guard.read_system_status",
            return_value=SystemStatus(
                cpu_usage_percent=80.0,
                ram_usage_percent=40.0,
                ram_used_gb=12.0,
                ram_total_gb=31.9,
            ),
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=GpuStatus(
                name="RTX 3070 Ti",
                temperature_c=50,
                gpu_utilization_percent=10,
                vram_used_mb=2000,
                vram_total_mb=8192,
            ),
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.HIGH,
        )

    def test_high_vram_creates_high_pressure(self) -> None:
        guard = ActivityGuard()

        with patch(
            "core.activity_guard.read_system_status",
            return_value=SystemStatus(
                cpu_usage_percent=20.0,
                ram_usage_percent=40.0,
                ram_used_gb=12.0,
                ram_total_gb=31.9,
            ),
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=GpuStatus(
                name="RTX 3070 Ti",
                temperature_c=50,
                gpu_utilization_percent=10,
                vram_used_mb=7000,
                vram_total_mb=8192,
            ),
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.HIGH,
        )

    def test_critical_vram_creates_critical_pressure(self) -> None:
        guard = ActivityGuard()

        with patch(
            "core.activity_guard.read_system_status",
            return_value=SystemStatus(
                cpu_usage_percent=20.0,
                ram_usage_percent=40.0,
                ram_used_gb=12.0,
                ram_total_gb=31.9,
            ),
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=GpuStatus(
                name="RTX 3070 Ti",
                temperature_c=50,
                gpu_utilization_percent=10,
                vram_used_mb=7600,
                vram_total_mb=8192,
            ),
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.CRITICAL,
        )

    def test_critical_gpu_temperature_creates_critical_pressure(self) -> None:
        guard = ActivityGuard()

        with patch(
            "core.activity_guard.read_system_status",
            return_value=SystemStatus(
                cpu_usage_percent=20.0,
                ram_usage_percent=40.0,
                ram_used_gb=12.0,
                ram_total_gb=31.9,
            ),
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=GpuStatus(
                name="RTX 3070 Ti",
                temperature_c=88,
                gpu_utilization_percent=10,
                vram_used_mb=2000,
                vram_total_mb=8192,
            ),
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.CRITICAL,
        )

    def test_normal_system_has_normal_pressure(self) -> None:
        guard = ActivityGuard()

        with patch(
            "core.activity_guard.read_system_status",
            return_value=SystemStatus(
                cpu_usage_percent=30.0,
                ram_usage_percent=50.0,
                ram_used_gb=16.0,
                ram_total_gb=31.9,
            ),
        ), patch(
            "core.activity_guard.read_gpu_status",
            return_value=GpuStatus(
                name="RTX 3070 Ti",
                temperature_c=45,
                gpu_utilization_percent=20,
                vram_used_mb=2500,
                vram_total_mb=8192,
            ),
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.NORMAL,
        )

    def test_resource_sensor_failure_fails_closed(self) -> None:
        guard = ActivityGuard()

        with patch(
            "core.activity_guard.read_system_status",
            side_effect=RuntimeError("sensor unavailable"),
        ):
            state = guard.detect()

        self.assertEqual(
            state.resource_pressure,
            ResourcePressure.CRITICAL,
        )


if __name__ == "__main__":
    unittest.main()
