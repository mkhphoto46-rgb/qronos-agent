"""Foreground Stop/Pause/close through real bounded loopback HTTP."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import runtime_bridge
from core.foreground_lifecycle import ForegroundBusy, ForegroundLifecycle
from core.inference_cancellation import LocalInferenceCancelled
from core.ollama_controller import OllamaController
from core.orchestrator import StepResult
from core.task_router import TaskType
from test_local_inference_cancellation import slow_daemon, start_worker
from test_runtime_bridge import FakeOrchestrator, FakeSpeechRuntime, prepared_runtime
from test_text_chat_bridge import FakeRouter


class HttpBrain(FakeOrchestrator):
    def __init__(self, url):
        super().__init__()
        self.controller = OllamaController(url)
        self.workers = SimpleNamespace(close=Mock())
        self.calls = 0

    def execute_plan(self, plan, conversation_messages=None):
        self.calls += 1
        try:
            answer = self.controller.chat("local:4b", prompt=plan.goal, timeout_seconds=10)
            return [StepResult(order=1, success=True, output=answer)]
        except Exception as exc:
            # Match the actual Orchestrator's failure-result seam: cancellation
            # may be swallowed by workers/providers before returning here.
            return [StepResult(order=1, success=False, output="", error=str(exc))]


def fake_runtime(tmp_path, monkeypatch, brain):
    events, reports = [], []
    runtime = prepared_runtime(speech=FakeSpeechRuntime(transcript="tell me a story", temp_dir=tmp_path),
                               orchestrator=brain, temp_dir=tmp_path)
    runtime.task_router = FakeRouter(TaskType.FAST)
    runtime._notify = lambda *args: events.append(args)
    runtime._write_latency_report = lambda report: reports.append(dict(report))
    monkeypatch.setattr(runtime_bridge, "foreground_window", lambda: None)
    return runtime, events, reports


def run_kind(runtime, kind, request_id="fixture-turn"):
    if kind == "text":
        return runtime.run_text_turn("tell me a story", request_id)
    if kind == "voice":
        return runtime._run_voice_turn("push_to_talk")
    return runtime.look_at_screen(approved=True, question="read this fixture", direct=True)


def terminal_cancelled(events, kind):
    matches = [item for item in events if item[0] == ("text_error" if kind == "text" else "voice_turn_stopped")]
    assert len(matches) == 1, events
    payload = json.loads(matches[0][2])
    assert payload["status"] == "cancelled"
    if kind == "text":
        assert matches[0][1] == "cancelled"
        assert payload["requestId"] == "fixture-turn"
    return payload


@pytest.mark.parametrize("kind", ["text", "voice", "screen"])
@pytest.mark.parametrize("operation,reason", [("stop", "user_stop"), ("pause", "runtime_paused"),
                                               ("close", "runtime_shutdown")])
@pytest.mark.parametrize("path,phase", [("/api/show", "headers"), ("/api/show", "body"),
                                        ("/api/chat", "headers"), ("/api/chat", "body")])
def test_foreground_lifecycle_closes_http_and_refuses_late_outputs(
        slow_daemon, tmp_path, monkeypatch, kind, operation, reason, path, phase):
    state = slow_daemon
    state.update(path=path, phase=phase)
    brain = HttpBrain(state["url"])
    runtime, events, reports = fake_runtime(tmp_path, monkeypatch, brain)
    voice = runtime.voice_output
    thread, result = start_worker(lambda: run_kind(runtime, kind))
    assert state["blocked"].wait(2), events
    if operation == "stop":
        assert runtime.request_stop_response()["foregroundInferenceCancelled"]
    elif operation == "pause":
        runtime.set_runtime_paused(True)
    else:
        runtime.close()
    acknowledged = len(events)
    assert state["closed"].wait(0.75)
    thread.join(1)
    assert not thread.is_alive() and "exception" not in result
    assert terminal_cancelled(events, kind)["reason"] == reason
    forbidden = {"text_response", "voice_response", "voice_audio_ready", "voice_turn_complete", "runtime_error"}
    assert not any(event[0] in forbidden for event in events[acknowledged:])
    assert not any(event[0] in forbidden for event in events)
    assert not [message for message in runtime.conversation_history.all_messages() if message.role == "assistant"]
    assert voice.calls == [] and brain.calls == 1
    assert not runtime.is_busy
    assert [route for route, _ in state["received"]] == (
        ["/api/show"] if path == "/api/show" else ["/api/show", "/api/chat"])
    state["readiness"].assert_not_called()
    if kind == "voice":
        assert reports[-1]["status"] == "cancelled"


@pytest.mark.parametrize("kind", ["text", "voice", "screen"])
def test_non_cancelled_foreground_output_is_preserved(slow_daemon, tmp_path, monkeypatch, kind):
    slow_daemon["path"] = "/unused"
    runtime, events, _ = fake_runtime(tmp_path, monkeypatch, HttpBrain(slow_daemon["url"]))
    run_kind(runtime, kind)
    expected = "text_response" if kind == "text" else "voice_response"
    assert len([item for item in events if item[0] == expected]) == 1
    assert not any(item[0] in {"runtime_error", "text_error", "voice_turn_stopped"} for item in events)


def test_stop_transition_cannot_reach_new_turn_during_old_ack_cleanup(slow_daemon, tmp_path, monkeypatch):
    runtime, events, _ = fake_runtime(tmp_path, monkeypatch, HttpBrain(slow_daemon["url"]))
    old, result = start_worker(lambda: run_kind(runtime, "text"))
    assert slow_daemon["blocked"].wait(2)
    transition, proceed = threading.Event(), threading.Event()
    original = runtime._discard_pending_approvals

    def delayed(reason):
        transition.set()
        assert proceed.wait(2)
        return original(reason)

    monkeypatch.setattr(runtime, "_discard_pending_approvals", delayed)
    stopping, stop_result = start_worker(runtime.request_stop_response)
    assert transition.wait(2)
    slow_daemon["path"] = "/unused"
    later, later_result = start_worker(lambda: runtime.run_text_turn("tell me a story", "later-turn"))
    assert later.is_alive()  # Admission waits until the old Stop transition exits.
    proceed.set()
    stopping.join(2)
    old.join(2)
    later.join(2)
    assert "exception" not in result and "exception" not in stop_result and "exception" not in later_result
    # Depending on worker scheduling it sees the unwinding old worker as busy,
    # or starts fresh. A retry after old cleanup must always be fresh.
    if not any(item[0] == "text_response" for item in events):
        runtime.run_text_turn("tell me a story", "later-turn")
    responses = [json.loads(item[2]) for item in events if item[0] == "text_response"]
    assert len(responses) == 1 and responses[0]["requestId"] == "later-turn"


def test_stop_before_publication_discards_history_and_correlated_response(tmp_path, monkeypatch):
    arrived, proceed = threading.Event(), threading.Event()
    brain = FakeOrchestrator()
    runtime, events, _ = fake_runtime(tmp_path, monkeypatch, brain)
    original = runtime._release_brains_for_voice_phase

    def late_voice_release():
        arrived.set()
        assert proceed.wait(2)
        original()

    monkeypatch.setattr(runtime, "_release_brains_for_voice_phase", late_voice_release)
    thread, result = start_worker(lambda: run_kind(runtime, "voice"))
    assert arrived.wait(2)
    runtime.request_stop_response()
    proceed.set()
    thread.join(2)
    assert "exception" not in result
    terminal_cancelled(events, "voice")
    assert not any(item[0] == "voice_response" for item in events)
    assert not [message for message in runtime.conversation_history.all_messages() if message.role == "assistant"]


def test_publication_finishes_before_stop_acknowledgement(tmp_path, monkeypatch):
    runtime, events, _ = fake_runtime(tmp_path, monkeypatch, FakeOrchestrator())
    history_added, proceed = threading.Event(), threading.Event()
    notify = runtime._notify

    def gated(event, status, message):
        notify(event, status, message)
        if event == "conversation_message_added" and json.loads(message)["role"] == "qronos":
            history_added.set()
            assert proceed.wait(2)

    runtime._notify = gated
    turn, result = start_worker(lambda: run_kind(runtime, "text"))
    assert history_added.wait(2)
    stop, stopped = start_worker(runtime.request_stop_response)
    assert stop.is_alive()
    proceed.set()
    turn.join(2)
    stop.join(2)
    assert "exception" not in result and "exception" not in stopped
    types = [item[0] for item in events]
    assert types.index("text_response") < types.index("voice_response_stopped")
    assert types.count("text_response") == 1 and "text_error" not in types


def test_reentrant_stop_after_history_never_emits_late_generated_text(tmp_path, monkeypatch):
    runtime, events, _ = fake_runtime(tmp_path, monkeypatch, FakeOrchestrator())
    notify = runtime._notify

    def stop_on_history(event, status, message):
        notify(event, status, message)
        if event == "conversation_message_added" and json.loads(message)["role"] == "qronos":
            runtime.request_stop_response()

    runtime._notify = stop_on_history
    run_kind(runtime, "text")
    types = [item[0] for item in events]
    assert "text_response" not in types
    terminal_cancelled(events, "text")
    # History publication preceded the acknowledged Stop and is never replayed.
    assert len([message for message in runtime.conversation_history.all_messages() if message.role == "assistant"]) == 1


def test_shutdown_refuses_all_new_worker_scopes(tmp_path, monkeypatch):
    brain = FakeOrchestrator()
    runtime, events, _ = fake_runtime(tmp_path, monkeypatch, brain)
    runtime.close()
    for kind in ("text", "voice", "screen"):
        run_kind(runtime, kind)
    runtime.push_to_talk()
    assert brain.plans == [] and runtime.command_recorder.calls == 0
    assert runtime.speech_runtime.warms == 0
    assert not any(item[0] in {"text_response", "voice_response", "voice_audio_ready"} for item in events)


def test_lifecycle_admission_and_cleanup_use_exact_identity():
    lifecycle = ForegroundLifecycle()
    with pytest.raises(LocalInferenceCancelled):
        with lifecycle.begin("text", state=lambda: (False, False)) as old:
            with pytest.raises(ForegroundBusy):
                with lifecycle.begin("voice", state=lambda: (False, False)):
                    pass
            lifecycle.cancel_current("user_stop")
            with pytest.raises(LocalInferenceCancelled):
                with lifecycle.publication():
                    pytest.fail("Cancelled result reached publication")
            old.token.raise_if_cancelled()
    with lifecycle.begin("voice", state=lambda: (False, False)) as new:
        assert new is not old and not new.token.cancelled


def test_stop_during_tts_discards_audio_without_recovery_or_new_followup(tmp_path, monkeypatch):
    runtime, events, reports = fake_runtime(tmp_path, monkeypatch, FakeOrchestrator())
    entered, proceed = threading.Event(), threading.Event()
    voice = runtime.voice_output
    original = voice.speak_to_file

    def generation(text):
        entered.set()
        assert proceed.wait(2)
        return original(text)

    monkeypatch.setattr(voice, "speak_to_file", generation)
    thread, result = start_worker(lambda: run_kind(runtime, "voice"))
    assert entered.wait(2)
    runtime.request_stop_response()
    acknowledged = len(events)
    proceed.set()
    thread.join(2)
    assert "exception" not in result and len(voice.calls) == 1
    assert not any(item[0] in {"voice_audio_ready", "voice_response", "runtime_error", "voice_turn_complete"}
                   for item in events[acknowledged:])
    terminal_cancelled(events, "voice")
    assert not runtime.conversation_session.is_active
    assert not runtime._last_voice_turn_had_valid_speech_evidence
    assert reports[-1]["status"] == "cancelled"


def test_provider_exception_after_stop_cannot_enter_voice_recovery(tmp_path, monkeypatch):
    runtime, events, reports = fake_runtime(tmp_path, monkeypatch, FakeOrchestrator())
    entered, proceed = threading.Event(), threading.Event()

    def failure(*args, **kwargs):
        entered.set()
        assert proceed.wait(2)
        raise RuntimeError("SYNTHETIC_PROVIDER_FAILURE")

    monkeypatch.setattr(runtime.orchestrator, "execute_plan", failure)
    thread, result = start_worker(lambda: run_kind(runtime, "voice"))
    assert entered.wait(2)
    runtime.request_stop_response()
    proceed.set()
    thread.join(2)
    assert "exception" not in result
    assert runtime.voice_output.calls == []
    assert not any(item[0] in {"runtime_error", "voice_response", "voice_audio_ready"} for item in events)
    terminal_cancelled(events, "voice")
    assert reports[-1]["status"] == "cancelled"
    assert "error" not in reports[-1]


def test_cancelled_gui_voice_turn_reports_actual_partial_work_without_audio(tmp_path, monkeypatch):
    runtime, events, reports = fake_runtime(tmp_path, monkeypatch, FakeOrchestrator())
    runtime.task_router = FakeRouter(TaskType.COMPUTER)
    partial = "One native step completed; the next step was cancelled."

    def actual_outcome(transcript, report):
        runtime.request_stop_response()
        report["voiceComputerCancelled"] = True
        report["calculatorStepsInvoked"] = 1
        return partial

    monkeypatch.setattr(runtime, "_execute_voice_computer_action", actual_outcome)
    run_kind(runtime, "voice")
    assert [item[2] for item in events if item[0] == "voice_response"] == [partial]
    assert runtime.voice_output.calls == []
    assert reports[-1]["calculatorStepsInvoked"] == 1
    terminal_cancelled(events, "voice")
    assert not any(item[0] in {"voice_audio_ready", "voice_turn_complete", "runtime_error"} for item in events)


def test_cancelled_native_text_outcome_is_reported_once_without_generated_fallback(tmp_path, monkeypatch):
    runtime, events, _ = fake_runtime(tmp_path, monkeypatch, FakeOrchestrator())
    partial = "One native step completed; the next step was cancelled."

    def action(text, request_id, started):
        runtime.request_stop_response()
        runtime._finish_text_file_action(request_id=request_id, response=partial, started_at=started)
        return True

    monkeypatch.setattr(runtime, "_begin_text_calculator_semantic_action", action)
    run_kind(runtime, "text")
    assert [json.loads(item[2])["text"] for item in events if item[0] == "text_response"] == [partial]
    assert not any(item[0] == "text_error" for item in events)
    assert runtime.orchestrator.plans == []
    assert len([message for message in runtime.conversation_history.all_messages() if message.role == "assistant"]) == 1


def test_native_failure_after_stop_has_one_cancelled_terminal_outcome(tmp_path, monkeypatch):
    runtime, events, _ = fake_runtime(tmp_path, monkeypatch, FakeOrchestrator())

    def action(text, request_id, started):
        runtime.request_stop_response()
        runtime._fail_text_file_action(request_id=request_id, error="synthetic native failure", started_at=started)
        return True

    monkeypatch.setattr(runtime, "_begin_text_calculator_semantic_action", action)
    run_kind(runtime, "text")
    payload = terminal_cancelled(events, "text")
    assert payload["reason"] == "user_stop" and "changes may remain" in payload["error"]
    assert not any(item[0] == "text_response" for item in events)


def test_stop_during_single_shot_calculation_cannot_publish_late_result(tmp_path, monkeypatch):
    runtime, events, _ = fake_runtime(tmp_path, monkeypatch, FakeOrchestrator())
    entered, proceed = threading.Event(), threading.Event()

    def calculate(text):
        entered.set()
        assert proceed.wait(2)
        return SimpleNamespace(spoken_text="four")

    monkeypatch.setattr(runtime_bridge, "solve_simple_arithmetic", calculate)
    thread, result = start_worker(lambda: run_kind(runtime, "text"))
    assert entered.wait(2)
    runtime.request_stop_response()
    proceed.set()
    thread.join(2)
    assert "exception" not in result and runtime.orchestrator.plans == []
    terminal_cancelled(events, "text")
    assert not any(item[0] == "text_response" for item in events)
