"""
The Smart Queue across restarts: what is remembered, and how honestly.

Two layers. The journal on its own — versioned, atomic, refusing to write
over a file it cannot trust — and then the runtime wired to it, restarted by
building a second runtime over the same files, which is what a restart is to
everything except the operating system. ``tests/test_queue_restart_process.py``
does the same with real processes and a real kill.

No test here reads or writes anything outside its own temporary directory;
``tests/conftest.py`` points history, settings and queue state there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.json_store import JsonStore
from core.load_signal import LoadLevel
from core.queue_journal import (
    MAX_FINISHED_RECORDS,
    QUEUE_STATE_PATH_ENV,
    QUEUE_STATE_VERSION,
    QueueJournal,
    QueueRecord,
    RecordState,
    StorageProblem,
)
from core.resource_ownership import (
    GLOBAL_RESOURCE_LEDGER,
    WorkloadPriority,
)
from core.runtime_bridge import (
    handle_queue_cancel,
    handle_queue_pause_task,
    handle_queue_resume_task,
    handle_queue_set_paused,
    handle_queue_submit,
)
from core.task_router import TaskType
from tests.test_queued_conversation import (  # noqa: F401 - fixture
    FakeBrain,
    FakeRouter,
    Harness,
    harness,
)


# ------------------------------------------------------------------ helpers


def a_record(
    task_id: str = "q-1",
    request_id: str = "req-1",
    state: RecordState = RecordState.QUEUED,
    at: float = 1000.0,
) -> QueueRecord:
    return QueueRecord(
        task_id=task_id,
        request_id=request_id,
        summary="list or tuple?",
        route="fast",
        weight="light",
        required_vram_mb=3_482,
        state=state,
        submitted_at=at,
        updated_at=at,
        text="list or tuple?",
        context=(("user", "earlier"), ("assistant", "earlier answer")),
    )


def state_path() -> Path:
    return Path(os.environ[QUEUE_STATE_PATH_ENV])


def saved() -> dict:
    return json.loads(state_path().read_text(encoding="utf-8"))


def restart(tmp_path, monkeypatch, **kwargs) -> Harness:
    """A new runtime over the same history and queue files."""
    return Harness(tmp_path, monkeypatch, **kwargs)


# ============================================================ the journal


def test_a_fresh_journal_proves_it_can_write_before_claiming_durability(
    tmp_path,
) -> None:
    journal = QueueJournal(tmp_path / "state" / "queue.json")

    journal.open()

    assert journal.status().durable
    assert (tmp_path / "state" / "queue.json").exists()


def test_records_round_trip_and_running_comes_back_interrupted(
    tmp_path,
) -> None:
    path = tmp_path / "queue.json"
    first = QueueJournal(path)
    first.open()
    first.add(a_record("q-run", "req-run"))
    first.add(a_record("q-wait", "req-wait", at=1001.0))
    first.update("q-run", RecordState.RUNNING, attempts=1)

    # A crash: nothing else is written.
    second = QueueJournal(path)
    recovered = second.open()

    states = {record.task_id: record.state for record in recovered.records}
    assert states == {
        "q-run": RecordState.INTERRUPTED,
        "q-wait": RecordState.QUEUED,
    }
    assert recovered.interrupted == ("q-run",)
    assert second.get("q-wait").text == "list or tuple?"
    assert second.get("q-wait").context[1] == ("assistant", "earlier answer")

    # The interrupted mark is on disk before anything could act on it, so a
    # second crash cannot turn it back into "running".
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert {r["taskId"]: r["state"] for r in on_disk["payload"]["records"]} == {
        "q-run": "interrupted",
        "q-wait": "queued",
    }
    assert on_disk["schemaVersion"] == QUEUE_STATE_VERSION


def test_running_work_whose_answer_was_recorded_comes_back_done(
    tmp_path,
) -> None:
    path = tmp_path / "queue.json"
    first = QueueJournal(path)
    first.open()
    first.add(a_record())
    first.update("q-1", RecordState.RUNNING)

    recovered = QueueJournal(path).open(
        answered=lambda request_id: request_id == "req-1",
    )

    assert recovered.records[0].state is RecordState.DONE
    assert recovered.answered == ("q-1",)
    assert recovered.interrupted == ()


def test_a_finished_request_keeps_no_copy_of_its_text(tmp_path) -> None:
    journal = QueueJournal(tmp_path / "queue.json")
    journal.open()
    journal.add(a_record())

    journal.update("q-1", RecordState.DONE)

    record = journal.get("q-1")
    assert record.text == "" and record.context == ()
    assert "list or tuple" not in json.dumps(
        json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))[
            "payload"
        ]["records"][0]["text"]
    )


def test_finished_history_is_bounded(tmp_path) -> None:
    journal = QueueJournal(tmp_path / "queue.json")
    journal.open()

    for index in range(MAX_FINISHED_RECORDS + 5):
        journal.add(a_record(f"q-{index}", f"req-{index}", at=1000.0 + index))
        journal.update(f"q-{index}", RecordState.CANCELLED)

    assert len(journal.records()) == MAX_FINISHED_RECORDS


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ("{ not json", StorageProblem.CORRUPT),
        (json.dumps({"schemaVersion": 1, "payload": []}), StorageProblem.CORRUPT),
        (
            json.dumps({"schemaVersion": 99, "payload": {"records": []}}),
            StorageProblem.TOO_NEW,
        ),
        (
            # A request that may still run but has lost its text.
            json.dumps(
                {
                    "schemaVersion": 1,
                    "payload": {
                        "paused": False,
                        "records": [
                            {**a_record().to_payload(), "text": ""},
                        ],
                    },
                }
            ),
            StorageProblem.CORRUPT,
        ),
        (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "payload": {
                        "paused": False,
                        "records": [
                            a_record().to_payload(),
                            a_record().to_payload(),
                        ],
                    },
                }
            ),
            StorageProblem.CORRUPT,
        ),
    ],
    ids=["not-json", "wrong-shape", "newer", "lost-text", "duplicate-task"],
)
def test_a_file_it_cannot_trust_is_reported_and_never_written_over(
    tmp_path,
    content: str,
    problem: StorageProblem,
) -> None:
    path = tmp_path / "queue.json"
    path.write_text(content, encoding="utf-8")

    journal = QueueJournal(path)
    recovered = journal.open()

    assert recovered.records == ()
    assert journal.status().durable is False
    assert journal.status().problem is problem
    # Accepting work still works, in memory, and says it is not saved.
    assert journal.add(a_record()) is False
    assert journal.set_paused(True) is False
    assert path.read_text(encoding="utf-8") == content
    # Nothing in the report leaks where the file lives.
    assert str(tmp_path) not in journal.status().detail


def test_an_unwritable_location_is_reported_then_recovers_in_full(
    tmp_path,
) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where a folder should be", encoding="utf-8")
    path = blocker / "queue.json"

    journal = QueueJournal(path)
    journal.open()

    assert journal.status().problem is StorageProblem.UNWRITABLE
    assert journal.add(a_record()) is False
    assert journal.status().durable is False

    # The disk comes back. The next write saves everything, including the
    # request accepted while nothing could be saved.
    blocker.unlink()
    assert journal.set_paused(True) is True
    assert journal.status().durable is True

    reread = QueueJournal(path)
    recovered = reread.open()
    assert [record.task_id for record in recovered.records] == ["q-1"]
    assert recovered.paused is True


def test_only_live_or_answered_requests_own_their_id(tmp_path) -> None:
    journal = QueueJournal(tmp_path / "queue.json")
    journal.open()
    journal.add(a_record("q-a", "req-a"))
    journal.add(a_record("q-b", "req-b"))
    journal.add(a_record("q-c", "req-c"))
    journal.update("q-b", RecordState.CANCELLED)
    journal.update("q-c", RecordState.DONE)

    assert journal.holds_request("req-a")
    assert not journal.holds_request("req-b")
    assert journal.holds_request("req-c")


def test_the_atomic_write_leaves_no_temporary_file(tmp_path) -> None:
    store = JsonStore(tmp_path / "doc.json", version=1)

    store.save({"x": 1})

    assert sorted(item.name for item in tmp_path.iterdir()) == ["doc.json"]


# ==================================================== restart, in process


def test_a_waiting_request_survives_a_restart_and_is_answered(
    tmp_path,
    monkeypatch,
) -> None:
    first = Harness(tmp_path, monkeypatch)
    first.machine.level = LoadLevel.BUSY
    history = first.runtime.conversation_history
    history.append(role="user", text="earlier question", source="text")
    task_id = first.submit("list or tuple?", "req-restart")
    first.runtime.close()

    second = restart(tmp_path, monkeypatch)
    task = second.task_for("req-restart")
    assert task["taskId"] == task_id
    assert task["state"] == "queued"

    second.scheduler.tick()

    route, description, context = second.brain.calls[0]
    assert (route, description) == ("fast", "list or tuple?")
    # The conversation it was asked in came back with it.
    assert [message.content for message in context] == ["earlier question"]
    assert second.brain.priorities == [WorkloadPriority.BACKGROUND]
    assert second.messages("req-restart")[-1][0] == "assistant"
    assert saved()["payload"]["records"][0]["state"] == "done"


def test_running_at_a_crash_waits_for_an_explicit_resume(
    tmp_path,
    monkeypatch,
) -> None:
    first = Harness(tmp_path, monkeypatch)
    first.machine.level = LoadLevel.BUSY
    task_id = first.submit("list or tuple?", "req-crash")
    # Exactly what is on disk the moment work starts, and then nothing more:
    # the process died mid-run.
    first.runtime._queue_journal.update(task_id, RecordState.RUNNING, attempts=1)

    second = restart(tmp_path, monkeypatch)

    task = second.task_for("req-crash")
    assert task["state"] == "interrupted"
    assert task["resumable"] is True
    assert task["cancellable"] is True
    interrupted = second.updates("req-crash")[-1]
    assert interrupted["phase"] == "interrupted"
    assert interrupted["resumable"] is True

    # Never re-run unasked, however free the machine is.
    for _ in range(3):
        second.scheduler.tick()
    assert second.brain.calls == []

    handle_queue_resume_task(second.runtime, {"taskId": task_id})
    second.scheduler.tick()

    assert len(second.brain.calls) == 1
    assert second.phases("req-crash")[-1] == "answered"
    assert second.task_for("req-crash")["state"] == "done"


def test_a_crash_after_the_answer_was_saved_is_not_resumed_twice(
    tmp_path,
    monkeypatch,
) -> None:
    first = Harness(tmp_path, monkeypatch)
    first.machine.level = LoadLevel.BUSY
    task_id = first.submit("list or tuple?", "req-answered")
    first.runtime._queue_journal.update(task_id, RecordState.RUNNING)
    first.runtime.conversation_history.append(
        role="assistant",
        text="the answer made it",
        source="text",
        request_id="req-answered",
    )

    second = restart(tmp_path, monkeypatch)

    assert second.scheduler.view().tasks == ()
    assert second.phases("req-answered") == ["answered"]
    assert saved()["payload"]["records"][0]["state"] == "done"


def test_a_clean_close_mid_run_interrupts_and_keeps_waiting_work(
    tmp_path,
    monkeypatch,
) -> None:
    import threading

    gate = threading.Event()
    first = Harness(
        tmp_path,
        monkeypatch,
        brain=FakeBrain(output="too late", gate=gate),
        threaded=True,
    )
    running_id = first.submit("list or tuple?", "req-running")
    waiting_id = first.submit("second question", "req-waiting")
    first.scheduler.tick()
    assert first.brain.entered.wait(5)

    first.runtime.close()
    gate.set()
    first.wait_settled()

    # The answer that arrived after the close went nowhere.
    assert first.messages("req-running") == [("user", "list or tuple?")]
    states = {
        record["taskId"]: record["state"]
        for record in saved()["payload"]["records"]
    }
    assert states == {running_id: "interrupted", waiting_id: "queued"}

    second = restart(tmp_path, monkeypatch)
    second.scheduler.tick()

    assert second.task_for("req-running")["state"] == "interrupted"
    assert second.phases("req-waiting")[-1] == "answered"


def test_a_cancellation_is_remembered(tmp_path, monkeypatch) -> None:
    first = Harness(tmp_path, monkeypatch)
    first.machine.level = LoadLevel.BUSY
    task_id = first.submit("list or tuple?", "req-cancelled")
    handle_queue_cancel(first.runtime, {"taskId": task_id})

    second = restart(tmp_path, monkeypatch)
    second.scheduler.tick()

    assert second.scheduler.view().tasks == ()
    assert second.brain.calls == []

    second.runtime.emit_queue_view()
    view = json.loads(
        [m for k, _s, m in second.events if k == "queue_changed"][-1]
    )
    assert view["recent"][0]["requestId"] == "req-cancelled"
    assert view["recent"][0]["state"] == "cancelled"
    assert view["storage"]["durable"] is True


def test_the_users_hold_is_remembered_but_qronos_pause_is_not(
    tmp_path,
    monkeypatch,
) -> None:
    first = Harness(tmp_path, monkeypatch)
    handle_queue_set_paused(first.runtime, {"paused": True})
    first.runtime.set_runtime_paused(True)
    first.runtime.set_runtime_paused(False)

    # Resuming Qronos did not lift the user's hold on the queue.
    assert first.scheduler.view().paused is True

    second = restart(tmp_path, monkeypatch)
    assert second.scheduler.view().paused is True
    assert second.scheduler.view().runtime_paused is False
    assert second.runtime.runtime_paused is False


def test_a_request_id_stays_taken_across_a_restart(
    tmp_path,
    monkeypatch,
) -> None:
    first = Harness(tmp_path, monkeypatch)
    first.machine.level = LoadLevel.BUSY
    first.submit("list or tuple?", "req-once")

    second = restart(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="already queued"):
        handle_queue_submit(
            second.runtime,
            {"text": "again", "requestId": "req-once"},
        )

    # Answered requests keep their id too.
    second.scheduler.tick()
    with pytest.raises(ValueError, match="already queued"):
        handle_queue_submit(
            second.runtime,
            {"text": "again", "requestId": "req-once"},
        )


def test_restored_work_is_validated_afresh_when_it_runs(
    tmp_path,
    monkeypatch,
) -> None:
    first = Harness(tmp_path, monkeypatch)
    first.machine.level = LoadLevel.BUSY
    first.submit("list or tuple?", "req-revalidate")
    first.submit("another question", "req-readmit")

    # After the restart the route is an action: refused, not run.
    second = restart(
        tmp_path,
        monkeypatch,
        brain=FakeBrain(blocked_times=1),
        route=TaskType.COMPUTER,
    )
    second.scheduler.tick()

    failed = second.updates("req-revalidate")[-1]
    assert failed["phase"] == "failed"
    assert "needs your approval" in failed["error"]
    assert second.brain.calls == []

    # And resource policy is asked again: refused once, deferred, admitted
    # again once the machine allows.
    second.runtime.task_router = FakeRouter(TaskType.FAST)
    second.clock.advance(31)
    second.scheduler.tick()
    assert second.task_for("req-readmit")["state"] == "queued"
    assert second.phases("req-readmit")[-1] == "deferred"

    second.clock.advance(31)
    second.scheduler.tick()
    assert second.phases("req-readmit")[-1] == "answered"


def test_a_corrupt_queue_file_keeps_working_in_memory_and_says_so(
    tmp_path,
    monkeypatch,
) -> None:
    path = Path(os.environ[QUEUE_STATE_PATH_ENV])
    path.write_text("{ damaged", encoding="utf-8")

    harness = Harness(tmp_path, monkeypatch)
    harness.submit("list or tuple?", "req-memory")

    problem = [m for k, _s, m in harness.events if k == "queue_storage_problem"]
    assert json.loads(problem[0])["problem"] == "corrupt"
    queued = harness.updates("req-memory")[0]
    assert queued["durable"] is False

    harness.scheduler.tick()
    assert harness.phases("req-memory")[-1] == "answered"
    assert path.read_text(encoding="utf-8") == "{ damaged"


def test_pause_and_resume_one_request(harness: Harness) -> None:
    harness.machine.level = LoadLevel.BUSY
    task_id = harness.submit("list or tuple?", "req-held")

    handle_queue_pause_task(harness.runtime, {"taskId": task_id})
    harness.machine.level = LoadLevel.FREE
    harness.scheduler.tick()

    assert harness.brain.calls == []
    assert harness.task_for("req-held")["state"] == "paused"
    assert harness.phases("req-held")[-1] == "paused"
    assert saved()["payload"]["records"][0]["state"] == "paused"

    handle_queue_resume_task(harness.runtime, {"taskId": task_id})
    harness.scheduler.tick()

    assert harness.phases("req-held")[-1] == "answered"


def test_resume_is_refused_for_a_request_that_is_not_held(
    harness: Harness,
) -> None:
    harness.machine.level = LoadLevel.BUSY
    task_id = harness.submit("list or tuple?", "req-waiting")

    handle_queue_resume_task(harness.runtime, {"taskId": task_id})

    refused = [m for k, _s, m in harness.events if k == "queue_task_refused"]
    assert json.loads(refused[-1])["action"] == "resume"
    assert harness.task_for("req-waiting")["state"] == "queued"


def test_resuming_a_task_paused_for_too_many_tries_gives_it_fresh_tries(
    tmp_path,
    monkeypatch,
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        brain=FakeBrain(blocked_times=3),
    )
    task_id = harness.submit("list or tuple?", "req-tries")

    for _ in range(4):
        harness.scheduler.tick()
        harness.clock.advance(31)

    assert harness.task_for("req-tries")["state"] == "paused"
    assert harness.task_for("req-tries")["resumable"] is True
    assert saved()["payload"]["records"][0]["state"] == "paused"

    handle_queue_resume_task(harness.runtime, {"taskId": task_id})

    assert harness.task_for("req-tries")["attempts"] == 0
    harness.scheduler.tick()
    assert harness.phases("req-tries")[-1] == "answered"


def test_resume_is_refused_while_qronos_is_paused(harness: Harness) -> None:
    harness.machine.level = LoadLevel.BUSY
    task_id = harness.submit("list or tuple?", "req-paused-runtime")
    handle_queue_pause_task(harness.runtime, {"taskId": task_id})
    harness.runtime.set_runtime_paused(True)

    handle_queue_resume_task(harness.runtime, {"taskId": task_id})

    assert harness.task_for("req-paused-runtime")["state"] == "paused"
    assert "runtime_paused_rejected" in harness.event_kinds()


def test_the_view_carries_what_the_panel_needs(harness: Harness) -> None:
    harness.machine.level = LoadLevel.BUSY
    harness.submit("list or tuple?", "req-view")

    harness.runtime.emit_queue_view()

    view = json.loads(
        [m for k, _s, m in harness.events if k == "queue_changed"][-1]
    )
    assert set(view) >= {
        "revision",
        "paused",
        "runtimePaused",
        "level",
        "tasks",
        "storage",
        "recent",
    }
    task = view["tasks"][0]
    assert set(task) >= {
        "taskId",
        "requestId",
        "summary",
        "state",
        "heldReason",
        "detail",
        "cancellable",
        "resumable",
        "inFlight",
        "kind",
        "route",
    }


# ======================================== background priority in the ledger


def test_queued_brain_work_is_recorded_as_background_in_the_ledger() -> None:
    from core.orchestrator import Orchestrator

    class NoRuntime:
        def list_running_models(self):
            return []

    orchestrator = Orchestrator(runtime=NoRuntime())  # type: ignore[arg-type]

    background = orchestrator._reserve_brain_resources(
        "queue-test-model",
        1.0,
        priority=WorkloadPriority.BACKGROUND,
    )
    try:
        assert (
            GLOBAL_RESOURCE_LEDGER.get(background.reservation_id).priority
            is WorkloadPriority.BACKGROUND
        )
    finally:
        orchestrator._release_brain_resources(background.reservation_id)

    direct = orchestrator._reserve_brain_resources("queue-test-model-2", 1.0)
    try:
        assert direct.priority is WorkloadPriority.ACTIVE_QRONOS_REQUEST
    finally:
        orchestrator._release_brain_resources(direct.reservation_id)
