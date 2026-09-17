from __future__ import annotations

import threading
import unittest

from core.runtime_bridge import QronosRuntime
from core.task_router import TaskRouter, TaskType


class FakeVoiceOutput:
    def __init__(self) -> None:
        self.is_loaded = False
        self.prewarm_calls = 0
        self.release_calls = 0

    def health_check(self) -> bool:
        return True

    def prewarm(self) -> None:
        self.prewarm_calls += 1
        self.is_loaded = True

    def release(self) -> None:
        self.release_calls += 1
        self.is_loaded = False


def bare_runtime() -> QronosRuntime:
    runtime = QronosRuntime.__new__(QronosRuntime)
    runtime._lock = threading.Lock()
    runtime.voice_output = None
    runtime._voice_output_prewarm_thread = None
    runtime._voice_output_prewarm_error = None
    return runtime


class TestVoiceLatencyHeavyPromotion(unittest.TestCase):
    def test_simple_why_stays_fast(self) -> None:
        runtime = bare_runtime()
        runtime.compute_estimator = __import__(
            "core.compute_estimator",
            fromlist=["ComputeEstimator"],
        ).ComputeEstimator()
        runtime.intent_gate = __import__(
            "core.intent_gate",
            fromlist=["IntentGate"],
        ).IntentGate()

        text = "چرا آسمان آبیه؟"
        legacy = TaskRouter().route(text).task_type
        selected, details = runtime._select_production_task_type(
            text,
            legacy,
        )

        self.assertIs(legacy, TaskType.FAST)
        self.assertIs(selected, TaskType.FAST)
        self.assertFalse(details["applied"])

    def test_multi_signal_reasoning_promotes_fast_to_heavy(self) -> None:
        runtime = bare_runtime()
        runtime.compute_estimator = __import__(
            "core.compute_estimator",
            fromlist=["ComputeEstimator"],
        ).ComputeEstimator()
        runtime.intent_gate = __import__(
            "core.intent_gate",
            fromlist=["IntentGate"],
        ).IntentGate()

        text = (
            "چرا این روش وقتی داده زیاد می‌شود کند می‌شود "
            "و اگر حافظه محدود باشد چه تغییری باید بدهیم؟"
        )

        legacy = TaskRouter().route(text).task_type
        selected, details = runtime._select_production_task_type(
            text,
            legacy,
        )

        self.assertIs(legacy, TaskType.FAST)
        self.assertIs(selected, TaskType.HEAVY)
        self.assertTrue(details["applied"])
        self.assertGreaterEqual(details["computeScore"], 16)

    def test_non_fast_routes_are_never_overridden(self) -> None:
        runtime = bare_runtime()

        for route in (
            TaskType.COMPUTER,
            TaskType.BROWSER,
            TaskType.VISION,
            TaskType.HEAVY,
        ):
            selected, details = runtime._select_production_task_type(
                "anything",
                route,
                {
                    "computeLevel": "heavy_deep",
                    "computeScore": 100,
                    "computeConfidence": 1.0,
                    "computeFactors": ["test"],
                },
            )
            self.assertIs(selected, route)
            self.assertFalse(details["applied"])

    def test_voice_output_prewarm_is_background_and_reusable(self) -> None:
        runtime = bare_runtime()
        voice = FakeVoiceOutput()
        runtime.voice_output = voice

        runtime._prewarm_voice_output_async()

        thread = runtime._voice_output_prewarm_thread
        self.assertIsNotNone(thread)
        assert thread is not None
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertTrue(voice.is_loaded)
        self.assertEqual(voice.prewarm_calls, 1)
        self.assertIsNone(runtime._voice_output_prewarm_error)

        # Already-hot voice must not start a second prewarm.
        runtime._prewarm_voice_output_async()
        self.assertEqual(voice.prewarm_calls, 1)

    def test_generic_brain_yield_releases_hot_tts(self) -> None:
        runtime = bare_runtime()
        voice = FakeVoiceOutput()
        voice.is_loaded = True
        runtime.voice_output = voice

        yielded = runtime._yield_voice_output_before_brain(
            timeout_seconds=0.01,
        )

        self.assertTrue(yielded)
        self.assertFalse(voice.is_loaded)
        self.assertEqual(voice.release_calls, 1)

    def test_heavy_yield_releases_hot_tts(self) -> None:
        runtime = bare_runtime()
        voice = FakeVoiceOutput()
        voice.is_loaded = True
        runtime.voice_output = voice

        yielded = runtime._yield_voice_output_before_heavy(
            timeout_seconds=0.01,
        )

        self.assertTrue(yielded)
        self.assertFalse(voice.is_loaded)
        self.assertEqual(voice.release_calls, 1)


if __name__ == "__main__":
    unittest.main()
