from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import core.runtime_bridge as runtime_bridge
from core.runtime_bridge import (
    QronosRuntime,
    handle_text_message,
)
from core.task_router import TaskType


class FakeRouter:
    def __init__(self, task_type: TaskType) -> None:
        self.task_type = task_type

    def route(self, text: str):
        return SimpleNamespace(
            task_type=self.task_type,
            reason=f"fake route for {text}",
        )


class FakeOrchestrator:
    def __init__(
        self,
        *,
        output: str = "پاسخ آزمایشی",
        success: bool = True,
        error: str | None = None,
    ) -> None:
        self.output = output
        self.success = success
        self.error = error
        self.calls = []

    def execute_plan(
        self,
        plan,
        conversation_messages=None,
    ):
        self.calls.append(plan)
        return [
            SimpleNamespace(
                success=self.success,
                output=self.output,
                error=self.error,
            )
        ]


def event_payload(events, event_type: str) -> dict:
    for current_type, _status, message in events:
        if current_type == event_type:
            return json.loads(message)

    raise AssertionError(
        f"Missing event: {event_type}. Events: {events!r}"
    )


def test_text_turn_uses_heavy_orchestrator_without_voice_output() -> None:
    events: list[tuple[str, str, str]] = []

    runtime = QronosRuntime(
        notify=lambda event_type, status, message: events.append(
            (event_type, status, message)
        )
    )
    runtime.task_router = FakeRouter(TaskType.HEAVY)
    runtime.orchestrator = FakeOrchestrator(
        output="طراحی → پیاده‌سازی → آزمایش"
    )

    runtime.run_text_turn(
        "یک ترتیب معتبر بده",
        "req-heavy-1",
    )

    response = event_payload(
        events,
        "text_response",
    )

    assert response["requestId"] == "req-heavy-1"
    assert response["text"] == "طراحی → پیاده‌سازی → آزمایش"
    assert response["route"] == "heavy"
    assert runtime.orchestrator.calls
    assert "voice_response" not in {
        item[0] for item in events
    }
    assert "voice_audio_ready" not in {
        item[0] for item in events
    }
    assert runtime.is_busy is False


def test_text_turn_fast_arithmetic_returns_correlated_response() -> None:
    events: list[tuple[str, str, str]] = []

    runtime = QronosRuntime(
        notify=lambda event_type, status, message: events.append(
            (event_type, status, message)
        )
    )
    runtime.task_router = FakeRouter(TaskType.FAST)
    runtime.orchestrator = FakeOrchestrator()

    runtime.run_text_turn(
        "دو به اضافه دو چند میشه؟",
        "req-fast-1",
    )

    response = event_payload(
        events,
        "text_response",
    )

    assert response["requestId"] == "req-fast-1"
    assert response["route"] == "fast"
    assert "چهار" in response["text"]
    assert runtime.orchestrator.calls == []


def test_text_turn_rejects_non_brain_action_routes() -> None:
    events: list[tuple[str, str, str]] = []

    runtime = QronosRuntime(
        notify=lambda event_type, status, message: events.append(
            (event_type, status, message)
        )
    )
    runtime.task_router = FakeRouter(TaskType.VISION)
    runtime.orchestrator = FakeOrchestrator()

    runtime.run_text_turn(
        "به صفحه نگاه کن",
        "req-vision-1",
    )

    error = event_payload(
        events,
        "text_error",
    )

    assert error["requestId"] == "req-vision-1"
    assert error["route"] == "vision"
    assert "Fast and Heavy" in error["error"]
    assert runtime.orchestrator.calls == []


def test_text_turn_busy_returns_correlated_error() -> None:
    events: list[tuple[str, str, str]] = []

    runtime = QronosRuntime(
        notify=lambda event_type, status, message: events.append(
            (event_type, status, message)
        )
    )
    runtime._set_busy(True)

    runtime.run_text_turn(
        "سلام",
        "req-busy-1",
    )

    error = event_payload(
        events,
        "text_error",
    )

    assert error["requestId"] == "req-busy-1"
    assert "already processing" in error["error"]
    assert runtime.is_busy is True


def test_handle_text_message_validates_required_fields() -> None:
    runtime = QronosRuntime()

    with pytest.raises(
        ValueError,
        match="must not be empty",
    ):
        handle_text_message(
            runtime,
            {
                "command": "text_message",
                "requestId": "req-1",
                "text": " ",
            },
        )

    with pytest.raises(
        ValueError,
        match="request id",
    ):
        handle_text_message(
            runtime,
            {
                "command": "text_message",
                "requestId": " ",
                "text": "سلام",
            },
        )


def test_handle_text_message_starts_daemon_worker(monkeypatch) -> None:
    calls: list[tuple[object, tuple, str, bool]] = []

    class FakeThread:
        def __init__(
            self,
            *,
            target,
            args,
            name,
            daemon,
        ) -> None:
            calls.append(
                (
                    target,
                    args,
                    name,
                    daemon,
                )
            )

        def start(self) -> None:
            return None

    monkeypatch.setattr(
        runtime_bridge.threading,
        "Thread",
        FakeThread,
    )

    runtime = QronosRuntime()

    handle_text_message(
        runtime,
        {
            "command": "text_message",
            "requestId": "req-thread-1",
            "text": "سلام",
        },
    )

    assert len(calls) == 1
    target, args, name, daemon = calls[0]

    assert target == runtime.run_text_turn
    assert args == (
        "سلام",
        "req-thread-1",
    )
    assert name == "qronos-text-turn"
    assert daemon is True
