from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from core.audio_input import AudioConfig, AudioInput
from core.audio_output import (
    OutputDeviceInfo,
    _resample_pcm16,
    canonical_output_device_id,
    list_output_devices,
    play_wave_file,
    resolve_output_device,
)


class _FakeOutputStream:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.closed = False
        self.writes: list[np.ndarray] = []

    def start(self) -> None:
        self.started = True

    def write(self, value) -> None:
        self.writes.append(np.asarray(value).copy())

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class _FakeSoundDevice:
    def __init__(self) -> None:
        self.default = SimpleNamespace(device=(1, 2))
        self.streams: list[_FakeOutputStream] = []
        self._devices = [
            {
                "name": "Primary Sound Driver",
                "hostapi": 0,
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 44_100.0,
            },
            {
                "name": "Speakers (USB Interface)",
                "hostapi": 0,
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 44_100.0,
            },
            {
                "name": "Speakers (USB Interface)",
                "hostapi": 1,
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48_000.0,
            },
            {
                "name": "Headphones (USB Interface)",
                "hostapi": 1,
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48_000.0,
            },
            {
                "name": "USB Mic",
                "hostapi": 1,
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48_000.0,
            },
        ]

    def query_devices(self):
        return list(self._devices)

    def query_hostapis(self):
        return [
            {"name": "Windows DirectSound"},
            {"name": "Windows WASAPI"},
        ]

    def OutputStream(self, **kwargs):
        stream = _FakeOutputStream(**kwargs)
        self.streams.append(stream)
        return stream


class NativeAudioOutputTests(unittest.TestCase):
    @patch("core.audio_output._get_sounddevice")
    def test_output_list_prefers_wasapi_and_deduplicates_aliases(
        self,
        get_sounddevice,
    ) -> None:
        fake = _FakeSoundDevice()
        get_sounddevice.return_value = fake

        devices = list_output_devices()

        self.assertEqual(devices[0].id, "")
        self.assertEqual(devices[0].name, "System Default Output")

        physical = devices[1:]
        self.assertEqual(
            {item.name for item in physical},
            {
                "Speakers (USB Interface)",
                "Headphones (USB Interface)",
            },
        )
        self.assertTrue(
            all(item.host_api == "Windows WASAPI" for item in physical)
        )
        self.assertTrue(
            all(item.id.startswith("portaudio-output:") for item in physical)
        )

        speakers = next(
            item for item in physical if item.name.startswith("Speakers")
        )
        self.assertEqual(resolve_output_device(speakers.id), speakers)
        self.assertEqual(canonical_output_device_id(speakers.id), speakers.id)

    def test_resample_24k_to_48k_doubles_frame_count(self) -> None:
        source = np.arange(240, dtype=np.int16).reshape((-1, 1))
        rendered = _resample_pcm16(source, 24_000, 48_000)
        self.assertEqual(rendered.shape, (480, 1))
        self.assertEqual(rendered.dtype, np.int16)

    @patch("core.audio_output._validated_wave_path")
    @patch("core.audio_output.resolve_output_device")
    @patch("core.audio_output._get_sounddevice")
    def test_native_playback_resamples_to_selected_device_rate(
        self,
        get_sounddevice,
        resolve_output,
        validate_path,
    ) -> None:
        fake = _FakeSoundDevice()
        get_sounddevice.return_value = fake
        resolve_output.return_value = OutputDeviceInfo(
            id="portaudio-output:test:1",
            name="Speakers (USB Interface)",
            host_api="Windows WASAPI",
            device_index=2,
            is_default=False,
            max_output_channels=2,
            default_samplerate=48_000.0,
        )

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "voice.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24_000)
                wav.writeframes(
                    np.zeros(2_400, dtype=np.int16).tobytes()
                )

            validate_path.return_value = path
            result = play_wave_file(path, "portaudio-output:test:1")

        self.assertTrue(result["ok"])
        self.assertTrue(result["resampled"])
        self.assertEqual(result["sourceSampleRate"], 24_000)
        self.assertEqual(result["outputSampleRate"], 48_000)
        self.assertEqual(len(fake.streams), 1)

        stream = fake.streams[0]
        self.assertEqual(stream.kwargs["device"], 2)
        self.assertEqual(stream.kwargs["samplerate"], 48_000)
        self.assertTrue(stream.started)
        self.assertTrue(stream.stopped)
        self.assertTrue(stream.closed)
        written = sum(chunk.shape[0] for chunk in stream.writes)
        self.assertEqual(written, 4_800)


class SelectedWasapiInputTests(unittest.TestCase):
    @patch("core.audio_input._get_sounddevice")
    def test_explicit_wasapi_input_uses_windows_auto_convert(
        self,
        get_sounddevice,
    ) -> None:
        stream = SimpleNamespace(
            active=True,
            start=lambda: None,
            stop=lambda: None,
            close=lambda: None,
        )

        class FakeSoundDevice:
            def __init__(self) -> None:
                self.InputStream = unittest.mock.MagicMock(return_value=stream)
                self.WasapiSettings = unittest.mock.MagicMock(
                    return_value="AUTO_CONVERT"
                )

            def query_devices(self, index):
                self.last_device = index
                return {"hostapi": 2}

            def query_hostapis(self, index):
                self.last_hostapi = index
                return {"name": "Windows WASAPI"}

        fake = FakeSoundDevice()
        get_sounddevice.return_value = fake

        audio = AudioInput(
            AudioConfig(device=24)
        )
        audio.start()

        fake.WasapiSettings.assert_called_once_with(
            auto_convert=True
        )
        fake.InputStream.assert_called_once_with(
            samplerate=16_000,
            channels=1,
            dtype="int16",
            blocksize=1_280,
            device=24,
            extra_settings="AUTO_CONVERT",
        )



if __name__ == "__main__":
    unittest.main()
