"""D4: a plain "draw a house in Paint" request, end to end, against a simulated Paint.

Real Qronos code runs the whole product path: PaintDrawExecutor, template
matching and critic-approved preview, the window binder, bound captures with
freshness, vision-grounding parsing, PaintStateNormalizer, canvas grounding,
the mouse controllers with their guards, PaintAdapter, and the deterministic
post-draw verifier on the resulting pixels.

Only the Windows and model boundaries are fakes:
- a simulated Paint window: a pixel buffer that the injected mouse-move and
  button senders draw into while the left button is down;
- the pixel grab, which returns that buffer;
- a scripted Vision grounder that answers each target from the frame it is
  given (its coordinates therefore carry the frame's real provenance);
- a visual critic that approves (or, in one test, rejects).
No screen, window, mouse, model or Paint is touched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from core.action_cancellation import CancellationToken
from core.actions import ActionOutcome
from core.computer_executor import ComputerAction, ComputerOperation
from core.input_capabilities import InputCapabilities
from core.keyboard_control import KeyboardController
from core.mouse_button import MouseButtonController
from core.mouse_click import MouseClickController
from core.mouse_hover import MouseHoverController
from core.paint_canvas_grounding import CANVAS_TARGET
from core.paint_draw_integration import (
    PaintDrawAction,
    PaintDrawExecutor,
    paint_draw_failure_response_fa,
)
from core.paint_state import PaintStateNormalizer
from core.paint_templates import TEMPLATES, house, match_paint_template
from core.pointer_stroke import PointerStrokeController
from core.screen_capture import ScreenCapture
from core.undo_journal import Reversal, UndoJournal
from core.vision_grounding import ground_structured_response
from core.window_binding import WindowBinder, WindowRect, WindowSnapshot, WindowTarget
from security.gate import Verdict
from security.permissions import PermissionDecision
from tests.test_screen_capture import fake_geometry

WIDTH, HEIGHT = 640, 480
CANVAS = WindowRect(240, 100, 600, 420)
TOOLBAR = {
    PaintStateNormalizer.PENCIL_TARGET: (300, 30),
    PaintStateNormalizer.SELECTED_PENCIL_TARGET: (300, 30),
    PaintStateNormalizer.BLACK_TARGET: (500, 30),
    PaintStateNormalizer.SELECTED_BLACK_TARGET: (500, 30),
    PaintStateNormalizer.MINIMUM_SIZE_TARGET: (100, 200),
    PaintStateNormalizer.SELECTED_MINIMUM_SIZE_TARGET: (100, 200),
}


# ------------------------------------------------------------ template matching


@pytest.mark.parametrize("prompt, name", [
    ("a house", "house"), ("house", "house"), ("a simple house", "house"),
    ("یک خانه", "house"), ("یه خونه ساده", "house"), ("خانه", "house"),
    ("a circle", "circle"), ("دایره", "circle"), ("a triangle", "triangle"),
    ("یک مثلث", "triangle"), ("a star", "star"), ("ستاره", "star"),
    ("a square", "square"), ("مربع", "square"), ("rectangle", "rectangle"),
    ("a straight line", "line"), ("خط", "line"), ("an arrow", "arrow"), ("فلش", "arrow"),
])
def test_plain_requests_name_exactly_one_template(prompt, name):
    assert match_paint_template(prompt).name == name


@pytest.mark.parametrize("prompt", [
    "a red house", "a house and a tree", "a cat", "two circles", "خانه و درخت",
    "a big house with a garden", "", "house house",
])
def test_anything_else_is_not_a_template(prompt):
    assert match_paint_template(prompt) is None


def test_every_template_is_a_bounded_plan_inside_the_unit_square():
    for template in TEMPLATES.values():
        plan = template.build()
        assert 1 <= len(plan.strokes) <= 8
        for stroke in plan.strokes:
            for point in stroke.points:
                assert 0.0 <= point.x <= 1.0 and 0.0 <= point.y <= 1.0
    assert [len(s.points) for s in house().strokes] == [5, 4, 4]


# ------------------------------------------------------------ simulated Paint


class SimulatedPaint:
    """The pixels of one Paint window, drawn into by injected native input."""

    def __init__(self) -> None:
        self.pixels = np.full((HEIGHT, WIDTH, 3), 255, dtype=np.uint8)
        self.cursor = (0, 0)
        self.down = False
        self.foreground = True
        self.presses = 0
        self.releases = 0
        self.on_move = None

    def move(self, x: int, y: int) -> None:
        if self.down:
            self._line(self.cursor, (x, y))
        self.cursor = (x, y)
        if self.on_move is not None:
            self.on_move()

    def button(self, name: str, down: bool) -> None:
        assert name == "left"
        if down:
            self.presses += 1
            self._line(self.cursor, self.cursor)
        else:
            self.releases += 1
        self.down = down

    def _line(self, a, b) -> None:
        steps = max(abs(b[0] - a[0]), abs(b[1] - a[1]), 1)
        for i in range(steps + 1):
            x = round(a[0] + (b[0] - a[0]) * i / steps)
            y = round(a[1] + (b[1] - a[1]) * i / steps)
            # Paint only marks its canvas; a click on a toolbar leaves no ink.
            if CANVAS.left <= x < CANVAS.right and CANVAS.top <= y < CANVAS.bottom:
                self.pixels[max(0, y - 1): y + 1, max(0, x - 1): x + 1] = 0

    def grab(self, window):
        bgra = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
        bgra[..., 0] = self.pixels[..., 2]
        bgra[..., 1] = self.pixels[..., 1]
        bgra[..., 2] = self.pixels[..., 0]
        return bgra.tobytes(), WIDTH, HEIGHT

    def ink_outside_canvas(self) -> int:
        mask = self.pixels.min(axis=2) < 128
        mask[CANVAS.top: CANVAS.bottom, CANVAS.left: CANVAS.right] = False
        return int(mask.sum())

    def ink_inside_canvas(self) -> int:
        region = self.pixels[CANVAS.top: CANVAS.bottom, CANVAS.left: CANVAS.right]
        return int((region.min(axis=2) < 128).sum())


def _snapshot(paint: SimulatedPaint) -> WindowSnapshot:
    return WindowSnapshot(
        hwnd=5150,
        pid=6160,
        thread_id=7170,
        title="Untitled - Paint",
        process_path=r"C:\Program Files\WindowsApps\Microsoft.Paint\mspaint.exe",
        process_start_time_100ns=987654321,
        window_rect=WindowRect(0, 0, WIDTH, HEIGHT),
        client_rect=WindowRect(0, 0, WIDTH, HEIGHT),
        dpi=96,
        visible=True,
        minimized=False,
        foreground=paint.foreground,
    )


class SimulatedComputer:
    """The parts of ComputerExecutor the Paint path uses, over a real WindowBinder."""

    TARGET = WindowTarget(app_id="paint", display_name="Paint", executable="mspaint.exe",
                          process_names=("mspaint.exe",))

    def __init__(self, paint: SimulatedPaint) -> None:
        self.paint = paint
        self.window_binder = WindowBinder(
            enumerate_fn=lambda: (_snapshot(paint),),
            snapshot_fn=lambda hwnd: _snapshot(paint),
        )
        self.opened = 0

    def _perform_with_context(self, action, *, cancel_token=None, effects=None):
        assert action == ComputerAction(ComputerOperation.OPEN_APP, app_id="paint")
        self.opened += 1
        return "opened", self.window_binder.bind(self.TARGET, require_foreground=True)

    def acquire_foreground(self, binding, *, cancel_token=None, **kwargs):
        return binding.snapshot

    def bind_app_window(self, app_id, *, require_foreground=False, expected_pid=None):
        assert app_id == "paint" and expected_pid == 6160
        return self.window_binder.bind(self.TARGET, require_foreground=require_foreground)


class ScriptedGrounder:
    """Answers each Vision request from the frame it is given, like the real one."""

    def __init__(self, *, canvas_confidence: float = 0.95) -> None:
        self.asked: list[str] = []
        self.canvas_confidence = canvas_confidence

    def locate(self, target, capture):
        self.asked.append(target)
        sx = capture.image.width / capture.source_width
        sy = capture.image.height / capture.source_height
        if target == CANVAS_TARGET:
            box, confidence = CANVAS, self.canvas_confidence
            point = ((CANVAS.left + CANVAS.right) // 2, (CANVAS.top + CANVAS.bottom) // 2)
        else:
            point = TOOLBAR[target]
            box = WindowRect(point[0] - 6, point[1] - 6, point[0] + 6, point[1] + 6)
            confidence = 0.95
        answer = {
            "status": "found",
            "image_width": capture.image.width,
            "image_height": capture.image.height,
            "candidates": [{
                "label": "target",
                "confidence": confidence,
                "point": {"x": int(point[0] * sx), "y": int(point[1] * sy)},
                "bbox": {"x": int(box.left * sx), "y": int(box.top * sy),
                         "width": max(1, int(box.width * sx)), "height": max(1, int(box.height * sy))},
            }],
        }
        return ground_structured_response(capture, json.dumps(answer))

    def close(self) -> None:
        pass


class Critic:
    def __init__(self, passes: bool = True) -> None:
        self.passes_value = passes
        self.subjects: list[str] = []

    def critique(self, request, preview):
        self.subjects.append(request)
        return SimpleNamespace(
            passes=lambda: self.passes_value, verdict="pass" if self.passes_value else "fail",
            confidence=0.9, summary="looks like the subject", model_name="fake-vision",
        )


class NoReference:
    def __init__(self) -> None:
        self.calls = 0

    def reference_for(self, request):
        self.calls += 1
        from core.paint_draw_integration import PaintReferenceUnavailable

        raise PaintReferenceUnavailable("no reference provider in this fixture")


def _allow(request, audit=None):
    verdict = Verdict(request=request, decision=PermissionDecision.ALLOW,
                      outcome=ActionOutcome.SUCCEEDED, reason="test", policy=None)
    if audit is not None:
        audit(verdict)
    return verdict


def _inputs(paint: SimulatedPaint):
    def factory(binder):
        hover = MouseHoverController(binder, move_fn=paint.move, cursor_fn=lambda: paint.cursor,
                                     sleep_fn=lambda s: None)
        buttons = MouseButtonController(binder, hover=hover, button_fn=paint.button, smooth_motion=True)
        return InputCapabilities(
            hover=hover,
            buttons=buttons,
            click=MouseClickController(binder, hover=hover, buttons=buttons),
            stroke=PointerStrokeController(binder, hover=hover, buttons=buttons, sleep_fn=lambda s: None),
            keyboard=KeyboardController(binder, text_fn=lambda t: None, key_fn=lambda k, d: None),
        )
    return factory


class Rig:
    def __init__(self, *, grounder=None, critic=None) -> None:
        self.paint = SimulatedPaint()
        self.computer = SimulatedComputer(self.paint)
        self.grounder = grounder or ScriptedGrounder()
        self.critic = critic or Critic()
        self.reference = NoReference()
        self.journal = UndoJournal(path=None)
        self.executor = PaintDrawExecutor(
            self.computer,
            reference_provider=self.reference,
            journal=self.journal,
            evaluate_fn=_allow,
            model_manager_factory=lambda: SimpleNamespace(ensure_runtime_ready=lambda: True,
                                                          runtime=object()),
            screen_capture_factory=lambda: ScreenCapture(grab=self.paint.grab,
                                                         geometry_fn=fake_geometry(WIDTH, HEIGHT)),
            inputs_factory=_inputs(self.paint),
            sleep_fn=lambda s: None,
            ui_grounder_factory=lambda runtime: self.grounder,
            critic_factory=lambda runtime: self.critic,
        )

    def draw(self, prompt: str, cancel_token=None):
        prepared = self.executor.prepare(PaintDrawAction(prompt=prompt))
        return self.executor.execute_prepared(prepared, cancel_token=cancel_token)


# ------------------------------------------------------------ the product path


def test_a_house_is_drawn_inside_the_grounded_canvas_and_verified():
    rig = Rig()
    execution = rig.draw("یک خانه")

    assert execution.result.outcome is ActionOutcome.SUCCEEDED, execution.result.detail
    assert "PASS" in execution.result.detail
    assert rig.reference.calls == 0, "a template never asks for a reference image"
    assert execution.run.stroke_count == 3
    assert rig.paint.ink_inside_canvas() > 500
    assert rig.paint.ink_outside_canvas() == 0
    assert rig.paint.down is False and rig.paint.presses == rig.paint.releases
    # Toolbar first, then the canvas, each from its own fresh frame.
    assert rig.grounder.asked[:3] == [PaintStateNormalizer.PENCIL_TARGET,
                                       PaintStateNormalizer.BLACK_TARGET,
                                       PaintStateNormalizer.MINIMUM_SIZE_TARGET]
    assert rig.grounder.asked[-1] == CANVAS_TARGET
    assert rig.critic.subjects == ["a simple house", "a simple house"]
    entry = execution.undo_entry
    assert entry is not None and entry.reversal is Reversal.APP_UNDO_STEPS


def test_other_requests_still_need_a_reference_and_touch_nothing():
    rig = Rig()
    execution = rig.draw("a cat")
    assert execution.result.outcome is ActionOutcome.FAILED
    assert "reference" in execution.result.detail
    assert rig.reference.calls == 1
    assert rig.computer.opened == 0 and rig.paint.presses == 0
    assert execution.failure_kind == "reference_unavailable"
    message = paint_draw_failure_response_fa(execution)
    assert "تصویر مرجع" in message and "خانه" in message
    assert "هیچ تغییری ایجاد نشد" in message


def test_a_rejected_preview_stops_before_paint_is_opened():
    rig = Rig(critic=Critic(passes=False))
    execution = rig.draw("a house")
    assert execution.result.outcome is ActionOutcome.FAILED
    assert rig.computer.opened == 0 and rig.paint.presses == 0
    assert execution.failure_kind == "preview_rejected"


def test_an_uncertain_canvas_stops_before_any_canvas_mark():
    rig = Rig(grounder=ScriptedGrounder(canvas_confidence=0.2))
    execution = rig.draw("a house")
    assert execution.result.outcome is ActionOutcome.FAILED
    assert rig.paint.ink_inside_canvas() == 0
    assert execution.failure_kind == "not_grounded"
    message = paint_draw_failure_response_fa(execution)
    assert "روی بوم چیزی کشیده نشد" in message


def test_focus_lost_to_a_dialog_stops_the_drawing_with_the_button_released():
    rig = Rig()
    moves = {"n": 0}

    def steal_focus():
        if rig.paint.down:
            moves["n"] += 1
            if moves["n"] == 20:
                rig.paint.foreground = False

    rig.paint.on_move = steal_focus
    import core.window_binding as window_binding

    original = window_binding.window_is_foreground
    window_binding.window_is_foreground = lambda hwnd: False
    try:
        execution = rig.draw("a house")
    finally:
        window_binding.window_is_foreground = original
    assert execution.result.outcome is ActionOutcome.FAILED
    assert rig.paint.down is False, "the synthetic button is always released"
    assert "PASS" not in execution.result.detail
    assert execution.failure_kind == "window_changed"


def test_a_popup_over_the_canvas_mid_drawing_stops_before_the_next_stroke():
    """Same window, geometry and focus; no Qronos input; the canvas content changed."""
    rig = Rig()
    original = rig.paint.button

    def button(name, down):
        original(name, down)
        if not down and rig.paint.releases == 4:  # 3 toolbar clicks, then stroke 1 ends
            rig.paint.pixels[300:340, 450:560] = 90  # a tooltip/popup inside the canvas

    rig.paint.button = button
    execution = rig.draw("a house")
    assert execution.result.outcome is ActionOutcome.FAILED
    assert execution.failure_kind == "window_changed"
    assert rig.paint.down is False and rig.paint.presses == rig.paint.releases == 4
    message = paint_draw_failure_response_fa(execution)
    assert "1 خط از 3 خط" in message and "دکمهٔ ماوس رها شد" in message


def test_stop_during_the_strokes_cancels_with_the_button_released():
    rig = Rig()
    token = CancellationToken()
    moves = {"n": 0}

    def stop_soon():
        if rig.paint.down:
            moves["n"] += 1
            if moves["n"] == 10:
                token.cancel()

    rig.paint.on_move = stop_soon
    execution = rig.draw("a house", cancel_token=token)
    assert execution.result.outcome is ActionOutcome.CANCELLED
    assert rig.paint.down is False and rig.paint.presses == rig.paint.releases
    # The partial drawing is reported honestly, never as a verified success.
    assert "canvas edit(s) committed" in execution.result.detail
    assert "PASS" not in execution.result.detail
