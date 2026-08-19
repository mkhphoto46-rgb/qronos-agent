from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import psutil


class ActivityMode(Enum):
    IDLE = "idle"
    NORMAL = "normal"
    GAMING_ASSIST = "gaming_assist"
    GAMING_PERFORMANCE = "gaming_performance"
    CREATOR_ASSIST = "creator_assist"
    CREATOR_PERFORMANCE = "creator_performance"


@dataclass(frozen=True)
class ActivityState:
    mode: ActivityMode
    detected_processes: tuple[str, ...]


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
    """Detect the current user activity and selected Qronos mode."""

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
        """Set a user-selected mode."""
        self.manual_mode = mode

    def clear_manual_mode(self) -> None:
        """Return to automatic activity detection."""
        self.manual_mode = None

    def detect(self) -> ActivityState:
        """Detect current activity."""
        if self.manual_mode is not None:
            return ActivityState(
                mode=self.manual_mode,
                detected_processes=(),
            )

        running = self._get_running_processes()

        game_matches = tuple(
            sorted(running.intersection(self.game_processes))
        )

        creator_matches = tuple(
            sorted(running.intersection(self.creator_processes))
        )

        if game_matches:
            return ActivityState(
                mode=ActivityMode.GAMING_ASSIST,
                detected_processes=game_matches,
            )

        if creator_matches:
            return ActivityState(
                mode=ActivityMode.CREATOR_ASSIST,
                detected_processes=creator_matches,
            )

        return ActivityState(
            mode=ActivityMode.NORMAL,
            detected_processes=(),
        )

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

    if state.detected_processes:
        print("Detected:")
        for process in state.detected_processes:
            print(f"- {process}")