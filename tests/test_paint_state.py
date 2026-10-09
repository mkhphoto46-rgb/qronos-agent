from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.actions import ActionRequest
from core.input_safety import BoundInputAuthorization
from core.paint_state import PaintStateError, PaintStateNormalizer
from security.gate import evaluate, issue_grant
from security.permissions import ActionCategory
from core.screen_capture import Capture, DisplayGeometry
from core.vision_grounding import GroundedPoint, GroundedTarget, ImageBox
from core.vision_image import PreparedImage
from core.vision_grounding import target_fingerprint, viewport_fingerprint
from tests.fixtures.still_window import StillWindowCapture
from core.window_binding import WindowBinding, WindowRect, WindowSnapshot


def _binding(*, app_id="paint", process_path=r"C:\Program Files\WindowsApps\Microsoft.Paint\mspaint.exe"):
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
        foreground=True,
    )
    return WindowBinding(
        app_id=app_id,
        display_name="Paint",
        snapshot=snapshot,
        identity_token="identity",
        layout_token="layout",
    )


def _authorization(binding: WindowBinding) -> BoundInputAuthorization:
    request = ActionRequest(
        category=ActionCategory.GUI_AUTOMATION,
        target=f"app:{binding.app_id}",
        summary="test Paint toolbar input",
    )
    verdict = evaluate(request)
    grant = issue_grant(
        request,
        verdict,
        approval=verdict.decision if verdict.needs_approval else None,
    )
    return BoundInputAuthorization.bind(grant, binding)


class FakeBinder:
    def __init__(self, binding):
        self.binding = binding
        self.calls = 0

    def revalidate(self, binding, *, require_foreground, require_same_layout):
        self.calls += 1
        assert binding is self.binding
        assert require_foreground is True
        assert require_same_layout is True
        return binding.snapshot


class FakeCapture:
    def __init__(self, binding):
        self.binding = binding
        self.calls = []
        self.window = StillWindowCapture()

    def capture_bound(self, binding, binder, **kwargs):
        # A real PNG of a controlled picture of the bound window.
        self.calls.append((binding, binder, kwargs))
        return self.window.capture_bound(binding, binder, **kwargs)


class FakeGrounder:
    def __init__(self, binding, points):
        self.binding = binding
        self.points = list(points)
        self.calls = []

    def locate(self, target, capture):
        self.calls.append(target)
        if not self.points:
            raise AssertionError("grounder called more times than expected")
        x, y = self.points.pop(0)
        return GroundedTarget(
            label=target,
            confidence=0.95,
            point=GroundedPoint(
                label=target,
                image_x=x - 90,
                image_y=y - 90,
                screen_x=x,
                screen_y=y,
                hwnd=self.binding.hwnd,
                binding_token=self.binding.identity_token,
                layout_token=self.binding.layout_token,
                frame_rect=WindowRect(90, 90, 900, 700),
                model_width=810,
                model_height=610,
                source_width=810,
                source_height=610,
                captured_at=capture.captured_at,
                state_epoch=capture.state_epoch,
            ),
            image_box=ImageBox(max(0, x - 95), max(0, y - 95), 10, 10),
            screen_box=WindowRect(x - 5, y - 5, x + 5, y + 5),
            fingerprint=target_fingerprint(capture, WindowRect(x - 5, y - 5, x + 5, y + 5)),
            context=viewport_fingerprint(capture),
        )


class FakeClick:
    def __init__(self):
        self.calls = []

    def click_at(self, binding, x, y, **kwargs):
        self.calls.append((binding, x, y, kwargs))
        return SimpleNamespace()


def _normalizer(binding, points):
    binder = FakeBinder(binding)
    capture = FakeCapture(binding)
    grounder = FakeGrounder(binding, points)
    click = FakeClick()
    inputs = SimpleNamespace(click=click)
    normalizer = PaintStateNormalizer(
        binder,
        capture,
        grounder,
        inputs=inputs,
        sleep_fn=lambda _: None,
        settle_seconds=0.0,
    )
    return normalizer, binder, capture, grounder, click


def test_normalizer_selects_pencil_black_and_minimum_size_before_verification():
    binding = _binding()
    # Three actionable controls, followed by three observational verification targets.
    normalizer, binder, capture, grounder, click = _normalizer(
        binding,
        [(150, 150), (250, 150), (200, 300), (150, 150), (250, 150), (200, 300)],
    )

    result = normalizer.normalize(
            binding,
            authorization=_authorization(binding),
            screen_approved=True,
        )

    assert len(click.calls) == 3
    assert [call[1:3] for call in click.calls] == [
        (150, 150),
        (250, 150),
        (200, 300),
    ]
    assert all(isinstance(call[3]["authorization"], BoundInputAuthorization) for call in click.calls)
    assert click.calls[0][3]["allowed_rect"] == binding.snapshot.client_rect
    assert click.calls[1][3]["allowed_rect"] == binding.snapshot.client_rect
    size_rect = click.calls[2][3]["allowed_rect"]
    assert size_rect.left == binding.snapshot.client_rect.left
    assert size_rect.right < binding.snapshot.client_rect.right
    assert size_rect.top > binding.snapshot.client_rect.top
    # Each click: one frame to ground, one fresh frame to confirm before the
    # click; then one frame to verify the selected state.
    assert len(capture.calls) == 7
    reasons = [call[2]["reason"] for call in capture.calls]
    assert [r.startswith("Confirm the Paint control") for r in reasons] == [
        False, True, False, True, False, True, False,
    ]
    assert len(grounder.calls) == 6
    assert result.pencil_selected_confidence == pytest.approx(0.95)
    assert result.black_selected_confidence == pytest.approx(0.95)
    assert result.minimum_size_selected_confidence == pytest.approx(0.95)
    assert binder.calls >= 4


def test_normalizer_refuses_non_paint_without_any_click():
    binding = _binding(app_id="notepad", process_path=r"C:\Windows\notepad.exe")
    normalizer, _, capture, grounder, click = _normalizer(binding, [])

    with pytest.raises(PaintStateError, match="non-Paint"):
        normalizer.normalize(
            binding,
            authorization=_authorization(binding),
            screen_approved=True,
        )

    assert click.calls == []
    assert capture.calls == []
    assert grounder.calls == []


def test_normalizer_fails_closed_when_grounded_ui_point_is_outside_client():
    binding = _binding()
    normalizer, _, _, _, click = _normalizer(
        binding,
        [(95, 95)],  # inside window frame but outside verified Paint client
    )

    with pytest.raises(PaintStateError, match="outside its verified safe control region"):
        normalizer.normalize(
            binding,
            authorization=_authorization(binding),
            screen_approved=True,
        )

    assert click.calls == []


def test_paint_state_grounder_keeps_model_warm_until_close():
    from core.paint_state import PaintStateGrounder

    class FakeRuntime:
        def __init__(self):
            self.calls = []
            self.stopped = []

        def chat(self, **kwargs):
            self.calls.append(kwargs)
            return (
                '{"status":"found","image_width":810,"image_height":610,'
                '"candidates":[{"label":"Pencil","confidence":0.95,'
                '"point":{"x":60,"y":60},'
                '"bbox":{"x":55,"y":55,"width":10,"height":10}}]}'
            )

        def stop_model(self, name):
            self.stopped.append(name)

    binding = _binding()
    capture = FakeCapture(binding).capture_bound(
        binding,
        FakeBinder(binding),
        approved=True,
        require_foreground=True,
    )
    runtime = FakeRuntime()
    grounder = PaintStateGrounder(runtime)  # type: ignore[arg-type]

    target = grounder.locate("Pencil", capture)
    assert target.label == "Pencil"
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["keep_alive"] == "90s"
    assert runtime.stopped == []

    grounder.close()
    assert len(runtime.stopped) == 1
    grounder.close()
    assert len(runtime.stopped) == 1


def test_normalizer_rejects_size_target_outside_left_control_region():
    binding = _binding()
    # Pencil and black are valid; the size target is deep in the right-side canvas.
    normalizer, _, _, _, click = _normalizer(
        binding,
        [(150, 150), (250, 150), (800, 400)],
    )

    with pytest.raises(PaintStateError, match="safe control region"):
        normalizer.normalize(
            binding,
            authorization=_authorization(binding),
            screen_approved=True,
        )

    assert len(click.calls) == 2
