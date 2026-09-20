from __future__ import annotations

from dataclasses import replace

import pytest

from core.paint_canvas_grounding import (
    PaintCanvasGroundingError,
    ground_paint_canvas,
)
from core.screen_capture import Capture, DisplayGeometry
from core.vision_grounding import GroundedPoint, GroundedTarget, ImageBox
from core.vision_image import PreparedImage
from core.window_binding import WindowBinding, WindowRect, WindowSnapshot


def _binding() -> WindowBinding:
    snapshot = WindowSnapshot(
        hwnd=100,
        pid=200,
        thread_id=300,
        title="Untitled - Paint",
        process_path=r"C:\Program Files\WindowsApps\Microsoft.Paint\mspaint.exe",
        process_start_time_100ns=123456,
        window_rect=WindowRect(90, 90, 1000, 800),
        client_rect=WindowRect(100, 120, 980, 780),
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


def _target(binding: WindowBinding, rect: WindowRect) -> GroundedTarget:
    point_x = (rect.left + rect.right) // 2
    point_y = (rect.top + rect.bottom) // 2
    return GroundedTarget(
        label="Paint canvas",
        confidence=0.96,
        point=GroundedPoint(
            label="Paint canvas",
            image_x=point_x - 90,
            image_y=point_y - 90,
            screen_x=point_x,
            screen_y=point_y,
            hwnd=binding.hwnd,
            binding_token=binding.identity_token,
            layout_token=binding.layout_token,
            frame_rect=binding.snapshot.window_rect,
            model_width=binding.snapshot.window_rect.width,
            model_height=binding.snapshot.window_rect.height,
            source_width=binding.snapshot.window_rect.width,
            source_height=binding.snapshot.window_rect.height,
        ),
        image_box=ImageBox(
            rect.left - 90,
            rect.top - 90,
            rect.width,
            rect.height,
        ),
        screen_box=rect,
    )


class FakeBinder:
    def __init__(self, binding: WindowBinding):
        self.binding = binding
        self.calls = 0

    def revalidate(self, binding, *, require_foreground=True, require_same_layout=True):
        self.calls += 1
        assert binding is self.binding
        assert require_foreground is True
        assert require_same_layout is True
        return binding.snapshot


class FakeCapture:
    def __init__(self, binding: WindowBinding):
        self.binding = binding
        self.calls = []

    def capture_bound(self, binding, binder, **kwargs):
        self.calls.append((binding, binder, kwargs))
        snap = binding.snapshot
        return Capture(
            image=PreparedImage(
                data=b"png",
                width=snap.window_rect.width,
                height=snap.window_rect.height,
                format="png",
            ),
            geometry=DisplayGeometry(1920, 1080, 1920, 1080),
            window=binding.hwnd,
            binding_token=binding.identity_token,
            layout_token=binding.layout_token,
            source_width=snap.window_rect.width,
            source_height=snap.window_rect.height,
            frame_rect=snap.window_rect,
        )


class FakeGrounder:
    def __init__(self, target: GroundedTarget):
        self.target = target
        self.calls = []

    def locate(self, target, capture):
        self.calls.append((target, capture))
        return self.target


def test_valid_vision_bbox_becomes_exact_canvas_capability():
    binding = _binding()
    rect = WindowRect(220, 260, 900, 720)
    binder = FakeBinder(binding)
    capture = FakeCapture(binding)
    grounder = FakeGrounder(_target(binding, rect))

    canvas = ground_paint_canvas(
        binding,
        binder,  # type: ignore[arg-type]
        capture,  # type: ignore[arg-type]
        grounder,
        screen_approved=True,
    )

    assert canvas.rect == rect
    assert canvas.matches(binding)
    assert capture.calls[0][2]["approved"] is True
    assert len(grounder.calls) == 1


def test_canvas_outside_verified_client_is_rejected():
    binding = _binding()
    rect = WindowRect(95, 200, 900, 720)
    binder = FakeBinder(binding)

    with pytest.raises(PaintCanvasGroundingError, match="outside"):
        ground_paint_canvas(
            binding,
            binder,  # type: ignore[arg-type]
            FakeCapture(binding),  # type: ignore[arg-type]
            FakeGrounder(_target(binding, rect)),
            screen_approved=True,
        )


def test_tiny_false_positive_canvas_is_rejected():
    binding = _binding()
    rect = WindowRect(200, 250, 420, 390)
    binder = FakeBinder(binding)

    with pytest.raises(PaintCanvasGroundingError, match="too small"):
        ground_paint_canvas(
            binding,
            binder,  # type: ignore[arg-type]
            FakeCapture(binding),  # type: ignore[arg-type]
            FakeGrounder(_target(binding, rect)),
            screen_approved=True,
        )


def test_stale_canvas_provenance_is_rejected():
    binding = _binding()
    rect = WindowRect(220, 260, 900, 720)
    stale = replace(
        _target(binding, rect),
        point=replace(_target(binding, rect).point, layout_token="old-layout"),
    )
    binder = FakeBinder(binding)

    with pytest.raises(PaintCanvasGroundingError, match="not safely actionable"):
        ground_paint_canvas(
            binding,
            binder,  # type: ignore[arg-type]
            FakeCapture(binding),  # type: ignore[arg-type]
            FakeGrounder(stale),
            screen_approved=True,
        )
