"""
Where queue state is written, and what of a request it keeps.

Location: the queue file lives under Qronos's per-user data root, as resolved
by ``core.storage_locations`` - which ``tests/conftest.py`` points at a
temporary profile, so no real locator or history is read here. An explicit
override must be absolute: a relative one used to be accepted and would have
resolved against the runtime's working directory, which for an installed copy
can be the folder holding its binaries.

Privacy: once a request is finished - done, failed or cancelled, including
an interrupted request that is then cancelled - the saved record keeps
nothing of what was asked, not even the short summary it used to keep.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from core.load_signal import LoadLevel
from core.queue_journal import (
    QUEUE_STATE_PATH_ENV,
    QueueJournal,
    QueueStatePathError,
    RecordState,
    StorageProblem,
    default_queue_state_path,
)
from core.runtime_bridge import handle_queue_cancel
from core.storage_locations import load_storage_locations
from tests.test_queue_durability import a_record
from tests.test_queued_conversation import FakeBrain, Harness


QUESTION = "list or tuple?"


# ----------------------------------------------------------------- location


def test_a_relative_override_is_refused_and_nothing_is_written_beside_cwd(
    tmp_path,
    monkeypatch,
) -> None:
    working = tmp_path / "install-dir"
    working.mkdir()
    monkeypatch.chdir(working)
    monkeypatch.setenv(QUEUE_STATE_PATH_ENV, "queue_state.json")

    try:
        default_queue_state_path()
    except QueueStatePathError:
        pass
    else:
        raise AssertionError("a relative override was accepted")

    harness = Harness(tmp_path, monkeypatch)
    harness.machine.level = LoadLevel.BUSY
    harness.submit(QUESTION, "req-relative")

    status = harness.runtime._queue_journal.status()
    assert status.durable is False
    assert status.problem is StorageProblem.UNAVAILABLE
    assert "absolute" in status.detail
    assert harness.updates("req-relative")[0]["durable"] is False
    assert list(working.iterdir()) == []


def test_without_an_override_state_goes_under_the_per_user_data_root(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv(QUEUE_STATE_PATH_ENV)

    path = default_queue_state_path()
    data_root = Path(load_storage_locations(create=False).data_root)

    assert path == data_root / "Queue" / "queue_state.json"
    # conftest's temporary profile, never the real one.
    assert tmp_path in path.parents


def test_only_the_queue_file_is_added_to_a_configured_data_root(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv(QUEUE_STATE_PATH_ENV)
    custom = tmp_path / "chosen-data-root"
    custom.mkdir()
    keep = custom / "users-own-file.txt"
    keep.write_text("untouched", encoding="utf-8")

    import core.storage_locations as storage_locations

    locator = storage_locations.locator_path()
    locator.parent.mkdir(parents=True, exist_ok=True)
    locator.write_text(json.dumps({"dataRoot": str(custom)}), encoding="utf-8")
    before = locator.read_bytes()

    harness = Harness(tmp_path, monkeypatch)
    harness.machine.level = LoadLevel.BUSY
    harness.submit(QUESTION, "req-custom-root")

    assert harness.runtime._queue_journal.status().durable is True
    assert (custom / "Queue" / "queue_state.json").exists()
    assert sorted(p.name for p in custom.iterdir()) == [
        "Queue",
        "users-own-file.txt",
    ]
    assert keep.read_text(encoding="utf-8") == "untouched"
    assert locator.read_bytes() == before


# ------------------------------------------------------------------ privacy


def saved_records() -> list[dict]:
    path = Path(os.environ[QUEUE_STATE_PATH_ENV])
    return json.loads(path.read_text(encoding="utf-8"))["payload"]["records"]


def assert_keeps_nothing_of_the_request(record: dict) -> None:
    assert record["text"] == ""
    assert record["summary"] == ""
    assert record["context"] == []
    assert QUESTION not in json.dumps(record)


def test_a_failed_request_keeps_nothing_of_what_was_asked(
    tmp_path,
    monkeypatch,
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        brain=FakeBrain(error="Local brain request failed."),
    )
    harness.submit(QUESTION, "req-failed")
    harness.scheduler.tick()

    (record,) = saved_records()
    assert record["state"] == "failed"
    assert_keeps_nothing_of_the_request(record)


def test_an_interrupted_request_cancelled_later_keeps_nothing(
    tmp_path,
    monkeypatch,
) -> None:
    first = Harness(tmp_path, monkeypatch)
    first.machine.level = LoadLevel.BUSY
    task_id = first.submit(QUESTION, "req-interrupted")
    first.runtime._queue_journal.update(task_id, RecordState.RUNNING)

    second = Harness(tmp_path, monkeypatch)
    assert second.task_for("req-interrupted")["state"] == "interrupted"
    handle_queue_cancel(second.runtime, {"taskId": task_id})

    (record,) = saved_records()
    assert record["state"] == "cancelled"
    assert_keeps_nothing_of_the_request(record)


def test_finished_records_from_the_earlier_format_are_scrubbed_on_open(
    tmp_path,
) -> None:
    path = tmp_path / "queue.json"
    old = {
        **a_record("q-old", "req-old", state=RecordState.CANCELLED).to_payload(),
    }
    old["text"] = ""
    old["context"] = []
    # The earlier format kept the summary of a finished request.
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "payload": {"paused": False, "records": [old]},
            }
        ),
        encoding="utf-8",
    )

    journal = QueueJournal(path)
    journal.open()

    (record,) = json.loads(path.read_text(encoding="utf-8"))["payload"][
        "records"
    ]
    assert record["state"] == "cancelled"
    assert_keeps_nothing_of_the_request(record)
    assert journal.get("q-old").summary == ""
