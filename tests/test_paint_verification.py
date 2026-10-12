from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw
import pytest

from core.paint_verification import PaintPostDrawError, PaintPostDrawVerifier
from core.sketch_approval import VisualApprovedSketch
from core.sketch_preview import SketchPreviewRenderer
from core.shape_planner import ShapePlanner
from core.vision_image import PreparedImage
from core.window_binding import WindowBinding, WindowRect, WindowSnapshot


def _png(image: Image.Image) -> PreparedImage:
    buffer = BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    data = buffer.getvalue()
    return PreparedImage(data=data, width=image.width, height=image.height, format="png")


def _binding() -> WindowBinding:
    snapshot = WindowSnapshot(
        hwnd=100,
        pid=200,
        thread_id=300,
        title="Untitled - Paint",
        process_path=r"C:\Program Files\WindowsApps\Microsoft.Paint\mspaint.exe",
        process_start_time_100ns=123456,
        window_rect=WindowRect(100, 100, 500, 400),
        client_rect=WindowRect(110, 120, 490, 390),
        dpi=96,
        visible=True,
        minimized=False,
        foreground=True,
    )
    return WindowBinding(
        app_id="paint",
        display_name="Paint",
        snapshot=snapshot,
        identity_token="identity",
        layout_token="layout",
    )


def _canvas(binding: WindowBinding):
    return SimpleNamespace(
        rect=WindowRect(150, 150, 450, 350),
        hwnd=binding.hwnd,
        identity_token=binding.identity_token,
        layout_token=binding.layout_token,
    )


def _approved(plan=None):
    plan = plan or ShapePlanner.custom(
        (
            ((0.2, 0.2), (0.8, 0.8)),
            ((0.2, 0.8), (0.8, 0.2)),
        ),
        name="verification-x",
    )
    preview = SketchPreviewRenderer().render(plan)
    return VisualApprovedSketch(
        request="draw an X",
        plan=plan,
        plan_digest=preview.plan_digest,
        preview_digest=preview.image_digest,
        preview_image=preview.image,
        critic_model="pre-critic",
        critic_confidence=0.99,
        candidate_attempts=1,
        critic_summary="Approved before execution.",
    )


@dataclass(frozen=True)
class _Critique:
    verdict: str = "pass"
    confidence: float = 0.99
    recognizable: bool = True
    request_satisfied: bool = True
    composition_coherent: bool = True
    summary: str = "Observed delta matches the requested sketch."
    issues: tuple[str, ...] = ()
    model_name: str = "fake-vision"

    def passes(self, *, min_confidence: float = 0.72) -> bool:
        return (
            self.verdict == "pass"
            and self.recognizable
            and self.request_satisfied
            and self.composition_coherent
            and self.confidence >= min_confidence
        )


class _Critic:
    def __init__(self, critique=None):
        self.critique_result = critique or _Critique()
        self.images = []

    def critique(self, request, preview):
        self.images.append((request, preview))
        return self.critique_result


def _draw_plan_on_full_frame(
    plan,
    *,
    canvas: WindowRect,
    frame: WindowRect,
    size=(400, 300),
    inset=12,
    preexisting=False,
    strokes=None,
):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    if preexisting:
        draw.line((15, 20, 90, 20), fill="black", width=3)

    local_canvas = WindowRect(
        canvas.left - frame.left,
        canvas.top - frame.top,
        canvas.right - frame.left,
        canvas.bottom - frame.top,
    )
    projected = plan.to_screen_strokes(
        local_canvas,
        inset_pixels=inset,
        preserve_aspect=True,
    )
    if strokes is not None:
        projected = tuple(projected[index] for index in strokes)
    for points in projected:
        draw.line(points, fill="black", width=3, joint="curve")
    return image


def _capture(binding, image, *, resized=False, layout_token=None):
    source_width = 400
    source_height = 300
    prepared_image = image
    if resized:
        prepared_image = image.resize((200, 150), Image.Resampling.LANCZOS)
    return SimpleNamespace(
        image=_png(prepared_image),
        window=binding.hwnd,
        binding_token=binding.identity_token,
        layout_token=layout_token or binding.layout_token,
        source_width=source_width,
        source_height=source_height,
        frame_rect=WindowRect(100, 100, 500, 400),
    )


def test_exact_new_drawing_passes_geometry_and_semantics():
    binding = _binding()
    canvas = _canvas(binding)
    approved = _approved()
    frame = WindowRect(100, 100, 500, 400)
    before_image = Image.new("RGB", (400, 300), "white")
    after_image = _draw_plan_on_full_frame(
        approved.plan,
        canvas=canvas.rect,
        frame=frame,
    )
    critic = _Critic()
    verifier = PaintPostDrawVerifier(critic)

    result = verifier.verify(
        binding,
        approved,
        canvas,
        _capture(binding, before_image),
        _capture(binding, after_image),
        inset_pixels=12,
    )

    assert result.passed
    assert result.geometric_passed
    assert result.semantic_passed
    assert result.expected_coverage > 0.95
    assert result.observed_precision > 0.95
    assert result.f1_score > 0.95
    assert critic.images[0][0] == approved.request
    assert critic.images[0][1].data.startswith(b"\x89PNG")


def test_missing_major_stroke_fails_geometry_even_if_critic_would_pass():
    binding = _binding()
    canvas = _canvas(binding)
    approved = _approved()
    frame = WindowRect(100, 100, 500, 400)
    before_image = Image.new("RGB", (400, 300), "white")
    after_image = _draw_plan_on_full_frame(
        approved.plan,
        canvas=canvas.rect,
        frame=frame,
        strokes=(0,),
    )
    verifier = PaintPostDrawVerifier(_Critic())

    result = verifier.verify(
        binding,
        approved,
        canvas,
        _capture(binding, before_image),
        _capture(binding, after_image),
        inset_pixels=12,
    )

    assert not result.passed
    assert not result.geometric_passed
    assert result.semantic_passed
    assert result.expected_coverage < 0.85
    with pytest.raises(PaintPostDrawError, match="did not match"):
        result.require_pass()


def test_semantic_rejection_fails_even_when_geometry_matches():
    binding = _binding()
    canvas = _canvas(binding)
    approved = _approved()
    frame = WindowRect(100, 100, 500, 400)
    before_image = Image.new("RGB", (400, 300), "white")
    after_image = _draw_plan_on_full_frame(
        approved.plan,
        canvas=canvas.rect,
        frame=frame,
    )
    failed = _Critique(
        verdict="fail",
        confidence=0.99,
        recognizable=False,
        request_satisfied=False,
        composition_coherent=True,
        summary="Wrong subject.",
    )
    verifier = PaintPostDrawVerifier(_Critic(failed))

    result = verifier.verify(
        binding,
        approved,
        canvas,
        _capture(binding, before_image),
        _capture(binding, after_image),
        inset_pixels=12,
    )

    assert result.geometric_passed
    assert not result.semantic_passed
    assert not result.passed


def test_preexisting_unchanged_marks_are_removed_by_before_after_delta():
    binding = _binding()
    canvas = _canvas(binding)
    approved = _approved()
    frame = WindowRect(100, 100, 500, 400)
    before_image = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(before_image).line((15, 20, 90, 20), fill="black", width=3)
    after_image = _draw_plan_on_full_frame(
        approved.plan,
        canvas=canvas.rect,
        frame=frame,
        preexisting=True,
    )
    verifier = PaintPostDrawVerifier(_Critic())

    result = verifier.verify(
        binding,
        approved,
        canvas,
        _capture(binding, before_image),
        _capture(binding, after_image),
        inset_pixels=12,
    )

    assert result.passed
    delta = np.asarray(Image.open(BytesIO(result.observed_delta_image.data)).convert("L"))
    assert int((delta < 128).sum()) > 0


def test_resized_bound_captures_map_screen_canvas_back_to_model_pixels():
    binding = _binding()
    canvas = _canvas(binding)
    approved = _approved()
    frame = WindowRect(100, 100, 500, 400)
    before_image = Image.new("RGB", (400, 300), "white")
    after_image = _draw_plan_on_full_frame(
        approved.plan,
        canvas=canvas.rect,
        frame=frame,
    )
    verifier = PaintPostDrawVerifier(_Critic(), expected_line_width=2)

    result = verifier.verify(
        binding,
        approved,
        canvas,
        _capture(binding, before_image, resized=True),
        _capture(binding, after_image, resized=True),
        inset_pixels=12,
    )

    assert result.passed
    assert result.actual_canvas_image.width == 150
    assert result.actual_canvas_image.height == 100


def test_stale_capture_or_tampered_approval_is_rejected_before_critic():
    binding = _binding()
    canvas = _canvas(binding)
    approved = _approved()
    critic = _Critic()
    verifier = PaintPostDrawVerifier(critic)
    image = Image.new("RGB", (400, 300), "white")

    with pytest.raises(PaintPostDrawError, match="layout token"):
        verifier.verify(
            binding,
            approved,
            canvas,
            _capture(binding, image, layout_token="old-layout"),
            _capture(binding, image),
        )
    assert critic.images == []

    tampered = VisualApprovedSketch(
        request=approved.request,
        plan=approved.plan,
        plan_digest="0" * 64,
        preview_digest=approved.preview_digest,
        preview_image=approved.preview_image,
        critic_model=approved.critic_model,
        critic_confidence=approved.critic_confidence,
        candidate_attempts=approved.candidate_attempts,
        critic_summary=approved.critic_summary,
    )
    with pytest.raises(PaintPostDrawError, match="digest"):
        verifier.verify(
            binding,
            tampered,
            canvas,
            _capture(binding, image),
            _capture(binding, image),
        )
    assert critic.images == []
