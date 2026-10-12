from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.runtime_bridge import QronosRuntime


class _FakeVoiceOutput:
    def __init__(self, fail_on_call: int | None = None) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call

    def speak_to_file(self, text: str):
        self.calls += 1

        if (
            self.fail_on_call is not None
            and self.calls == self.fail_on_call
        ):
            raise RuntimeError(
                f"synthetic TTS failure on call {self.calls}"
            )

        return SimpleNamespace(
            audio_path=Path(
                f"H:/QronosAI/test_chunk_{self.calls}.wav"
            ),
            audio_seconds=1.25,
            took_seconds=0.4,
            real_time_factor=0.32,
        )


class VoicePartialChunkRecoveryTests(unittest.TestCase):
    def runtime(
        self,
        *,
        fail_on_call: int | None,
    ) -> tuple[QronosRuntime, list[tuple[str, str, str]]]:
        runtime = QronosRuntime.__new__(QronosRuntime)
        runtime.voice_output = _FakeVoiceOutput(
            fail_on_call=fail_on_call
        )
        runtime._voice_response_stop_requested = (
            threading.Event()
        )

        events: list[tuple[str, str, str]] = []
        runtime._notify = (
            lambda event_type, status, message:
                events.append(
                    (event_type, status, message)
                )
        )

        runtime.mark_voice_first_audio_ready = (
            lambda playback_id: True
        )
        runtime.set_voice_playback_audio_metadata = (
            lambda *args, **kwargs: None
        )

        return runtime, events

    def test_later_chunk_failure_preserves_generated_prefix(
        self,
    ) -> None:
        runtime, events = self.runtime(
            fail_on_call=2
        )

        with patch(
            "core.runtime_bridge._split_semantic_tts_chunks",
            return_value=[
                "chunk one",
                "chunk two",
                "chunk three",
            ],
        ):
            result = runtime._synthesize_response_chunks(
                "ignored",
                playback_id=17,
            )

        self.assertEqual(
            result["chunkCount"],
            1,
        )
        self.assertEqual(
            result["spokenChunks"],
            ["chunk one"],
        )
        self.assertAlmostEqual(
            result["totalAudioSeconds"],
            1.25,
        )
        self.assertEqual(
            result["partialTtsError"],
            "synthetic TTS failure on call 2",
        )
        self.assertFalse(
            result["stoppedByUser"]
        )
        self.assertTrue(
            any(
                event_type == "runtime_warning"
                and "continuation stopped" in message
                for event_type, _, message in events
            )
        )

    def test_first_chunk_failure_remains_real_failure(
        self,
    ) -> None:
        runtime, _ = self.runtime(
            fail_on_call=1
        )

        with patch(
            "core.runtime_bridge._split_semantic_tts_chunks",
            return_value=[
                "chunk one",
                "chunk two",
            ],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "synthetic TTS failure on call 1",
            ):
                runtime._synthesize_response_chunks(
                    "ignored",
                    playback_id=18,
                )


if __name__ == "__main__":
    unittest.main()
