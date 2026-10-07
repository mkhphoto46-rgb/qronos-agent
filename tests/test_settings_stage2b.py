from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from core import runtime_bridge
from core.audio_input import (
    _device_fingerprint,
    canonical_input_device_id,
    list_input_devices,
    resolve_input_device,
)
from core.runtime_bridge import (
    INPUT_METER_START_ACTION,
    INPUT_METER_STOP_ACTION,
    QronosRuntime,
)
from core.command_recorder import (
    CommandRecorder,
    CommandRecorderConfig,
)
from core.settings_service import (
    DEFAULT_DEFINITIONS,
    SettingsService,
)


ROOT = Path(__file__).resolve().parent.parent


class _FakeSoundDevice:
    def __init__(self) -> None:
        # Windows/PortAudio exposes routing aliases plus the same physical
        # microphone through legacy MME/DirectSound and WASAPI. The user-facing
        # Settings selector must keep only real capture endpoints.
        self.default = SimpleNamespace(
            device=(8, 99),
        )
        self._devices = [
            {
                "name": "Microsoft Sound Mapper - Input",
                "hostapi": 0,
                "max_input_channels": 2,
                "default_samplerate": 48_000.0,
            },
            {
                "name": "USB Mic",
                "hostapi": 0,
                "max_input_channels": 1,
                "default_samplerate": 48_000.0,
            },
            {
                "name": "Primary Sound Capture Driver",
                "hostapi": 1,
                "max_input_channels": 2,
                "default_samplerate": 48_000.0,
            },
            {
                "name": "USB Mic",
                "hostapi": 1,
                "max_input_channels": 1,
                "default_samplerate": 48_000.0,
            },
            {
                "name": "Speakers",
                "hostapi": 1,
                "max_input_channels": 0,
                "default_samplerate": 48_000.0,
            },
            {
                "name": "USB Mic",
                "hostapi": 2,
                "max_input_channels": 1,
                "default_samplerate": 48_000.0,
            },
            {
                "name": "USB Mic",
                "hostapi": 2,
                "max_input_channels": 1,
                "default_samplerate": 48_000.0,
            },
            {
                "name": "Speakers",
                "hostapi": 2,
                "max_input_channels": 0,
                "default_samplerate": 48_000.0,
            },
            {
                "name": "Built-in Mic",
                "hostapi": 2,
                "max_input_channels": 2,
                "default_samplerate": 48_000.0,
            },
        ]

    def query_devices(self):
        return list(self._devices)

    def query_hostapis(self):
        return [
            {"name": "MME"},
            {"name": "Windows DirectSound"},
            {"name": "Windows WASAPI"},
        ]


class SettingsStage2BTests(unittest.TestCase):
    def test_new_settings_are_durable_control_plane_definitions(self) -> None:
        keys = {
            definition.key
            for definition in
            DEFAULT_DEFINITIONS
        }

        self.assertIn(
            "voice.input_device_id",
            keys,
        )
        self.assertIn(
            "voice.output_device_id",
            keys,
        )
        self.assertIn(
            "appearance.ui_scale_percent",
            keys,
        )
        self.assertIn(
            "system.keep_in_tray",
            keys,
        )

        with tempfile.TemporaryDirectory() as temp:
            path = (
                Path(temp)
                / "settings.json"
            )
            service = SettingsService(
                DEFAULT_DEFINITIONS,
                path=path,
            )

            service.update(
                {
                    "voice.input_device_id":
                        "portaudio:example:1",
                    "voice.output_device_id":
                        "portaudio-output:example:1",
                    "appearance.ui_scale_percent":
                        110,
                    "system.keep_in_tray":
                        True,
                }
            )

            reloaded = SettingsService(
                DEFAULT_DEFINITIONS,
                path=path,
            )

            self.assertEqual(
                reloaded.get(
                    "voice.input_device_id"
                ),
                "portaudio:example:1",
            )
            self.assertEqual(
                reloaded.get(
                    "voice.output_device_id"
                ),
                "portaudio-output:example:1",
            )
            self.assertEqual(
                reloaded.get(
                    "appearance.ui_scale_percent"
                ),
                110,
            )
            self.assertIs(
                reloaded.get(
                    "system.keep_in_tray"
                ),
                True,
            )

    @patch(
        "core.audio_input._get_sounddevice"
    )
    def test_microphone_list_collapses_host_api_aliases_and_keeps_inputs_only(
        self,
        get_sounddevice,
    ) -> None:
        fake = _FakeSoundDevice()
        get_sounddevice.return_value = fake

        first = list_input_devices()
        second = list_input_devices()

        self.assertEqual(first[0].id, "")
        self.assertEqual(first[0].name, "Default Microphone")

        physical = first[1:]
        self.assertEqual(len(physical), 2)
        self.assertEqual(
            {item.name for item in physical},
            {"USB Mic", "Built-in Mic"},
        )
        self.assertTrue(
            all(item.host_api == "Windows WASAPI" for item in physical)
        )
        self.assertNotIn(
            "Speakers",
            {item.name for item in physical},
        )
        self.assertNotIn(
            "Primary Sound Capture Driver",
            {item.name for item in physical},
        )
        self.assertNotIn(
            "Microsoft Sound Mapper - Input",
            {item.name for item in physical},
        )
        self.assertEqual(
            len([item for item in physical if item.name == "USB Mic"]),
            1,
        )
        self.assertEqual(
            [item.id for item in physical],
            [item.id for item in second[1:]],
        )

        built_in = next(
            item for item in physical if item.name == "Built-in Mic"
        )
        self.assertTrue(built_in.is_default)
        self.assertEqual(resolve_input_device(built_in.id), 8)

        # A Stage 2B id persisted from the MME alias transparently maps to the
        # single preferred WASAPI entry instead of becoming unavailable.
        legacy_fingerprint = _device_fingerprint(
            host_api="MME",
            name="USB Mic",
            max_input_channels=1,
            default_samplerate=48_000.0,
        )
        legacy_id = f"portaudio:{legacy_fingerprint}:1"
        usb = next(item for item in physical if item.name == "USB Mic")
        self.assertEqual(canonical_input_device_id(legacy_id), usb.id)
        self.assertEqual(resolve_input_device(legacy_id), 5)

        with self.assertRaises(LookupError):
            resolve_input_device("portaudio:missing:1")

    def test_input_meter_is_real_pcm_math_and_clamped(self) -> None:
        silence = np.zeros(
            1280,
            dtype=np.int16,
        ).tobytes()

        loud = np.full(
            1280,
            20_000,
            dtype=np.int16,
        ).tobytes()

        self.assertEqual(
            QronosRuntime._frame_input_level(
                silence
            ),
            0.0,
        )

        level = (
            QronosRuntime._frame_input_level(
                loud
            )
        )

        self.assertGreater(
            level,
            0.0,
        )
        self.assertLessEqual(
            level,
            1.0,
        )

    def test_settings_cli_audio_devices_is_payloadless(self) -> None:
        from core import settings_cli

        with patch.object(
            settings_cli,
            "_payload",
            side_effect=AssertionError(
                "audio device enumeration attempted to read stdin"
            ),
        ):
            self.assertEqual(
                settings_cli._payload_for_operation(
                    "audio_devices"
                ),
                {},
            )
            self.assertEqual(
                settings_cli._payload_for_operation(
                    "audio_output_devices"
                ),
                {},
            )

    def test_runtime_wake_gate_does_not_wait_for_command_vad_or_stt(self) -> None:
        source = (
            ROOT
            / "core"
            / "runtime_bridge.py"
        ).read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "def prepare_wake_gate(",
            source,
        )
        self.assertIn(
            "def prepare_wake_capture(",
            source,
        )
        self.assertIn(
            "def _warm_wake_command_capture_async(",
            source,
        )
        self.assertIn(
            "def _buffer_wake_command_audio_until_capture_ready(",
            source,
        )
        self.assertIn(
            '"qronos.input_meter_start"',
            source,
        )
        self.assertIn(
            '"qronos.input_meter_stop"',
            source,
        )
        self.assertEqual(
            INPUT_METER_START_ACTION,
            "qronos.input_meter_start",
        )
        self.assertEqual(
            INPUT_METER_STOP_ACTION,
            "qronos.input_meter_stop",
        )

        wake_method_start = source.index(
            "def _wake_listener_loop("
        )
        synth_start = source.index(
            "def _synthesize_response",
            wake_method_start,
        )
        wake_source = source[
            wake_method_start:
            synth_start
        ]

        self.assertIn(
            "self.prepare_wake_gate()",
            wake_source,
        )
        self.assertNotIn(
            "self.prepare_wake_capture()",
            wake_source,
        )
        self.assertLess(
            wake_source.index(
                "self._wake_ready.set()"
            ),
            wake_source.index(
                "self._warm_wake_command_capture_async()"
            ),
        )
        self.assertIn(
            "initial_command_frames = (",
            wake_source,
        )
        self.assertNotIn(
            "ChatterboxRuntime()",
            wake_source,
        )
        self.assertNotIn(
            "Orchestrator()",
            wake_source,
        )

    def test_command_recorder_consumes_buffered_wake_frames_before_live_input(self) -> None:
        class FakeAudio:
            def __init__(self) -> None:
                self.config = SimpleNamespace(
                    sample_width=2,
                    channels=1,
                    sample_rate=16_000,
                    frame_size=1_600,
                )
                self.read_calls = 0

            def is_running(self) -> bool:
                return True

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def read_frame(self) -> bytes:
                self.read_calls += 1
                return b"\x00\x00" * 1_600

        class FakeVad:
            sample_rate = 16_000

            def __init__(self) -> None:
                self.probabilities = [0.9, 0.0]

            def reset(self) -> None:
                pass

            def process_pcm16(self, _frame: bytes):
                return [self.probabilities.pop(0)]

        audio = FakeAudio()
        recorder = CommandRecorder(
            audio_input=audio,
            vad_runtime=FakeVad(),
            config=CommandRecorderConfig(
                silence_seconds=0.1,
                start_timeout_seconds=1.0,
                pre_roll_seconds=0.0,
                min_speech_seconds=0.1,
            ),
        )

        buffered = (
            b"\x01\x00" * 1_600,
            b"\x00\x00" * 1_600,
        )

        with tempfile.TemporaryDirectory() as temp:
            result = recorder.record_to_file(
                Path(temp) / "buffered.wav",
                initial_frames=buffered,
            )

        self.assertEqual(audio.read_calls, 0)
        self.assertGreaterEqual(
            result.speech_seconds,
            0.1,
        )

    def test_wake_startup_has_automatic_retry_without_settings_mutation(self) -> None:
        source = (
            ROOT
            / "core"
            / "runtime_bridge.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "WAKE_STARTUP_RETRY_DELAYS_SECONDS",
            source,
        )
        self.assertIn(
            "def _schedule_wake_start_retry(",
            source,
        )
        self.assertIn(
            '"wake_word_retrying"',
            source,
        )
        self.assertIn(
            "retry_scheduled = (",
            source,
        )
        self.assertIn(
            "failed_wake_thread = threading.current_thread()",
            source,
        )
        self.assertIn(
            "while failed_wake_thread.is_alive():",
            source,
        )
        self.assertIn(
            "if self._wake_thread is failed_wake_thread:",
            source,
        )

    def test_settings_ui_has_live_microphone_scale_and_system_controls(self) -> None:
        source = (
            ROOT
            / "desktop"
            / "src"
            / "components"
            / "SettingsView.tsx"
        ).read_text(
            encoding="utf-8"
        )

        for marker in (
            '"voice.input_device_id"',
            'operation: "audio_devices"',
            "selectedDeviceId",
            '"microphone_input_level"',
            '"qronos.input_meter_start"',
            '"qronos.input_meter_stop"',
            '"appearance.ui_scale_percent"',
            '"set_launch_at_startup"',
            '"set_keep_in_tray"',
        ):
            self.assertIn(
                marker,
                source,
            )

        self.assertIn(
            'options={["90%", "100%", "110%"]}',
            source,
        )
        self.assertNotIn(
            '<Select value="100%" onChange={() => undefined}',
            source,
        )

        # Explicit Stage 2B exclusions.
        self.assertIn(
            'const [uiLanguage, setUiLanguage] = useState("فارسی")',
            source,
        )
        self.assertIn(
            'const [responseLanguage, setResponseLanguage] = useState("Auto")',
            source,
        )
        self.assertIn(
            'defaultValue="100"',
            source,
        )
        self.assertIn(
            "Test Wake Word برای اتصال به Voice Runtime آماده است.",
            source,
        )

    def test_voice_output_device_is_real_native_portaudio_routing(self) -> None:
        settings_source = (
            ROOT
            / "desktop"
            / "src"
            / "components"
            / "SettingsView.tsx"
        ).read_text(encoding="utf-8")

        helper_source = (
            ROOT
            / "desktop"
            / "src"
            / "audio"
            / "audioOutput.ts"
        ).read_text(encoding="utf-8")

        player_source = (
            ROOT
            / "desktop"
            / "src"
            / "audio"
            / "QronosVoicePlayer.ts"
        ).read_text(encoding="utf-8")

        rust_source = (
            ROOT
            / "desktop"
            / "src-tauri"
            / "src"
            / "runtime"
            / "mod.rs"
        ).read_text(encoding="utf-8")

        app_source = (
            ROOT
            / "desktop"
            / "src"
            / "App.tsx"
        ).read_text(encoding="utf-8")

        input_source = (
            ROOT
            / "core"
            / "audio_input.py"
        ).read_text(encoding="utf-8")

        output_source = (
            ROOT
            / "core"
            / "audio_output.py"
        ).read_text(encoding="utf-8")

        cli_source = (
            ROOT
            / "core"
            / "settings_cli.py"
        ).read_text(encoding="utf-8")

        for marker in (
            '"voice.output_device_id"',
            'operation: "audio_output_devices"',
            "canonicalOutputDeviceId",
            "Unavailable output — using system default",
        ):
            self.assertIn(marker, settings_source)

        self.assertNotIn(
            "enumerateAudioOutputDevices",
            settings_source,
        )
        self.assertNotIn(
            "chooseAudioOutputDevice",
            settings_source,
        )
        self.assertNotIn(
            "selectAudioOutput",
            helper_source,
        )
        self.assertNotIn(
            "setSinkId",
            helper_source,
        )

        self.assertIn(
            'NATIVE_OUTPUT_DEVICE_PREFIX = "portaudio-output:"',
            helper_source,
        )
        self.assertIn(
            '"native_audio_output_play"',
            player_source,
        )
        self.assertIn(
            "native_audio_output_play(",
            rust_source,
        )
        self.assertIn(
            "command(Service::Audio)?",
            rust_source,
        )
        self.assertIn(
            'elif operation == "audio_output_devices":',
            cli_source,
        )
        self.assertIn(
            'id=f"portaudio-output:{fingerprint}:{ordinal}"',
            output_source,
        )
        self.assertIn(
            "def _resample_pcm16(",
            output_source,
        )

        # The explicit SB Omni WASAPI endpoints in the field reject 16 kHz
        # direct opens. Shared-mode auto-convert is therefore part of the real
        # selected-microphone contract, while system-default capture is kept
        # unchanged.
        self.assertIn(
            "sd.WasapiSettings(",
            input_source,
        )
        self.assertIn(
            "auto_convert=True",
            input_source,
        )

        self.assertIn(
            'settingsState.values["voice.output_device_id"]',
            app_source,
        )
        self.assertIn(
            ".setOutputDeviceId(",
            app_source,
        )

    def test_app_scale_can_grow_for_4k_and_nav_is_bilingual(self) -> None:
        source = (
            ROOT
            / "desktop"
            / "src"
            / "App.tsx"
        ).read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '"appearance.ui_scale_percent"',
            source,
        )
        self.assertIn(
            "fitScale <= 1.25",
            source,
        )
        self.assertIn(
            "Math.min(\n              2.4,",
            source,
        )

        for english in (
            "<small>Home</small>",
            "<small>Chat</small>",
            "<small>Library</small>",
            "<small>Permissions</small>",
            "<small>Settings</small>",
        ):
            self.assertIn(
                english,
                source,
            )

    def test_oracle_ring_is_owned_by_the_same_local_anchor_as_core_canvas(self) -> None:
        app_source = (
            ROOT
            / "desktop"
            / "src"
            / "App.tsx"
        ).read_text(
            encoding="utf-8"
        )

        orb_source = (
            ROOT
            / "desktop"
            / "src"
            / "components"
            / "QronosOrb.tsx"
        ).read_text(
            encoding="utf-8"
        )

        self.assertNotIn(
            '<div className="orb-ring orb-ring-1" />',
            app_source,
        )
        self.assertIn(
            'data-qronos-oracle-anchor="true"',
            orb_source,
        )
        self.assertIn(
            '<div className="orb-ring orb-ring-1" />',
            orb_source,
        )
        self.assertIn(
            '<div className="orb-ring orb-ring-2" />',
            orb_source,
        )

    def test_tauri_system_integration_is_registered(self) -> None:
        cargo = (
            ROOT
            / "desktop"
            / "src-tauri"
            / "Cargo.toml"
        ).read_text(
            encoding="utf-8"
        )

        lib = (
            ROOT
            / "desktop"
            / "src-tauri"
            / "src"
            / "lib.rs"
        ).read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '"tray-icon"',
            cargo,
        )
        self.assertIn(
            'tauri-plugin-autostart = "2"',
            cargo,
        )

        for marker in (
            "get_system_integration_state",
            "set_launch_at_startup",
            "set_keep_in_tray",
            "TrayIconBuilder",
            "tauri_plugin_autostart::init",
        ):
            self.assertIn(
                marker,
                lib,
            )


class SettingsStage2BDesktopWakeBootstrapTests(unittest.TestCase):
    def test_real_desktop_runtime_bootstraps_wake_before_runtime_ready(self) -> None:
        runtime_source = (
            ROOT
            / "core"
            / "runtime_bridge.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'DESKTOP_WAKE_BOOTSTRAP_ENV = "QRONOS_DESKTOP_WAKE_BOOTSTRAP"',
            runtime_source,
        )
        self.assertIn(
            "def _preload_openwakeword_for_desktop() -> None:",
            runtime_source,
        )
        self.assertIn(
            "runtime.start_wake_listener()",
            runtime_source,
        )
        self.assertIn(
            "runtime._wake_ready.wait(5.0)",
            runtime_source,
        )
        self.assertIn(
            '"wake_bootstrap_ready"',
            runtime_source,
        )

        main_position = runtime_source.index(
            "def main() -> int:"
        )
        bootstrap_position = runtime_source.index(
            "runtime._wake_ready.wait(5.0)",
            main_position,
        )
        ready_position = runtime_source.index(
            '"runtime_ready"',
            main_position,
        )
        self.assertLess(
            bootstrap_position,
            ready_position,
        )

    def test_tauri_marks_only_real_desktop_for_wake_bootstrap(self) -> None:
        rust_source = (
            ROOT
            / "desktop"
            / "src-tauri"
            / "src"
            / "runtime"
            / "mod.rs"
        ).read_text(encoding="utf-8")

        self.assertIn(
            '.env("QRONOS_DESKTOP_WAKE_BOOTSTRAP", "1")',
            rust_source,
        )

    def test_raw_bridge_startup_remains_opt_in(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "QRONOS_DESKTOP_WAKE_BOOTSTRAP": "",
            },
        ):
            self.assertFalse(
                runtime_bridge._desktop_wake_bootstrap_enabled()
            )

        with patch.dict(
            "os.environ",
            {
                "QRONOS_DESKTOP_WAKE_BOOTSTRAP": "1",
            },
        ):
            self.assertTrue(
                runtime_bridge._desktop_wake_bootstrap_enabled()
            )


if __name__ == "__main__":
    unittest.main()
