"""
User-facing "undo my Paint drawing".

The command reaches the real guarded PaintDrawExecutor undo through the same
screen/UI approval flow, and Stop/Pause/stale rules, as drawing. Success is
only ever claimed when the canvas was verified restored; otherwise the user
hears that nothing (or only part) was reversed. A fake Paint document stands
in for the application; no real window, key or app is touched.
"""

from __future__ import annotations

import pytest

from core.actions import ActionOutcome, ActionRequest
from core.paint_draw_parser import (
    PAINT_UNDO,
    PAINT_UNDO_AMBIGUOUS,
    classify_paint_undo_request,
)
from core.task_router import TaskRouter, TaskType
from core.undo_journal import EntryState, Reversal
from security.gate import Verdict
from security.permissions import ActionCategory, PermissionDecision

from test_paint_draw_undo import Rig
from test_runtime_bridge import captured_events, event_types, prepared_runtime


UNDO = "undo my Paint drawing"
SUCCESS_MARK = "انجام شد"


def _gate(gui: PermissionDecision, screen: PermissionDecision):
    outcomes = {
        PermissionDecision.ALLOW: ActionOutcome.SUCCEEDED,
        PermissionDecision.DENY: ActionOutcome.REFUSED,
    }

    def evaluate(request, audit=None):
        decision = gui if request.category is ActionCategory.GUI_AUTOMATION else screen
        verdict = Verdict(
            request=request,
            decision=decision,
            outcome=outcomes.get(decision, ActionOutcome.AWAITING_APPROVAL),
            reason="test",
            policy=None,
        )
        if audit is not None:
            audit(verdict)
        return verdict

    return evaluate


VOICE_UI = (PermissionDecision.REQUIRE_VOICE_CONFIRMATION, PermissionDecision.REQUIRE_UI_CONFIRMATION)


@pytest.fixture()
def setup():
    rig = Rig()
    rig.document.edit("user", 130)  # the user's own earlier work
    drawing = rig.draw()  # drawn under ALLOW
    assert drawing.undo_available

    runtime = prepared_runtime()
    runtime.undo_journal = rig.journal
    runtime.action_audit = rig.audit
    runtime.computer_executor = rig.computer
    runtime.paint_draw_executor = rig.executor
    rig.executor._evaluate = _gate(*VOICE_UI)
    return runtime, rig, drawing


_LAST_PROMPT: dict = {}


def _voice(runtime, text=UNDO):
    report: dict = {}
    events = captured_events(
        lambda: report.setdefault(
            "response", runtime._prepare_voice_paint_undo_action(text, report)
        )
    )
    if report.get("screenApprovalId"):
        _LAST_PROMPT[id(runtime)] = report["screenApprovalId"]
    return report, events


def _answer(runtime, approved=True, approval_id=None):
    """Answer the most recent undo prompt with its own single-use identity."""
    if approval_id is None:
        approval_id = _LAST_PROMPT[id(runtime)]
    return captured_events(
        lambda: runtime.look_at_screen(approved=approved, approval_id=approval_id)
    )


def _responses(events):
    return [e["message"] for e in events if e["eventType"] == "voice_response"]


# --------------------------------------------------------------- recognition


@pytest.mark.parametrize(
    "text",
    [
        "undo the last drawing in Paint",
        "undo my Paint drawing",
        "آخرین نقاشی Paint رو برگردون",
        "نقاشی پینت رو برگردون",
    ],
)
def test_explicit_paint_undo_routes_to_computer_not_chat(text):
    assert classify_paint_undo_request(text) == PAINT_UNDO
    assert TaskRouter().route(text).task_type is TaskType.COMPUTER


@pytest.mark.parametrize("text", ["undo that", "برگردون", "draw a cat in Paint"])
def test_bare_undo_or_draw_is_not_a_paint_undo(text):
    assert classify_paint_undo_request(text) == ""


def test_undo_and_draw_in_one_sentence_does_neither(setup):
    runtime, rig, _ = setup
    report, _ = _voice(runtime, "undo the drawing and draw a cat in Paint")

    assert classify_paint_undo_request("undo the drawing and draw a cat in Paint") == PAINT_UNDO_AMBIGUOUS
    assert "هیچ کاری انجام نشد" in report["response"]
    assert runtime.pending_paint_undo is None and runtime.pending_paint_draw is None
    assert rig.keyboard.chords == []


# --------------------------------------------------------------- happy path


def test_voice_undo_waits_for_screen_approval_then_restores_exactly(setup):
    runtime, rig, drawing = setup

    report, _ = _voice(runtime)
    assert report["response"] == ""
    assert report["paintDrawScreenApprovalPending"] is True
    assert report["paintUndoTarget"] == drawing.request.action_id
    assert rig.keyboard.chords == []  # nothing before the person answers

    events = _answer(runtime, approved=True)

    assert "paint_undo_completed" in event_types(events)
    assert SUCCESS_MARK in _responses(events)[-1]
    assert rig.document.owners() == ["user"]
    assert rig.journal.entries()[-1].state is EntryState.UNDONE


def test_declining_the_undo_prompt_sends_nothing_and_keeps_undo_possible(setup):
    runtime, rig, drawing = setup
    _voice(runtime)

    events = _answer(runtime, approved=False)

    assert "voice_screen_declined" in event_types(events)
    assert rig.keyboard.chords == []
    assert rig.journal.next_undoable().action_id == drawing.request.action_id


# ----------------------------------------------------------- Stop and Pause


def test_stop_while_undo_awaits_approval_discards_it_and_late_yes_is_stale(setup):
    runtime, rig, drawing = setup
    _voice(runtime)

    result = runtime.request_stop_response()
    events = _answer(runtime, approved=True)

    assert result["pendingApprovalsDiscarded"] == 1
    assert "screen_approval_stale" in event_types(events)
    assert rig.keyboard.chords == []
    cancelled = [
        r for r in rig.audit.records()
        if r.event.value == "completed" and r.outcome == ActionOutcome.CANCELLED.value
    ]
    assert len(cancelled) == 2  # undo GUI + screen requests, nothing ran
    # The drawing itself is still reversible later.
    assert rig.journal.next_undoable().action_id == drawing.request.action_id


def test_pause_while_undo_awaits_approval_discards_it(setup):
    runtime, rig, _ = setup
    _voice(runtime)

    captured_events(lambda: runtime.set_runtime_paused(True))
    captured_events(lambda: runtime.set_runtime_paused(False))
    events = _answer(runtime, approved=True)

    assert runtime.pending_paint_undo is None
    assert "screen_approval_stale" in event_types(events)
    assert rig.keyboard.chords == []


def test_one_paint_prompt_at_a_time(setup):
    runtime, rig, _ = setup
    _voice(runtime)

    report, _ = _voice(runtime, "undo the last drawing in Paint")

    assert "منتظر تأیید" in report["response"]
    assert rig.keyboard.chords == []


# ----------------------------------------------------- never claim success


def test_undo_refused_when_canvas_changed_is_reported_as_not_done(setup):
    runtime, rig, _ = setup
    rig.document.edit("user", 228)  # the user drew after Qronos
    _voice(runtime)

    events = _answer(runtime, approved=True)
    response = _responses(events)[-1]

    assert SUCCESS_MARK not in response
    assert "هیچ کلیدی ارسال نشد" in response
    assert "paint_undo_not_completed" in event_types(events)
    assert rig.keyboard.chords == []


def test_partial_undo_is_reported_as_partial_never_success(setup):
    runtime, rig, _ = setup
    rig.document.ignore_undo = True
    _voice(runtime)

    response = _responses(_answer(runtime, approved=True))[-1]

    assert SUCCESS_MARK not in response
    assert "کامل نشد" in response
    assert rig.journal.entries()[-1].state is EntryState.FAILED


def test_no_recorded_drawing_means_no_prompt_and_no_keys():
    rig = Rig()
    runtime = prepared_runtime()
    runtime.undo_journal = rig.journal
    runtime.action_audit = rig.audit
    runtime.computer_executor = rig.computer
    runtime.paint_draw_executor = rig.executor

    report, _ = _voice(runtime)

    assert report["paintUndoUnavailable"] == "nothing"
    assert runtime.pending_paint_undo is None
    assert rig.keyboard.chords == []


def test_irreversible_paint_record_is_reported_unavailable():
    rig = Rig()
    rig.draw(strokes=1, unproven=1)  # edit count unproven -> irreversible
    runtime = prepared_runtime()
    runtime.undo_journal = rig.journal
    runtime.computer_executor = rig.computer
    runtime.paint_draw_executor = rig.executor

    report, _ = _voice(runtime)

    assert report["paintUndoUnavailable"] == "unavailable"
    assert "در دسترس نیست" in report["response"]
    assert rig.keyboard.chords == []


def test_newer_non_paint_reversible_action_blocks_paint_undo(setup):
    runtime, rig, _ = setup
    rig.journal.record(
        ActionRequest(
            category=ActionCategory.CREATE_OR_EDIT_FILE,
            target=r"C:\example\file.txt",
            summary="Rename one file.",
        ),
        Reversal.MOVE_BACK,
        {"from": "a", "to": "b"},
    )

    report, _ = _voice(runtime)

    assert report["paintUndoUnavailable"] == "not_paint"
    assert runtime.pending_paint_undo is None
    assert rig.keyboard.chords == []


# ----------------------------------------------------------------- text path


def test_text_undo_needing_voice_says_so_and_sends_nothing(setup):
    runtime, rig, _ = setup
    events = captured_events(
        lambda: runtime._begin_text_paint_undo_action(UNDO, "req-1", 0.0)
    )

    assert any("Voice" in e["message"] for e in events)
    assert rig.keyboard.chords == []


def test_text_undo_runs_only_when_policy_needs_no_approval(setup):
    runtime, rig, _ = setup
    rig.executor._evaluate = _gate(PermissionDecision.ALLOW, PermissionDecision.ALLOW)

    events = captured_events(
        lambda: runtime._begin_text_paint_undo_action(UNDO, "req-2", 0.0)
    )

    assert rig.document.owners() == ["user"]
    assert any(SUCCESS_MARK in e["message"] for e in events)


def test_stop_while_undo_turn_is_routing_never_opens_a_prompt(setup):
    runtime, rig, drawing = setup
    runtime._computer_actions.mark_turn_start()
    runtime.request_stop_response()

    report, _ = _voice(runtime)

    assert report["voiceComputerCancelled"] is True
    assert runtime.pending_paint_undo is None
    assert rig.keyboard.chords == []
    assert rig.journal.next_undoable().action_id == drawing.request.action_id


def test_direct_read_or_bare_answer_cannot_consume_a_pending_undo(setup):
    runtime, rig, _ = setup
    _voice(runtime)

    bare = captured_events(lambda: runtime.look_at_screen(approved=True, question=UNDO))
    direct = captured_events(
        lambda: runtime.look_at_screen(approved=True, question="what is on my screen?", direct=True)
    )

    assert "screen_approval_stale" in event_types(bare)
    assert "voice_turn_complete" in event_types(direct)
    assert runtime.pending_paint_undo is not None
    assert rig.keyboard.chords == []


def test_undo_identity_is_single_use(setup):
    runtime, rig, _ = setup
    report, _ = _voice(runtime)
    approval_id = report["screenApprovalId"]

    _answer(runtime, approval_id=approval_id)
    chords = len(rig.keyboard.chords)
    replay = _answer(runtime, approval_id=approval_id)

    assert "screen_approval_stale" in event_types(replay)
    assert len(rig.keyboard.chords) == chords
