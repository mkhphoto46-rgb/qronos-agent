from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import psutil

from core.resource_guard import read_gpu_status, read_system_status


class ActivityMode(Enum):
    IDLE = "idle"
    NORMAL = "normal"
    GAMING_ASSIST = "gaming_assist"
    GAMING_PERFORMANCE = "gaming_performance"
    CREATOR_ASSIST = "creator_assist"
    CREATOR_PERFORMANCE = "creator_performance"


class ResourcePressure(Enum):
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ActivityState:
    mode: ActivityMode
    detected_processes: tuple[str, ...]
    resource_pressure: ResourcePressure


DEFAULT_GAME_PROCESSES = {
    "steam.exe",
    "steamwebhelper.exe",
    "epicgameslauncher.exe",
    "battle.net.exe",
    "valorant-win64-shipping.exe",
    "eldenring.exe",
    "minecraft.exe",
}

DEFAULT_CREATOR_PROCESSES = {
    "premiere.exe",
    "adobepremierepro.exe",
    "afterfx.exe",
    "photoshop.exe",
}


class ActivityGuard:
    """Detect user activity and system resource pressure."""

    HIGH_CPU_PERCENT = 75.0
    CRITICAL_CPU_PERCENT = 90.0

    HIGH_RAM_PERCENT = 75.0
    CRITICAL_RAM_PERCENT = 90.0

    HIGH_VRAM_PERCENT = 75.0
    CRITICAL_VRAM_PERCENT = 90.0

    HIGH_GPU_TEMP_C = 75
    CRITICAL_GPU_TEMP_C = 85

    def __init__(
        self,
        game_processes: set[str] | None = None,
        creator_processes: set[str] | None = None,
    ) -> None:
        self.game_processes = {
            process.lower()
            for process in (
                game_processes
                if game_processes is not None
                else DEFAULT_GAME_PROCESSES
            )
        }

        self.creator_processes = {
            process.lower()
            for process in (
                creator_processes
                if creator_processes is not None
                else DEFAULT_CREATOR_PROCESSES
            )
        }

        self.manual_mode: ActivityMode | None = None

    def set_manual_mode(self, mode: ActivityMode) -> None:
        """Set a user-selected activity mode."""
        self.manual_mode = mode

    def clear_manual_mode(self) -> None:
        """Return to automatic activity detection."""
        self.manual_mode = None

    def detect(self) -> ActivityState:
        """Detect activity mode and current resource pressure."""

        running = self._get_running_processes()

        game_matches = tuple(
            sorted(running.intersection(self.game_processes))
        )

        creator_matches = tuple(
            sorted(running.intersection(self.creator_processes))
        )

        pressure = self._detect_resource_pressure()

        if self.manual_mode is not None:
            return ActivityState(
                mode=self.manual_mode,
                detected_processes=(),
                resource_pressure=pressure,
            )

        if game_matches:
            return ActivityState(
                mode=ActivityMode.GAMING_ASSIST,
                detected_processes=game_matches,
                resource_pressure=pressure,
            )

        if creator_matches:
            return ActivityState(
                mode=ActivityMode.CREATOR_ASSIST,
                detected_processes=creator_matches,
                resource_pressure=pressure,
            )

        return ActivityState(
            mode=ActivityMode.NORMAL,
            detected_processes=(),
            resource_pressure=pressure,
        )

    @classmethod
    def _detect_resource_pressure(cls) -> ResourcePressure:
        """Evaluate overall system pressure."""

        try:
            system = read_system_status()
            gpu = read_gpu_status()
        except Exception:
            return ResourcePressure.NORMAL

        critical = False
        high = False

        if system.cpu_usage_percent >= cls.CRITICAL_CPU_PERCENT:
            critical = True
        elif system.cpu_usage_percent >= cls.HIGH_CPU_PERCENT:
            high = True

        if system.ram_usage_percent >= cls.CRITICAL_RAM_PERCENT:
            critical = True
        elif system.ram_usage_percent >= cls.HIGH_RAM_PERCENT:
            high = True

        if gpu is not None:
            if gpu.vram_total_mb > 0:
                vram_percent = (
                    gpu.vram_used_mb / gpu.vram_total_mb
                ) * 100.0

                if vram_percent >= cls.CRITICAL_VRAM_PERCENT:
                    critical = True
                elif vram_percent >= cls.HIGH_VRAM_PERCENT:
                    high = True

            if gpu.temperature_c >= cls.CRITICAL_GPU_TEMP_C:
                critical = True
            elif gpu.temperature_c >= cls.HIGH_GPU_TEMP_C:
                high = True

        if critical:
            return ResourcePressure.CRITICAL

        if high:
            return ResourcePressure.HIGH

        return ResourcePressure.NORMAL

    @staticmethod
    def _get_running_processes() -> set[str]:
        """Return running process names in lowercase."""

        processes: set[str] = set()

        for process in psutil.process_iter(["name"]):
            try:
                name = process.info["name"]

                if name:
                    processes.add(name.lower())

            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
            ):
                continue

        return processes


if __name__ == "__main__":
    guard = ActivityGuard()
    state = guard.detect()

    print(f"Mode: {state.mode.value}")
    print(f"Resource pressure: {state.resource_pressure.value}")

    if state.detected_processes:
        print("Detected processes:")

        for process in state.detected_processes:
            print(f"- {process}")