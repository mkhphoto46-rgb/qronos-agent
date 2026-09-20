from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from core.reference_sketch_pipeline import ReferenceSketchPipeline, ReferenceSketchRejected
from core.reference_vectorizer import ReferenceStrokeVectorizer
from core.sketch_preview import SketchPreviewRenderer
from core.vision_image import prepare_bytes


def _reference():
    image = Image.new("RGB", (300, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((100, 30, 200, 130), outline="black", width=5)
    draw.line((150, 130, 150, 220), fill="black", width=5)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return prepare_bytes(buffer.getvalue(), long_edge=None)


@dataclass(frozen=True)
class FakeCritique:
    ok: bool
    model_name: str = "fake-vision"
    confidence: float = 0.95
    summary: str = "approved"

    def passes(self):
        return self.ok


class FakeCritic:
    def __init__(self, *, passed: bool):
        self.passed = passed
        self.calls = 0

    def critique(self, request, preview):
        self.calls += 1
        return FakeCritique(
            ok=self.passed,
            confidence=0.95 if self.passed else 0.1,
            summary="approved" if self.passed else "not recognizable",
        )


def test_pipeline_returns_existing_visual_approval_capability_on_pass():
    critic = FakeCritic(passed=True)
    pipeline = ReferenceSketchPipeline(
        SimpleNamespace(),
        vectorizer=ReferenceStrokeVectorizer(),
        renderer=SketchPreviewRenderer(),
        critic=critic,
    )
    approved, diagnostics = pipeline.plan("simple reference", _reference())

    assert approved.plan == diagnostics.vectorization.plan
    assert approved.plan_digest == diagnostics.preview.plan_digest
    assert approved.preview_digest == diagnostics.preview.image_digest
    assert approved.critic_model == "fake-vision"
    assert critic.calls == 1


def test_pipeline_rejects_before_paint_when_visual_gate_fails():
    pipeline = ReferenceSketchPipeline(
        SimpleNamespace(),
        critic=FakeCritic(passed=False),
    )
    with pytest.raises(ReferenceSketchRejected, match="visual gate"):
        pipeline.plan("simple reference", _reference())
