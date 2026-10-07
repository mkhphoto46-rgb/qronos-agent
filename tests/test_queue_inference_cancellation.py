"""
Queued work and its model request: closed by whatever stops it, and only then.

Each run of a queued request gets its own local inference cancellation token,
bound around the model call. These tests drive the queue with a fake brain
that behaves the way the real transport does - it checks the run's token
while it "generates", and a closed request surfaces as an exception that the
real Orchestrator turns into a failed step - so each lifecycle path can be
proved without a model:

    a user's cancel closes the request and the request ends;
    the user's own turn and Pause close it and the request runs again later;
    closing Qronos interrupts it, and only an explicit resume runs it again;
    every run has a fresh token, so nothing aimed at an earlier run reaches
    a later one; and after the answer is committed, no stop changes anything.

``tests/test_queue_inference_transport.py`` proves the same against a real
slow loopback HTTP server through the actual model client.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from core.inference_cancellation import (
    LocalInferenceCancellation,
    LocalInferenceCancelled,
    current_inference_cancellation,
    inference_checkpoint,
)
from core.load_signal import LoadLevel
from core.orchestrator import StepResult
from core.queue_scheduler import HoldReason
from core.runtime_bridge import handle_queue_cancel, handle_queue_resume_task
from tests.test_queue_durability import saved
from tests.test_queued_conversation import FakeBrain, Harness


WAIT = 5.0
PRIVATE = "PRIVATE-TRANSPORT-DETAIL"


class GeneratingBrain(FakeBrain):
    """
    A brain that "generates" until released or until its run is stopped.

    Like the real model client it checks the run's token while it waits. A
    stop surfaces as LocalInferenceCancelled; by default it is reported the
    way the real Orchestrator reports any exception from a step - as a
    failed StepResult carrying the exception text, which here also carries
    transport detail that must never reach the user.
    """

    def __init__(self, *, raise_on_stop: bool = False, **kw) -> None:
        super().__init__(**kw)
        self.raise_on_stop = raise_on_stop
        self.release = threading.Event()
        self.tokens: list[LocalInferenceCancellation | None] = []
        self.stopped = threading.Event()
        self.release_scopes: list[object] = []
        self.resident: set[str] = set()

    def execute_plan(self, plan, conversation_messages=None, priority=None):
        token = current_inference_cancellation()
        self.tokens.append(token)
        self.calls.append((plan.steps[0].task_type.value, plan.steps[0].description, ()))
        self.entered.set()
        deadline = time.monotonic() + WAIT

        try:
            while not self.release.is_set():
                inference_checkpoint()
                assert time.monotonic() < deadline, "never released or stopped"
                time.sleep(0.005)
        except LocalInferenceCancelled as exc:
            self.stopped.set()

            if self.raise_on_stop:
                raise RuntimeError(f"{exc} {PRIVATE}") from None

            return [
                StepResult(
                    order=1,
                    success=False,
                    output="",
                    error=f"{exc} {PRIVATE}",
                )
            ]

        return [StepResult(order=1, success=True, output="the one answer")]

    def resident_brain_names(self):
        return frozenset(self.resident)

    def release_brains_loaded_since(self, before):
        self.release_scopes.append(current_inference_cancellation())
        return False


def a_harness(tmp_path, monkeypatch, **brain_kw) -> Harness:
    return Harness(
        tmp_path,
        monkeypatch,
        brain=GeneratingBrain(**brain_kw),
        threaded=True,
    )


def start_one(harness: Harness, request_id: str) -> str:
    task_id = harness.submit("list or tuple?", request_id)
    harness.scheduler.tick()
    assert harness.brain.entered.wait(WAIT)
    return task_id


def settle(harness: Harness) -> None:
    harness.wait_settled()


def saved_state_becomes(harness: Harness, state: str) -> None:
    """
    The journal write follows the scheduler's report on the worker thread,
    a moment after the work stops being in flight. Wait for it through the
    journal itself, then read the file once: polling the file would hold it
    open, and on Windows that makes the atomic replace being waited for fail.
    """
    journal = harness.runtime._queue_journal
    deadline = time.monotonic() + WAIT

    while time.monotonic() < deadline:
        records = journal.records()

        if records and records[0].state.value == state:
            break

        time.sleep(0.01)

    assert journal.status().durable
    assert saved()["payload"]["records"][0]["state"] == state


def answers(harness: Harness, request_id: str) -> list[str]:
    return [t for role, t in harness.messages(request_id) if role == "assistant"]


def nothing_private_was_published(harness: Harness) -> None:
    published = json.dumps(harness.events) + json.dumps(saved())
    assert PRIVATE not in published
    assert "Local inference cancelled" not in published


# --------------------------------------------------------------- the scope


def test_the_model_call_runs_inside_its_own_runs_token_only(
    tmp_path,
    monkeypatch,
) -> None:
    harness = a_harness(tmp_path, monkeypatch)
    start_one(harness, "req-scope")
    harness.brain.release.set()
    settle(harness)

    (token,) = harness.brain.tokens
    assert isinstance(token, LocalInferenceCancellation)
    assert token.cancelled is False
    # Giving back what the run loaded happens outside the run's scope, so
    # it can never be cut short by that run's stop.
    assert harness.brain.release_scopes == [None]
    assert answers(harness, "req-scope") == ["the one answer"]


# ------------------------------------------------------------ terminal stops


@pytest.mark.parametrize("raise_on_stop", [False, True])
def test_a_users_cancel_closes_the_request_and_attaches_nothing(
    tmp_path,
    monkeypatch,
    raise_on_stop: bool,
) -> None:
    harness = a_harness(tmp_path, monkeypatch, raise_on_stop=raise_on_stop)
    task_id = start_one(harness, "req-cancel")

    handle_queue_cancel(harness.runtime, {"taskId": task_id})

    # The run's own request is closed at once, not when generation ends.
    assert harness.brain.stopped.wait(1.0)
    assert harness.brain.tokens[0].reason == "user_stop"

    harness.brain.release.set()
    settle(harness)

    phases = harness.phases("req-cancel")
    assert phases[-1] == "cancelled"
    assert "failed" not in phases
    assert answers(harness, "req-cancel") == []
    assert harness.task_for("req-cancel")["state"] == "cancelled"
    saved_state_becomes(harness, "cancelled")
    nothing_private_was_published(harness)


def test_closing_qronos_interrupts_and_only_an_explicit_resume_answers(
    tmp_path,
    monkeypatch,
) -> None:
    first = a_harness(tmp_path, monkeypatch)
    task_id = start_one(first, "req-close")

    first.runtime.close()

    assert first.brain.stopped.wait(1.0)
    assert first.brain.tokens[0].reason == "runtime_shutdown"
    first.brain.release.set()
    settle(first)

    assert answers(first, "req-close") == []
    saved_state_becomes(first, "interrupted")
    nothing_private_was_published(first)

    second = a_harness(tmp_path, monkeypatch)
    second.brain.release.set()
    second.scheduler.tick()
    time.sleep(0.1)
    assert second.brain.calls == []
    assert second.task_for("req-close")["state"] == "interrupted"

    handle_queue_resume_task(second.runtime, {"taskId": task_id})
    second.scheduler.tick()
    settle(second)

    assert answers(second, "req-close") == ["the one answer"]
    assert second.brain.tokens[0] is not first.brain.tokens[0]


# ------------------------------------------------------------ run-scoped stops


def test_the_users_turn_closes_the_queued_request_and_it_runs_again(
    tmp_path,
    monkeypatch,
) -> None:
    harness = a_harness(tmp_path, monkeypatch)
    start_one(harness, "req-preempt")

    harness.runtime._set_busy(True)

    assert harness.brain.stopped.wait(1.0)
    first_token = harness.brain.tokens[0]
    assert first_token.reason == "preempted"
    settle(harness)

    task = harness.task_for("req-preempt")
    assert task["state"] == "queued"
    assert task["heldReason"] == HoldReason.FOREGROUND_ACTIVE.value
    assert task["attempts"] == 0
    assert "failed" not in harness.phases("req-preempt")
    assert answers(harness, "req-preempt") == []
    saved_state_becomes(harness, "queued")

    harness.runtime._set_busy(False)
    harness.brain.release.set()
    harness.clock.advance(31)
    harness.scheduler.tick()
    settle(harness)

    second_token = harness.brain.tokens[1]
    assert second_token is not first_token
    assert second_token.cancelled is False
    assert answers(harness, "req-preempt") == ["the one answer"]
    nothing_private_was_published(harness)


def test_pause_closes_the_queued_request_and_resume_answers_once(
    tmp_path,
    monkeypatch,
) -> None:
    harness = a_harness(tmp_path, monkeypatch)
    start_one(harness, "req-pause")

    harness.runtime.set_runtime_paused(True)

    assert harness.brain.stopped.wait(1.0)
    assert harness.brain.tokens[0].reason == "runtime_paused"
    settle(harness)

    task = harness.task_for("req-pause")
    assert task["state"] == "queued"
    assert task["attempts"] == 0
    assert answers(harness, "req-pause") == []

    harness.runtime.set_runtime_paused(False)
    harness.brain.release.set()
    harness.clock.advance(31)
    harness.scheduler.tick()
    settle(harness)

    assert answers(harness, "req-pause") == ["the one answer"]
    assert harness.brain.tokens[1].cancelled is False
    nothing_private_was_published(harness)


# ----------------------------------------------------- one run, one token


def test_nothing_aimed_at_an_earlier_run_reaches_a_later_one(
    tmp_path,
    monkeypatch,
) -> None:
    harness = a_harness(tmp_path, monkeypatch)
    start_one(harness, "req-fresh")
    harness.runtime._set_busy(True)
    assert harness.brain.stopped.wait(1.0)
    settle(harness)
    harness.runtime._set_busy(False)

    work = harness.scheduler.work_for(harness.task_for("req-fresh")["taskId"])
    old_token = harness.brain.tokens[0]

    # Between runs there is nothing to stop.
    assert work.preempt() is False
    assert work.pause() is False

    harness.brain.entered.clear()
    harness.brain.stopped.clear()
    harness.clock.advance(31)
    harness.scheduler.tick()
    assert harness.brain.entered.wait(WAIT)

    # A stop through the earlier run's token - by anything still holding
    # it - is a no-op for this run.
    assert old_token.cancel("user_stop") is False
    assert old_token.cancel("runtime_paused") is False
    time.sleep(0.05)
    assert not harness.brain.stopped.is_set()
    assert harness.brain.tokens[1].cancelled is False

    harness.brain.release.set()
    settle(harness)
    assert answers(harness, "req-fresh") == ["the one answer"]


def test_no_stop_changes_anything_once_the_answer_is_committed(
    tmp_path,
    monkeypatch,
) -> None:
    harness = a_harness(tmp_path, monkeypatch)
    original = harness.runtime._record_queued_answer
    outcomes: dict[str, object] = {}

    def record_then_try_every_stop(request, response):
        original(request, response)
        work = harness.scheduler.work_for(
            harness.task_for("req-committed")["taskId"]
        )
        outcomes["preempt"] = work.preempt()
        outcomes["pause"] = work.pause()
        outcomes["interrupt"] = work.interrupt()
        outcomes["cancel"] = work.cancel()

    harness.runtime._record_queued_answer = record_then_try_every_stop  # type: ignore
    harness.brain.release.set()
    start_one(harness, "req-committed")
    settle(harness)

    assert outcomes == {
        "preempt": False,
        "pause": False,
        "interrupt": False,
        "cancel": False,
    }
    assert harness.brain.tokens[0].cancelled is False
    assert answers(harness, "req-committed") == ["the one answer"]
    assert harness.task_for("req-committed")["state"] == "done"
    saved_state_becomes(harness, "done")


@pytest.mark.parametrize("stop", ["preempt", "pause"])
def test_a_run_stopped_before_commit_can_never_commit(stop: str) -> None:
    from core.queued_conversation import ConversationRequest, QueuedConversationWork
    from core.task_router import TaskType

    work = QueuedConversationWork(
        ConversationRequest("req-unit", "list or tuple?", TaskType.FAST),
        executor=None,  # type: ignore[arg-type]
    )
    token = work.begin_run()

    # The stop lands after the executor's last look and before delivery.
    assert getattr(work, stop)() is True
    assert work.commit() is False
    work.end_run(token)

    # The request itself is not over: its next run gets a fresh token.
    fresh = work.begin_run()
    assert fresh is not None and fresh is not token
    assert work.commit() is True


def test_a_cancelled_request_never_starts_another_run(
    tmp_path,
    monkeypatch,
) -> None:
    harness = a_harness(tmp_path, monkeypatch)
    harness.machine.level = LoadLevel.BUSY
    task_id = harness.submit("list or tuple?", "req-never")
    work = harness.scheduler.work_for(task_id)

    handle_queue_cancel(harness.runtime, {"taskId": task_id})

    assert work.begin_run() is None
    harness.machine.level = LoadLevel.FREE
    harness.scheduler.tick()
    assert harness.brain.calls == []
