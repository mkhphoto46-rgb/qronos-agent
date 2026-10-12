"""
Paint drawing recoverability: honest journal entries and exact, guarded undo.

A fake Paint document (pixels + its own undo history) stands in for the real
application. Undo may only remove Qronos's own edits, only from the exact
window it drew in, only while nothing else changed, and never more steps than
it recorded. No real window, keyboard or application is touched.
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from core.action_audit import ActionAuditLog, AuditEvent
from core.action_cancellation import ActionCancelled, CancellationToken
from core.actions import ActionOutcome, ActionRequest
from core.computer_executor import ComputerAction, ComputerExecutor, ComputerOperation
from core.input_safety import BoundInputAuthorization
from core.paint_draw_integration import (
    MAX_PAINT_UNDO_EVIDENCE,
    PaintDrawAction,
    PaintDrawError,
    PaintDrawExecutor,
)
from core.undo_journal import EntryState, Reversal, UndoJournal
from core.window_binding import WindowBinding, WindowRect, WindowSnapshot
from security.gate import Verdict
from security.permissions import ActionCategory, PermissionDecision


FRAME = WindowRect(100, 100, 300, 250)
CANVAS = WindowRect(120, 120, 280, 230)


def _binding(*, hwnd=101, identity="identity", layout="layout") -> WindowBinding:
    return WindowBinding(
        app_id="paint",
        display_name="Paint",
        snapshot=WindowSnapshot(
            hwnd=hwnd,
            pid=202,
            thread_id=303,
            title="Untitled - Paint",
            process_path=r"C:\Program Files\WindowsApps\Microsoft.Paint\mspaint.exe",
            process_start_time_100ns=404,
            window_rect=FRAME,
            client_rect=FRAME,
            dpi=96,
            visible=True,
            minimized=False,
            foreground=True,
        ),
        identity_token=identity,
        layout_token=layout,
    )


class FakePaintDocument:
    """Pixels plus Paint's own edit history (each edit = one Ctrl+Z step)."""

    def __init__(self) -> None:
        self.pixels = np.full((FRAME.height, FRAME.width, 3), 255, dtype=np.uint8)
        self.history: list[tuple[str, np.ndarray]] = []
        self.ignore_undo = False

    def edit(self, owner: str, row: int) -> None:
        self.history.append((owner, self.pixels.copy()))
        y = row - FRAME.top
        self.pixels[y : y + 2, 30:170] = 0

    def undo(self) -> None:
        if self.ignore_undo or not self.history:
            return
        _owner, previous = self.history.pop()
        self.pixels = previous

    def owners(self) -> list[str]:
        return [owner for owner, _ in self.history]


class FakeCapture:
    def __init__(self, document: FakePaintDocument) -> None:
        self.document = document
        self.calls = 0

    def capture_bound(self, binding, binder, *, approved, require_foreground, reason):
        assert approved is True
        self.calls += 1
        buffer = BytesIO()
        Image.fromarray(self.document.pixels, "RGB").save(buffer, format="PNG")
        return SimpleNamespace(
            image=SimpleNamespace(
                data=buffer.getvalue(),
                width=FRAME.width,
                height=FRAME.height,
                format="png",
            ),
            frame_rect=FRAME,
            window=binding.hwnd,
            binding_token=binding.identity_token,
            layout_token=binding.layout_token,
        )


class FakeKeyboard:
    def __init__(self, document: FakePaintDocument, on_press=None) -> None:
        self.document = document
        self.chords: list[tuple[str, ...]] = []
        self.on_press = on_press

    def press_chord(self, binding, keys, *, authorization):
        assert isinstance(authorization, BoundInputAuthorization)
        authorization.require_for(binding)
        self.chords.append(tuple(keys))
        assert tuple(keys) == ("ctrl", "z")
        self.document.undo()
        if self.on_press is not None:
            self.on_press(len(self.chords))


class FakeBinder:
    def revalidate(self, binding, *, require_foreground=True, require_same_layout=True):
        return binding.snapshot


class FakeComputer:
    def __init__(self, binding: WindowBinding) -> None:
        self.window_binder = FakeBinder()
        self.current = binding
        self.foreground_requests = 0

    def bind_app_window(self, app_id, *, require_foreground=False, expected_pid=None):
        assert app_id == "paint"
        assert expected_pid == 202
        return self.current

    def acquire_foreground(self, binding, *, cancel_token=None, **kwargs):
        self.foreground_requests += 1
        return binding.snapshot


def gate(decision: PermissionDecision):
    outcome = {
        PermissionDecision.ALLOW: ActionOutcome.SUCCEEDED,
        PermissionDecision.DENY: ActionOutcome.REFUSED,
    }.get(decision, ActionOutcome.AWAITING_APPROVAL)

    def evaluate(request, audit=None):
        verdict = Verdict(
            request=request,
            decision=decision,
            outcome=outcome,
            reason="test",
            policy=None,
        )
        if audit is not None:
            audit(verdict)
        return verdict

    return evaluate


class Rig:
    """One PaintDrawExecutor whose ``_perform`` simulates real Paint effects."""

    def __init__(self, decision=PermissionDecision.ALLOW) -> None:
        self.document = FakePaintDocument()
        self.capture = FakeCapture(self.document)
        self.keyboard = FakeKeyboard(self.document)
        self.computer = FakeComputer(_binding())
        self.journal = UndoJournal(path=None)
        self.audit = ActionAuditLog(path=None)
        self.decision = {"value": decision}
        rig = self

        class SimulatedPaintDrawExecutor(PaintDrawExecutor):
            def _perform(self, action, gui_grant, *, state=None, cancel_token=None):
                return rig.scenario(state, cancel_token)

        self.executor = SimulatedPaintDrawExecutor(
            self.computer,
            audit=self.audit,
            journal=self.journal,
            evaluate_fn=lambda request, audit=None: gate(self.decision["value"])(request, audit),
            screen_capture_factory=lambda: self.capture,
            inputs_factory=lambda binder: SimpleNamespace(keyboard=self.keyboard),
            sleep_fn=lambda _seconds: None,
        )
        self.scenario = self.draw_scenario(strokes=3)

    def draw_scenario(
        self,
        *,
        strokes: int,
        planned: int | None = None,
        cancel_after: int | None = None,
        unproven: int = 0,
        toolbar_only: bool = False,
        fail_verification: bool = False,
        record_history: bool = True,
    ):
        rows = iter(range(150, 230, 6))

        def scenario(state, cancel_token):
            state.computer_effects.append("app_activation_requested")
            state.normalizer = SimpleNamespace(toolbar_clicks_attempted=3)
            if toolbar_only:
                raise PaintDrawError("canvas could not be grounded")
            binding = self.computer.current
            state.binding = binding
            state.canvas = SimpleNamespace(rect=CANVAS)
            state.capture = self.capture
            state.before = self.capture.capture_bound(
                binding, None, approved=True, require_foreground=True, reason="before"
            )
            progress = state.canvas_progress
            progress.planned_strokes = planned if planned is not None else strokes
            for index in range(strokes):
                row = next(rows)
                if record_history:
                    self.document.edit("qronos", row)
                else:
                    self.document.pixels[row - FRAME.top, 30:170] = 0
                progress.committed_marks += 1
                progress.completed_strokes += 1
                if cancel_after is not None and index + 1 == cancel_after:
                    raise ActionCancelled("user_stop")
            progress.unproven_marks += unproven
            if unproven:
                raise RuntimeError("focus stolen mid-stroke")
            state.after = self.capture.capture_bound(
                binding, None, approved=True, require_foreground=True, reason="after"
            )
            if fail_verification:
                raise PaintDrawError("post-draw verification failed")
            return SimpleNamespace(
                verification=SimpleNamespace(summary=lambda: "verified"),
                stroke_count=strokes,
                point_count=strokes * 10,
                pid=binding.snapshot.pid,
                hwnd=binding.hwnd,
            )

        return scenario

    def draw(self, **kwargs):
        if kwargs:
            self.scenario = self.draw_scenario(**kwargs)
        prepared = self.executor.prepare(PaintDrawAction("a simple house"))
        return self.executor.execute_prepared(prepared, reevaluate=False)

    def undo(self, action_id, *, cancel_token=None, **approvals):
        prepared = self.executor.prepare_undo(action_id)
        return self.executor.execute_undo(prepared, cancel_token=cancel_token, **approvals)


@pytest.fixture()
def rig() -> Rig:
    rig = Rig()
    # The user's own earlier work in the same document.
    rig.document.edit("user", 130)
    return rig


# ------------------------------------------------------------ journal entries


def test_verified_drawing_records_exact_app_undo_steps(rig):
    execution = rig.draw()

    assert execution.result.outcome is ActionOutcome.SUCCEEDED
    entry = execution.undo_entry
    assert entry.reversal is Reversal.APP_UNDO_STEPS
    assert entry.restoration["steps"] == 3
    assert entry.restoration["toolStateRestored"] is False
    assert entry.target == "app:paint"
    assert execution.undo_available


def test_cancel_before_any_side_effect_journals_nothing():
    rig = Rig()

    def cancelled_early(state, cancel_token):
        raise ActionCancelled("user_stop")

    rig.scenario = cancelled_early
    execution = rig.draw()

    assert execution.result.outcome is ActionOutcome.CANCELLED
    assert "no Windows side effect" in execution.result.detail
    assert execution.undo_entry is None
    assert rig.journal.entries() == ()


def test_cancel_mid_drawing_is_honest_partial_and_still_exactly_undoable(rig):
    execution = rig.draw(strokes=2, planned=5, cancel_after=2)

    assert execution.result.outcome is ActionOutcome.CANCELLED
    assert "2 of 5 stroke(s) completed" in execution.result.detail
    assert execution.side_effects.canvas_marks_committed == 2
    # A passive post-interruption frame was taken so undo can be verified.
    assert execution.undo_entry.reversal is Reversal.APP_UNDO_STEPS
    assert execution.undo_entry.restoration["steps"] == 2
    completed = [r for r in rig.audit.records() if r.event is AuditEvent.COMPLETED]
    assert ActionOutcome.CANCELLED.value in {r.outcome for r in completed}


def test_unproven_release_is_recorded_irreversible_not_undoable(rig):
    execution = rig.draw(strokes=1, unproven=1)

    assert execution.result.outcome is ActionOutcome.FAILED
    assert execution.undo_entry.reversal is Reversal.IRREVERSIBLE
    assert execution.undo_entry.restoration["reason"] == "paint_edit_count_unproven"
    assert not execution.undo_available


def test_tool_state_only_change_is_recorded_irreversible(rig):
    execution = rig.draw(strokes=0, toolbar_only=True)

    assert execution.result.outcome is ActionOutcome.FAILED
    assert execution.undo_entry.reversal is Reversal.IRREVERSIBLE
    assert execution.undo_entry.restoration["reason"] == "paint_tool_state_not_restorable"


def test_failed_verification_with_strokes_is_still_exactly_undoable(rig):
    execution = rig.draw(strokes=2, fail_verification=True)

    assert execution.result.outcome is ActionOutcome.FAILED
    assert execution.undo_available


# ----------------------------------------------------------------------- undo


def test_undo_reverses_only_qronos_edits_and_verifies_canvas(rig):
    execution = rig.draw()
    before_owners = rig.document.owners()
    assert before_owners == ["user", "qronos", "qronos", "qronos"]

    undo = rig.undo(execution.request.action_id)

    assert undo.result.outcome is ActionOutcome.SUCCEEDED
    assert undo.restored and undo.steps_sent == 3
    assert rig.document.owners() == ["user"]
    assert rig.journal.entries()[-1].state is EntryState.UNDONE
    assert rig.audit.count(AuditEvent.UNDONE) == 1
    # A second undo of the same action is refused without input.
    again = rig.undo(execution.request.action_id)
    assert again.result.outcome is ActionOutcome.REFUSED
    assert len(rig.keyboard.chords) == 3


def test_undo_refuses_when_user_edited_after_qronos(rig):
    execution = rig.draw()
    rig.document.edit("user", 228)

    undo = rig.undo(execution.request.action_id)

    assert undo.result.outcome is ActionOutcome.REFUSED
    assert "changed since Qronos drew" in undo.result.detail
    assert rig.keyboard.chords == []
    assert rig.document.owners()[-1] == "user"
    assert rig.journal.next_undoable().action_id == execution.request.action_id


def test_undo_refuses_a_different_window_or_process_without_input(rig):
    execution = rig.draw()
    rig.computer.current = _binding(identity="reused-hwnd-other-process")

    undo = rig.undo(execution.request.action_id)

    assert undo.result.outcome is ActionOutcome.REFUSED
    assert "not the one Qronos drew in" in undo.result.detail
    assert rig.keyboard.chords == []
    assert rig.computer.foreground_requests == 0


def test_undo_refuses_after_layout_change_without_input(rig):
    execution = rig.draw()
    rig.computer.current = _binding(layout="resized")

    undo = rig.undo(execution.request.action_id)

    assert undo.result.outcome is ActionOutcome.REFUSED
    assert "layout changed" in undo.result.detail
    assert rig.keyboard.chords == []


def test_undo_rechecks_permission_at_undo_time(rig):
    execution = rig.draw()

    rig.decision["value"] = PermissionDecision.DENY
    denied = rig.undo(execution.request.action_id)
    assert denied.result.outcome is ActionOutcome.REFUSED

    rig.decision["value"] = PermissionDecision.REQUIRE_VOICE_CONFIRMATION
    waiting = rig.undo(execution.request.action_id)
    assert waiting.result.outcome is ActionOutcome.AWAITING_APPROVAL

    assert rig.keyboard.chords == []
    assert rig.computer.foreground_requests == 0

    rig.decision["value"] = PermissionDecision.REQUIRE_VOICE_CONFIRMATION
    approved = rig.undo(
        execution.request.action_id,
        gui_approval=PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
        screen_approval=PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
    )
    assert approved.result.outcome is ActionOutcome.SUCCEEDED


def test_undo_of_older_drawing_refused_while_newer_paint_action_stands(rig):
    first = rig.draw()
    rig.scenario = rig.draw_scenario(strokes=1)
    second = rig.draw()

    refused = rig.undo(first.request.action_id)
    assert refused.result.outcome is ActionOutcome.REFUSED
    assert "later Qronos action" in refused.result.detail
    assert rig.keyboard.chords == []

    assert rig.undo(second.request.action_id).restored


def test_undo_stops_early_and_never_reaches_older_user_work(rig):
    # Paint recorded only 2 history entries for 3 Qronos marks (e.g. one
    # stroke merged). Pressing the recorded 3 steps would undo the user's
    # own earlier edit; the per-step canvas check must stop at 2.
    rig.scenario = rig.draw_scenario(strokes=3)
    execution = rig.draw()
    rig.document.history.pop(-2)

    undo = rig.undo(execution.request.action_id)

    assert undo.steps_sent == 2
    assert undo.restored
    assert rig.document.owners() == ["user"]


def test_undo_never_sends_more_steps_than_recorded(rig):
    execution = rig.draw()
    rig.document.ignore_undo = True

    undo = rig.undo(execution.request.action_id)

    assert undo.result.outcome is ActionOutcome.FAILED
    assert undo.steps_sent == 3
    assert len(rig.keyboard.chords) == 3
    assert rig.journal.entries()[-1].state is EntryState.FAILED
    # Terminal: a failed partial undo is never replayed blind.
    assert rig.undo(execution.request.action_id).result.outcome is ActionOutcome.REFUSED
    assert len(rig.keyboard.chords) == 3


def test_stop_during_undo_is_terminal_partial_and_reported(rig):
    execution = rig.draw()
    token = CancellationToken()
    rig.keyboard.on_press = lambda count: token.cancel() if count == 1 else None

    undo = rig.undo(execution.request.action_id, cancel_token=token)

    assert undo.result.outcome is ActionOutcome.CANCELLED
    assert undo.steps_sent == 1
    assert "1 of 3" in undo.result.detail
    assert rig.journal.entries()[-1].state is EntryState.FAILED
    assert rig.document.owners() == ["user", "qronos", "qronos"]


def test_irreversible_computer_action_is_never_undone():
    journal = UndoJournal(path=None)
    computer = ComputerExecutor(
        journal=journal,
        evaluate_fn=gate(PermissionDecision.ALLOW),
        window_action=lambda spec, operation: "maximized",
    )
    opened = computer.execute(ComputerAction(ComputerOperation.MAXIMIZE_WINDOW, app_id="paint"))
    rig = Rig()
    rig.executor.journal = journal

    undo = rig.undo(opened.request.action_id)

    assert undo.result.outcome is ActionOutcome.REFUSED
    assert rig.keyboard.chords == []
    assert rig.computer.foreground_requests == 0


def test_evidence_eviction_expires_old_entries_instead_of_promising_undo(rig):
    executions = []
    for _ in range(MAX_PAINT_UNDO_EVIDENCE + 1):
        rig.scenario = rig.draw_scenario(strokes=1)
        executions.append(rig.draw())

    oldest = executions[0].request.action_id
    states = {entry.action_id: entry.state for entry in rig.journal.entries()}
    assert states[oldest] is EntryState.EXPIRED
    assert oldest not in {entry.action_id for entry in rig.journal.undoable()}


def test_undo_request_is_scoped_to_paint_and_names_the_original_action(rig):
    execution = rig.draw()
    prepared = rig.executor.prepare_undo(execution.request.action_id)

    assert prepared.gui_request.category is ActionCategory.GUI_AUTOMATION
    assert prepared.gui_request.target == "app:paint"
    assert prepared.gui_request.parameters["undoesActionId"] == execution.request.action_id
    assert prepared.screen_request.category is ActionCategory.READ_SCREEN
    with pytest.raises(TypeError):
        rig.executor.execute_undo(
            ActionRequest(  # type: ignore[arg-type]
                category=ActionCategory.GUI_AUTOMATION,
                target="app:paint",
                summary="forged",
            )
        )
