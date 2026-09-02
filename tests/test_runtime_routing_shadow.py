from __future__ import annotations

import unittest
from unittest.mock import patch

from core.runtime_bridge import QronosRuntime


class RoutingShadowRuntimeTests(unittest.TestCase):
    def test_shadow_is_non_authoritative_and_classifies_stable_knowledge(self) -> None:
        runtime = QronosRuntime()

        shadow = runtime._routing_shadow(
            "پایتخت کانادا چیه؟"
        )

        self.assertFalse(shadow["authoritative"])
        self.assertEqual(
            shadow["primaryIntent"],
            "knowledge_stable",
        )
        self.assertEqual(
            shadow["computeLevel"],
            "fast",
        )
        self.assertIn(
            "knowledge_stable",
            shadow["requiredIntents"],
        )
        self.assertGreaterEqual(
            shadow["elapsedMs"],
            0.0,
        )

    def test_shadow_detects_multilabel_vision_reasoning(self) -> None:
        runtime = QronosRuntime()

        shadow = runtime._routing_shadow(
            "این ارور روی صفحه رو ببین و تحلیل کن چرا اتفاق افتاده"
        )

        self.assertFalse(shadow["authoritative"])
        self.assertEqual(
            shadow["primaryIntent"],
            "vision_analysis",
        )
        self.assertIn(
            "vision_analysis",
            shadow["requiredIntents"],
        )
        self.assertIn(
            "reasoning",
            shadow["requiredIntents"],
        )

    def test_shadow_detects_direct_arithmetic(self) -> None:
        runtime = QronosRuntime()

        shadow = runtime._routing_shadow(
            "دو به علاوه دو چند میشه؟"
        )

        self.assertEqual(
            shadow["primaryIntent"],
            "direct_deterministic",
        )
        self.assertEqual(
            shadow["computeLevel"],
            "none",
        )

    def test_shadow_failure_is_contained_and_never_escapes(self) -> None:
        runtime = QronosRuntime()

        with patch.object(
            runtime.intent_gate,
            "classify",
            side_effect=RuntimeError("shadow-only failure"),
        ):
            shadow = runtime._routing_shadow(
                "سلام کرونوس"
            )

        self.assertFalse(shadow["authoritative"])
        self.assertIn(
            "shadow-only failure",
            shadow["error"],
        )
        self.assertGreaterEqual(
            shadow["elapsedMs"],
            0.0,
        )


if __name__ == "__main__":
    unittest.main()

