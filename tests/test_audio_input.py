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

    @patch("core.audio_input.sd.InputStream")
    def test_start_changes_state_to_running(
        self,
        mock_input_stream: MagicMock,
    ) -> None:
        stream = mock_input_stream.return_value
        stream.active = True

        audio = AudioInput()

        audio.start()

        mock_input_stream.assert_called_once()
        stream.start.assert_called_once()

        self.assertTrue(audio.is_running())

    @patch("core.audio_input.sd.InputStream")
    def test_stop_changes_state_to_stopped(
        self,
        mock_input_stream: MagicMock,
    ) -> None:
        stream = mock_input_stream.return_value
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

    @patch("core.audio_input.sd.InputStream")
    def test_read_frame_returns_expected_size(
        self,
        mock_input_stream: MagicMock,
    ) -> None:
        stream = mock_input_stream.return_value
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

    @patch("core.audio_input.sd.InputStream")
    def test_read_frame_returns_bytes(
        self,
        mock_input_stream: MagicMock,
    ) -> None:
        stream = mock_input_stream.return_value
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

    @patch("core.audio_input.sd.InputStream")
    def test_stream_uses_expected_audio_configuration(
        self,
        mock_input_stream: MagicMock,
    ) -> None:
        stream = mock_input_stream.return_value
        stream.active = True

        config = AudioConfig(
            sample_rate=16_000,
            channels=1,
            frame_size=1_280,
            device=1,
        )

        audio = AudioInput(config)
        audio.start()

        mock_input_stream.assert_called_once_with(
            samplerate=16_000,
            channels=1,
            dtype="int16",
            blocksize=1_280,
            device=1,
        )

    @patch("core.audio_input.sd.InputStream")
    def test_start_is_idempotent(
        self,
        mock_input_stream: MagicMock,
    ) -> None:
        stream = mock_input_stream.return_value
        stream.active = True

        audio = AudioInput()

        audio.start()
        audio.start()

        mock_input_stream.assert_called_once()
        stream.start.assert_called_once()

    @patch("core.audio_input.sd.InputStream")
    def test_start_closes_stream_when_start_fails(
        self,
        mock_input_stream: MagicMock,
    ) -> None:
        stream = mock_input_stream.return_value
        stream.start.side_effect = RuntimeError(
            "Audio device unavailable."
        )

        audio = AudioInput()

        with self.assertRaises(RuntimeError):
            audio.start()

        stream.close.assert_called_once()
        self.assertFalse(audio.is_running())


if __name__ == "__main__":
    unittest.main()