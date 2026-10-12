from __future__ import annotations

from dataclasses import dataclass

from core.shape_planner import ShapePlan
from core.vision_image import PreparedImage


@dataclass(frozen=True)
class VisualApprovedSketch:
    """
    A ShapePlan that was rendered and semantically approved before GUI execution.

    This object is the production handoff to application adapters. It contains the
    exact frozen plan that the critic saw, plus digests for audit/debugging.
    """

    request: str
    plan: ShapePlan
    plan_digest: str
    preview_digest: str
    preview_image: PreparedImage
    critic_model: str
    critic_confidence: float
    candidate_attempts: int
    critic_summary: str
