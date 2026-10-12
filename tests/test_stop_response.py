from __future__ import annotations

import threading
import unittest

from core.action_cancellation import ActiveActionRegistry
from core.approval_ledger import ApprovalLedger
from core.runtime_bridge import QronosRuntime


class StopResponseRuntimeTests(unittest.TestCase):
    def bare_runtime(self) -> QronosRuntime:
        runtime = QronosRuntime.__new__(QronosRuntime)
        runtime._lock = threading.RLock()
        runtime._busy = False
        runtime._media_host = None
        runtime._voice_playback_complete = threading.Event()
        runtime._voice_playback_id = 1
        runtime._pending_playback_id = None
        runtime._voice_playback_metrics = {}
        runtime._voice_response_stop_requested = threading.Event()
        runtime._computer_actions = ActiveActionRegistry()
        runtime._approval_lock = threading.Lock()
        runtime._approvals = ApprovalLedger()
        runtime._file_write_approval_lease_until = 0.0
        runtime._file_approval_clock = lambda: 0.0
        runtime.file_executor = None
        runtime.paint_draw_executor = None
        runtime._notify = lambda *args, **kwargs: None
        runtime._write_voice_playback_telemetry = (
            lambda *args, **kwargs: None
        )
        return runtime

    def test_stop_cancels_pending_playback(self) -> None:
        runtime = self.bare_runtime()
        runtime._pending_playback_id = 7
        runtime._voice_playback_metrics[7] = {
            "playbackId": 7,
            "status": "playing",
        }

        result = runtime.request_stop_response()

        self.assertTrue(result["playbackCancelled"])
        self.assertEqual(result["playbackId"], 7)
        self.assertIsNone(runtime._pending_playback_id)
        self.assertTrue(runtime._voice_playback_complete.is_set())
        self.assertEqual(
            runtime._voice_playback_metrics[7]["status"],
            "cancelled",
        )
        self.assertTrue(
            runtime._voice_playback_metrics[7]["cancelledByUser"]
        )

    def test_stop_marks_busy_turn_for_suppression(self) -> None:
        runtime = self.bare_runtime()
        runtime._busy = True

        result = runtime.request_stop_response()

        self.assertTrue(result["busyTurnStopRequested"])
        self.assertTrue(
            runtime._voice_response_stop_requested.is_set()
        )

    def test_idle_stop_does_not_poison_next_turn(self) -> None:
        runtime = self.bare_runtime()
        runtime._busy = False

        result = runtime.request_stop_response()

        self.assertFalse(result["busyTurnStopRequested"])
        self.assertFalse(
            runtime._voice_response_stop_requested.is_set()
        )


if __name__ == "__main__":
    unittest.main()
