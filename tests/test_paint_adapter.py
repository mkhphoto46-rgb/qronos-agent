from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.paint_adapter import PaintAdapter, PaintAdapterError, PaintCanvasRegion
from core.sketch_approval import VisualApprovedSketch
from core.sketch_preview import SketchPreviewRenderer
from core.shape_planner import ShapePlanner
from core.window_binding import WindowBinding, WindowRect, WindowSnapshot


def _binding(
    *,
    app_id="paint",
    process_path=r"C:\Program Files\WindowsApps\Microsoft.Paint\mspaint.exe",
    foreground=True,
):
    snapshot = WindowSnapshot(
        hwnd=100,
        pid=200,
        thread_id=300,
        title="Untitled - Paint",
        process_path=process_path,
        process_start_time_100ns=123456,
        window_rect=WindowRect(90, 90, 900, 700),
        client_rect=WindowRect(100, 120, 880, 680),
        dpi=96,
        visible=True,
        minimized=False,
        foreground=foreground,
    )
    return WindowBinding(
        app_id=app_id,
        display_name="Paint",
        snapshot=snapshot,
        identity_token="identity",
        layout_token="layout",
    )


class FakeBinder:
    def __init__(self, binding):
        self.binding = binding
        self.calls = 0

    def revalidate(self, binding, *, require_foreground, require_same_layout):
        self.calls += 1
        assert require_foreground is True
        assert require_same_layout is True
        return self.binding.snapshot


class FakeStroke:
    def __init__(self):
        self.calls = []

    def stroke(self, binding, points, **kwargs):
        self.calls.append((binding, tuple(points), kwargs))
        return SimpleNamespace(points=tuple(points))


def _adapter(binding):
    stroke = FakeStroke()
    inputs = SimpleNamespace(stroke=stroke)
    binder = FakeBinder(binding)
    return PaintAdapter(binder, inputs=inputs), binder, stroke


def test_canvas_region_is_tied_to_exact_paint_layout():
    binding = _binding()
    canvas = PaintCanvasRegion.verified(
        binding,
        WindowRect(200, 250, 800, 650),
    )
    assert canvas.matches(binding)

    changed = WindowBinding(
        app_id=binding.app_id,
        display_name=binding.display_name,
        snapshot=binding.snapshot,
        identity_token=binding.identity_token,
        layout_token="different-layout",
    )
    assert not canvas.matches(changed)


def test_canvas_region_rejects_non_paint_and_outside_client():
    with pytest.raises(PaintAdapterError):
        PaintCanvasRegion.verified(
            _binding(app_id="notepad", process_path=r"C:\Windows\notepad.exe"),
            WindowRect(200, 250, 800, 650),
        )

    with pytest.raises(PaintAdapterError):
        PaintCanvasRegion.verified(
            _binding(),
            WindowRect(0, 0, 1000, 1000),
        )


def test_adapter_executes_generic_plan_only_inside_supplied_canvas():
    binding = _binding()
    adapter, binder, stroke = _adapter(binding)
    canvas = PaintCanvasRegion.verified(binding, WindowRect(200, 250, 800, 650))
    plan = ShapePlanner.custom(
        (
            ((0.1, 0.2), (0.9, 0.2)),
            ((0.2, 0.8), (0.8, 0.8)),
        ),
        name="arbitrary-brain-plan",
    )

    result = adapter.execute(
        binding,
        plan,
        canvas,
        approved=True,
        step_delay_seconds=0.0,
    )

    assert result.plan_name == "arbitrary-brain-plan"
    assert len(result.strokes) == 2
    assert binder.calls == 1
    assert len(stroke.calls) == 2
    for _, points, kwargs in stroke.calls:
        assert kwargs["allowed_rect"] == canvas.rect
        assert all(
            canvas.rect.left <= x < canvas.rect.right
            and canvas.rect.top <= y < canvas.rect.bottom
            for x, y in points
        )


def test_adapter_refuses_stale_canvas_capability_before_side_effect():
    binding = _binding()
    adapter, _, stroke = _adapter(binding)
    canvas = PaintCanvasRegion.verified(binding, WindowRect(200, 250, 800, 650))
    stale = PaintCanvasRegion(
        rect=canvas.rect,
        hwnd=canvas.hwnd,
        identity_token=canvas.identity_token,
        layout_token="old-layout",
    )

    with pytest.raises(PaintAdapterError):
        adapter.execute(binding, ShapePlanner.line(), stale, approved=True)

    assert stroke.calls == []

def test_adapter_production_path_accepts_only_visual_approval_capability():
    binding = _binding()
    adapter, _, stroke = _adapter(binding)
    canvas = PaintCanvasRegion.verified(binding, WindowRect(200, 250, 800, 650))
    plan = ShapePlanner.line()
    preview = SketchPreviewRenderer().render(plan)
    approved = VisualApprovedSketch(
        request="draw a line",
        plan=plan,
        plan_digest=preview.plan_digest,
        preview_digest=preview.image_digest,
        preview_image=preview.image,
        critic_model="vision",
        critic_confidence=0.9,
        candidate_attempts=1,
        critic_summary="Recognizable.",
    )

    result = adapter.execute_approved(
        binding,
        approved,
        canvas,
        approved=True,
        step_delay_seconds=0.0,
    )

    assert result.plan_name == plan.name
    assert len(stroke.calls) == 1

    with pytest.raises(PaintAdapterError, match="VisualApprovedSketch"):
        adapter.execute_approved(
            binding,
            plan,  # type: ignore[arg-type]
            canvas,
            approved=True,
        )

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
    with pytest.raises(PaintAdapterError, match="digest"):
        adapter.execute_approved(
            binding,
            tampered,
            canvas,
            approved=True,
        )


def test_adapter_uses_fidelity_projection_and_pointer_delivery_defaults():
    binding = _binding()
    adapter, _, stroke = _adapter(binding)
    canvas = PaintCanvasRegion.verified(binding, WindowRect(200, 250, 800, 650))
    plan = ShapePlanner.line((0.0, 0.0), (1.0, 1.0))

    adapter.execute(binding, plan, canvas, approved=True)

    _, points, kwargs = stroke.calls[0]
    # 600x400 canvas contains a centered 400x400 projection.
    assert points == ((300, 250), (699, 649))
    assert kwargs["max_step_pixels"] == 8.0
    assert kwargs["press_settle_seconds"] == 0.012
    assert kwargs["release_settle_seconds"] == 0.012
