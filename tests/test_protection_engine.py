from __future__ import annotations

import unittest

from core.activity_guard import ActivityMode, ResourcePressure
from core.protection_engine import (
    ProtectionEngine,
    ProtectionLevel,
)


class TestProtectionEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ProtectionEngine()

    def test_normal_mode_has_no_protection(self) -> None:
        state = self.engine.evaluate(
            ActivityMode.NORMAL,
            ResourcePressure.NORMAL,
        )

        self.assertEqual(
            state.level,
            ProtectionLevel.NONE,
        )

        self.assertTrue(state.fast_brain_allowed)
        self.assertFalse(state.fast_brain_on_demand)
        self.assertTrue(state.heavy_brain_allowed)
        self.assertTrue(state.vision_allowed)
        self.assertTrue(state.background_ai_allowed)

    def test_high_pressure_activates_light_protection(self) -> None:
        state = self.engine.evaluate(
            ActivityMode.NORMAL,
            ResourcePressure.HIGH,
        )

        self.assertEqual(
            state.level,
            ProtectionLevel.LIGHT,
        )

        self.assertTrue(state.fast_brain_allowed)
        self.assertTrue(state.fast_brain_on_demand)
        self.assertFalse(state.heavy_brain_allowed)
        self.assertFalse(state.vision_allowed)
        self.assertFalse(state.background_ai_allowed)

    def test_gaming_assist_uses_light_protection(self) -> None:
        state = self.engine.evaluate(
            ActivityMode.GAMING_ASSIST,
            ResourcePressure.NORMAL,
        )

        self.assertEqual(
            state.level,
            ProtectionLevel.LIGHT,
        )

        self.assertTrue(state.fast_brain_allowed)
        self.assertTrue(state.fast_brain_on_demand)
        self.assertFalse(state.heavy_brain_allowed)
        self.assertFalse(state.vision_allowed)
        self.assertFalse(state.background_ai_allowed)

    def test_gaming_performance_uses_strong_protection(self) -> None:
        state = self.engine.evaluate(
            ActivityMode.GAMING_PERFORMANCE,
            ResourcePressure.NORMAL,
        )

        self.assertEqual(
            state.level,
            ProtectionLevel.STRONG,
        )

        self.assertTrue(state.fast_brain_allowed)
        self.assertTrue(state.fast_brain_on_demand)
        self.assertFalse(state.heavy_brain_allowed)
        self.assertFalse(state.vision_allowed)
        self.assertFalse(state.background_ai_allowed)

    def test_creator_assist_uses_light_protection(self) -> None:
        state = self.engine.evaluate(
            ActivityMode.CREATOR_ASSIST,
            ResourcePressure.NORMAL,
        )

        self.assertEqual(
            state.level,
            ProtectionLevel.LIGHT,
        )

        self.assertTrue(state.fast_brain_allowed)
        self.assertTrue(state.fast_brain_on_demand)
        self.assertFalse(state.heavy_brain_allowed)
        self.assertFalse(state.vision_allowed)
        self.assertFalse(state.background_ai_allowed)

    def test_creator_performance_uses_strong_protection(self) -> None:
        state = self.engine.evaluate(
            ActivityMode.CREATOR_PERFORMANCE,
            ResourcePressure.NORMAL,
        )

        self.assertEqual(
            state.level,
            ProtectionLevel.STRONG,
        )

        self.assertTrue(state.fast_brain_allowed)
        self.assertTrue(state.fast_brain_on_demand)
        self.assertFalse(state.heavy_brain_allowed)
        self.assertFalse(state.vision_allowed)
        self.assertFalse(state.background_ai_allowed)

    def test_critical_pressure_uses_emergency_protection(self) -> None:
        state = self.engine.evaluate(
            ActivityMode.NORMAL,
            ResourcePressure.CRITICAL,
        )

        self.assertEqual(
            state.level,
            ProtectionLevel.EMERGENCY,
        )

        self.assertTrue(state.fast_brain_allowed)
        self.assertTrue(state.fast_brain_on_demand)
        self.assertFalse(state.heavy_brain_allowed)
        self.assertFalse(state.vision_allowed)
        self.assertFalse(state.background_ai_allowed)

    def test_critical_pressure_overrides_gaming_assist(self) -> None:
        state = self.engine.evaluate(
            ActivityMode.GAMING_ASSIST,
            ResourcePressure.CRITICAL,
        )

        self.assertEqual(
            state.level,
            ProtectionLevel.EMERGENCY,
        )

    def test_critical_pressure_overrides_creator_assist(self) -> None:
        state = self.engine.evaluate(
            ActivityMode.CREATOR_ASSIST,
            ResourcePressure.CRITICAL,
        )

        self.assertEqual(
            state.level,
            ProtectionLevel.EMERGENCY,
        )


if __name__ == "__main__":
    unittest.main()