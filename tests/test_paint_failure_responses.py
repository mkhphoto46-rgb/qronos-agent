"""The three Paint failure routes in the runtime bridge answer in honest Persian (R1-R3).

The Paint executor is a fake that fails exactly as the real one reports a
failure (a PaintDrawExecution with an English diagnostic detail and a typed
failure_kind). No Paint, window, screen, model or input is touched; audit and
undo journals are in memory.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from core.action_audit import ActionAuditLog
from core.actions import ActionOutcome, ActionRequest, ActionResult
from core.paint_draw_integration import PaintDrawExecution, PaintDrawSideEffects
from core.runtime_bridge import PendingPaintDrawAction, QronosRuntime
from core.undo_journal import UndoJournal
from security.gate import Verdict
from security.permissions import ActionCategory, PermissionDecision

ENGLISH_DETAIL = "PaintReferenceUnavailable: no reference provider is connected; no Windows side effect occurred."


def _allow(request):
    return Verdict(request=request, decision=PermissionDecision.ALLOW, outcome=ActionOutcome.SUCCEEDED,
                   reason="test", policy=None)


class FailingPaintExecutor:
    def __init__(self):
        self.executed = 0

    def prepare(self, action):
        gui = ActionRequest(category=ActionCategory.GUI_AUTOMATION, target="app:paint", summary="draw")
        screen = ActionRequest(category=ActionCategory.READ_SCREEN, target="app:paint", summary="observe")
        return SimpleNamespace(action=action, gui_request=gui, gui_verdict=_allow(gui),
                               screen_request=screen, screen_verdict=_allow(screen))

    def execute_prepared(self, prepared, **kwargs):
        self.executed += 1
        result = ActionResult(action_id=prepared.gui_request.action_id, outcome=ActionOutcome.FAILED,
                              detail=ENGLISH_DETAIL)
        return PaintDrawExecution(prepared.gui_request, result, side_effects=PaintDrawSideEffects(),
                                  failure_kind="reference_unavailable")

    def discard_pending(self, prepared, *, reason):
        pass

    def decline_screen(self, prepared):
        pass


def _runtime():
    events = []
    runtime = QronosRuntime(notify=lambda event, status, message: events.append((event, message)))
    runtime.action_audit = ActionAuditLog(path=None)
    runtime.undo_journal = UndoJournal(path=None)
    runtime.computer_executor = object()  # never used: the Paint executor is the fake
    runtime.paint_draw_executor = FailingPaintExecutor()
    return runtime, events


def _persian_reference_summary(text: str) -> bool:
    return "تصویر مرجع" in text and "هیچ تغییری ایجاد نشد" in text and ENGLISH_DETAIL not in text


def test_R1_text_route_shows_the_persian_summary_not_the_english_detail():
    runtime, events = _runtime()
    assert runtime._begin_text_paint_draw_action("draw a cat in paint", "req-1", time.perf_counter())
    errors = [json.loads(message) for event, message in events if event == "text_error"]
    assert errors and _persian_reference_summary(errors[-1]["error"])


def test_R2_voice_route_returns_the_persian_summary_and_keeps_the_english_detail_in_the_report():
    runtime, events = _runtime()
    report = {}
    response = runtime._prepare_voice_paint_draw_action("draw a cat in paint", report)
    assert _persian_reference_summary(response)
    assert report["voiceComputerFailed"] is True and report["voiceComputerFailure"] == ENGLISH_DETAIL


def test_R3_screen_approval_route_speaks_persian_and_keeps_the_english_diagnostic_event():
    runtime, events = _runtime()
    prepared = runtime.paint_draw_executor.prepare(SimpleNamespace(prompt="a cat"))
    runtime._resolve_paint_draw_decision(
        PendingPaintDrawAction(prepared=prepared, gui_approval=None, transcript="draw a cat in paint"),
        approved=True,
    )
    spoken = [message for event, message in events if event == "voice_response"]
    assert spoken and _persian_reference_summary(spoken[-1])
    assert ("paint_draw_failed", ENGLISH_DETAIL) in events
