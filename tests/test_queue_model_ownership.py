"""
Queued work gives back what it loaded, and nothing that belongs to anyone else.

Two defects found in review, pinned here:

    After a queued run, every configured Qronos brain was unloaded - including
    one that was already warm for the user's own conversation before the
    queued run touched anything.

    A background Orchestrator step ended a ledger reservation it had only
    reused (the user's turn's, or the warm-residency one) and, with
    keep_alive "0", stopped a model another request might still be using.

No model or model server is involved: the brain runtime is a fake that
records what it was asked to load and unload.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.activity_guard import ActivityMode, ResourcePressure
from core.brain_runtime import BrainRuntimeModelStatus
from core.model_registry import get_model
from core.orchestrator import Orchestrator
from core.resource_ownership import (
    GLOBAL_RESOURCE_LEDGER,
    ResourceBudget,
    ResourceOwner,
    WorkloadPriority,
)
from core.resource_policy import ResourceDecision
from core.task_plan import TaskPlan
from core.task_router import TaskType
from tests.test_queued_conversation import FakeBrain, Harness


FAST = get_model("fast").name
HEAVY = get_model("heavy").name


# ----------------------------------------------------------------- executor


class ResidentBrain(FakeBrain):
    """A fake brain that knows which models are loaded."""

    def __init__(self, resident: set[str], loads: str | None = None, **kw):
        super().__init__(**kw)
        self.resident = set(resident)
        self.loads = loads
        self.unloaded: list[str] = []
        self.residency_known = True

    def execute_plan(self, plan, conversation_messages=None, priority=None):
        if self.loads:
            self.resident.add(self.loads)

        return super().execute_plan(plan, conversation_messages, priority)

    def resident_brain_names(self) -> frozenset[str]:
        if not self.residency_known:
            raise RuntimeError("model server did not answer")

        return frozenset(self.resident)

    def release_brains_loaded_since(self, before) -> bool:
        gone = self.resident - set(before)
        self.unloaded.extend(sorted(gone))
        self.resident -= gone
        return bool(gone)


def run_one(tmp_path, monkeypatch, brain: ResidentBrain) -> Harness:
    harness = Harness(tmp_path, monkeypatch, brain=brain)
    harness.submit("list or tuple?", "req-owner")
    harness.scheduler.tick()
    assert harness.phases("req-owner")[-1] == "answered"
    return harness


def test_a_brain_already_warm_for_the_user_is_left_loaded(
    tmp_path,
    monkeypatch,
) -> None:
    brain = ResidentBrain(resident={FAST})

    run_one(tmp_path, monkeypatch, brain)

    assert brain.unloaded == []
    assert brain.resident == {FAST}
    # And the old blanket release was not used instead.
    assert brain.released == 0


def test_only_the_brain_the_queued_run_loaded_is_given_back(
    tmp_path,
    monkeypatch,
) -> None:
    brain = ResidentBrain(resident={FAST}, loads=HEAVY)

    run_one(tmp_path, monkeypatch, brain)

    assert brain.unloaded == [HEAVY]
    assert brain.resident == {FAST}


def test_nothing_is_unloaded_when_residency_is_unknown(
    tmp_path,
    monkeypatch,
) -> None:
    brain = ResidentBrain(resident={FAST}, loads=HEAVY)
    brain.residency_known = False

    run_one(tmp_path, monkeypatch, brain)

    assert brain.unloaded == []
    assert brain.released == 0


# ------------------------------------------------------------- orchestrator


class FakeModelRuntime:
    def __init__(self, running: set[str]) -> None:
        self.running = set(running)
        self.stopped: list[str] = []
        self.chats: list[dict] = []

    def chat(self, model_name, **kwargs):
        self.chats.append({"model": model_name, **kwargs})
        return "an answer"

    def list_running_models(self):
        return [BrainRuntimeModelStatus(name=name) for name in self.running]

    def stop_model(self, model_name):
        self.stopped.append(model_name)
        self.running.discard(model_name)

    def unload_all(self):
        self.stopped.extend(sorted(self.running))
        self.running.clear()

    def health_check(self):
        return True


class AllowingModels:
    """Admits the Fast brain and never swaps the slot."""

    def __init__(self, runtime: FakeModelRuntime) -> None:
        self.runtime = runtime

    def prepare_model_slot(self, task_class) -> bool:
        return False

    def select_model(self, **kwargs):
        return SimpleNamespace(
            decision=ResourceDecision.ALLOW,
            model=get_model("fast"),
            already_resident=FAST in self.runtime.running,
        )

    def is_model_resident(self, name: str) -> bool:
        return name in self.runtime.running

    def unload_qronos_models(self, except_name=None) -> bool:
        self.runtime.stopped.append("ALL-QRONOS")
        return True


def an_orchestrator(
    runtime: FakeModelRuntime,
    mode: ActivityMode,
) -> Orchestrator:
    state = SimpleNamespace(mode=mode, resource_pressure=ResourcePressure.NORMAL)

    return Orchestrator(
        runtime=runtime,  # type: ignore[arg-type]
        model_manager=AllowingModels(runtime),  # type: ignore[arg-type]
        activity_guard=SimpleNamespace(detect=lambda: state),  # type: ignore
        telemetry=SimpleNamespace(  # type: ignore[arg-type]
            current=lambda: SimpleNamespace(system=None, gpu=None),
            invalidate=lambda: None,
        ),
    )


def a_fast_plan() -> TaskPlan:
    plan = TaskPlan(goal="list or tuple?")
    plan.add_step(task_type=TaskType.FAST, description="list or tuple?")
    return plan


@pytest.fixture
def users_reservation():
    """The user's own turn holds the Fast brain right now."""
    reservation = GLOBAL_RESOURCE_LEDGER.reserve(
        owner=ResourceOwner.QRONOS,
        workload=f"brain:{FAST}",
        priority=WorkloadPriority.ACTIVE_QRONOS_REQUEST,
        budget=ResourceBudget(vram_mb=1024),
        allow_duplicate_workload=False,
    )
    yield reservation
    GLOBAL_RESOURCE_LEDGER.release(reservation.reservation_id)


def is_active(reservation) -> bool:
    return any(
        item.reservation_id == reservation.reservation_id
        for item in GLOBAL_RESOURCE_LEDGER.active(ResourceOwner.QRONOS)
    )


def test_a_background_step_never_stops_or_disowns_a_shared_brain(
    users_reservation,
) -> None:
    runtime = FakeModelRuntime(running={FAST})
    # Gaming mode makes keep_alive "0", the path that used to stop the model.
    orchestrator = an_orchestrator(runtime, ActivityMode.GAMING_ASSIST)

    (result,) = orchestrator.execute_plan(
        a_fast_plan(),
        priority=WorkloadPriority.BACKGROUND,
    )

    assert result.success
    assert runtime.chats[0]["keep_alive"] == "0"
    assert runtime.stopped == []
    assert is_active(users_reservation)


def test_a_foreground_step_keeps_its_existing_cleanup(
    users_reservation,
) -> None:
    runtime = FakeModelRuntime(running={FAST})
    orchestrator = an_orchestrator(runtime, ActivityMode.GAMING_ASSIST)

    orchestrator.execute_plan(a_fast_plan())

    # Unchanged for the user's own turns: keep_alive "0" still unloads.
    assert runtime.stopped == [FAST]


def test_a_background_step_ends_the_reservation_it_created() -> None:
    runtime = FakeModelRuntime(running=set())
    orchestrator = an_orchestrator(runtime, ActivityMode.NORMAL)

    orchestrator.execute_plan(
        a_fast_plan(),
        priority=WorkloadPriority.BACKGROUND,
    )

    assert not any(
        item.workload == f"brain:{FAST}"
        for item in GLOBAL_RESOURCE_LEDGER.active(ResourceOwner.QRONOS)
    )


def test_release_since_unloads_only_what_was_added() -> None:
    runtime = FakeModelRuntime(running={FAST, "someone-elses-model"})
    orchestrator = an_orchestrator(runtime, ActivityMode.NORMAL)
    before = orchestrator.resident_brain_names()

    runtime.running.add(HEAVY)
    orchestrator.release_brains_loaded_since(before)

    assert before == frozenset({FAST})
    assert runtime.stopped == [HEAVY]
    assert "someone-elses-model" in runtime.running
