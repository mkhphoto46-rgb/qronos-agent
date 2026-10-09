"""
Stop must end an in-flight computer/Paint action safely.

Every test here drives the real input controllers through injected native
functions and fake window binders. No real window, mouse or application is
touched (see tests/conftest.py).
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import core.input_safety as input_safety
from core.action_audit import ActionAuditLog, AuditEvent
from core.action_cancellation import (
    CANCEL_REASON_SHUTDOWN,
    CANCEL_REASON_USER_STOP,
    ActionCancelled,
    ActiveActionRegistry,
    CancellationToken,
)
from core.actions import ActionOutcome, ActionRequest, ActionResult
from core.computer_executor import ComputerAction, ComputerExecutor, ComputerOperation
from core.input_safety import BoundInputAuthorization, InputPermissionRequired
from core.mouse_button import MouseButton, MouseButtonController
from core.mouse_click import MouseClickController
from core.mouse_hover import MouseHoverController
from core.paint_adapter import PaintAdapter, PaintCanvasProgress, PaintCanvasRegion
from core.paint_draw_integration import (
    PaintDrawAction,
    PaintDrawExecution,
    PaintDrawSideEffects,
)
from core.pointer_stroke import PointerStrokeController, StrokeProgress
from core.runtime_bridge import STOP_RESPONSE_ACTION, QronosRuntime, handle_action
from core.shape_planner import ShapePlanner
from core.undo_journal import Reversal, UndoJournal
from core.window_binding import WindowBinding, WindowBindingStale, WindowRect, WindowSnapshot
from security.gate import Verdict, evaluate, issue_grant
from security.permissions import ActionCategory, PermissionDecision


# --------------------------------------------------------------- fixtures


def _snapshot() -> WindowSnapshot:
    return WindowSnapshot(
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
    )


def _binding() -> WindowBinding:
    return WindowBinding(
        app_id="paint",
        display_name="Paint",
        snapshot=_snapshot(),
        identity_token="identity",
        layout_token="layout",
    )


def _authorization(binding: WindowBinding | None = None) -> BoundInputAuthorization:
    binding = binding or _binding()
    request = ActionRequest(
        category=ActionCategory.GUI_AUTOMATION,
        target="app:paint",
        summary="test bounded Paint input",
    )
    verdict = evaluate(request)
    grant = issue_grant(
        request,
        verdict,
        approval=verdict.decision if verdict.needs_approval else None,
    )
    return BoundInputAuthorization.bind(grant, binding)


class _Binder:
    """Revalidation succeeds until ``fail_from`` calls, then the window is stale."""

    def __init__(self, *, fail_from: int | None = None) -> None:
        self.calls = 0
        self.fail_from = fail_from

    def revalidate(self, binding, *, require_foreground=True, require_same_layout=True):
        self.calls += 1
        if self.fail_from is not None and self.calls >= self.fail_from:
            raise WindowBindingStale("foreground moved to another window")
        return binding.snapshot


class _Rig:
    """Real hover/button/stroke controllers over recorded native calls."""

    def __init__(self, binder=None, *, on_move=None, button_fn=None) -> None:
        self.binder = binder or _Binder()
        self.events: list[tuple] = []
        self._on_move = on_move

        def move(x, y):
            self.events.append(("move", x, y))
            if self._on_move is not None:
                self._on_move(len([e for e in self.events if e[0] == "move"]))

        def button(name, down):
            self.events.append(("button", name, down))

        self.hover = MouseHoverController(self.binder, move_fn=move)
        self.buttons = MouseButtonController(
            self.binder,
            hover=self.hover,
            button_fn=button_fn or button,
        )
        self.stroke = PointerStrokeController(
            self.binder,
            hover=self.hover,
            buttons=self.buttons,
            sleep_fn=lambda _seconds: None,
        )

    def button_events(self):
        return [event for event in self.events if event[0] == "button"]

    def moves(self):
        return [(event[1], event[2]) for event in self.events if event[0] == "move"]


_PATH = [(150, 150), (160, 150), (170, 150), (180, 150), (190, 150), (200, 150)]


# ------------------------------------------------------- token and registry


def test_token_cancels_once_with_fixed_reason_vocabulary():
    token = CancellationToken(label="paint_draw")
    assert token.cancel(CANCEL_REASON_USER_STOP) is True
    assert token.cancel(CANCEL_REASON_SHUTDOWN) is False
    assert token.reason == CANCEL_REASON_USER_STOP
    with pytest.raises(ActionCancelled) as raised:
        token.raise_if_cancelled()
    assert raised.value.reason == CANCEL_REASON_USER_STOP

    with pytest.raises(ValueError):
        CancellationToken().cancel("arbitrary caller text")


def test_token_sleep_wakes_immediately_on_cancel():
    token = CancellationToken()
    threading.Timer(0.05, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(ActionCancelled):
        token.sleep(5.0)
    assert time.monotonic() - started < 2.0


def test_registry_cancels_only_actions_active_at_stop_time():
    registry = ActiveActionRegistry()
    with registry.begin("paint_draw") as first:
        cancelled = registry.cancel_all(CANCEL_REASON_USER_STOP)
        assert cancelled == ("paint_draw",)
        assert first.cancelled
    # The registration ended with the action, and a later action is fresh:
    # a Stop that already happened can never poison the next command.
    assert registry.active() == ()
    with registry.begin("computer_action") as later:
        assert not later.cancelled
    assert registry.cancel_all() == ()


def test_registry_never_touches_tokens_it_does_not_own():
    gui = ActiveActionRegistry()
    unrelated = ActiveActionRegistry()
    with unrelated.begin("queue_task") as other, gui.begin("paint_draw") as mine:
        gui.cancel_all(CANCEL_REASON_USER_STOP)
        assert mine.cancelled
        assert not other.cancelled


def test_registry_wait_idle_is_bounded():
    registry = ActiveActionRegistry()
    release = threading.Event()

    def worker():
        with registry.begin("paint_draw") as token:
            while not token.cancelled and not release.is_set():
                time.sleep(0.01)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while not registry.active():
        time.sleep(0.005)
    assert registry.wait_idle(0.05) is False
    registry.cancel_all(CANCEL_REASON_SHUTDOWN)
    assert registry.wait_idle(2.0) is True
    thread.join(2.0)


# ----------------------------------------------------------- pointer stroke


def test_stop_mid_stroke_releases_in_place_and_sends_no_further_moves():
    token = CancellationToken()
    rig = _Rig(on_move=lambda count: token.cancel() if count == 3 else None)
    progress = StrokeProgress()

    with pytest.raises(ActionCancelled):
        rig.stroke.stroke(
            _binding(),
            _PATH,
            authorization=_authorization(),
            cancel_token=token,
            progress=progress,
        )

    # Exactly one press and one release, in that order.
    assert rig.button_events() == [("button", "left", True), ("button", "left", False)]
    # down (150), two moves, then the guarded release at the last delivered
    # point. The remaining path points were never sent.
    assert rig.moves() == [(150, 150), (160, 150), (170, 150), (170, 150)]
    assert progress.pressed and progress.released and progress.clean_release
    assert not rig.buttons.is_held(MouseButton.LEFT)


def test_cancelled_token_before_stroke_sends_no_input_at_all():
    token = CancellationToken()
    token.cancel()
    rig = _Rig()
    progress = StrokeProgress()

    with pytest.raises(ActionCancelled):
        rig.stroke.stroke(
            _binding(),
            _PATH,
            authorization=_authorization(),
            cancel_token=token,
            progress=progress,
        )

    assert rig.events == []
    assert not progress.pressed


def test_native_failure_mid_stroke_still_releases_button():
    def failing_move_after(count):
        if count == 3:
            raise OSError("SendInput rejected the move")

    rig = _Rig(on_move=failing_move_after)
    progress = StrokeProgress()

    with pytest.raises(OSError):
        rig.stroke.stroke(_binding(), _PATH, authorization=_authorization(), progress=progress)

    assert rig.button_events()[-1] == ("button", "left", False)
    assert [e for e in rig.button_events() if e[2] is True] == [("button", "left", True)]
    assert not rig.buttons.is_held(MouseButton.LEFT)
    assert progress.pressed and progress.released


def test_keyboard_interrupt_mid_stroke_still_releases_button():
    def interrupt(count):
        if count == 2:
            raise KeyboardInterrupt

    rig = _Rig(on_move=interrupt)
    with pytest.raises(KeyboardInterrupt):
        rig.stroke.stroke(_binding(), _PATH, authorization=_authorization())

    assert rig.button_events() == [("button", "left", True), ("button", "left", False)]


def test_foreground_change_mid_stroke_is_terminal_and_release_is_unproven():
    # down_at uses 4 revalidations; fail early inside the stroke loop.
    rig = _Rig(binder=_Binder(fail_from=6))
    progress = StrokeProgress()

    with pytest.raises(WindowBindingStale):
        rig.stroke.stroke(_binding(), _PATH, authorization=_authorization(), progress=progress)

    assert rig.button_events() == [("button", "left", True), ("button", "left", False)]
    assert progress.pressed and progress.released
    # The window changed, so Qronos cannot prove where the release landed.
    assert progress.clean_release is False


def test_expired_authorization_mid_stroke_releases_and_stops(monkeypatch):
    authorization = _authorization()
    real_monotonic = time.monotonic
    moves = {"count": 0}

    def expire_after_two_moves(count):
        moves["count"] = count

    rig = _Rig(on_move=expire_after_two_moves)
    monkeypatch.setattr(
        input_safety.time,
        "monotonic",
        lambda: real_monotonic() + (10_000.0 if moves["count"] >= 2 else 0.0),
    )
    progress = StrokeProgress()

    with pytest.raises(InputPermissionRequired):
        rig.stroke.stroke(_binding(), _PATH, authorization=authorization, progress=progress)

    assert rig.button_events() == [("button", "left", True), ("button", "left", False)]
    # Permission expiry is terminal: no move was sent after expiry.
    assert len(rig.moves()) == 2
    # Window unchanged: the fail-safe release provably landed in place.
    assert progress.clean_release is True


def test_post_press_revalidation_failure_releases_atomically_without_duplicate_up():
    # down_at: smooth/plain hover (2 revalidations) + pre-press guard (1),
    # then the post-press guard fails.
    rig = _Rig(binder=_Binder(fail_from=4))
    progress = StrokeProgress()

    with pytest.raises(WindowBindingStale):
        rig.stroke.stroke(_binding(), _PATH, authorization=_authorization(), progress=progress)

    assert rig.button_events() == [("button", "left", True), ("button", "left", False)]
    assert not rig.buttons.is_held(MouseButton.LEFT)
    # A press reached Windows, so a dot may exist; where is unproven.
    assert progress.pressed and progress.released and not progress.clean_release


def test_down_at_is_atomic_when_window_changes_right_after_press():
    # Any caller (not only strokes) must never be left holding a button it
    # does not know about: down_at either returns pressed or raises released.
    rig = _Rig(binder=_Binder(fail_from=4))

    with pytest.raises(WindowBindingStale):
        rig.buttons.down_at(_binding(), 200, 200, authorization=_authorization())

    assert rig.button_events() == [("button", "left", True), ("button", "left", False)]
    assert not rig.buttons.is_held(MouseButton.LEFT)
    assert rig.buttons.presses_delivered == 1


def test_click_never_sends_a_second_up_after_a_delivered_release():
    binder = _Binder(fail_from=8)  # fails on up_at's post-release check
    rig = _Rig(binder=binder)
    click = MouseClickController(binder, hover=rig.hover, buttons=rig.buttons)

    with pytest.raises(WindowBindingStale):
        click.click_at(_binding(), 200, 200, authorization=_authorization())

    assert rig.button_events() == [("button", "left", True), ("button", "left", False)]


# ------------------------------------------------------------- Paint adapter


class _AdapterBinder(_Binder):
    pass


def _paint_canvas(binding: WindowBinding) -> PaintCanvasRegion:
    from tests.fixtures.still_window import StillWindowCapture, canvas_from

    rect = WindowRect(150, 150, 450, 350)
    return canvas_from(binding, rect, StillWindowCapture().capture_bound(binding, None))


def _still_window():
    from tests.fixtures.still_window import StillWindowCapture

    return StillWindowCapture()


def test_stop_between_strokes_reports_exact_committed_edits():
    binding = _binding()
    token = CancellationToken()
    calls = []

    class _Stroke:
        def stroke(self, binding, points, *, cancel_token, progress, **kwargs):
            calls.append(points)
            progress.pressed = progress.released = progress.clean_release = True
            if len(calls) == 2:
                token.cancel()
            return SimpleNamespace(points=points)

    adapter = PaintAdapter(_AdapterBinder(), inputs=SimpleNamespace(stroke=_Stroke()), capture=_still_window())
    plan = ShapePlanner.custom(
        (
            ((0.1, 0.1), (0.9, 0.1)),
            ((0.1, 0.5), (0.9, 0.5)),
            ((0.1, 0.9), (0.9, 0.9)),
        ),
        name="three-lines",
    )
    progress = PaintCanvasProgress()

    with pytest.raises(ActionCancelled):
        adapter.execute(
            binding,
            plan,
            _paint_canvas(binding),
            authorization=_authorization(binding),
            cancel_token=token,
            progress=progress,
            tool_regions=(WindowRect(105, 105, 135, 125),),
        )

    assert len(calls) == 2
    assert progress.planned_strokes == 3
    assert progress.completed_strokes == 2
    assert progress.committed_marks == 2
    assert progress.edit_count_certain


def test_interrupted_stroke_with_unproven_release_makes_edit_count_uncertain():
    binding = _binding()

    class _Stroke:
        def stroke(self, binding, points, *, progress, **kwargs):
            progress.pressed = progress.released = True
            progress.clean_release = False
            raise WindowBindingStale("focus stolen mid-stroke")

    adapter = PaintAdapter(_AdapterBinder(), inputs=SimpleNamespace(stroke=_Stroke()), capture=_still_window())
    progress = PaintCanvasProgress()

    with pytest.raises(WindowBindingStale):
        adapter.execute(
            binding,
            ShapePlanner.line(),
            _paint_canvas(binding),
            authorization=_authorization(binding),
            progress=progress,
            tool_regions=(WindowRect(105, 105, 135, 125),),
        )

    assert progress.unproven_marks == 1
    assert progress.touched_canvas
    assert not progress.edit_count_certain


# ---------------------------------------------------------- computer executor


def _allow(request, audit=None):
    verdict = Verdict(
        request=request,
        decision=PermissionDecision.ALLOW,
        outcome=ActionOutcome.SUCCEEDED,
        reason="test allow",
        policy=None,
    )
    if audit is not None:
        audit(verdict)
    return verdict


class _LaunchBinder:
    def __init__(self, on_bind=None) -> None:
        self.on_bind = on_bind

    def bind(self, target, *, require_foreground=False, expected_pid=None):
        if self.on_bind is not None:
            self.on_bind()
        raise WindowBindingStale("window not settled yet")

    def revalidate(self, binding, **kwargs):  # pragma: no cover - never bound
        raise AssertionError


def test_cancelled_before_run_launches_nothing_and_journals_nothing():
    launches = []
    journal = UndoJournal(path=None)
    executor = ComputerExecutor(
        audit=ActionAuditLog(path=None),
        journal=journal,
        evaluate_fn=_allow,
        packaged_launcher=lambda aumid: launches.append(aumid) or 4321,
        window_binder=_LaunchBinder(),
        sleep_fn=lambda _s: None,
    )
    token = CancellationToken()
    token.cancel()

    execution = executor.execute(
        ComputerAction(ComputerOperation.OPEN_APP, app_id="paint"),
        cancel_token=token,
    )

    assert execution.result.outcome is ActionOutcome.CANCELLED
    assert "before any Windows side effect" in execution.result.detail
    assert launches == []
    assert journal.entries() == ()


def test_stop_during_launch_wait_is_cancelled_and_journaled_honestly():
    token = CancellationToken()
    journal = UndoJournal(path=None)
    audit = ActionAuditLog(path=None)
    binds = []
    executor = ComputerExecutor(
        audit=audit,
        journal=journal,
        evaluate_fn=_allow,
        packaged_launcher=lambda aumid: 4321,
        window_binder=_LaunchBinder(
            on_bind=lambda: binds.append(1) or (token.cancel() if len(binds) == 2 else None)
        ),
        sleep_fn=lambda _s: None,
        launch_binding_attempts=25,
    )

    execution = executor.execute(
        ComputerAction(ComputerOperation.OPEN_APP, app_id="paint"),
        cancel_token=token,
    )

    assert execution.result.outcome is ActionOutcome.CANCELLED
    assert "already received" in execution.result.detail
    # Polling stopped at the first checkpoint after Stop.
    assert len(binds) == 2
    entry = execution.undo_entry
    assert entry is not None
    assert entry.reversal is Reversal.IRREVERSIBLE
    assert entry.restoration["reason"] == "closing_app_could_discard_user_state"
    assert not entry.undoable
    completed = [r for r in audit.records() if r.event is AuditEvent.COMPLETED]
    assert completed[-1].outcome == ActionOutcome.CANCELLED.value


def test_successful_window_action_is_never_journaled_as_nothing_to_undo():
    journal = UndoJournal(path=None)
    executor = ComputerExecutor(
        journal=journal,
        evaluate_fn=_allow,
        window_action=lambda spec, operation: f"{operation.value} done",
    )

    execution = executor.execute(
        ComputerAction(ComputerOperation.MINIMIZE_WINDOW, app_id="notepad")
    )

    assert execution.result.outcome is ActionOutcome.SUCCEEDED
    assert execution.undo_entry.reversal is Reversal.IRREVERSIBLE
    assert execution.undo_entry.restoration == {
        "automaticUndo": "unavailable",
        "reason": "previous_window_state_not_captured",
    }
    # Nothing is offered for undo, so "undo that" can never replay it.
    assert journal.undoable() == ()


# ------------------------------------------------------- runtime integration


class _BlockingPaintExecutor:
    """Stands in for PaintDrawExecutor; blocks like a long stroke loop."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.tokens: list[CancellationToken] = []

    def prepare(self, action):
        allowed = SimpleNamespace(
            refused=False,
            allowed=True,
            needs_approval=False,
            decision=PermissionDecision.ALLOW,
        )
        return SimpleNamespace(action=action, gui_verdict=allowed, screen_verdict=allowed)

    def execute_prepared(self, prepared, *, cancel_token=None, **kwargs):
        assert cancel_token is not None, "production dispatch must pass a token"
        self.tokens.append(cancel_token)
        self.started.set()
        request = PaintDrawAction("a cat").gui_request()
        try:
            cancel_token.sleep(5.0)
        except ActionCancelled as exc:
            return PaintDrawExecution(
                request,
                ActionResult(request.action_id, ActionOutcome.CANCELLED, exc.reason),
                side_effects=PaintDrawSideEffects(
                    paint_activation_requested=True,
                    strokes_planned=5,
                    strokes_completed=2,
                    canvas_marks_committed=3,
                ),
            )
        return PaintDrawExecution(
            request,
            ActionResult(request.action_id, ActionOutcome.FAILED, "not stopped"),
        )


def _runtime_with(paint_executor) -> QronosRuntime:
    runtime = QronosRuntime(notify=lambda *args: None)
    runtime.action_audit = ActionAuditLog(path=None)
    runtime.undo_journal = UndoJournal(path=None)
    runtime.computer_executor = ComputerExecutor(dynamic_apps=False, evaluate_fn=_allow)
    runtime.paint_draw_executor = paint_executor
    return runtime


def test_user_stop_cancels_in_flight_paint_drawing_through_runtime():
    paint = _BlockingPaintExecutor()
    runtime = _runtime_with(paint)
    report: dict = {}
    outcome: dict = {}

    def voice_turn():
        outcome["response"] = runtime._prepare_voice_paint_draw_action(
            "draw a simple cat in Paint",
            report,
        )

    worker = threading.Thread(target=voice_turn, daemon=True)
    worker.start()
    assert paint.started.wait(2.0)

    result = runtime.request_stop_response()
    worker.join(3.0)

    assert not worker.is_alive()
    assert result["computerActionsCancelled"] == 1
    assert paint.tokens[0].reason == CANCEL_REASON_USER_STOP
    assert report["voiceComputerCancelled"] is True
    assert report["paintDrawCanvasEdits"] == 3
    assert report["paintDrawUndoAvailable"] is False
    assert "2 خط از 5 خط" in outcome["response"]
    assert "بازگردانی خودکار" in outcome["response"]


def test_stop_while_idle_does_not_cancel_the_next_drawing():
    paint = _BlockingPaintExecutor()
    runtime = _runtime_with(paint)
    assert runtime.request_stop_response()["computerActionsCancelled"] == 0

    worker = threading.Thread(
        target=runtime._prepare_voice_paint_draw_action,
        args=("draw a simple cat in Paint", {}),
        daemon=True,
    )
    worker.start()
    assert paint.started.wait(2.0)
    assert not paint.tokens[0].cancelled
    runtime.request_stop_response()
    worker.join(3.0)
    assert paint.tokens[0].cancelled


def test_existing_stop_action_reports_cancelled_computer_actions(capsys):
    paint = _BlockingPaintExecutor()
    runtime = _runtime_with(paint)
    worker = threading.Thread(
        target=runtime._prepare_voice_paint_draw_action,
        args=("draw a simple cat in Paint", {}),
        daemon=True,
    )
    worker.start()
    assert paint.started.wait(2.0)

    handle_action(runtime, {"actionId": STOP_RESPONSE_ACTION})
    worker.join(3.0)

    output = capsys.readouterr().out
    assert "voice_stop_acknowledged" in output
    assert "computerActionsCancelled" in output
    assert paint.tokens[0].cancelled


def test_runtime_close_cancels_with_shutdown_reason_and_waits():
    paint = _BlockingPaintExecutor()
    runtime = _runtime_with(paint)
    worker = threading.Thread(
        target=runtime._prepare_voice_paint_draw_action,
        args=("draw a simple cat in Paint", {}),
        daemon=True,
    )
    worker.start()
    assert paint.started.wait(2.0)

    runtime.close()

    assert paint.tokens[0].reason == CANCEL_REASON_SHUTDOWN
    # close() waited for the action to unwind (bounded).
    assert runtime._computer_actions.active() == ()
    worker.join(2.0)
