"""
Stop, Pause and shutdown must end computer work for good.

These tests attack the lifecycle around an action rather than the action
itself: approvals answered after Stop, turns that were still routing when
Stop arrived, Pause in the middle of a stroke, repeated Stop/close, and
cancellation at every boundary of multi-step accessibility, visual and
Calculator actions. Every native boundary is a fake; nothing real runs.
"""

from __future__ import annotations

from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest

import core.calculator_semantic as calculator_semantic
from core.accessibility_action_executor import AccessibilityActionExecutor
from core.accessibility_targeting import AccessibilityTargetLocator
from core.action_audit import ActionAuditLog
from core.action_cancellation import (
    CANCEL_REASON_PAUSE,
    CANCEL_REASON_SHUTDOWN,
    CANCEL_REASON_USER_STOP,
    ActionCancelled,
    ActiveActionRegistry,
    CancellationToken,
)
from core.actions import ActionOutcome, ActionResult
from core.calculator_semantic import (
    CalculatorSemanticCancelled,
    CalculatorSemanticExecutor,
    parse_calculator_semantic_command,
)
from core.computer_executor import ComputerExecutor
from core.mouse_button import MouseButton, MouseButtonController
from core.mouse_hover import MouseHoverController
from core.paint_draw_integration import PaintDrawAction, PaintDrawExecution
from core.pointer_stroke import PointerStrokeController, StrokeProgress
from core.undo_journal import UndoJournal
from core.visual_action_executor import VisualActionExecutor
from core.window_binding import WindowBinder, WindowBinding, WindowRect, WindowSnapshot
from core.windows_accessibility import ActionableUiElement
from core.windows_accessibility_mutation import UiInvokeReceipt
from security.gate import Verdict
from security.permissions import PermissionDecision

from test_accessibility_action_executor import FakeInputs as A11yInputs
from test_accessibility_action_executor import address, good_focus
from test_accessibility_action_executor import grant as a11y_grant
from test_accessibility_action_executor import snapshot as a11y_snapshot
from test_runtime_bridge import (
    FakeOrchestrator,
    FakeSpeechRuntime,
    captured_events,
    event_types,
    prepared_runtime,
    screenless_capture,
)
from test_visual_action_executor import FakeCapture as VisualCapture
from test_visual_action_executor import FakeInputs as VisualInputs
from test_visual_action_executor import grant_for as visual_grant
from test_visual_action_executor import setup_context as visual_context


DRAW = "draw a simple cat in Paint"
OTHER_DRAW = "draw a simple house in Paint"


# ------------------------------------------------------------ registry epoch


def test_stop_reaches_a_turn_that_has_not_begun_its_action_yet():
    registry = ActiveActionRegistry()
    registry.mark_turn_start()
    registry.cancel_all(CANCEL_REASON_USER_STOP)

    with registry.begin("paint_draw") as token:
        assert token.cancelled
        assert token.reason == CANCEL_REASON_USER_STOP
        with pytest.raises(ActionCancelled):
            token.raise_if_cancelled()


def test_a_turn_started_after_stop_is_not_poisoned():
    registry = ActiveActionRegistry()
    registry.mark_turn_start()
    registry.cancel_all(CANCEL_REASON_USER_STOP)
    registry.mark_turn_start()

    with registry.begin("paint_draw") as token:
        assert not token.cancelled


def test_stop_epoch_is_per_thread_turn():
    registry = ActiveActionRegistry()
    registry.mark_turn_start()  # this thread's turn predates the Stop
    other: dict = {}

    def later_turn():
        registry.mark_turn_start()
        with registry.begin("computer_action") as token:
            other["cancelled"] = token.cancelled

    registry.cancel_all(CANCEL_REASON_PAUSE)
    worker = threading.Thread(target=later_turn)
    worker.start()
    worker.join(2.0)

    assert other == {"cancelled": False}
    assert registry.stopped_since_turn_start() == CANCEL_REASON_PAUSE


def test_close_is_idempotent_and_refuses_every_later_action():
    registry = ActiveActionRegistry()
    with registry.begin("paint_draw") as running:
        registry.close()
        registry.close()
        assert running.reason == CANCEL_REASON_SHUTDOWN
    # Even a thread that never marked a turn cannot start work after close.
    registry.mark_turn_start()
    with registry.begin("computer_action") as later:
        assert later.cancelled and later.reason == CANCEL_REASON_SHUTDOWN
    registry.cancel_all(CANCEL_REASON_USER_STOP)
    assert registry.closed


def test_repeated_stop_is_harmless_and_keeps_reason_vocabulary_closed():
    registry = ActiveActionRegistry()
    with registry.begin("paint_draw") as token:
        assert registry.cancel_all() == ("paint_draw",)
        assert registry.cancel_all() == ()
        assert token.reason == CANCEL_REASON_USER_STOP
    with pytest.raises(ValueError):
        registry.cancel_all("whatever the caller typed")


# ----------------------------------------------------- runtime approval flow


def _verdict(decision: PermissionDecision):
    return SimpleNamespace(
        refused=decision is PermissionDecision.DENY,
        allowed=decision is PermissionDecision.ALLOW,
        needs_approval=decision
        not in {PermissionDecision.ALLOW, PermissionDecision.DENY},
        decision=decision,
    )


class ApprovalPaintExecutor:
    """Paint executor double whose drawing needs on-screen approval."""

    def __init__(self, screen=PermissionDecision.REQUIRE_UI_CONFIRMATION) -> None:
        self.screen = screen
        self.executed: list = []
        self.discarded: list = []
        self.declined: list = []

    def prepare(self, action):
        return SimpleNamespace(
            action=action,
            gui_verdict=_verdict(PermissionDecision.REQUIRE_VOICE_CONFIRMATION),
            screen_verdict=_verdict(self.screen),
        )

    def execute_prepared(self, prepared, *, cancel_token=None, **kwargs):
        request = PaintDrawAction(prepared.action.prompt).gui_request()
        cancelled = cancel_token is not None and cancel_token.cancelled
        self.executed.append((prepared.action.prompt, cancelled))
        outcome = ActionOutcome.CANCELLED if cancelled else ActionOutcome.SUCCEEDED
        return PaintDrawExecution(request, ActionResult(request.action_id, outcome, ""))

    def discard_pending(self, prepared, *, reason):
        self.discarded.append((prepared.action.prompt, reason))

    def decline_screen(self, prepared):
        self.declined.append(prepared.action.prompt)


def _runtime(paint=None, **kwargs):
    runtime = prepared_runtime(**kwargs)
    runtime.undo_journal = UndoJournal(path=None)
    runtime.computer_executor = ComputerExecutor(dynamic_apps=False)
    runtime.paint_draw_executor = paint or ApprovalPaintExecutor()
    runtime.screen_grabs = []
    runtime.screen_capture = screenless_capture(watch=runtime.screen_grabs)
    return runtime


def _await_approval(runtime, transcript=DRAW):
    report: dict = {}
    response = runtime._prepare_voice_paint_draw_action(transcript, report)
    assert response == ""
    assert runtime.pending_paint_draw is not None
    return report


def _answer(runtime, *, approved=True, question=DRAW):
    return event_types(
        captured_events(
            lambda: runtime.look_at_screen(approved=approved, question=question)
        )
    )


def test_stop_while_awaiting_approval_discards_it_and_late_yes_does_nothing():
    runtime = _runtime()
    _await_approval(runtime)

    events = captured_events(runtime.request_stop_response)
    assert runtime.pending_paint_draw is None
    assert runtime.paint_draw_executor.discarded == [
        ("a simple cat", CANCEL_REASON_USER_STOP)
    ]
    assert "computer_approval_discarded" in event_types(events)

    kinds = _answer(runtime, approved=True, question=DRAW)

    assert "screen_approval_stale" in kinds
    assert runtime.paint_draw_executor.executed == []
    # The late "yes" must not fall through to a generic screen read either.
    assert runtime.screen_grabs == []
    assert runtime.orchestrator.plans == []


def test_stop_result_reports_discarded_approvals():
    runtime = _runtime()
    _await_approval(runtime)

    result = runtime.request_stop_response()

    assert result["pendingApprovalsDiscarded"] == 1
    assert runtime.request_stop_response()["pendingApprovalsDiscarded"] == 0


def test_late_answer_for_a_stopped_prompt_cannot_approve_a_newer_one():
    runtime = _runtime()
    _await_approval(runtime, DRAW)
    runtime.request_stop_response()
    _await_approval(runtime, OTHER_DRAW)

    kinds = _answer(runtime, approved=True, question=DRAW)

    assert "screen_approval_stale" in kinds
    assert runtime.paint_draw_executor.executed == []
    assert runtime.pending_paint_draw is not None

    # The answer to the current prompt still works, exactly once.
    _answer(runtime, approved=True, question=OTHER_DRAW)
    assert runtime.paint_draw_executor.executed == [("a simple house", False)]
    assert runtime.pending_paint_draw is None


def test_unrelated_answer_cannot_consume_a_pending_drawing():
    runtime = _runtime()
    _await_approval(runtime, DRAW)

    kinds = _answer(runtime, approved=True, question="what is on my screen?")

    assert "screen_approval_stale" in kinds
    assert runtime.pending_paint_draw is not None
    assert runtime.paint_draw_executor.executed == []
    assert runtime.screen_grabs == []


def test_asking_again_after_stop_creates_a_fresh_usable_prompt():
    runtime = _runtime()
    _await_approval(runtime, DRAW)
    runtime.request_stop_response()
    _await_approval(runtime, DRAW)

    _answer(runtime, approved=True, question=DRAW)

    assert runtime.paint_draw_executor.executed == [("a simple cat", False)]


def test_pause_discards_pending_approval_and_resume_does_not_revive_it():
    runtime = _runtime()
    _await_approval(runtime)

    captured_events(lambda: runtime.set_runtime_paused(True))
    assert runtime.pending_paint_draw is None
    assert runtime.paint_draw_executor.discarded[0][1] == CANCEL_REASON_PAUSE

    # Answered while paused: rejected by the pause gate.
    _answer(runtime, approved=True, question=DRAW)
    captured_events(lambda: runtime.set_runtime_paused(False))
    # Answered after Resume: stale.
    kinds = _answer(runtime, approved=True, question=DRAW)

    assert "screen_approval_stale" in kinds
    assert runtime.paint_draw_executor.executed == []
    assert runtime.screen_grabs == []


def test_close_discards_approvals_and_nothing_can_run_afterwards():
    paint = ApprovalPaintExecutor()
    runtime = _runtime(paint)
    _await_approval(runtime)

    runtime.close()
    runtime.close()

    assert paint.discarded == [("a simple cat", CANCEL_REASON_SHUTDOWN)]
    kinds = _answer(runtime, approved=True, question="")
    assert "screen_approval_stale" in kinds
    assert paint.executed == [] and runtime.screen_grabs == []

    # A worker that reaches dispatch after close cannot open a new prompt.
    report: dict = {}
    response = runtime._prepare_voice_paint_draw_action(OTHER_DRAW, report)
    assert report.get("voiceComputerCancelled") is True
    assert runtime.pending_paint_draw is None
    assert response


def test_stop_during_routing_prevents_the_prompt_from_ever_opening():
    paint = ApprovalPaintExecutor()
    runtime = _runtime(paint)
    runtime._computer_actions.mark_turn_start()
    runtime.request_stop_response()  # arrives while this turn is still routing

    report: dict = {}
    runtime._prepare_voice_paint_draw_action(DRAW, report)

    assert runtime.pending_paint_draw is None
    assert report["voiceComputerCancelled"] is True
    assert paint.discarded == [("a simple cat", CANCEL_REASON_USER_STOP)]


def test_stop_during_routing_cancels_an_allowed_drawing_before_any_effect():
    paint = ApprovalPaintExecutor(screen=PermissionDecision.ALLOW)
    paint.prepare = lambda action: SimpleNamespace(
        action=action,
        gui_verdict=_verdict(PermissionDecision.ALLOW),
        screen_verdict=_verdict(PermissionDecision.ALLOW),
    )
    runtime = _runtime(paint)
    runtime._computer_actions.mark_turn_start()
    runtime.request_stop_response()

    report: dict = {}
    runtime._prepare_voice_paint_draw_action(DRAW, report)

    # The executor ran only to observe a token that was already cancelled.
    assert paint.executed == [("a simple cat", True)]
    assert report["voiceComputerCancelled"] is True


class StoppingSpeech(FakeSpeechRuntime):
    """Stop arrives while the voice command is still being transcribed."""

    def __init__(self, transcript, runtime_ref):
        super().__init__(transcript=transcript)
        self.runtime_ref = runtime_ref

    def transcribe_file(self, audio_path, language=None):
        self.runtime_ref["runtime"].request_stop_response()
        return super().transcribe_file(audio_path, language)


def test_stop_during_transcription_never_opens_a_screen_prompt():
    ref: dict = {}
    runtime = _runtime(speech=StoppingSpeech("به این پنجره نگاه کن", ref))
    ref["runtime"] = runtime

    kinds = event_types(captured_events(runtime.push_to_talk))

    assert "voice_needs_screen" not in kinds
    assert runtime.pending_look is None
    _answer(runtime, approved=True, question="")
    assert runtime.screen_grabs == []


def test_stop_while_screen_read_prompt_is_open_refuses_the_late_yes():
    runtime = _runtime(speech=FakeSpeechRuntime(transcript="به این پنجره نگاه کن"))
    captured_events(runtime.push_to_talk)
    assert runtime.pending_look == "به این پنجره نگاه کن"

    runtime.request_stop_response()
    kinds = _answer(runtime, approved=True, question="به این پنجره نگاه کن")

    assert runtime.pending_look is None
    assert "screen_approval_stale" in kinds
    assert runtime.screen_grabs == []


def test_direct_screen_request_without_any_prompt_still_works():
    runtime = _runtime()
    kinds = _answer(runtime, approved=True, question="What is on my screen?")
    assert "voice_turn_complete" in kinds
    assert runtime.screen_grabs == [None]


# --------------------------------------------------------- Pause mid-stroke


class _Binder:
    def revalidate(self, binding, *, require_foreground=True, require_same_layout=True):
        return binding.snapshot


def _paint_binding() -> WindowBinding:
    return WindowBinding(
        app_id="paint",
        display_name="Paint",
        snapshot=WindowSnapshot(
            hwnd=101,
            pid=202,
            thread_id=303,
            title="Untitled - Paint",
            process_path=r"C:\Program Files\WindowsApps\Microsoft.Paint\mspaint.exe",
            process_start_time_100ns=404,
            window_rect=WindowRect(90, 90, 510, 410),
            client_rect=WindowRect(100, 100, 500, 400),
            dpi=96,
            visible=True,
            minimized=False,
            foreground=True,
        ),
        identity_token="identity",
        layout_token="layout",
    )


def test_pause_mid_stroke_releases_the_button_and_stops_moving():
    from test_computer_action_cancellation import _authorization

    runtime = _runtime()
    events: list = []
    paused = {"done": False}

    def move(x, y):
        events.append(("move", x, y))
        if len([e for e in events if e[0] == "move"]) == 3 and not paused["done"]:
            paused["done"] = True
            captured_events(lambda: runtime.set_runtime_paused(True))

    binder = _Binder()
    hover = MouseHoverController(binder, move_fn=move)
    buttons = MouseButtonController(
        binder,
        hover=hover,
        button_fn=lambda name, down: events.append(("button", name, down)),
    )
    stroke = PointerStrokeController(binder, hover=hover, buttons=buttons)
    binding = _paint_binding()
    progress = StrokeProgress()

    with runtime._computer_actions.begin("paint_draw") as token:
        with pytest.raises(ActionCancelled) as raised:
            stroke.stroke(
                binding,
                [(150, 150), (160, 150), (170, 150), (180, 150), (190, 150)],
                authorization=_authorization(binding),
                cancel_token=token,
                progress=progress,
            )

    assert raised.value.reason == CANCEL_REASON_PAUSE
    assert [e for e in events if e[0] == "button"] == [
        ("button", "left", True),
        ("button", "left", False),
    ]
    assert len([e for e in events if e[0] == "move"]) == 4  # 3 + guarded release
    assert not buttons.is_held(MouseButton.LEFT)
    assert progress.clean_release
    assert runtime.runtime_paused

    captured_events(lambda: runtime.set_runtime_paused(False))
    runtime._computer_actions.mark_turn_start()
    with runtime._computer_actions.begin("paint_draw") as fresh:
        assert not fresh.cancelled


# ------------------------------------------------ accessibility boundaries


class _A11yComputer:
    def __init__(self, binder):
        self.binder = binder
        self.foreground = 0

    def acquire_foreground(self, binding, *, cancel_token=None, **kwargs):
        self.foreground += 1
        return self.binder.revalidate(binding, require_foreground=True, require_same_layout=True)


def _a11y(token, *, item=None, focus_target_fn=None, invoke_fn=None, focus=None):
    snap = a11y_snapshot()
    binder = WindowBinder(enumerate_fn=lambda: (snap,), snapshot_fn=lambda hwnd: snap)
    binding = binder.bind_snapshot(
        snap, app_id="example", display_name="Example Application", require_foreground=True,
    )
    element = item or address()
    locator = AccessibilityTargetLocator(None, enumerate_fn=lambda hwnd: (element,))
    inputs = A11yInputs()
    computer = _A11yComputer(binder)
    executor = AccessibilityActionExecutor(
        binder,
        computer,
        locator,
        inputs=inputs,
        focus_fn=lambda: focus or good_focus(),
        focus_target_fn=focus_target_fn or (lambda hwnd, runtime_id, **kwargs: good_focus()),
        invoke_fn=invoke_fn or (lambda hwnd, runtime_id, **kwargs: UiInvokeReceipt(runtime_id)),
        lookup_runtime_fn=lambda hwnd, runtime_id: element,
        cancel_token=token,
    )
    return executor, binding, inputs, computer


def test_a11y_stop_after_native_focus_returns_sends_no_text():
    token = CancellationToken()

    def focus_then_stop(hwnd, runtime_id, **kwargs):
        token.cancel()  # Stop lands while the single native call runs
        return good_focus()

    executor, binding, inputs, _ = _a11y(token, focus_target_fn=focus_then_stop)

    with pytest.raises(ActionCancelled):
        executor.focus_and_type_target(
            binding, "Address Bar", "secret path", role="text_input", grant=a11y_grant(binding),
        )

    assert inputs.keyboard.texts == []


def test_a11y_stop_after_click_sends_no_text():
    token = CancellationToken()
    executor, binding, inputs, _ = _a11y(token)
    original = inputs.click.click_at

    def click_then_stop(*args, **kwargs):
        result = original(*args, **kwargs)
        token.cancel()
        return result

    inputs.click.click_at = click_then_stop

    with pytest.raises(ActionCancelled):
        executor.click_and_type_target(
            binding, "Address Bar", "text", role="text_input", grant=a11y_grant(binding),
        )

    assert len(inputs.click.calls) == 1
    assert inputs.keyboard.texts == []


def test_a11y_stop_during_semantic_reread_never_invokes():
    token = CancellationToken()
    invoked = []
    button = ActionableUiElement(
        runtime_id="42.9",
        name="Equals",
        automation_id="equalButton",
        class_name="Button",
        control_type="ControlType.Button",
        value="",
        enabled=True,
        focusable=True,
        focused=False,
        value_pattern=False,
        value_read_only=None,
        text_pattern=False,
        invoke_pattern=True,
        expand_pattern=False,
        left=200,
        top=100,
        width=60,
        height=40,
    )
    executor, binding, _inputs, computer = _a11y(
        token,
        item=button,
        invoke_fn=lambda hwnd, runtime_id, **kwargs: invoked.append(runtime_id),
    )

    def reread_then_stop(hwnd, runtime_id):
        token.cancel()  # Stop lands during the last UIA read before Invoke
        return button

    executor.lookup_runtime_fn = reread_then_stop

    with pytest.raises(ActionCancelled):
        executor.invoke_target(binding, "Equals", role="button", grant=a11y_grant(binding))

    assert invoked == []


def test_a11y_cancelled_press_key_sends_nothing_and_token_never_replaces_grant():
    token = CancellationToken()
    executor, binding, inputs, computer = _a11y(token)

    with pytest.raises(PermissionError):
        executor.focus_target(binding, "Address Bar", role="text_input", grant=None)
    assert computer.foreground == 0

    token.cancel()
    with pytest.raises(ActionCancelled):
        executor.press_key(binding, "enter", grant=a11y_grant(binding))
    assert inputs.keyboard.keys == []


def test_a11y_focus_wait_wakes_on_stop():
    token = CancellationToken()
    calls = {"n": 0}

    def focus_fn():
        calls["n"] += 1
        if calls["n"] == 2:
            token.cancel()
        return replace(good_focus(), name="Somewhere else")

    executor, binding, inputs, _ = _a11y(token)
    executor.focus_fn = focus_fn

    with pytest.raises(ActionCancelled):
        executor.click_and_type_target(
            binding, "Address Bar", "text", role="text_input",
            grant=a11y_grant(binding), focus_timeout=30.0,
        )

    assert calls["n"] == 2
    assert inputs.keyboard.texts == []


# ------------------------------------------------------- visual boundaries


class _VisualComputer:
    def __init__(self, binder):
        self.binder = binder

    def acquire_foreground(self, binding, *, cancel_token=None, **kwargs):
        return self.binder.revalidate(binding, require_foreground=True, require_same_layout=True)


def test_visual_stop_during_vision_inference_never_clicks():
    binder, binding, capture, target = visual_context()
    token = CancellationToken()

    class StoppingLocator:
        def locate(self, description, frame):
            token.cancel()  # Stop while the vision model was thinking
            return target

    inputs = VisualInputs()
    executor = VisualActionExecutor(
        binder, _VisualComputer(binder), VisualCapture(capture), StoppingLocator(),
        inputs=inputs, cancel_token=token,
    )

    with pytest.raises(ActionCancelled):
        executor.click_target(binding, "the control", grant=visual_grant(binding), screen_approved=True)

    assert inputs.click.calls == []


def test_visual_stop_between_click_and_type_sends_no_text():
    binder, binding, capture, target = visual_context()
    token = CancellationToken()

    class Locator:
        def locate(self, description, frame):
            return target

    inputs = VisualInputs()
    original = inputs.click.click_at

    def click_then_stop(*args, **kwargs):
        result = original(*args, **kwargs)
        token.cancel()
        return result

    inputs.click.click_at = click_then_stop
    executor = VisualActionExecutor(
        binder, _VisualComputer(binder), VisualCapture(capture), Locator(),
        inputs=inputs, cancel_token=token,
    )

    with pytest.raises(ActionCancelled):
        executor.click_and_type_target(
            binding, "the field", "hello", grant=visual_grant(binding), screen_approved=True,
        )

    assert len(inputs.click.calls) == 1
    assert inputs.keyboard.text_calls == []


# ------------------------------------------------- Calculator boundaries


def _allow(request, audit=None):
    verdict = Verdict(
        request=request,
        decision=PermissionDecision.ALLOW,
        outcome=ActionOutcome.SUCCEEDED,
        reason="test",
        policy=None,
    )
    if audit is not None:
        audit(verdict)
    return verdict


def _calc_button(automation_id: str, runtime_id: str, name: str) -> ActionableUiElement:
    return ActionableUiElement(
        runtime_id=runtime_id,
        name=name,
        automation_id=automation_id,
        class_name="Button",
        control_type="ControlType.Button",
        value="",
        enabled=True,
        focusable=True,
        focused=False,
        value_pattern=False,
        value_read_only=None,
        text_pattern=False,
        invoke_pattern=True,
        expand_pattern=False,
        left=10,
        top=10,
        width=40,
        height=40,
    )


class CalcRig:
    """Calculator executor with every native boundary replaced."""

    def __init__(self, monkeypatch, *, cancel_at: str | None, cancel_after_invokes: int = 0):
        self.token = CancellationToken()
        self.cancel_at = cancel_at
        self.cancel_after_invokes = cancel_after_invokes
        self.calls: list[str] = []
        self.invoked: list[str] = []
        self.audit = ActionAuditLog(path=None)
        self.plan = parse_calculator_semantic_command(
            "\u0645\u0627\u0634\u06cc\u0646 \u062d\u0633\u0627\u0628 \u062f\u0648 "
            "\u0631\u0648 \u0636\u0631\u0628\u062f\u0631 \u0686\u0647\u0627\u0631 \u06a9\u0646"
        )
        rig = self
        # The plan itself starts with clearButton; a recovery after Stop
        # would show up as a second, unplanned clearButton invoke.
        self.elements = [
            _calc_button(aid, f"rt-{aid}", aid)
            for aid in dict.fromkeys(self.plan.automation_ids)
        ] + [
            SimpleNamespace(
                automation_id="CalculatorResults",
                runtime_id="rt-display",
                role="text",
                invoke_pattern=False,
                enabled=True,
                name="Display is 0",
            ),
        ]

        binding = SimpleNamespace(app_id="calculator", hwnd=55, snapshot=SimpleNamespace(pid=66))

        class Computer:
            audit = rig.audit
            window_binder = SimpleNamespace(revalidate=lambda *a, **k: None)

            def prepare(self, action):
                rig.hit("prepare")
                return SimpleNamespace(verdict=_verdict(PermissionDecision.ALLOW), action=action)

            def execute_prepared(self, prepared, *, cancel_token=None, **kwargs):
                # The launch's own waits must see the same token.
                assert cancel_token is rig.token
                rig.hit("launch")
                if cancel_token is not None and cancel_token.cancelled:
                    return SimpleNamespace(
                        result=ActionResult("x", ActionOutcome.CANCELLED, ""),
                        window_binding=None,
                    )
                return SimpleNamespace(
                    result=ActionResult("x", ActionOutcome.SUCCEEDED, ""),
                    window_binding=binding,
                )

        class Executor:
            def __init__(self, binder, computer, locator, *, cancel_token=None):
                assert cancel_token is rig.token, "runtime token must reach UIA steps"

            def invoke_target(self, binding, name, *, role, grant):
                rig.invoked.append(name)
                if rig.cancel_at == "invoke" and len(rig.invoked) == rig.cancel_after_invokes:
                    rig.token.cancel()
                element = next(e for e in rig.elements if getattr(e, "name", "") == name)
                return SimpleNamespace(target=SimpleNamespace(element=element))

        def discover(hwnd):
            rig.hit("discover")
            return rig.elements

        monkeypatch.setattr(calculator_semantic, "AccessibilityActionExecutor", Executor)
        monkeypatch.setattr(calculator_semantic, "actionable_elements_in_window", discover)
        def gate(request, audit=None):
            rig.hit("evaluate")
            return _allow(request, audit)

        monkeypatch.setattr(calculator_semantic, "evaluate", gate)
        monkeypatch.setattr(
            calculator_semantic,
            "actionable_element_by_runtime_id",
            lambda hwnd, rid: (rig.hit("postcondition"), None)[1],
        )
        self.executor = CalculatorSemanticExecutor(Computer())

    def hit(self, name: str) -> None:
        self.calls.append(name)
        if self.cancel_at == name:
            self.token.cancel()

    def run(self):
        return self.executor.execute(self.plan, voice_confirmed=True, cancel_token=self.token)


@pytest.mark.parametrize(
    ("cancel_at", "expected_calls"),
    [
        ("prepare", ["prepare", "launch"]),  # cancelled before launch side effects
        ("launch", ["prepare", "launch"]),  # launch observed the Stop
        ("discover", ["prepare", "launch", "discover"]),  # after UIA discovery
        ("evaluate", ["prepare", "launch", "discover", "evaluate"]),  # at the grant
    ],
)
def test_calculator_stop_before_any_invoke(monkeypatch, cancel_at, expected_calls):
    rig = CalcRig(monkeypatch, cancel_at=cancel_at)

    with pytest.raises(CalculatorSemanticCancelled) as raised:
        rig.run()

    assert rig.calls == expected_calls
    assert rig.invoked == []
    assert raised.value.steps_invoked == 0


def test_calculator_pre_cancelled_token_does_nothing(monkeypatch):
    rig = CalcRig(monkeypatch, cancel_at=None)
    rig.token.cancel()

    with pytest.raises(CalculatorSemanticCancelled):
        rig.run()

    assert rig.calls == [] and rig.invoked == []


@pytest.mark.parametrize("after", [1, 2])
def test_calculator_stop_mid_sequence_sends_no_recovery_and_reports_partial(monkeypatch, after):
    rig = CalcRig(monkeypatch, cancel_at="invoke", cancel_after_invokes=after)
    total = len(rig.plan.automation_ids)

    with pytest.raises(CalculatorSemanticCancelled) as raised:
        rig.run()

    # Exactly the planned prefix: Stop means no recovery input either.
    assert rig.invoked == list(rig.plan.automation_ids[:after])
    assert "postcondition" not in rig.calls
    assert (raised.value.steps_invoked, raised.value.steps_total) == (after, total)
    completed = [r for r in rig.audit.records() if r.event.value == "completed"]
    assert completed[-1].outcome == ActionOutcome.CANCELLED.value


def test_runtime_wires_calculator_to_the_stop_registry(monkeypatch):
    runtime = _runtime()
    seen = {}

    def fake_execute(self, plan, *, voice_confirmed, cancel_token=None):
        seen["token"] = cancel_token
        runtime.request_stop_response()
        raise CalculatorSemanticCancelled(
            cancel_token.reason, steps_invoked=1, steps_total=3,
        )

    monkeypatch.setattr(CalculatorSemanticExecutor, "execute", fake_execute)
    report: dict = {}
    response = runtime._execute_voice_calculator_semantic_action(
        "\u0645\u0627\u0634\u06cc\u0646 \u062d\u0633\u0627\u0628 \u062f\u0648 "
        "\u0631\u0648 \u0636\u0631\u0628\u062f\u0631 \u0686\u0647\u0627\u0631 \u06a9\u0646",
        report,
    )

    assert seen["token"].cancelled
    assert seen["token"].reason == CANCEL_REASON_USER_STOP
    assert report["voiceComputerCancelled"] is True
    assert report["calculatorStepsInvoked"] == 1
    assert "1 از 3" in response
