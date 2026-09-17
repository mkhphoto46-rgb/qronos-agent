from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import core.runtime_bridge as runtime_bridge


class TestLivePlaybackTelemetry(unittest.TestCase):
    def _runtime(self) -> runtime_bridge.QronosRuntime:
        runtime = object.__new__(runtime_bridge.QronosRuntime)
        runtime._lock = threading.RLock()
        runtime._voice_playback_complete = threading.Event()
        runtime._voice_playback_id = 0
        runtime._pending_playback_id = None
        runtime._voice_playback_metrics = {}
        return runtime

    def test_response_to_real_desktop_playback_timing_is_persisted(self) -> None:
        runtime = self._runtime()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "voice_playback_latest.json"

            with patch.object(
                runtime_bridge,
                "VOICE_PLAYBACK_TELEMETRY_PATH",
                target,
            ):
                playback_id = runtime._begin_voice_playback_wait()
                runtime.set_voice_playback_context(
                    playback_id,
                    trigger_source="wake_word",
                    transcript="دو به اضافه دو چند میشه؟",
                    response="می‌شود چهار.",
                )
                time.sleep(0.005)
                runtime.mark_voice_first_audio_ready(playback_id)
                runtime.set_voice_playback_audio_metadata(
                    playback_id,
                    total_audio_seconds=1.04,
                    chunk_count=1,
                )
                time.sleep(0.005)
                self.assertTrue(
                    runtime.mark_voice_playback_started(playback_id)
                )
                time.sleep(0.005)
                self.assertTrue(
                    runtime.mark_voice_playback_complete(playback_id)
                )

                payload = json.loads(
                    target.read_text(encoding="utf-8")
                )

        self.assertEqual(payload["playbackId"], playback_id)
        self.assertEqual(payload["triggerSource"], "wake_word")
        self.assertEqual(payload["transcript"], "دو به اضافه دو چند میشه؟")
        self.assertEqual(payload["response"], "می‌شود چهار.")
        self.assertEqual(payload["chunkCount"], 1)
        self.assertEqual(payload["generatedAudioSeconds"], 1.04)
        self.assertEqual(payload["status"], "complete")

        self.assertIsNotNone(
            payload["responseReadyToFirstAudioReadySeconds"]
        )
        self.assertIsNotNone(
            payload["firstAudioReadyToDesktopPlaybackStartSeconds"]
        )
        self.assertIsNotNone(
            payload["responseReadyToDesktopPlaybackStartSeconds"]
        )
        self.assertIsNotNone(
            payload["desktopPlaybackDurationSeconds"]
        )

        self.assertGreaterEqual(
            payload["responseReadyToDesktopPlaybackStartSeconds"],
            0.0,
        )
        self.assertGreaterEqual(
            payload["desktopPlaybackDurationSeconds"],
            0.0,
        )

    def test_stale_playback_start_is_rejected(self) -> None:
        runtime = self._runtime()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "voice_playback_latest.json"
            with patch.object(
                runtime_bridge,
                "VOICE_PLAYBACK_TELEMETRY_PATH",
                target,
            ):
                playback_id = runtime._begin_voice_playback_wait()
                self.assertFalse(
                    runtime.mark_voice_playback_started(
                        playback_id + 1
                    )
                )
                self.assertTrue(
                    runtime.mark_voice_playback_started(
                        playback_id
                    )
                )


if __name__ == "__main__":
    unittest.main()
