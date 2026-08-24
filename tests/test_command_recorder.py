from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from core.audio_input import AudioConfig
from core.command_recorder import (
    CommandRecorder,
    CommandRecorderConfig,
)
from core.vad_runtime import VADRuntime


def make_frame(
    value: int = 0,
    frame_size: int = 1_600,
) -> bytes:
    return np.full(
        frame_size,
        value,
        dtype=np.int16,
    ).tobytes()


class FakeAudioInput:
    def __init__(
        self,
        frames: list[bytes],
        running: bool = False,
        sample_rate: int = 16_000,
    ) -> None:
        self.config = AudioConfig(
            sample_rate=sample_rate,
            channels=1,
            frame_size=1_600,
            sample_width=2,
        )

        self.frames = list(
            frames
        )

        self.running = running

        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        self.running = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.running = False

    def is_running(self) -> bool:
        return self.running

    def read_frame(self) -> bytes:
        if not self.running:
            raise RuntimeError(
                "Fake audio input is not running."
            )

        if self.frames:
            return self.frames.pop(0)

        return make_frame()


class FakeVADRuntime(VADRuntime):
    def __init__(
        self,
        probabilities: list[
            tuple[float, ...]
        ],
    ) -> None:
        self.probabilities = list(
            probabilities
        )

        self.reset_calls = 0
        self.close_calls = 0

    @property
    def sample_rate(self) -> int:
        return 16_000

    def health_check(self) -> bool:
        return True

    def process_pcm16(
        self,
        audio_data: bytes,
    ) -> tuple[float, ...]:
        del audio_data

        if self.probabilities:
            return self.probabilities.pop(
                0
            )

        return (
            0.0,
        )

    def reset(self) -> None:
        self.reset_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class TestCommandRecorder(
    unittest.TestCase
):
    def test_default_configuration(
        self,
    ) -> None:
        config = CommandRecorderConfig()

        self.assertEqual(
            config.speech_start_threshold,
            0.50,
        )

        self.assertEqual(
            config.speech_continue_threshold,
            0.50,
        )

        self.assertEqual(
            config.silence_seconds,
            2.0,
        )

        self.assertEqual(
            config.max_duration_seconds,
            60.0,
        )

    def test_invalid_configuration_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(
            ValueError
        ):
            CommandRecorderConfig(
                speech_start_threshold=0,
            )

        with self.assertRaises(
            ValueError
        ):
            CommandRecorderConfig(
                speech_start_threshold=1.1,
            )

        with self.assertRaises(
            ValueError
        ):
            CommandRecorderConfig(
                speech_continue_threshold=0,
            )

        with self.assertRaises(
            ValueError
        ):
            CommandRecorderConfig(
                speech_start_threshold=0.5,
                speech_continue_threshold=0.6,
            )

        with self.assertRaises(
            ValueError
        ):
            CommandRecorderConfig(
                silence_seconds=0,
            )

        with self.assertRaises(
            ValueError
        ):
            CommandRecorderConfig(
                max_duration_seconds=0,
            )

        with self.assertRaises(
            ValueError
        ):
            CommandRecorderConfig(
                start_timeout_seconds=0,
            )

    def test_audio_sample_rate_must_match_vad(
        self,
    ) -> None:
        audio = FakeAudioInput(
            [],
            sample_rate=8_000,
        )

        vad = FakeVADRuntime(
            []
        )

        with self.assertRaises(
            ValueError
        ):
            CommandRecorder(
                audio,
                vad,
            )

    def test_recording_stops_after_trailing_silence(
        self,
    ) -> None:
        audio = FakeAudioInput(
            [
                make_frame(),
                make_frame(),
                make_frame(),
                make_frame(),
                make_frame(),
            ]
        )

        vad = FakeVADRuntime(
            [
                (0.05,),
                (0.80,),
                (0.70,),
                (0.10,),
                (0.10,),
            ]
        )

        recorder = CommandRecorder(
            audio,
            vad,
            CommandRecorderConfig(
                speech_start_threshold=0.50,
                speech_continue_threshold=0.35,
                silence_seconds=0.2,
                max_duration_seconds=2.0,
                start_timeout_seconds=1.0,
                pre_roll_seconds=0.1,
                min_speech_seconds=0.2,
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            output = (
                Path(directory)
                / "command.wav"
            )

            result = (
                recorder.record_to_file(
                    output
                )
            )

            self.assertTrue(
                output.is_file()
            )

            self.assertTrue(
                result.stopped_by_silence
            )

            self.assertAlmostEqual(
                result.duration_seconds,
                0.5,
                places=5,
            )

            self.assertAlmostEqual(
                result.speech_seconds,
                0.2,
                places=5,
            )

            self.assertAlmostEqual(
                result.peak_speech_probability,
                0.80,
                places=5,
            )

            self.assertEqual(
                audio.start_calls,
                1,
            )

            self.assertEqual(
                audio.stop_calls,
                1,
            )

            with wave.open(
                str(output),
                "rb",
            ) as wav_file:
                self.assertEqual(
                    wav_file.getframerate(),
                    16_000,
                )

                self.assertEqual(
                    wav_file.getnchannels(),
                    1,
                )

                self.assertEqual(
                    wav_file.getsampwidth(),
                    2,
                )

    def test_continue_threshold_keeps_quiet_speech_alive(
        self,
    ) -> None:
        audio = FakeAudioInput(
            [
                make_frame(),
                make_frame(),
                make_frame(),
                make_frame(),
                make_frame(),
            ]
        )

        vad = FakeVADRuntime(
            [
                (0.80,),
                (0.40,),
                (0.40,),
                (0.10,),
                (0.10,),
            ]
        )

        recorder = CommandRecorder(
            audio,
            vad,
            CommandRecorderConfig(
                speech_start_threshold=0.50,
                speech_continue_threshold=0.35,
                silence_seconds=0.2,
                max_duration_seconds=2.0,
                start_timeout_seconds=1.0,
                pre_roll_seconds=0,
                min_speech_seconds=0.2,
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            result = (
                recorder.record_to_file(
                    Path(directory)
                    / "command.wav"
                )
            )

        self.assertTrue(
            result.stopped_by_silence
        )

        self.assertAlmostEqual(
            result.speech_seconds,
            0.3,
            places=5,
        )

    def test_probability_below_start_threshold_does_not_start(
        self,
    ) -> None:
        audio = FakeAudioInput(
            [
                make_frame(),
                make_frame(),
                make_frame(),
            ]
        )

        vad = FakeVADRuntime(
            [
                (0.40,),
                (0.40,),
                (0.40,),
            ]
        )

        recorder = CommandRecorder(
            audio,
            vad,
            CommandRecorderConfig(
                speech_start_threshold=0.50,
                speech_continue_threshold=0.35,
                silence_seconds=0.2,
                max_duration_seconds=1.0,
                start_timeout_seconds=0.3,
                pre_roll_seconds=0.1,
                min_speech_seconds=0.1,
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(
                TimeoutError
            ):
                recorder.record_to_file(
                    Path(directory)
                    / "command.wav"
                )

    def test_no_speech_times_out(
        self,
    ) -> None:
        audio = FakeAudioInput(
            [
                make_frame(),
                make_frame(),
                make_frame(),
            ]
        )

        vad = FakeVADRuntime(
            [
                (0.05,),
                (0.04,),
                (0.03,),
            ]
        )

        recorder = CommandRecorder(
            audio,
            vad,
            CommandRecorderConfig(
                speech_start_threshold=0.50,
                speech_continue_threshold=0.35,
                silence_seconds=0.2,
                max_duration_seconds=1.0,
                start_timeout_seconds=0.3,
                pre_roll_seconds=0.1,
                min_speech_seconds=0.1,
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(
                TimeoutError
            ):
                recorder.record_to_file(
                    Path(directory)
                    / "command.wav"
                )

        self.assertEqual(
            vad.reset_calls,
            1,
        )

        self.assertEqual(
            audio.start_calls,
            1,
        )

        self.assertEqual(
            audio.stop_calls,
            1,
        )

    def test_existing_audio_stream_is_not_stopped(
        self,
    ) -> None:
        audio = FakeAudioInput(
            [
                make_frame(),
                make_frame(),
                make_frame(),
            ],
            running=True,
        )

        vad = FakeVADRuntime(
            [
                (0.80,),
                (0.10,),
                (0.10,),
            ]
        )

        recorder = CommandRecorder(
            audio,
            vad,
            CommandRecorderConfig(
                speech_start_threshold=0.50,
                speech_continue_threshold=0.35,
                silence_seconds=0.2,
                max_duration_seconds=1.0,
                start_timeout_seconds=0.5,
                pre_roll_seconds=0,
                min_speech_seconds=0.1,
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            recorder.record_to_file(
                Path(directory)
                / "command.wav"
            )

        self.assertEqual(
            audio.start_calls,
            0,
        )

        self.assertEqual(
            audio.stop_calls,
            0,
        )

        self.assertTrue(
            audio.is_running()
        )

    def test_max_duration_limits_recording(
        self,
    ) -> None:
        audio = FakeAudioInput(
            [
                make_frame(),
                make_frame(),
                make_frame(),
                make_frame(),
            ]
        )

        vad = FakeVADRuntime(
            [
                (0.90,),
                (0.90,),
                (0.90,),
                (0.90,),
            ]
        )

        recorder = CommandRecorder(
            audio,
            vad,
            CommandRecorderConfig(
                speech_start_threshold=0.50,
                speech_continue_threshold=0.35,
                silence_seconds=0.5,
                max_duration_seconds=0.3,
                start_timeout_seconds=0.5,
                pre_roll_seconds=0,
                min_speech_seconds=0.1,
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            result = (
                recorder.record_to_file(
                    Path(directory)
                    / "command.wav"
                )
            )

        self.assertFalse(
            result.stopped_by_silence
        )

        self.assertAlmostEqual(
            result.duration_seconds,
            0.3,
            places=5,
        )

    def test_vad_is_reset_before_recording(
        self,
    ) -> None:
        audio = FakeAudioInput(
            [
                make_frame(),
                make_frame(),
                make_frame(),
            ]
        )

        vad = FakeVADRuntime(
            [
                (0.80,),
                (0.10,),
                (0.10,),
            ]
        )

        recorder = CommandRecorder(
            audio,
            vad,
            CommandRecorderConfig(
                speech_start_threshold=0.50,
                speech_continue_threshold=0.35,
                silence_seconds=0.2,
                max_duration_seconds=1.0,
                start_timeout_seconds=0.5,
                pre_roll_seconds=0,
                min_speech_seconds=0.1,
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            recorder.record_to_file(
                Path(directory)
                / "command.wav"
            )

        self.assertEqual(
            vad.reset_calls,
            1,
        )


if __name__ == "__main__":
    unittest.main()