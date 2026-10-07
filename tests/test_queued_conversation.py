"""
The Smart Queue doing real work: queued conversation requests, answered.

Every brain here is a fake that records what it was asked and answers from a
script, so these tests prove the queue's behaviour around a model — what it is
given, when it is allowed to run, what happens to its answer — without ever
loading one. Nothing in this file touches the network or a GPU.

The scheduler is driven by ``tick()`` with a hand-moved clock, as in
``tests/test_queue_scheduler.py``. The tests about cancelling or preempting
work *while it runs* use the bridge's real worker-thread runner, because the
race between "the user pressed cancel" and "the model returned" only exists
when the two genuinely happen on different threads.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

import core.runtime_bridge as runtime_bridge
from core.brain_runtime import BrainMessageRole
from core.load_signal import LoadLevel
from core.orchestrator import StepResult
from core.queue_scheduler import (
    HoldReason,
    QueueScheduler,
    SchedulerConfig,
    WorkOutcome,
    WorkResult,
)
from core.queued_conversation import (
    ConversationPhase,
    ConversationQueueExecutor,
    ConversationRequest,
    QueuedConversationWork,
)
from core.resource_governor import ResourceGovernor, Weight
from core.resource_guard import GpuStatus, SystemStatus
from core.runtime_bridge import (
    QronosRuntime,
    handle_queue_cancel,
    handle_queue_submit,
)
from core.safe_queue import SafeQueue, TaskState
from core.task_router import TaskType
from tests.fixtures.clock import FakeClock
from tests.test_queue_scheduler import Machine


WAIT_SECONDS = 5.0


# --------------------------------------------------------------- the fakes


class FakeRouter:
    def __init__(self, task_type: TaskType) -> None:
        self.task_type = task_type

    def route(self, text: str):
        return SimpleNamespace(
            task_type=self.task_type,
            reason=f"fake route for {text}",
        )


class FakeBrain:
    """
    An orchestrator that answers from a script.

    ``gate`` makes it block mid-call until the test lets it go, which is how
    a test holds a request "running" while it cancels or preempts it.
    """

    def __init__(
        self,
        output: str = "A tuple is immutable; a list is not.",
        *,
        blocked_times: int = 0,
        error: str | None = None,
        raises: Exception | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        self.output = output
        self.blocked_times = blocked_times
        self.error = error
        self.raises = raises
        self.gate = gate
        self.entered = threading.Event()
        self.calls: list[tuple[str, str, tuple]] = []
        self.priorities: list[object] = []
        self.released = 0

    def execute_plan(self, plan, conversation_messages=None, priority=None):
        self.priorities.append(priority)
        step = plan.steps[0]
        self.calls.append(
            (
                step.task_type.value,
                step.description,
                tuple(conversation_messages or ()),
            )
        )
        self.entered.set()

        if self.gate is not None:
            assert self.gate.wait(WAIT_SECONDS), "the test never let go"

        if self.raises is not None:
            raise self.raises

        if self.blocked_times > 0:
            self.blocked_times -= 1
            return [
                StepResult(
                    order=1,
                    success=False,
                    output="",
                    error="Qronos blocked this task because ...",
                    resource_blocked=True,
                )
            ]

        if self.error is not None:
            return [
                StepResult(
                    order=1,
                    success=False,
                    output="",
                    error=self.error,
                )
            ]

        return [StepResult(order=1, success=True, output=self.output)]

    def release_brains_for_voice_phase(self) -> bool:
        self.released += 1
        return True


class Harness:
    """A runtime, a fake brain and a hand-driven scheduler wired together."""

    def __init__(
        self,
        tmp_path,
        monkeypatch,
        *,
        brain: FakeBrain | None = None,
        threaded: bool = False,
        route: TaskType | None = TaskType.FAST,
    ) -> None:
        monkeypatch.setenv(
            "QRONOS_CONVERSATION_HISTORY_PATH",
            str(tmp_path / "history.sqlite3"),
        )
        monkeypatch.setenv(
            "QRONOS_SETTINGS_PATH",
            str(tmp_path / "settings.json"),
        )

        self.events: list[tuple[str, str, str]] = []
        self.runtime = QronosRuntime(
            notify=lambda kind, status, message: self.events.append(
                (kind, status, message)
            )
        )
        self.brain = brain or FakeBrain()
        self.runtime.orchestrator = self.brain

        if route is not None:
            self.runtime.task_router = FakeRouter(route)

        self.clock = FakeClock()
        self.machine = Machine(self.clock)
        self.scheduler = QueueScheduler(
            queue=SafeQueue(clock=self.clock),
            governor=ResourceGovernor(
                read_system=lambda: SystemStatus(
                    cpu_usage_percent=10.0,
                    ram_usage_percent=30.0,
                    ram_used_gb=5.0,
                    ram_total_gb=16.0,
                ),
                read_gpu=lambda: GpuStatus(
                    name="test card",
                    temperature_c=45,
                    gpu_utilization_percent=5,
                    vram_used_mb=1_000,
                    vram_total_mb=16_303,
                ),
                clock=self.clock,
            ),
            monitor=self.machine,  # type: ignore[arg-type]
            runner=(
                self.runtime._run_queue_work_in_thread if threaded else None
            ),
            notify=self.runtime._on_queue_event,
            clock=self.clock,
            config=SchedulerConfig(),
            foreground_busy=lambda: self.runtime.is_busy,
        )
        self.runtime._scheduler = self.scheduler
        # As ensure_scheduler does: open the durable record and put back
        # whatever an earlier run left in it.
        self.runtime._recover_queue(self.scheduler)

    # ------------------------------------------------------------ actions

    def submit(self, text: str, request_id: str, **fields) -> str:
        handle_queue_submit(
            self.runtime,
            {
                "command": "queue_submit",
                "text": text,
                "requestId": request_id,
                **fields,
            },
        )

        return self.task_for(request_id)["taskId"]

    def wait_settled(self) -> None:
        deadline = time.time() + WAIT_SECONDS

        while time.time() < deadline:
            if not self.scheduler.running_work():
                return

            time.sleep(0.01)

        raise AssertionError("queued work never reported back")

    # ------------------------------------------------------------ reading

    def task_for(self, request_id: str) -> dict:
        for task in self.scheduler.view().tasks:
            if task.get("requestId") == request_id:
                return task

        raise AssertionError(f"no task for {request_id}")

    def updates(self, request_id: str) -> list[dict]:
        found = []

        for kind, _status, message in self.events:
            if kind != "queue_conversation_update":
                continue

            payload = json.loads(message)

            if payload["requestId"] == request_id:
                found.append(payload)

        return found

    def phases(self, request_id: str) -> list[str]:
        return [update["phase"] for update in self.updates(request_id)]

    def messages(self, request_id: str) -> list[tuple[str, str]]:
        return [
            (message.role, message.text)
            for message in self.runtime.conversation_history.all_messages()
            if message.request_id == request_id
        ]

    def event_kinds(self) -> list[str]:
        return [kind for kind, _status, _message in self.events]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


# ----------------------------------------------------- real output, delivered


def test_a_queued_request_is_answered_by_the_brain_into_its_conversation(
    harness: Harness,
) -> None:
    history = harness.runtime.conversation_history
    history.append(role="user", text="earlier question", source="text")
    history.append(role="assistant", text="earlier answer", source="text")

    task_id = harness.submit("list or tuple?", "req-1")

    # The request is in the conversation at once, under its own id.
    assert harness.messages("req-1") == [("user", "list or tuple?")]
    assert harness.brain.calls == []

    harness.scheduler.tick()

    route, description, context = harness.brain.calls[0]
    assert (route, description) == ("fast", "list or tuple?")

    # The context is the conversation as it was when the request was made,
    # without the request duplicated into it.
    assert [message.content for message in context] == [
        "earlier question",
        "earlier answer",
    ]
    assert context[0].role is BrainMessageRole.USER

    assert harness.messages("req-1") == [
        ("user", "list or tuple?"),
        ("assistant", "A tuple is immutable; a list is not."),
    ]
    assert harness.phases("req-1") == ["queued", "running", "answered"]

    answered = harness.updates("req-1")[-1]
    assert answered["taskId"] == task_id
    assert answered["text"] == "A tuple is immutable; a list is not."
    assert answered["persisted"] is True

    assert harness.task_for("req-1")["state"] == "done"
    assert harness.scheduler.governor.active() == ()
    assert harness.brain.released == 1


def test_the_task_carries_its_request_identity_to_the_interface(
    harness: Harness,
) -> None:
    harness.submit("list or tuple?", "req-identity")

    task = harness.task_for("req-identity")

    assert task["kind"] == "conversation"
    assert task["route"] == "fast"
    assert task["summary"] == "list or tuple?"
    assert task["weight"] == "light"
    assert task["cancellable"] is True


def test_a_heavy_route_holds_the_heavy_slot(tmp_path, monkeypatch) -> None:
    harness = Harness(tmp_path, monkeypatch, route=TaskType.HEAVY)

    harness.submit("plan the migration step by step", "req-heavy")

    assert harness.task_for("req-heavy")["weight"] == "heavy"

    harness.scheduler.tick()

    assert harness.brain.calls[0][0] == "heavy"


def test_simple_arithmetic_needs_no_brain(harness: Harness) -> None:
    harness.submit("2 + 2", "req-sum")
    harness.scheduler.tick()

    assert harness.brain.calls == []
    assert harness.phases("req-sum")[-1] == "answered"
    # No model was loaded, so there is nothing to release.
    assert harness.brain.released == 0


# ------------------------------------------------- pressure: hold and resume


def test_a_busy_machine_holds_the_request_and_a_free_one_runs_it(
    harness: Harness,
) -> None:
    harness.machine.level = LoadLevel.BUSY
    harness.submit("list or tuple?", "req-busy")

    harness.scheduler.tick()
    harness.scheduler.tick()

    assert harness.brain.calls == []
    task = harness.task_for("req-busy")
    assert task["state"] == "queued"
    assert task["heldReason"] == HoldReason.SUSTAINED_LOAD.value

    harness.machine.level = LoadLevel.FREE
    harness.scheduler.tick()

    assert len(harness.brain.calls) == 1
    assert harness.phases("req-busy")[-1] == "answered"


def test_a_resource_refusal_at_run_time_defers_instead_of_failing(
    tmp_path,
    monkeypatch,
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        brain=FakeBrain(blocked_times=1),
    )
    harness.submit("list or tuple?", "req-deferred")

    harness.scheduler.tick()

    task = harness.task_for("req-deferred")
    assert task["state"] == "queued"
    assert task["heldReason"] == HoldReason.SUSTAINED_LOAD.value
    assert harness.phases("req-deferred") == ["queued", "running", "deferred"]
    assert ("assistant", "A tuple is immutable; a list is not.") not in (
        harness.messages("req-deferred")
    )
    # It touched the brain path, so it gave the brain slot back, and it gave
    # the machine back.
    assert harness.brain.released == 1
    assert harness.scheduler.governor.active() == ()

    # Not offered again straight away: the machine just said no.
    harness.scheduler.tick()
    assert len(harness.brain.calls) == 1

    harness.clock.advance(31)
    harness.scheduler.tick()

    assert len(harness.brain.calls) == 2
    assert harness.phases("req-deferred")[-1] == "answered"
    assert harness.task_for("req-deferred")["state"] == "done"


def test_a_text_turn_refused_for_resources_is_queued_and_later_answered(
    harness: Harness,
) -> None:
    harness.brain.blocked_times = 1

    harness.runtime.run_text_turn("list or tuple?", "req-text")

    kinds = harness.event_kinds()
    assert "text_deferred" in kinds
    assert "text_error" not in kinds

    deferred = json.loads(
        next(
            message
            for kind, _status, message in harness.events
            if kind == "text_deferred"
        )
    )
    assert deferred["requestId"] == "req-text"
    assert deferred["taskId"] == harness.task_for("req-text")["taskId"]

    harness.scheduler.tick()

    # The user's message was recorded once, by the text turn, and the answer
    # attached to the same request.
    assert harness.messages("req-text") == [
        ("user", "list or tuple?"),
        ("assistant", "A tuple is immutable; a list is not."),
    ]
    assert harness.runtime.is_busy is False


# ------------------------------------------ release on failure, honest errors


def test_a_brain_failure_is_delivered_as_an_error_and_releases_everything(
    tmp_path,
    monkeypatch,
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        brain=FakeBrain(error="Local brain request failed."),
    )
    harness.submit("list or tuple?", "req-fail")

    harness.scheduler.tick()

    failed = harness.updates("req-fail")[-1]
    assert failed["phase"] == "failed"
    assert failed["error"] == "Local brain request failed."
    assert harness.messages("req-fail") == [("user", "list or tuple?")]
    assert harness.task_for("req-fail")["state"] == "failed"
    assert harness.brain.released == 1
    assert harness.scheduler.governor.active() == ()


def test_a_brain_that_raises_is_a_failure_not_a_crash(
    tmp_path,
    monkeypatch,
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        brain=FakeBrain(raises=RuntimeError("model runtime vanished")),
    )
    harness.submit("list or tuple?", "req-raise")

    harness.scheduler.tick()

    assert harness.updates("req-raise")[-1]["phase"] == "failed"
    assert "model runtime vanished" in harness.updates("req-raise")[-1]["error"]
    assert harness.task_for("req-raise")["state"] == "failed"
    assert harness.brain.released == 1
    assert harness.scheduler.governor.active() == ()


# ---------------------------------------------- cancellation and late output


def test_cancelling_a_waiting_request_runs_nothing(harness: Harness) -> None:
    harness.machine.level = LoadLevel.BUSY
    task_id = harness.submit("list or tuple?", "req-cancel-early")

    handle_queue_cancel(harness.runtime, {"taskId": task_id})
    harness.machine.level = LoadLevel.FREE
    harness.scheduler.tick()

    assert harness.brain.calls == []
    cancelled = harness.updates("req-cancel-early")[-1]
    assert cancelled["phase"] == "cancelled"
    assert cancelled["whileRunning"] is False
    assert harness.task_for("req-cancel-early")["state"] == "cancelled"


def test_cancelling_while_running_discards_the_late_answer(
    tmp_path,
    monkeypatch,
) -> None:
    gate = threading.Event()
    harness = Harness(
        tmp_path,
        monkeypatch,
        brain=FakeBrain(output="too late", gate=gate),
        threaded=True,
    )
    task_id = harness.submit("list or tuple?", "req-cancel-late")
    harness.submit("a second question", "req-behind")

    harness.scheduler.tick()
    assert harness.brain.entered.wait(WAIT_SECONDS)

    handle_queue_cancel(harness.runtime, {"taskId": task_id})

    cancelled = harness.updates("req-cancel-late")[-1]
    assert cancelled["phase"] == "cancelled"
    assert cancelled["whileRunning"] is True
    assert "discarded" in cancelled["detail"]

    # Cancelled, but its model call is still on the GPU: nothing else may
    # start, and the interface is told it is still in flight.
    task = harness.task_for("req-cancel-late")
    assert task["state"] == "cancelled"
    assert task["inFlight"] is True
    harness.scheduler.tick()
    assert len(harness.brain.calls) == 1
    assert harness.scheduler.governor.active() != ()

    gate.set()
    harness.wait_settled()

    # The answer arrived after the cancel and went nowhere.
    assert harness.messages("req-cancel-late") == [
        ("user", "list or tuple?"),
    ]
    assert "answered" not in harness.phases("req-cancel-late")
    assert harness.phases("req-cancel-late")[-1] == "cancelled"
    assert harness.task_for("req-cancel-late")["state"] == "cancelled"
    assert harness.task_for("req-cancel-late")["inFlight"] is False
    assert harness.brain.released == 1

    # Only now is the request behind it allowed to start.
    harness.scheduler.tick()
    harness.wait_settled()
    assert harness.brain.calls[-1][1] == "a second question"


def test_a_cancel_after_the_answer_is_committed_is_refused() -> None:
    work = QueuedConversationWork(
        ConversationRequest(
            request_id="req-committed",
            text="list or tuple?",
            route=TaskType.FAST,
        ),
        executor=None,  # type: ignore[arg-type]
    )

    assert work.commit() is True
    assert work.cancel() is False
    assert work.cancelled is False


def test_the_scheduler_will_not_misreport_a_committed_task_as_cancelled(
) -> None:
    clock = FakeClock()

    class Committed:
        def describe(self) -> str:
            return "already answered"

        def run(self):
            return None

        def cancel(self) -> bool:
            return False

    scheduler = QueueScheduler(
        queue=SafeQueue(clock=clock),
        governor=ResourceGovernor(clock=clock),
        monitor=Machine(clock),  # type: ignore[arg-type]
        runner=lambda entry: None,
        clock=clock,
    )
    task = scheduler.submit(
        work=Committed(),
        weight=Weight.LIGHT,
        summary="already answered",
        required_vram_mb=0,
    )
    scheduler.tick()

    assert scheduler.cancel(task.task_id) is False
    assert scheduler.queue.get(task.task_id).state is TaskState.RUNNING


def test_cancelling_a_committed_request_says_so(tmp_path, monkeypatch) -> None:
    harness = Harness(tmp_path, monkeypatch, threaded=True)
    gate = threading.Event()
    recorded = threading.Event()
    original = harness.runtime._record_queued_answer

    def slow_record(request, response):
        original(request, response)
        recorded.set()
        assert gate.wait(WAIT_SECONDS)

    harness.runtime._record_queued_answer = slow_record  # type: ignore
    task_id = harness.submit("list or tuple?", "req-too-late")

    harness.scheduler.tick()
    assert recorded.wait(WAIT_SECONDS)

    handle_queue_cancel(harness.runtime, {"taskId": task_id})
    gate.set()
    harness.wait_settled()

    assert "queue_cancel_refused" in harness.event_kinds()
    assert "cancelled" not in harness.phases("req-too-late")
    assert harness.task_for("req-too-late")["state"] == "done"


# ------------------------------------- the user's own turn owns the GPU phase


def test_a_foreground_turn_holds_queued_work_until_it_ends(
    harness: Harness,
) -> None:
    harness.runtime._set_busy(True)
    harness.submit("list or tuple?", "req-foreground")

    harness.scheduler.tick()

    assert harness.brain.calls == []
    task = harness.task_for("req-foreground")
    assert task["heldReason"] == HoldReason.FOREGROUND_ACTIVE.value
    # Not a politeness hold: there is no "run anyway" for this one.
    assert task["overridable"] is False

    harness.runtime._set_busy(False)
    harness.scheduler.tick()

    assert harness.phases("req-foreground")[-1] == "answered"


def test_a_turn_that_starts_mid_run_preempts_without_unloading_its_model(
    tmp_path,
    monkeypatch,
) -> None:
    gate = threading.Event()
    harness = Harness(
        tmp_path,
        monkeypatch,
        brain=FakeBrain(error="model was swapped out", gate=gate),
        threaded=True,
    )
    harness.submit("list or tuple?", "req-preempted")

    harness.scheduler.tick()
    assert harness.brain.entered.wait(WAIT_SECONDS)

    # The user starts talking. Their turn now owns the model phase.
    harness.runtime._set_busy(True)
    (work,) = harness.scheduler.running_work()
    assert work.preempted

    gate.set()
    harness.wait_settled()

    # Losing the brain to the user's own turn is not the request's failure.
    task = harness.task_for("req-preempted")
    assert task["state"] == "queued"
    assert task["heldReason"] == HoldReason.FOREGROUND_ACTIVE.value
    assert task["attempts"] == 0
    assert "failed" not in harness.phases("req-preempted")

    # And it did not unload models out from under the user's turn.
    assert harness.brain.released == 0

    harness.runtime._set_busy(False)
    harness.brain.error = None
    harness.brain.gate = None
    harness.clock.advance(31)
    harness.scheduler.tick()
    harness.wait_settled()

    assert harness.phases("req-preempted")[-1] == "answered"
    assert harness.brain.released == 1


def test_a_paused_runtime_defers_rather_than_runs() -> None:
    request = ConversationRequest(
        request_id="req-paused",
        text="list or tuple?",
        route=TaskType.FAST,
    )
    brain = FakeBrain()
    seen: list[ConversationPhase] = []
    executor = ConversationQueueExecutor(
        orchestrator=lambda: brain,
        route=lambda text: TaskType.FAST,
        record_answer=lambda request, response: None,
        notify=lambda work, phase, fields: seen.append(phase),
        foreground_busy=lambda: False,
        runtime_paused=lambda: True,
        release_brains=lambda before: brain.release_brains_for_voice_phase(),
    )

    result = executor.execute(QueuedConversationWork(request, executor))

    assert result.outcome is WorkOutcome.DEFERRED
    assert result.hold is HoldReason.PAUSED
    assert result.count_attempt is False
    assert brain.calls == []
    assert seen == [ConversationPhase.DEFERRED]


# ------------------------------------ no background mutation without approval


@pytest.mark.parametrize(
    "route",
    [TaskType.COMPUTER, TaskType.BROWSER, TaskType.VISION],
)
def test_action_vision_and_browser_requests_are_never_queued(
    tmp_path,
    monkeypatch,
    route: TaskType,
) -> None:
    harness = Harness(tmp_path, monkeypatch, route=route)

    with pytest.raises(ValueError, match="cannot be queued"):
        handle_queue_submit(
            harness.runtime,
            {"text": "do the thing", "requestId": "req-action"},
        )

    assert harness.scheduler.view().tasks == ()
    assert harness.messages("req-action") == []


def test_the_real_router_refuses_an_app_action(tmp_path, monkeypatch) -> None:
    harness = Harness(tmp_path, monkeypatch, route=None)

    with pytest.raises(ValueError, match="cannot be queued"):
        handle_queue_submit(
            harness.runtime,
            {"text": "open notepad", "requestId": "req-notepad"},
        )

    assert harness.scheduler.view().tasks == ()


def test_the_route_is_checked_again_when_the_work_runs(
    harness: Harness,
) -> None:
    harness.submit("list or tuple?", "req-reroute")

    # Whatever changed between submission and run, an action route found at
    # run time is refused rather than executed.
    harness.runtime.task_router = FakeRouter(TaskType.COMPUTER)
    harness.scheduler.tick()

    assert harness.brain.calls == []
    failed = harness.updates("req-reroute")[-1]
    assert failed["phase"] == "failed"
    assert "needs your approval" in failed["error"]
    assert harness.runtime.computer_executor is None
    assert harness.runtime.file_executor is None


# ---------------------------------------------------------- input validation


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"text": "   ", "requestId": "r"}, "must say what it is"),
        ({"text": 7, "requestId": "r"}, "must be text"),
        ({"text": "x" * 16_001, "requestId": "r"}, "at most"),
        ({"summary": "x" * 201}, "at most"),
        ({"text": "hi", "requestId": "  "}, "request id"),
        ({"text": "hi", "requestId": 12}, "request id"),
        ({"text": "hi", "requestId": "r" * 129}, "request id"),
        ({"text": "hi", "requestId": "r", "weight": "huge"}, "weight"),
        ({"text": "hi", "requestId": "r", "requiredVramMb": True}, "whole"),
        ({"text": "hi", "requestId": "r", "requiredVramMb": -1}, "between"),
    ],
)
def test_bad_submissions_are_refused_and_record_nothing(
    harness: Harness,
    payload: dict,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        handle_queue_submit(harness.runtime, payload)

    assert harness.scheduler.view().tasks == ()
    assert harness.runtime.conversation_history.all_messages() == ()


def test_a_request_id_cannot_be_queued_twice(harness: Harness) -> None:
    harness.machine.level = LoadLevel.BUSY
    harness.submit("list or tuple?", "req-twice")

    with pytest.raises(ValueError, match="already queued"):
        handle_queue_submit(
            harness.runtime,
            {"text": "again", "requestId": "req-twice"},
        )

    assert len(harness.scheduler.view().tasks) == 1


def test_the_queue_is_bounded(harness: Harness, monkeypatch) -> None:
    monkeypatch.setattr(runtime_bridge, "MAX_QUEUED_CONVERSATIONS", 2)
    harness.machine.level = LoadLevel.BUSY
    harness.submit("one", "req-a")
    harness.submit("two", "req-b")

    with pytest.raises(ValueError, match="already holds"):
        handle_queue_submit(
            harness.runtime,
            {"text": "three", "requestId": "req-c"},
        )


def test_a_summary_only_submission_is_real_work_too(harness: Harness) -> None:
    handle_queue_submit(
        harness.runtime,
        {"summary": "list or tuple?", "weight": "heavy"},
    )

    (task,) = harness.scheduler.view().tasks
    assert task["kind"] == "conversation"
    assert task["weight"] == "heavy"
    assert task["requestId"].startswith("queue-")

    harness.scheduler.tick()

    assert harness.brain.calls[0][1] == "list or tuple?"
    assert harness.phases(task["requestId"])[-1] == "answered"


# ------------------------------------------------------------- shutdown


def test_closing_keeps_saved_waiting_requests_for_the_next_start(
    harness: Harness,
) -> None:
    harness.machine.level = LoadLevel.BUSY
    task_id = harness.submit("list or tuple?", "req-closing")
    work = harness.scheduler.work_for(task_id)

    harness.runtime.close()

    # Saved, so it comes back on the next start; nothing is said to the
    # conversation as if it were lost.
    assert harness.phases("req-closing") == ["queued"]
    assert not work.cancelled
    record = harness.runtime._queue_journal.get(task_id)
    assert record.state.value == "queued"


def test_closing_without_durable_storage_says_the_request_is_lost(
    harness: Harness,
    monkeypatch,
) -> None:
    from core.queue_journal import StorageProblem, StorageStatus

    harness.machine.level = LoadLevel.BUSY
    task_id = harness.submit("list or tuple?", "req-lost")
    work = harness.scheduler.work_for(task_id)
    monkeypatch.setattr(
        harness.runtime._queue_journal,
        "status",
        lambda: StorageStatus(False, StorageProblem.UNWRITABLE, "disk full"),
    )

    harness.runtime.close()

    interrupted = harness.updates("req-lost")[-1]
    assert interrupted["phase"] == "interrupted"
    assert interrupted["resumable"] is False
    assert "will not come back" in interrupted["detail"]
    # Any model call still under way can no longer attach its answer.
    assert work.cancelled


# --------------------------------------------- scheduler-level bookkeeping


def test_a_deferral_from_the_users_own_turn_does_not_use_up_attempts() -> None:
    clock = FakeClock()
    queue = SafeQueue(clock=clock)
    queue.submit("t", Weight.LIGHT, "x")
    queue.start("t")

    task = queue.requeue("t", count_attempt=False)

    assert task.attempts == 0


def test_in_flight_leases_are_renewed_while_work_runs() -> None:
    clock = FakeClock()
    governor = ResourceGovernor(clock=clock, lease_seconds=60.0)
    scheduler = QueueScheduler(
        queue=SafeQueue(clock=clock),
        governor=governor,
        monitor=Machine(clock),  # type: ignore[arg-type]
        runner=lambda entry: None,
        clock=clock,
    )

    class Slow:
        def describe(self) -> str:
            return "a long generation"

        def run(self):
            return None

    task = scheduler.submit(
        work=Slow(),
        weight=Weight.HEAVY,
        summary="a long generation",
        required_vram_mb=0,
    )
    scheduler.tick()

    for _ in range(5):
        clock.advance(50)
        scheduler.tick()

    # Without renewal the lease would have lapsed four minutes ago and the
    # heavy slot would look free while the work still holds the GPU.
    assert governor.heavy_owner() == task.task_id

    scheduler.report_outcome(task.task_id, WorkResult(WorkOutcome.DONE))

    assert governor.heavy_owner() is None
