"""
Orchestrator.answer_chat_only: the Device Link's tool-free remote answer path.

Review finding L4: remote questions went through answer_web_prompt, which runs
the Fast brain with web-research overrides (num_predict 768, keep_alive "0").
The user's configured Fast profile must apply instead, unchanged. These tests
use an injected model runtime and admission; no model, no Ollama, no GPU.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.activity_guard import ActivityMode, ResourcePressure
from core.brain_runtime import BrainMessage, BrainMessageRole, BrainRuntimeModelStatus
from core.model_registry import get_model
from core.orchestrator import QRONOS_SYSTEM_PROMPT, Orchestrator
from core.resource_ownership import GLOBAL_RESOURCE_LEDGER, ResourceOwner
from core.resource_policy import ResourceDecision


FAST = get_model("fast")
PROFILE = FAST.generation_for("default")


class ModelRuntime:
    def __init__(self, running: set[str] | None = None) -> None:
        self.running = set(running or ())
        self.chats: list[dict] = []
        self.stopped: list[str] = []

    def chat(self, model_name, **kwargs):
        self.chats.append({"model": model_name, **kwargs})
        return "پاسخ"

    def list_running_models(self):
        return [BrainRuntimeModelStatus(name=name) for name in self.running]

    def stop_model(self, model_name):
        self.stopped.append(model_name)
        self.running.discard(model_name)

    def unload_all(self):
        self.running.clear()

    def health_check(self):
        return True


class Models:
    def __init__(self, runtime: ModelRuntime, decision=ResourceDecision.ALLOW) -> None:
        self.runtime = runtime
        self.decision = decision
        self.selected: list[object] = []

    def prepare_model_slot(self, task_class) -> bool:
        return False

    def select_model(self, **kwargs):
        self.selected.append(kwargs["task_class"])

        return SimpleNamespace(
            decision=self.decision,
            model=FAST,
            already_resident=FAST.name in self.runtime.running,
        )

    def is_model_resident(self, name: str) -> bool:
        return name in self.runtime.running

    def unload_qronos_models(self, except_name=None) -> bool:
        return False


class Forbidden:
    """Workers and memory: touching them at all fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"the chat-only path reached {name}")


def orchestrator(runtime, models, mode=ActivityMode.NORMAL) -> Orchestrator:
    state = SimpleNamespace(mode=mode, resource_pressure=ResourcePressure.NORMAL)
    built = Orchestrator(
        runtime=runtime,  # type: ignore[arg-type]
        model_manager=models,  # type: ignore[arg-type]
        activity_guard=SimpleNamespace(detect=lambda: state),  # type: ignore[arg-type]
        telemetry=SimpleNamespace(  # type: ignore[arg-type]
            current=lambda: SimpleNamespace(system=None, gpu=None),
            invalidate=lambda: None,
        ),
        workers=Forbidden(),  # type: ignore[arg-type]
    )
    built.memory_runtime = Forbidden()  # type: ignore[assignment]

    return built


def question(text: str = "پایتخت فرانسه کجاست") -> list[BrainMessage]:
    return [
        BrainMessage(role=BrainMessageRole.SYSTEM, content="Answer in words only."),
        BrainMessage(role=BrainMessageRole.USER, content=text),
    ]


@pytest.fixture(autouse=True)
def clean_ledger():
    yield

    for reservation in GLOBAL_RESOURCE_LEDGER.active(ResourceOwner.QRONOS):
        GLOBAL_RESOURCE_LEDGER.release(reservation.reservation_id)


def test_the_configured_fast_profile_is_applied_exactly() -> None:
    runtime = ModelRuntime()
    answer = orchestrator(runtime, Models(runtime)).answer_chat_only(question())

    assert answer == "پاسخ"
    (call,) = runtime.chats
    assert call["model"] == FAST.name
    assert call["num_predict"] == PROFILE.num_predict == 256
    assert call["keep_alive"] == PROFILE.keep_alive == "60s"
    assert call["think"] == PROFILE.think
    assert call["num_ctx"] == FAST.context_tokens


def test_it_is_not_the_web_research_profile() -> None:
    runtime = ModelRuntime()
    orchestrator(runtime, Models(runtime)).answer_chat_only(question())

    (call,) = runtime.chats
    assert call["num_predict"] != 768
    assert call["keep_alive"] != "0"
    assert "prompt" not in call


def test_the_activity_keep_alive_rule_still_applies() -> None:
    # The same rule a Fast plan step follows: never hold VRAM while gaming.
    runtime = ModelRuntime()
    orchestrator(runtime, Models(runtime), ActivityMode.GAMING_ASSIST).answer_chat_only(question())

    assert runtime.chats[0]["keep_alive"] == "0"
    assert runtime.stopped == [FAST.name]


def test_the_qronos_identity_comes_first_and_caller_rules_follow() -> None:
    runtime = ModelRuntime()
    orchestrator(runtime, Models(runtime)).answer_chat_only(question())

    messages = runtime.chats[0]["messages"]
    assert messages[0] == BrainMessage(role=BrainMessageRole.SYSTEM, content=QRONOS_SYSTEM_PROMPT)
    assert messages[1].content == "Answer in words only."
    assert messages[-1] == BrainMessage(role=BrainMessageRole.USER, content="پایتخت فرانسه کجاست")
    assert len(messages) == 3


def test_only_the_fast_brain_is_admitted() -> None:
    from core.model_manager import TaskClass

    runtime = ModelRuntime()
    models = Models(runtime)
    orchestrator(runtime, models).answer_chat_only(question())

    # Checked twice, as a plan step is: before, and immediately before the call.
    assert models.selected == [TaskClass.FAST, TaskClass.FAST]


def test_a_refused_admission_raises_and_never_calls_the_model() -> None:
    runtime = ModelRuntime()

    with pytest.raises(RuntimeError):
        orchestrator(runtime, Models(runtime, ResourceDecision.BLOCK)).answer_chat_only(question())

    assert runtime.chats == []


def test_a_warm_model_keeps_its_reservation_and_a_cold_one_returns_it() -> None:
    warm = ModelRuntime(running={FAST.name})
    orchestrator(warm, Models(warm)).answer_chat_only(question())
    assert any(r.workload == f"brain:{FAST.name}" for r in GLOBAL_RESOURCE_LEDGER.active(ResourceOwner.QRONOS))

    for reservation in GLOBAL_RESOURCE_LEDGER.active(ResourceOwner.QRONOS):
        GLOBAL_RESOURCE_LEDGER.release(reservation.reservation_id)

    cold = ModelRuntime()
    orchestrator(cold, Models(cold)).answer_chat_only(question())
    assert not any(r.workload == f"brain:{FAST.name}" for r in GLOBAL_RESOURCE_LEDGER.active(ResourceOwner.QRONOS))
