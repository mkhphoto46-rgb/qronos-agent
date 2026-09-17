from __future__ import annotations

import math
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from core.runtime_bridge import (
    TTS_FIRST_CHUNK_TARGET_CHARACTERS,
    TTS_LATER_CHUNK_TARGET_CHARACTERS,
    TTS_MAX_CHUNK_CHARACTERS,
    TTS_MIN_FIRST_CHUNK_CHARACTERS,
    TTS_MIN_CHUNK_CHARACTERS,
    _analyze_voice_wav_quality,
    _split_semantic_tts_chunks,
)


class VoiceContinuityQualityR7Tests(unittest.TestCase):
    def test_r7_chunk_policy_is_bounded(self) -> None:
        self.assertEqual(
            TTS_FIRST_CHUNK_TARGET_CHARACTERS,
            260,
        )
        self.assertEqual(
            TTS_LATER_CHUNK_TARGET_CHARACTERS,
            440,
        )
        self.assertEqual(
            TTS_MIN_FIRST_CHUNK_CHARACTERS,
            230,
        )
        self.assertEqual(
            TTS_MIN_CHUNK_CHARACTERS,
            96,
        )
        self.assertEqual(
            TTS_MAX_CHUNK_CHARACTERS,
            520,
        )

    def test_short_first_sentence_borrows_words_instead_of_underfilling(self) -> None:
        text = (
            "این جملهٔ اول کوتاه است. "
            "جملهٔ دوم عمداً طولانی نوشته شده تا مشخص شود تقسیم‌کنندهٔ "
            "صوتی باید فقط به اندازهٔ لازم از ادامهٔ متن قرض بگیرد و "
            "چانک اول را به یک بافر واقعی و قابل استفاده تبدیل کند، "
            "بدون آن‌که کل جملهٔ بعدی را یک‌جا ببلعد یا کلمه‌ای حذف شود. "
            "بعد از آن هم پاسخ باید به شکل طبیعی ادامه پیدا کند."
        )

        chunks = _split_semantic_tts_chunks(
            text
        )

        self.assertGreaterEqual(
            len(chunks[0]),
            TTS_MIN_FIRST_CHUNK_CHARACTERS,
        )
        self.assertLessEqual(
            len(chunks[0]),
            TTS_MAX_CHUNK_CHARACTERS,
        )

    def test_no_chunk_can_exceed_hard_quality_cap(self) -> None:
        text = " ".join(
            f"واژه{i}"
            for i in range(600)
        )

        chunks = _split_semantic_tts_chunks(
            text
        )

        self.assertTrue(chunks)
        self.assertTrue(
            all(
                len(chunk)
                <= TTS_MAX_CHUNK_CHARACTERS
                for chunk in chunks
            )
        )

    def test_chunking_preserves_all_normalized_text(self) -> None:
        text = (
            "بخش اول پاسخ.   بخش دوم، با فاصله‌های اضافی و جمله‌های متعدد. "
            "هیچ کلمه‌ای نباید هنگام تقسیم برای TTS حذف یا جابه‌جا شود. "
            "این بخش آخر هم باید باقی بماند."
        )

        expected = " ".join(
            text.split()
        )
        chunks = _split_semantic_tts_chunks(
            text
        )
        reconstructed = " ".join(chunks)

        self.assertEqual(
            reconstructed,
            expected,
        )

    def test_pcm_quality_probe_reports_clean_sine_without_clipping(self) -> None:
        sample_rate = 24_000
        seconds = 0.5
        frequency = 440.0
        t = np.arange(
            int(sample_rate * seconds),
            dtype=np.float64,
        ) / sample_rate
        samples = (
            np.sin(
                2.0 * math.pi * frequency * t
            )
            * 0.25
            * 32767.0
        ).astype(np.int16)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "clean.wav"

            with wave.open(
                str(path),
                "wb",
            ) as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(
                    sample_rate
                )
                wav_file.writeframes(
                    samples.tobytes()
                )

            metrics = _analyze_voice_wav_quality(
                path
            )

        self.assertEqual(
            metrics["sampleRate"],
            sample_rate,
        )
        self.assertEqual(
            metrics["channels"],
            1,
        )
        self.assertEqual(
            metrics["clippedSamplePercent"],
            0.0,
        )
        self.assertLess(
            abs(
                float(
                    metrics["dcOffset"]
                )
            ),
            0.001,
        )
        self.assertLess(
            float(
                metrics[
                    "highFrequencyEnergyPercent"
                ]
            ),
            0.1,
        )


if __name__ == "__main__":
    unittest.main()
