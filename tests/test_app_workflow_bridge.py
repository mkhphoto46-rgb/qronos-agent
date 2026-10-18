"""
Runtime contract for application workflows through QronosRuntime.

The runtime, its approval ledger, its Stop/Pause handling and its action
dispatcher are real. Only the native boundaries (the visible browser launcher,
the page reader and the window list) are test doubles, so nothing on this
computer is opened or read.
"""

from __future__ import annotations

import json
import time

import pytest

from core.action_audit import ActionAuditLog
from core.app_workflow_runtime import APP_WORKFLOW_DECISION_ACTION, AppWorkflowCoordinator
from core.runtime_bridge import QronosRuntime, handle_action
from core.task_router import TaskRouter
from test_file_control_text_bridge import _FakeHistory
from tests.test_browser_research import PAGE, make

OPEN = "open https://example.org/docs"


class BridgeHarness:
    def __init__(self, tmp_path) -> None:
        self.events: list[tuple[str, str, str]] = []
        runtime = QronosRuntime(notify=lambda event, status, message: self.events.append((event, status, message)))
        runtime.task_router = TaskRouter()
        runtime.orchestrator = object()
        runtime.conversation_history = _FakeHistory()
        runtime.action_audit = ActionAuditLog(path=None)
        browser, self.launcher, self.reader, _ = make([PAGE])
        runtime._app_workflows = AppWorkflowCoordinator(
            ledger=runtime._approvals,
            notify=runtime._notify,
            actions=runtime._computer_actions,
            paused=lambda: runtime.runtime_paused,
            run_async=lambda work: work(),
            audit=runtime.action_audit,
            browser_workflow=lambda: browser,
        )
        self.runtime = runtime

    def payloads(self, event_type):
        return [json.loads(message) for event, _, message in self.events if event == event_type]

    def last(self, event_type):
        found = self.payloads(event_type)
        assert found, f"no {event_type}; events: {[event for event, _, _ in self.events]}"
        return found[-1]

    def wait_for(self, event_type, count):
        deadline = time.monotonic() + 5
        while len(self.payloads(event_type)) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.payloads(event_type)


@pytest.fixture
def h(tmp_path):
    return BridgeHarness(tmp_path)


def test_text_turn_prompts_and_answers_the_chat_without_opening_anything(h):
    h.runtime.run_text_turn(OPEN, "req-1")
    prompt = h.last("app_workflow_approval_required")
    assert prompt["requestId"] == "req-1" and prompt["phase"] == "browser_navigate"
    responses = h.payloads("text_response")
    assert responses and responses[-1]["requestId"] == "req-1"
    assert "تأیید" in responses[-1]["text"]
    assert h.launcher.opened == []


def test_desktop_decisions_run_through_the_action_dispatcher(h):
    h.runtime.run_text_turn(OPEN, "req-1")
    prompt = h.last("app_workflow_approval_required")
    handle_action(h.runtime, {"actionId": APP_WORKFLOW_DECISION_ACTION,
                              "data": {"actionToken": prompt["actionToken"], "approved": True}})
    finished = h.wait_for("app_workflow_finished", 1)[-1]
    assert finished["outcome"] == "succeeded"
    assert h.launcher.opened == ["https://example.org/docs"]


def test_malformed_decisions_are_rejected(h):
    with pytest.raises(ValueError, match="boolean"):
        handle_action(h.runtime, {"actionId": APP_WORKFLOW_DECISION_ACTION,
                                  "data": {"actionToken": "x", "approved": "yes"}})


def test_pause_revokes_the_open_prompt_and_resume_does_not_revive_it(h):
    h.runtime.run_text_turn(OPEN, "req-1")
    token = h.last("app_workflow_approval_required")["actionToken"]
    h.runtime.set_runtime_paused(True)
    assert h.last("app_workflow_stale")["actionToken"] == token
    h.runtime.set_runtime_paused(False)
    h.runtime.resolve_app_workflow_decision(token, approved=True)
    assert h.payloads("app_workflow_stale")[-1]["actionToken"] == token
    assert h.launcher.opened == []


def test_stop_revokes_the_open_prompt(h):
    h.runtime.run_text_turn(OPEN, "req-1")
    token = h.last("app_workflow_approval_required")["actionToken"]
    h.runtime.request_stop_response()
    h.runtime.resolve_app_workflow_decision(token, approved=True)
    assert any(item.get("actionToken") == token for item in h.payloads("app_workflow_stale"))
    assert h.launcher.opened == []


def test_ordinary_text_is_not_captured_by_the_workflow_router(h):
    assert h.runtime._begin_text_app_workflow("سلام، حالت چطوره؟", "req-x", time.perf_counter()) is False
    assert h.runtime._begin_text_app_workflow("open Telegram", "req-y", time.perf_counter()) is False


def test_unknown_or_forged_tokens_are_stale(h):
    h.runtime.resolve_app_workflow_decision("forged-token-that-was-never-issued", approved=True)
    assert h.last("app_workflow_stale")["reason"] in {"unknown_or_used", "unknown"}


@pytest.fixture
def temporary_data_root(tmp_path, monkeypatch):
    """Production wiring under test writes its data here, never the checkout."""
    from dataclasses import replace
    import core.runtime_bridge as runtime_bridge

    root = tmp_path / "qronos-data"
    monkeypatch.setattr(
        runtime_bridge, "CONFIG",
        replace(runtime_bridge.CONFIG, paths=replace(runtime_bridge.CONFIG.paths, data=root)),
    )
    return root


def test_production_wiring_routes_web_navigation_to_an_approval_prompt(temporary_data_root):
    events = []
    runtime = QronosRuntime(notify=lambda event, status, message: events.append((event, message)))
    runtime.task_router = TaskRouter()
    runtime.orchestrator = object()
    runtime.conversation_history = _FakeHistory()
    runtime.action_audit = ActionAuditLog(path=None)
    runtime.run_text_turn("open https://example.org/docs", "req-w")
    prompt = [json.loads(message) for event, message in events if event == "app_workflow_approval_required"][-1]
    assert prompt["phase"] == "browser_navigate" and prompt["details"]["url"] == "https://example.org/docs"
    assert runtime._begin_text_app_workflow("search my files for the report", "req-x", time.perf_counter()) is False
