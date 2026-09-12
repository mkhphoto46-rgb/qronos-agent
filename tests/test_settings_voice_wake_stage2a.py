from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from core.json_store import JsonStore
from core.runtime_bridge import (
    QronosRuntime,
    SETTINGS_RELOAD_ACTION,
    WAKE_SENSITIVITY_DEFAULT,
    WAKE_THRESHOLD_ACCEPTED,
    wake_sensitivity_to_threshold,
)
from core.settings_service import (
    DEFAULT_DEFINITIONS,
    SettingsService,
)


ROOT = Path(__file__).resolve().parent.parent


class SettingsVoiceWakeStage2ATests(unittest.TestCase):
    def test_shipped_voice_defaults_preserve_the_accepted_runtime(self) -> None:
        service = SettingsService(
            DEFAULT_DEFINITIONS,
            path=None,
        )

        self.assertIs(
            service.get("voice.wake_word_enabled"),
            True,
        )
        self.assertEqual(
            service.get("voice.wake_sensitivity"),
            WAKE_SENSITIVITY_DEFAULT,
        )
        self.assertEqual(
            service.get("voice.language"),
            "fa",
        )

    def test_dormant_v1_false_wake_value_migrates_to_live_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"

            JsonStore(
                path,
                version=1,
            ).save(
                {
                    "voice.wake_word_enabled": False,
                    "voice.language": "fa",
                }
            )

            service = SettingsService(
                DEFAULT_DEFINITIONS,
                path=path,
            )

            self.assertIs(
                service.get("voice.wake_word_enabled"),
                True,
            )
            self.assertEqual(
                service.get("voice.language"),
                "fa",
            )

            service.set(
                "voice.wake_sensitivity",
                56,
            )

            raw = json.loads(
                path.read_text(
                    encoding="utf-8",
                )
            )

            self.assertEqual(
                raw["schemaVersion"],
                2,
            )
            self.assertNotIn(
                "voice.wake_word_enabled",
                raw["payload"],
            )

    def test_sensitivity_56_is_exactly_the_accepted_threshold(self) -> None:
        self.assertAlmostEqual(
            wake_sensitivity_to_threshold(
                WAKE_SENSITIVITY_DEFAULT
            ),
            WAKE_THRESHOLD_ACCEPTED,
            places=6,
        )

    def test_sensitivity_mapping_is_conservative_and_monotonic(self) -> None:
        low = wake_sensitivity_to_threshold(0)
        baseline = wake_sensitivity_to_threshold(56)
        high = wake_sensitivity_to_threshold(100)

        self.assertEqual(low, 0.72)
        self.assertEqual(high, 0.62)
        self.assertGreater(low, baseline)
        self.assertGreater(baseline, high)

    def test_disabled_wake_refuses_start_before_a_thread_is_created(self) -> None:
        settings = SettingsService(
            DEFAULT_DEFINITIONS,
            path=None,
        )
        settings.set(
            "voice.wake_word_enabled",
            False,
        )

        events: list[tuple[str, str, str]] = []

        runtime = QronosRuntime.__new__(
            QronosRuntime
        )
        runtime.settings = settings
        runtime._runtime_paused = False
        runtime._wake_ready = threading.Event()
        runtime._wake_thread = None
        runtime._notify = (
            lambda event_type, status, message:
            events.append(
                (
                    event_type,
                    status,
                    message,
                )
            )
        )

        runtime.start_wake_listener()

        self.assertIsNone(
            runtime._wake_thread
        )
        self.assertFalse(
            runtime._wake_ready.is_set()
        )
        self.assertEqual(
            events[0][0],
            "wake_word_disabled",
        )

    def test_runtime_source_uses_authoritative_voice_settings(self) -> None:
        source = (
            ROOT
            / "core"
            / "runtime_bridge.py"
        ).read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "threshold=self.wake_threshold",
            source,
        )
        self.assertIn(
            "language=self.voice_language",
            source,
        )
        self.assertIn(
            'SETTINGS_RELOAD_ACTION = "qronos.settings_reload"',
            source,
        )
        self.assertIn(
            "runtime.reload_settings()",
            source,
        )
        self.assertIn(
            "SETTINGS_RELOAD_ACTION,",
            source,
        )

    def test_settings_ui_writes_voice_values_through_the_backend(self) -> None:
        source = (
            ROOT
            / "desktop"
            / "src"
            / "components"
            / "SettingsView.tsx"
        ).read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'from "../settingsStore";',
            source,
        )
        self.assertIn(
            '"voice.wake_word_enabled"',
            source,
        )
        self.assertIn(
            '"voice.wake_sensitivity"',
            source,
        )
        self.assertIn(
            '"voice.language"',
            source,
        )
        self.assertIn(
            '"qronos.settings_reload"',
            source,
        )
        self.assertNotIn(
            "const [wakeWord, setWakeWord]",
            source,
        )
        self.assertNotIn(
            "const [sensitivity, setSensitivity]",
            source,
        )

    def test_stage_2b_claims_real_microphone_but_keeps_wake_test_deferred(self) -> None:
        source = (
            ROOT
            / "desktop"
            / "src"
            / "components"
            / "SettingsView.tsx"
        ).read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '"voice.input_device_id"',
            source,
        )
        self.assertIn(
            'operation: "audio_devices"',
            source,
        )
        self.assertIn(
            '"microphone_input_level"',
            source,
        )
        self.assertNotIn(
            '<Select value="Default Microphone" onChange={() => undefined}',
            source,
        )
        self.assertIn(
            "Test Wake Word برای اتصال به Voice Runtime آماده است.",
            source,
        )
        self.assertEqual(
            SETTINGS_RELOAD_ACTION,
            "qronos.settings_reload",
        )


if __name__ == "__main__":
    unittest.main()
