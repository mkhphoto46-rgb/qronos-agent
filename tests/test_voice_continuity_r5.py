from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.runtime_bridge import (
    QronosRuntime,
    TTS_FIRST_CHUNK_TARGET_CHARACTERS,
    TTS_LATER_CHUNK_TARGET_CHARACTERS,
    TTS_MIN_CHUNK_CHARACTERS,
)


class _SequenceVoiceOutput:
    def __init__(
        self,
        values: list[tuple[float, float]],
    ) -> None:
        self.values = list(values)
        self.index = 0

    def speak_to_file(self, text: str):
        audio_seconds, generation_seconds = (
            self.values[self.index]
        )
        self.index += 1

        return SimpleNamespace(
            audio_path=Path(
                f"H:/QronosAI/r5_chunk_{self.index}.wav"
            ),
            audio_seconds=audio_seconds,
            took_seconds=generation_seconds,
            real_time_factor=(
                generation_seconds / audio_seconds
            ),
        )


class VoiceContinuityR5Tests(unittest.TestCase):
    def bare_runtime(
        self,
        values: list[tuple[float, float]],
    ) -> QronosRuntime:
        runtime = QronosRuntime.__new__(QronosRuntime)
        runtime.voice_output = _SequenceVoiceOutput(
            values
        )
        runtime._voice_response_stop_requested = (
            threading.Event()
        )
        runtime._notify = lambda *args, **kwargs: None
        runtime.mark_voice_first_audio_ready = (
            lambda playback_id: True
        )
        runtime.set_voice_playback_audio_metadata = (
            lambda *args, **kwargs: None
        )
        return runtime

    def test_continuity_targets_remain_large_enough_to_amortize_tts(self):
        self.assertGreaterEqual(
            TTS_FIRST_CHUNK_TARGET_CHARACTERS,
            220,
        )
        self.assertGreaterEqual(
            TTS_LATER_CHUNK_TARGET_CHARACTERS,
            400,
        )
        self.assertGreaterEqual(
            TTS_MIN_CHUNK_CHARACTERS,
            64,
        )

    def test_predicted_underrun_is_recorded_per_chunk(self):
        runtime = self.bare_runtime(
            [
                (6.0, 5.0),
                (15.0, 7.0),
                (15.0, 7.0),
            ]
        )

        with patch(
            "core.runtime_bridge._split_semantic_tts_chunks",
            return_value=[
                "a" * 220,
                "b" * 480,
                "c" * 480,
            ],
        ):
            result = runtime._synthesize_response_chunks(
                "ignored",
                playback_id=71,
            )

        metrics = result["chunkMetrics"]

        self.assertEqual(len(metrics), 3)
        self.assertEqual(
            metrics[0][
                "predictedGapBeforeChunkSeconds"
            ],
            0.0,
        )
        self.assertEqual(
            metrics[1][
                "predictedGapBeforeChunkSeconds"
            ],
            1.0,
        )
        self.assertEqual(
            metrics[2][
                "predictedGapBeforeChunkSeconds"
            ],
            0.0,
        )
        self.assertEqual(
            result["predictedUnderrunSeconds"],
            1.0,
        )

    def test_continuity_metrics_preserve_every_generated_chunk(self):
        runtime = self.bare_runtime(
            [
                (8.0, 5.0),
                (20.0, 9.0),
            ]
        )

        chunks = [
            "اولین بخش پاسخ برای تست پیوستگی صوتی.",
            "بخش دوم باید بدون حذف شدن در متریک‌ها باقی بماند.",
        ]

        with patch(
            "core.runtime_bridge._split_semantic_tts_chunks",
            return_value=chunks,
        ):
            result = runtime._synthesize_response_chunks(
                "ignored",
                playback_id=72,
            )

        self.assertEqual(
            result["spokenChunks"],
            chunks,
        )
        self.assertEqual(
            result["chunkCount"],
            2,
        )
        self.assertEqual(
            len(result["chunkMetrics"]),
            2,
        )


if __name__ == "__main__":
    unittest.main()
