from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.activity_guard import ActivityMode, ResourcePressure


class ProtectionLevel(Enum):
    NONE = "none"
    LIGHT = "light"
    STRONG = "strong"
    EMERGENCY = "emergency"


@dataclass(frozen=True)
class ProtectionState:
    level: ProtectionLevel
    fast_brain_allowed: bool
    fast_brain_on_demand: bool
    heavy_brain_allowed: bool
    vision_allowed: bool
    background_ai_allowed: bool
    reason: str


class ProtectionEngine:
    """
    Decide how aggressively Qronos should reduce resource usage.

    This engine only makes a protection decision.
    It does not execute system actions.
    """

    def evaluate(
        self,
        activity_mode: ActivityMode,
        resource_pressure: ResourcePressure,
    ) -> ProtectionState:

        if resource_pressure is ResourcePressure.CRITICAL:
            return ProtectionState(
                level=ProtectionLevel.EMERGENCY,
                fast_brain_allowed=True,
                fast_brain_on_demand=True,
                heavy_brain_allowed=False,
                vision_allowed=False,
                background_ai_allowed=False,
                reason="System resources are in a critical state.",
            )

        if activity_mode in {
            ActivityMode.GAMING_PERFORMANCE,
            ActivityMode.CREATOR_PERFORMANCE,
        }:
            return ProtectionState(
                level=ProtectionLevel.STRONG,
                fast_brain_allowed=True,
                fast_brain_on_demand=True,
                heavy_brain_allowed=False,
                vision_allowed=False,
                background_ai_allowed=False,
                reason="Performance mode is active.",
            )

        if activity_mode in {
            ActivityMode.GAMING_ASSIST,
            ActivityMode.CREATOR_ASSIST,
        }:
            return ProtectionState(
                level=ProtectionLevel.LIGHT,
                fast_brain_allowed=True,
                fast_brain_on_demand=True,
                heavy_brain_allowed=False,
                vision_allowed=False,
                background_ai_allowed=False,
                reason="Assist mode is active.",
            )

        if resource_pressure is ResourcePressure.HIGH:
            return ProtectionState(
                level=ProtectionLevel.LIGHT,
                fast_brain_allowed=True,
                fast_brain_on_demand=True,
                heavy_brain_allowed=False,
                vision_allowed=False,
                background_ai_allowed=False,
                reason="System resources are under high pressure.",
            )

        return ProtectionState(
            level=ProtectionLevel.NONE,
            fast_brain_allowed=True,
            fast_brain_on_demand=False,
            heavy_brain_allowed=True,
            vision_allowed=True,
            background_ai_allowed=True,
            reason="System resources and activity are normal.",
        )


if __name__ == "__main__":
    engine = ProtectionEngine()

    state = engine.evaluate(
        activity_mode=ActivityMode.NORMAL,
        resource_pressure=ResourcePressure.NORMAL,
    )

    print(f"Protection level: {state.level.value}")
    print(f"Fast Brain allowed: {state.fast_brain_allowed}")
    print(f"Fast Brain on-demand: {state.fast_brain_on_demand}")
    print(f"Heavy Brain allowed: {state.heavy_brain_allowed}")
    print(f"Vision allowed: {state.vision_allowed}")
    print(
        f"Background AI allowed: "
        f"{state.background_ai_allowed}"
    )
    print(f"Reason: {state.reason}")