"""
Late and stale workers: what they may no longer do to the queue.

Each test pins one defect found in review, fixed alongside it:

    a request whose answer already reached the conversation ran again;
    a crash after a failed save could replay work unasked;
    a cancelled request still started its model call, and still published
    progress after the user was told it was cancelled;
    a finished record could be moved back to a runnable state;
    a request reported lost at close stayed queued.

Everything runs against fake brains and the per-test files that
``tests/conftest.py`` provides.
"""

from __future__ import annotations

import json

from core.load_signal import LoadLevel
from core.queue_journal import (
    QueueJournal,
    RecordState,
    StorageProblem,
    StorageStatus,
)
from core.queue_scheduler import HoldReason
from core.runtime_bridge import handle_queue_cancel
from core.task_router import TaskType
from tests.test_queue_durability import a_record, saved
from tests.test_queued_conversation import (  # noqa: F401 - fixture
    FakeBrain,
    Harness,
    harness,
)


# ------------------------------------------------------- one request, one answer


def test_an_answer_already_in_history_is_never_produced_twice(
    harness: Harness,
) -> None:
    harness.machine.level = LoadLevel.BUSY
    harness.submit("list or tuple?", "req-once")
    harness.runtime.conversation_history.append(
        role="assistant",
        text="the first answer",
        source="text",
        request_id="req-once",
    )

    harness.machine.level = LoadLevel.FREE
    harness.scheduler.tick()

    assert harness.brain.calls == []
    assert [m for m in harness.messages("req-once") if m[0] == "assistant"] == [
        ("assistant", "the first answer"),
    ]
    assert harness.task_for("req-once")["state"] == "done"
    assert harness.updates("req-once")[-1]["alreadyAnswered"] is True


def test_recovery_finishes_any_saved_request_that_was_already_answered(
    tmp_path,
) -> None:
    path = tmp_path / "queue.json"
    first = QueueJournal(path)
    first.open()

    # A stale file: the start of these runs was never saved, but their
    # answers reached the conversation.
    for task_id, state in (
        ("q-queued", RecordState.QUEUED),
        ("q-paused", RecordState.PAUSED),
        ("q-interrupted", RecordState.INTERRUPTED),
    ):
        first.add(a_record(task_id, f"req-{task_id}"))
        first.update(task_id, state)

    first.add(a_record("q-open", "req-open", at=2000.0))

    recovered = QueueJournal(path).open(
        answered=lambda request_id: request_id != "req-open",
    )

    states = {record.task_id: record.state for record in recovered.records}
    assert states == {
        "q-queued": RecordState.DONE,
        "q-paused": RecordState.DONE,
        "q-interrupted": RecordState.DONE,
        "q-open": RecordState.QUEUED,
    }
    assert sorted(recovered.answered) == [
        "q-interrupted",
        "q-paused",
        "q-queued",
    ]


# --------------------------------------------- a failed save never risks replay


def test_work_does_not_start_while_the_queue_cannot_be_saved(
    harness: Harness,
    monkeypatch,
) -> None:
    harness.machine.level = LoadLevel.BUSY
    harness.submit("list or tuple?", "req-disk")
    journal = harness.runtime._queue_journal
    store = journal._store
    working_save = store.save

    def full_disk(payload):
        raise OSError("no space left")

    monkeypatch.setattr(store, "save", full_disk)
    journal.set_paused(False)
    journal.update(
        harness.task_for("req-disk")["taskId"],
        RecordState.QUEUED,
        attempts=0,
        detail="force a save",
    )
    assert journal.status().problem is StorageProblem.UNWRITABLE

    harness.machine.level = LoadLevel.FREE
    harness.scheduler.tick()

    # The file still says "queued". Had it started, a crash now would
    # replay it on the next start without asking.
    assert harness.brain.calls == []
    task = harness.task_for("req-disk")
    assert task["state"] == "queued"
    assert task["heldReason"] == HoldReason.STORAGE_UNAVAILABLE.value
    assert task["attempts"] == 0
    assert saved()["payload"]["records"][0]["state"] == "queued"

    # The disk recovers: the next attempt saves first, then runs.
    monkeypatch.setattr(store, "save", working_save)
    harness.clock.advance(31)
    harness.scheduler.tick()

    assert len(harness.brain.calls) == 1
    assert harness.phases("req-disk")[-1] == "answered"
    assert saved()["payload"]["records"][0]["state"] == "done"


# ------------------------------------------------- cancelled work stays quiet


def test_work_cancelled_while_routing_never_calls_the_model_or_speaks(
    tmp_path,
    monkeypatch,
) -> None:
    harness = Harness(tmp_path, monkeypatch, route=None)

    from types import SimpleNamespace

    class CancellingRouter:
        calls = 0

        def route(self, text):
            # The first call is submission. On the second - the run-time
            # route check - the user presses Cancel mid-routing.
            self.calls += 1

            if self.calls == 2:
                handle_queue_cancel(
                    harness.runtime,
                    {"taskId": harness.task_for("req-late")["taskId"]},
                )

            return SimpleNamespace(task_type=TaskType.FAST, reason="test")

    harness.runtime.task_router = CancellingRouter()
    harness.submit("list or tuple?", "req-late")

    harness.scheduler.tick()

    assert harness.brain.calls == []
    phases = harness.phases("req-late")
    assert phases[-1] == "cancelled"
    assert "running" not in phases[phases.index("cancelled"):]
    assert harness.task_for("req-late")["state"] == "cancelled"
    assert saved()["payload"]["records"][0]["state"] == "cancelled"


# ---------------------------------------------------- nothing is resurrected


def test_a_finished_record_can_never_be_made_runnable_again(tmp_path) -> None:
    journal = QueueJournal(tmp_path / "queue.json")
    journal.open()
    journal.add(a_record("q-c", "req-c"))
    journal.add(a_record("q-d", "req-d"))
    journal.update("q-c", RecordState.CANCELLED, detail="by you")
    journal.update("q-d", RecordState.DONE)

    journal.update("q-c", RecordState.QUEUED)
    journal.update("q-c", RecordState.DONE)
    journal.update("q-d", RecordState.INTERRUPTED)
    journal.update("q-d", RecordState.CANCELLED)

    assert journal.get("q-c").state is RecordState.CANCELLED
    assert journal.get("q-c").detail == "by you"
    assert journal.get("q-d").state is RecordState.DONE


def test_a_request_reported_lost_at_close_is_cancelled_not_kept(
    harness: Harness,
    monkeypatch,
) -> None:
    harness.machine.level = LoadLevel.BUSY
    task_id = harness.submit("list or tuple?", "req-lost")
    journal = harness.runtime._queue_journal
    monkeypatch.setattr(
        journal,
        "status",
        lambda: StorageStatus(False, StorageProblem.UNWRITABLE, "disk full"),
    )

    harness.runtime.close()

    assert harness.updates("req-lost")[-1]["phase"] == "interrupted"
    assert harness.scheduler.queue.get(task_id).state.value == "cancelled"
    # Had the disk recovered before exit, the record would now say what the
    # user was told rather than coming back on the next start.
    assert journal.get(task_id).state is RecordState.CANCELLED
    assert json.loads(
        [m for k, _s, m in harness.events if k == "queue_changed"][-1]
    )["tasks"][0]["state"] == "cancelled"
