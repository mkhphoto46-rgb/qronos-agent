from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.runtime_bridge import QronosRuntime


class _FakeVoiceOutput:
    def __init__(self) -> None:
        self.calls = 0

    def speak_to_file(self, text: str):
        self.calls += 1
        return SimpleNamespace(
            audio_path=Path(
                f"H:/QronosAI/r6_chunk_{self.calls}.wav"
            ),
            audio_seconds=4.0,
            took_seconds=1.0,
            real_time_factor=0.25,
        )


class VoiceEchoBarrierR6Tests(unittest.TestCase):
    def runtime(self):
        runtime = QronosRuntime.__new__(QronosRuntime)
        runtime.voice_output = _FakeVoiceOutput()
        runtime._voice_response_stop_requested = threading.Event()
        runtime.mark_voice_first_audio_ready = (
            lambda playback_id: True
        )
        runtime.set_voice_playback_audio_metadata = (
            lambda *args, **kwargs: None
        )

        events: list[tuple[str, str, str]] = []
        runtime._notify = (
            lambda event_type, status, message:
                events.append(
                    (event_type, status, message)
                )
        )

        return runtime, events

    def test_stream_complete_is_emitted_after_all_audio_ready_events(self):
        runtime, events = self.runtime()

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
                playback_id=91,
            )

        self.assertEqual(
            result["chunkCount"],
            3,
        )

        audio_events = [
            event
            for event in events
            if event[0] == "voice_audio_ready"
        ]
        stream_events = [
            event
            for event in events
            if event[0] == "voice_audio_stream_complete"
        ]

        self.assertEqual(
            len(audio_events),
            3,
        )
        self.assertEqual(
            len(stream_events),
            1,
        )

        event_types = [
            event[0]
            for event in events
        ]

        self.assertGreater(
            event_types.index(
                "voice_audio_stream_complete"
            ),
            max(
                index
                for index, event_type
                in enumerate(event_types)
                if event_type == "voice_audio_ready"
            ),
        )

        payload = json.loads(
            stream_events[0][2]
        )

        self.assertEqual(
            payload["playbackId"],
            91,
        )
        self.assertEqual(
            payload["generatedChunkCount"],
            3,
        )
        self.assertEqual(
            payload["plannedChunkCount"],
            3,
        )
        self.assertFalse(
            payload["stoppedByUser"]
        )
        self.assertIsNone(
            payload["partialTtsError"]
        )

    def test_partial_generation_still_closes_stream(self):
        runtime, events = self.runtime()

        original = runtime.voice_output.speak_to_file

        def fail_second(text: str):
            if runtime.voice_output.calls == 1:
                raise RuntimeError(
                    "synthetic second-chunk failure"
                )
            return original(text)

        runtime.voice_output.speak_to_file = fail_second

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
                playback_id=92,
            )

        self.assertEqual(
            result["chunkCount"],
            1,
        )

        stream_events = [
            event
            for event in events
            if event[0] == "voice_audio_stream_complete"
        ]

        self.assertEqual(
            len(stream_events),
            1,
        )

        payload = json.loads(
            stream_events[0][2]
        )

        self.assertEqual(
            payload["generatedChunkCount"],
            1,
        )
        self.assertEqual(
            payload["plannedChunkCount"],
            3,
        )
        self.assertIn(
            "synthetic second-chunk failure",
            payload["partialTtsError"],
        )


if __name__ == "__main__":
    unittest.main()
