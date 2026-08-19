from __future__ import annotations

import unittest
from unittest.mock import patch

from core.activity_guard import (
    ActivityGuard,
    ActivityMode,
)


class TestActivityGuard(unittest.TestCase):
    def test_default_mode_is_normal_when_no_special_process_is_running(self) -> None:
        guard = ActivityGuard(
            game_processes={"game.exe"},
            creator_processes={"premiere.exe"},
        )

        with patch.object(
            ActivityGuard,
            "_get_running_processes",
            return_value={"explorer.exe", "chrome.exe"},
        ):
            state = guard.detect()

        self.assertEqual(state.mode, ActivityMode.NORMAL)
        self.assertEqual(state.detected_processes, ())

    def test_game_process_selects_gaming_assist(self) -> None:
        guard = ActivityGuard(
            game_processes={"game.exe"},
            creator_processes={"premiere.exe"},
        )

        with patch.object(
            ActivityGuard,
            "_get_running_processes",
            return_value={"explorer.exe", "game.exe"},
        ):
            state = guard.detect()

        self.assertEqual(
            state.mode,
            ActivityMode.GAMING_ASSIST,
        )

        self.assertEqual(
            state.detected_processes,
            ("game.exe",),
        )

    def test_creator_process_selects_creator_assist(self) -> None:
        guard = ActivityGuard(
            game_processes={"game.exe"},
            creator_processes={"premiere.exe"},
        )

        with patch.object(
            ActivityGuard,
            "_get_running_processes",
            return_value={"explorer.exe", "premiere.exe"},
        ):
            state = guard.detect()

        self.assertEqual(
            state.mode,
            ActivityMode.CREATOR_ASSIST,
        )

        self.assertEqual(
            state.detected_processes,
            ("premiere.exe",),
        )

    def test_gaming_has_priority_when_both_are_detected(self) -> None:
        guard = ActivityGuard(
            game_processes={"game.exe"},
            creator_processes={"premiere.exe"},
        )

        with patch.object(
            ActivityGuard,
            "_get_running_processes",
            return_value={
                "explorer.exe",
                "game.exe",
                "premiere.exe",
            },
        ):
            state = guard.detect()

        self.assertEqual(
            state.mode,
            ActivityMode.GAMING_ASSIST,
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
        ):
            state = guard.detect()

        self.assertEqual(
            state.mode,
            ActivityMode.GAMING_PERFORMANCE,
        )

    def test_manual_mode_can_be_cleared(self) -> None:
        guard = ActivityGuard()

        guard.set_manual_mode(
            ActivityMode.CREATOR_PERFORMANCE
        )

        guard.clear_manual_mode()

        with patch.object(
            ActivityGuard,
            "_get_running_processes",
            return_value={"explorer.exe"},
        ):
            state = guard.detect()

        self.assertEqual(
            state.mode,
            ActivityMode.NORMAL,
        )

    def test_process_names_are_normalized(self) -> None:
        guard = ActivityGuard(
            game_processes={"Game.EXE"},
            creator_processes={"Premiere.EXE"},
        )

        with patch.object(
            ActivityGuard,
            "_get_running_processes",
            return_value={"game.exe"},
        ):
            state = guard.detect()

        self.assertEqual(
            state.mode,
            ActivityMode.GAMING_ASSIST,
        )


if __name__ == "__main__":
    unittest.main()