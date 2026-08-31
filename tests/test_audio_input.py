from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from core.audio_input import AudioConfig, AudioInput


class TestAudioInput(unittest.TestCase):
    def test_default_configuration(self) -> None:
        audio = AudioInput()

        self.assertEqual(audio.config.sample_rate, 16_000)
        self.assertEqual(audio.config.channels, 1)
        self.assertEqual(audio.config.frame_size, 1_280)
        self.assertEqual(audio.config.sample_width, 2)
        self.assertIsNone(audio.config.device)

    def test_custom_configuration(self) -> None:
        config = AudioConfig(
            sample_rate=8_000,
            channels=2,
            frame_size=640,
            sample_width=2,
            device=1,
        )

        audio = AudioInput(config)

        self.assertEqual(audio.config, config)

    def test_initial_state_is_stopped(self) -> None:
        audio = AudioInput()

        self.assertFalse(audio.is_running())

    @patch("core.audio_input._get_sounddevice")
    def test_start_changes_state_to_running(
        self,
        mock_get_sounddevice: MagicMock,
    ) -> None:
        sounddevice = mock_get_sounddevice.return_value
        stream = sounddevice.InputStream.return_value
        stream.active = True

        audio = AudioInput()

        audio.start()

        sounddevice.InputStream.assert_called_once()
        stream.start.assert_called_once()

        self.assertTrue(audio.is_running())

    @patch("core.audio_input._get_sounddevice")
    def test_stop_changes_state_to_stopped(
        self,
        mock_get_sounddevice: MagicMock,
    ) -> None:
        sounddevice = mock_get_sounddevice.return_value
        stream = sounddevice.InputStream.return_value
        stream.active = True

        audio = AudioInput()

        audio.start()
        audio.stop()

        stream.stop.assert_called_once()
        stream.close.assert_called_once()

        self.assertFalse(audio.is_running())

    def test_read_frame_requires_running_input(self) -> None:
        audio = AudioInput()

        with self.assertRaises(RuntimeError):
            audio.read_frame()

    @patch("core.audio_input._get_sounddevice")
    def test_read_frame_returns_expected_size(
        self,
        mock_get_sounddevice: MagicMock,
    ) -> None:
        sounddevice = mock_get_sounddevice.return_value
        stream = sounddevice.InputStream.return_value
        stream.active = True

        stream.read.return_value = (
            np.zeros(
                (1_280, 1),
                dtype=np.int16,
            ),
            False,
        )

        audio = AudioInput()
        audio.start()

        result = audio.read_frame()

        expected_size = (
            audio.config.frame_size
            * audio.config.channels
            * audio.config.sample_width
        )

        self.assertEqual(
            len(result),
            expected_size,
        )

    @patch("core.audio_input._get_sounddevice")
    def test_read_frame_returns_bytes(
        self,
        mock_get_sounddevice: MagicMock,
    ) -> None:
        sounddevice = mock_get_sounddevice.return_value
        stream = sounddevice.InputStream.return_value
        stream.active = True

        stream.read.return_value = (
            np.zeros(
                (1_280, 1),
                dtype=np.int16,
            ),
            False,
        )

        audio = AudioInput()
        audio.start()

        frame = audio.read_frame()

        self.assertIsInstance(frame, bytes)

    @patch("core.audio_input._get_sounddevice")
    def test_stream_uses_expected_audio_configuration(
        self,
        mock_get_sounddevice: MagicMock,
    ) -> None:
        sounddevice = mock_get_sounddevice.return_value
        stream = sounddevice.InputStream.return_value
        stream.active = True

        config = AudioConfig(
            sample_rate=16_000,
            channels=1,
            frame_size=1_280,
            device=1,
        )

        audio = AudioInput(config)
        audio.start()

        sounddevice.InputStream.assert_called_once_with(
            samplerate=16_000,
            channels=1,
            dtype="int16",
            blocksize=1_280,
            device=1,
        )

    @patch("core.audio_input._get_sounddevice")
    def test_start_is_idempotent(
        self,
        mock_get_sounddevice: MagicMock,
    ) -> None:
        sounddevice = mock_get_sounddevice.return_value
        stream = sounddevice.InputStream.return_value
        stream.active = True

        audio = AudioInput()

        audio.start()
        audio.start()

        sounddevice.InputStream.assert_called_once()
        stream.start.assert_called_once()

    @patch("core.audio_input._get_sounddevice")
    def test_start_closes_stream_when_start_fails(
        self,
        mock_get_sounddevice: MagicMock,
    ) -> None:
        sounddevice = mock_get_sounddevice.return_value
        stream = sounddevice.InputStream.return_value
        stream.start.side_effect = RuntimeError(
            "Audio device unavailable."
        )

        audio = AudioInput()

        with self.assertRaises(RuntimeError):
            audio.start()

        stream.close.assert_called_once()
        self.assertFalse(audio.is_running())


class _FakeStream:
    def __init__(
        self,
        *,
        active: bool = False,
    ) -> None:
        self.active = active
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        self.active = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.active = False

    def close(self) -> None:
        self.close_calls += 1
        self.active = False

    def read(
        self,
        _frame_size: int,
    ):
        raise AssertionError(
            "read() is not needed by this regression test."
        )


class _FakeSoundDevice:
    def __init__(
        self,
        replacement: _FakeStream,
    ) -> None:
        self.replacement = replacement
        self.input_stream_calls = 0

    def InputStream(
        self,
        **_kwargs,
    ) -> _FakeStream:
        self.input_stream_calls += 1
        return self.replacement


def test_start_replaces_an_inactive_stale_stream() -> None:
    audio = AudioInput()

    stale = _FakeStream(
        active=False,
    )

    replacement = _FakeStream(
        active=False,
    )

    fake_sd = _FakeSoundDevice(
        replacement
    )

    audio._stream = stale

    with patch(
        "core.audio_input._get_sounddevice",
        return_value=fake_sd,
    ):
        audio.start()

    assert stale.close_calls == 1
    assert fake_sd.input_stream_calls == 1
    assert replacement.start_calls == 1
    assert audio._stream is replacement
    assert audio.is_running() is True


def test_start_keeps_an_already_active_stream() -> None:
    audio = AudioInput()

    active = _FakeStream(
        active=True,
    )

    replacement = _FakeStream(
        active=False,
    )

    fake_sd = _FakeSoundDevice(
        replacement
    )

    audio._stream = active

    with patch(
        "core.audio_input._get_sounddevice",
        return_value=fake_sd,
    ):
        audio.start()

    assert active.close_calls == 0
    assert fake_sd.input_stream_calls == 0
    assert audio._stream is active


if __name__ == "__main__":
    unittest.main()
