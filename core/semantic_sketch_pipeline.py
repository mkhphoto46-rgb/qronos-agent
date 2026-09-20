from __future__ import annotations

from dataclasses import dataclass

from core.brain_runtime import BrainRuntime
from core.semantic_sketch import SemanticSketchError, validate_semantic_requirements
from core.semantic_sketch_brain import SemanticSketchPlanner
from core.semantic_sketch_layout import SemanticLayoutError, compile_semantic_sketch
from core.sketch_approval import VisualApprovedSketch
from core.sketch_critic import (
    DEFAULT_CRITIC_MIN_CONFIDENCE,
    SketchCritique,
    SketchVisualCritic,
)
from core.sketch_preview import SketchPreviewRenderer


MAX_VISUAL_REPAIR_ATTEMPTS = 2


class SemanticSketchRejected(RuntimeError):
    """No candidate passed the visual semantic gate within bounded attempts."""


@dataclass(frozen=True)
class SemanticSketchDiagnostics:
    candidate_count: int
    critiques: tuple[SketchCritique, ...]
    structural_errors: tuple[str, ...] = ()


class SemanticSketchPipeline:
    """
    Natural language -> semantic scene graph -> deterministic layout -> preview
    -> vision gate. No mouse or application side effect occurs in this class.
    """

    def __init__(
        self,
        runtime: BrainRuntime,
        *,
        max_visual_repair_attempts: int = MAX_VISUAL_REPAIR_ATTEMPTS,
        min_critic_confidence: float = DEFAULT_CRITIC_MIN_CONFIDENCE,
        planner: SemanticSketchPlanner | None = None,
        critic: SketchVisualCritic | None = None,
        renderer: SketchPreviewRenderer | None = None,
    ) -> None:
        if (
            isinstance(max_visual_repair_attempts, bool)
            or not isinstance(max_visual_repair_attempts, int)
            or not (0 <= max_visual_repair_attempts <= MAX_VISUAL_REPAIR_ATTEMPTS)
        ):
            raise ValueError(
                "max_visual_repair_attempts must be an integer from 0 through "
                f"{MAX_VISUAL_REPAIR_ATTEMPTS}."
            )
        if not 0.0 <= float(min_critic_confidence) <= 1.0:
            raise ValueError("min_critic_confidence must be within 0.0..1.0.")

        self._planner = planner or SemanticSketchPlanner(runtime)
        self._critic = critic or SketchVisualCritic(runtime)
        self._renderer = renderer or SketchPreviewRenderer()
        self._max_visual_repairs = max_visual_repair_attempts
        self._min_confidence = float(min_critic_confidence)

    def plan(self, request: str) -> tuple[VisualApprovedSketch, SemanticSketchDiagnostics]:
        feedback = ""
        critiques: list[SketchCritique] = []
        structural_errors: list[str] = []

        for candidate_index in range(self._max_visual_repairs + 1):
            try:
                semantic = self._planner.plan(
                    request,
                    visual_feedback=feedback,
                )
                # Defense in depth: the planner/parser already validates model output,
                # but custom planners/tests cannot bypass the semantic requirement gate.
                validate_semantic_requirements(semantic.document)
                plan = compile_semantic_sketch(semantic.document)
                preview = self._renderer.render(plan)
            except (SemanticSketchError, SemanticLayoutError) as exc:
                message = str(exc).strip() or exc.__class__.__name__
                structural_errors.append(message)
                feedback = (
                    "The previous candidate was rejected before visual review because its "
                    "semantic structure/layout was invalid. Regenerate from scratch and fix "
                    f"this exact problem:\n- {message}"
                )
                continue

            critique = self._critic.critique(request, preview.image)
            critiques.append(critique)

            if critique.passes(min_confidence=self._min_confidence):
                approved = VisualApprovedSketch(
                    request=str(request).strip(),
                    plan=plan,
                    plan_digest=preview.plan_digest,
                    preview_digest=preview.image_digest,
                    preview_image=preview.image,
                    critic_model=critique.model_name,
                    critic_confidence=critique.confidence,
                    candidate_attempts=candidate_index + 1,
                    critic_summary=critique.summary,
                )
                return approved, SemanticSketchDiagnostics(
                    candidate_count=candidate_index + 1,
                    critiques=tuple(critiques),
                    structural_errors=tuple(structural_errors),
                )

            feedback = critique.feedback() or (
                "- The preview did not pass the visual semantic gate. "
                "Create a more recognizable composition."
            )

        if critiques:
            last = critiques[-1]
            detail = f"Last critic summary: {last.summary or 'none'}"
        elif structural_errors:
            detail = f"Last structural error: {structural_errors[-1]}"
        else:
            detail = "No candidate reached semantic or visual evaluation."
        raise SemanticSketchRejected(
            "No sketch candidate passed the semantic/visual gates after "
            f"{self._max_visual_repairs + 1} candidate(s). {detail}"
        )
