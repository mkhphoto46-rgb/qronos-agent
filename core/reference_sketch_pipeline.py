from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from core.brain_runtime import BrainRuntime
from core.reference_vectorizer import ReferenceStrokeVectorizer, ReferenceVectorization
from core.sketch_approval import VisualApprovedSketch
from core.sketch_preview import SketchPreview, SketchPreviewRenderer
from core.vision_image import PreparedImage

if TYPE_CHECKING:
    from core.sketch_critic import SketchCritique


class _Critic(Protocol):
    def critique(self, request: str, preview: PreparedImage) -> "SketchCritique": ...


class ReferenceSketchRejected(RuntimeError):
    """Reference line art could not pass the visual gate before GUI execution."""


@dataclass(frozen=True)
class ReferenceSketchDiagnostics:
    vectorization: ReferenceVectorization
    preview: SketchPreview
    critique: "SketchCritique"


class ReferenceSketchPipeline:
    """Reference image -> bounded strokes -> preview -> Qwen visual approval."""

    def __init__(
        self,
        runtime: BrainRuntime,
        *,
        vectorizer: ReferenceStrokeVectorizer | None = None,
        renderer: SketchPreviewRenderer | None = None,
        critic: _Critic | None = None,
    ) -> None:
        self._vectorizer = vectorizer or ReferenceStrokeVectorizer()
        self._renderer = renderer or SketchPreviewRenderer()
        if critic is None:
            # Keep vectorization usable/testable without loading model registry or
            # Ollama-facing modules.  Only the visual-gate path needs them.
            from core.sketch_critic import SketchVisualCritic

            critic = SketchVisualCritic(runtime)
        self._critic = critic

    def plan(
        self,
        request: str,
        reference: str | Path | PreparedImage,
    ) -> tuple[VisualApprovedSketch, ReferenceSketchDiagnostics]:
        cleaned = str(request).strip()
        if not cleaned:
            raise ReferenceSketchRejected("Reference sketch request cannot be empty.")

        vectorization = self._vectorizer.vectorize(reference, name="reference-trace")
        preview = self._renderer.render(vectorization.plan)
        critique = self._critic.critique(cleaned, preview.image)
        diagnostics = ReferenceSketchDiagnostics(
            vectorization=vectorization,
            preview=preview,
            critique=critique,
        )
        if not critique.passes():
            raise ReferenceSketchRejected(
                "Reference trace failed the visual gate: "
                + (critique.summary or "preview was not approved")
            )

        approved = VisualApprovedSketch(
            request=cleaned,
            plan=vectorization.plan,
            plan_digest=preview.plan_digest,
            preview_digest=preview.image_digest,
            preview_image=preview.image,
            critic_model=critique.model_name,
            critic_confidence=critique.confidence,
            candidate_attempts=1,
            critic_summary=critique.summary,
        )
        return approved, diagnostics
