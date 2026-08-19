from __future__ import annotations

import unittest

from core.audio_input import AudioConfig, AudioInput


class TestAudioInput(unittest.TestCase):
    def test_default_configuration(self) -> None:
        audio = AudioInput()

        self.assertEqual(audio.config.sample_rate, 16_000)
        self.assertEqual(audio.config.channels, 1)
        self.assertEqual(audio.config.frame_size, 1_280)
        self.assertEqual(audio.config.sample_width, 2)

    def test_custom_configuration(self) -> None:
        config = AudioConfig(
            sample_rate=8_000,
            channels=2,
            frame_size=640,
            sample_width=2,
        )

        audio = AudioInput(config)

        self.assertEqual(audio.config, config)

    def test_initial_state_is_stopped(self) -> None:
        audio = AudioInput()

        self.assertFalse(audio.is_running())

    def test_start_changes_state_to_running(self) -> None:
        audio = AudioInput()

        audio.start()

        self.assertTrue(audio.is_running())

    def test_stop_changes_state_to_stopped(self) -> None:
        audio = AudioInput()

        audio.start()
        audio.stop()

        self.assertFalse(audio.is_running())

    def test_read_frame_requires_running_input(self) -> None:
        audio = AudioInput()

        with self.assertRaises(RuntimeError):
            audio.read_frame()

    def test_read_frame_returns_expected_size(self) -> None:
        audio = AudioInput()

        audio.start()

        frame = audio.read_frame()

        expected_size = (
            audio.config.frame_size
            * audio.config.channels
            * audio.config.sample_width
        )

        self.assertEqual(
            len(frame),
            expected_size,
        )

    def test_read_frame_returns_bytes(self) -> None:
        audio = AudioInput()

        audio.start()

        frame = audio.read_frame()

        self.assertIsInstance(frame, bytes)


if __name__ == "__main__":
    unittest.main()