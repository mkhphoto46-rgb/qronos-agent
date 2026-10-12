"""
The Smart Queue across real restarts and a real crash.

Each test starts the actual bridge process (through
``tests/fixtures/fake_brain_bridge.py``, which swaps in a scripted brain and a
free-machine monitor and changes nothing else), stops it — politely with
``shutdown``, or by killing it — and starts a second one over the same files.
What the second process does with what the first left behind is the contract
under test.

Every file lives in a temporary directory. No model is loaded, no model server
is contacted, and nothing on the developer's machine outside that directory
is read or written.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_runtime_bridge_process import (
    REPLY_TIMEOUT,
    STARTUP_TIMEOUT,
    BridgeProcess,
    private_profile_has_local_appdata,
)


FIXTURE = "tests.fixtures.fake_brain_bridge"
QUESTION = "explain the difference between a list and a tuple"


class RestartTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.state = Path(self._dir.name)
        self.marker = self.state / "brain_entered"
        self.bridges: list[BridgeProcess] = []

    def tearDown(self) -> None:
        for bridge in self.bridges:
            bridge.close()

        self._dir.cleanup()

    # ---------------------------------------------------------- plumbing

    def start(
        self,
        mode: str = "answer",
        **extra_env: str,
    ) -> BridgeProcess:
        bridge = BridgeProcess(
            module=FIXTURE,
            state_dir=self.state,
            env_overrides={
                "QRONOS_TEST_BRAIN_MODE": mode,
                "QRONOS_TEST_BRAIN_MARKER": str(self.marker),
                "QRONOS_QUEUE_TICK_SECONDS": "0.2",
                **extra_env,
            },
        )
        self.bridges.append(bridge)

        ready = bridge.read_event(timeout=STARTUP_TIMEOUT)
        self.assertIsNotNone(ready, "the bridge never announced itself")
        self.assertEqual(ready["eventType"], "runtime_ready")

        return bridge

    def stop(self, bridge: BridgeProcess) -> None:
        bridge.send({"command": "shutdown"})
        self.assertEqual(bridge.process.wait(timeout=30), 0)

    def crash(self, bridge: BridgeProcess) -> None:
        bridge.process.kill()
        bridge.process.wait(timeout=30)

    def open_queue(self, bridge: BridgeProcess) -> dict:
        bridge.send({"command": "queue_list"})
        return self.next_view(bridge)

    def next_view(self, bridge: BridgeProcess) -> dict:
        event = bridge.read_until("queue_changed", timeout=60.0)
        self.assertIsNotNone(event, "no queue arrived")
        return json.loads(event["message"])

    def view_where(self, bridge: BridgeProcess, predicate) -> dict:
        deadline = time.time() + REPLY_TIMEOUT

        while time.time() < deadline:
            bridge.send({"command": "queue_list"})
            view = self.next_view(bridge)

            if predicate(view):
                return view

        self.fail("the queue never reached the expected state")

    def update_for(
        self,
        bridge: BridgeProcess,
        request_id: str,
        phase: str,
        timeout: float = REPLY_TIMEOUT,
    ) -> dict:
        deadline = time.time() + timeout

        while time.time() < deadline:
            event = bridge.read_until(
                "queue_conversation_update",
                timeout=max(0.1, deadline - time.time()),
            )

            if event is None:
                break

            payload = json.loads(event["message"])

            if (
                payload.get("requestId") == request_id
                and payload.get("phase") == phase
            ):
                return payload

        self.fail(f"{request_id} never reached {phase}")

    def submit(self, bridge: BridgeProcess, request_id: str) -> dict:
        bridge.send(
            {
                "command": "queue_submit",
                "text": QUESTION,
                "requestId": request_id,
            }
        )
        return self.update_for(bridge, request_id, "queued")

    def hold(self, bridge: BridgeProcess, held: bool) -> None:
        bridge.send({"command": "queue_set_paused", "paused": held})
        self.view_where(bridge, lambda view: view["paused"] is held)

    def events_until_pong(self, bridge: BridgeProcess) -> list[dict]:
        """Everything the bridge says up to a ping's answer."""
        bridge.send({"command": "ping"})
        seen = []

        while True:
            event = bridge.read_event()
            self.assertIsNotNone(event)

            if event["eventType"] == "runtime_pong":
                return seen

            seen.append(event)

    def answers_in_history(self, request_id: str) -> list[str]:
        connection = sqlite3.connect(
            str(self.state / "conversation_history.sqlite3")
        )
        try:
            rows = connection.execute(
                "SELECT text FROM conversation_messages "
                "WHERE request_id = ? AND role = 'assistant'",
                (request_id,),
            ).fetchall()
        finally:
            connection.close()

        return [row[0] for row in rows]

    def saved_state(self) -> dict:
        return json.loads(
            (self.state / "queue_state.json").read_text(encoding="utf-8")
        )


class TestWaitingWorkSurvivesARestart(RestartTestCase):
    def test_a_held_request_and_the_hold_come_back_then_it_is_answered(
        self,
    ) -> None:
        first = self.start()
        self.open_queue(first)
        self.hold(first, True)
        queued = self.submit(first, "req-survive")
        self.assertIs(queued["durable"], True)
        self.stop(first)

        second = self.start()
        view = self.open_queue(second)

        self.assertIs(view["paused"], True)
        self.assertEqual(view["storage"]["durable"], True)
        (task,) = view["tasks"]
        self.assertEqual(task["requestId"], "req-survive")
        self.assertEqual(task["state"], "queued")

        self.hold(second, False)
        answered = self.update_for(second, "req-survive", "answered")

        self.assertIn(QUESTION, answered["text"])
        self.assertEqual(len(self.answers_in_history("req-survive")), 1)
        # The brain was asked as background work.
        self.assertEqual(
            self.marker.read_text(encoding="utf-8").strip(),
            "background",
        )
        self.stop(second)

        records = self.saved_state()["payload"]["records"]
        self.assertEqual(records[0]["state"], "done")
        # Finished: the saved copy of the request text is gone.
        self.assertEqual(records[0]["text"], "")


class TestACrashMidRun(RestartTestCase):
    def test_interrupted_work_waits_for_an_explicit_resume(self) -> None:
        first = self.start(mode="hang")
        self.open_queue(first)
        self.submit(first, "req-crash")

        deadline = time.time() + REPLY_TIMEOUT
        while not self.marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.marker.exists(), "the brain was never asked")

        # Mid-generation, the process dies. Nothing gets to say goodbye.
        self.crash(first)
        self.assertEqual(
            self.saved_state()["payload"]["records"][0]["state"],
            "running",
        )
        self.marker.unlink()

        second = self.start(mode="answer")
        second.send({"command": "queue_list"})
        interrupted = self.update_for(second, "req-crash", "interrupted")
        self.assertIs(interrupted["resumable"], True)

        view = self.view_where(
            second,
            lambda view: bool(view["tasks"]),
        )
        self.assertEqual(view["tasks"][0]["state"], "interrupted")

        # Several ticks pass on a free machine. It is not run again unasked.
        time.sleep(1.5)
        later = self.events_until_pong(second)
        phases = [
            json.loads(event["message"]).get("phase")
            for event in later
            if event["eventType"] == "queue_conversation_update"
        ]
        self.assertNotIn("running", phases)
        self.assertNotIn("answered", phases)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.answers_in_history("req-crash"), [])

        second.send(
            {
                "command": "queue_resume_task",
                "taskId": view["tasks"][0]["taskId"],
            }
        )
        self.update_for(second, "req-crash", "answered")

        self.assertEqual(len(self.answers_in_history("req-crash")), 1)
        self.stop(second)


class TestACrashAfterTheAnswerWasSaved(RestartTestCase):
    def test_the_restarted_bridge_does_not_answer_twice(self) -> None:
        first = self.start(QRONOS_TEST_CRASH_AFTER_ANSWER="1")
        self.open_queue(first)
        self.submit(first, "req-saved")

        # It dies on its own, right after the answer reached the history and
        # before the queue could record that.
        self.assertEqual(first.process.wait(timeout=60), 3)
        self.assertEqual(len(self.answers_in_history("req-saved")), 1)
        self.assertEqual(
            self.saved_state()["payload"]["records"][0]["state"],
            "running",
        )

        second = self.start()
        view = self.open_queue(second)

        # Recognised as answered: not interrupted, not queued, not re-run.
        self.assertEqual(view["tasks"], [])
        self.assertEqual(view["recent"][0]["requestId"], "req-saved")
        self.assertEqual(view["recent"][0]["state"], "done")

        time.sleep(1.0)
        self.events_until_pong(second)
        self.assertEqual(len(self.answers_in_history("req-saved")), 1)

        # And it cannot be queued again under the same id.
        second.send(
            {
                "command": "queue_submit",
                "text": QUESTION,
                "requestId": "req-saved",
            }
        )
        refused = second.read_until("runtime_error")
        self.assertIsNotNone(refused)
        self.assertIn("already queued", refused["message"])
        self.stop(second)

        self.assertEqual(len(self.answers_in_history("req-saved")), 1)
        self.assertEqual(
            self.saved_state()["payload"]["records"][0]["state"],
            "done",
        )


class TestCancellationIsRemembered(RestartTestCase):
    def test_a_cancelled_request_does_not_come_back(self) -> None:
        first = self.start()
        self.open_queue(first)
        self.hold(first, True)
        self.submit(first, "req-cancel")
        view = self.view_where(first, lambda view: bool(view["tasks"]))
        first.send(
            {"command": "queue_cancel", "taskId": view["tasks"][0]["taskId"]}
        )
        self.update_for(first, "req-cancel", "cancelled")
        self.stop(first)

        second = self.start()
        view = self.open_queue(second)
        self.hold(second, False)

        self.assertEqual(view["tasks"], [])
        self.assertEqual(view["recent"][0]["requestId"], "req-cancel")
        self.assertEqual(view["recent"][0]["state"], "cancelled")
        self.assertEqual(self.answers_in_history("req-cancel"), [])
        self.stop(second)


class TestDuplicateIdsAcrossARestart(RestartTestCase):
    def test_a_request_id_cannot_be_queued_again(self) -> None:
        first = self.start()
        self.open_queue(first)
        self.hold(first, True)
        self.submit(first, "req-dup")
        self.stop(first)

        second = self.start()
        self.open_queue(second)
        second.send(
            {
                "command": "queue_submit",
                "text": "something else",
                "requestId": "req-dup",
            }
        )
        refused = second.read_until("runtime_error")

        self.assertIsNotNone(refused)
        self.assertIn("already queued", refused["message"])
        self.stop(second)


class TestStorageItCannotTrust(RestartTestCase):
    def assert_left_untouched(self, content: str, problem: str) -> None:
        path = self.state / "queue_state.json"
        path.write_text(content, encoding="utf-8")

        bridge = self.start()
        bridge.send({"command": "queue_list"})
        reported = bridge.read_until("queue_storage_problem")

        self.assertIsNotNone(reported)
        self.assertEqual(json.loads(reported["message"])["problem"], problem)

        self.hold(bridge, True)
        queued = self.submit(bridge, "req-memory")
        self.assertIs(queued["durable"], False)
        view = self.open_queue(bridge)
        self.assertIs(view["storage"]["durable"], False)
        self.stop(bridge)

        self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_a_corrupt_file_is_reported_and_preserved(self) -> None:
        self.assert_left_untouched("{ damaged beyond reading", "corrupt")

    def test_a_newer_file_is_reported_and_preserved(self) -> None:
        self.assert_left_untouched(
            json.dumps({"schemaVersion": 99, "payload": {"records": []}}),
            "too_new",
        )

    def test_an_unwritable_location_is_reported(self) -> None:
        # A file where the queue's folder would have to be.
        blocker = self.state / "not-a-folder"
        blocker.write_text("x", encoding="utf-8")

        bridge = self.start(
            QRONOS_QUEUE_STATE_PATH=str(blocker / "queue_state.json"),
        )
        bridge.send({"command": "queue_list"})
        reported = bridge.read_until("queue_storage_problem")

        self.assertIsNotNone(reported)
        self.assertEqual(
            json.loads(reported["message"])["problem"],
            "unwritable",
        )

        self.hold(bridge, True)
        queued = self.submit(bridge, "req-unsaved")
        self.assertIs(queued["durable"], False)
        self.stop(bridge)

    def test_an_unreadable_location_is_reported_and_preserved(self) -> None:
        # A folder where the file should be: the disk will not read it.
        blocker = self.state / "queue_state.json"
        blocker.mkdir()
        (blocker / "keep.txt").write_text("keep", encoding="utf-8")

        bridge = self.start()
        bridge.send({"command": "queue_list"})
        reported = bridge.read_until("queue_storage_problem")

        self.assertIsNotNone(reported)
        self.assertEqual(
            json.loads(reported["message"])["problem"],
            "unreadable",
        )
        self.stop(bridge)

        self.assertEqual(
            (blocker / "keep.txt").read_text(encoding="utf-8"),
            "keep",
        )


if __name__ == "__main__":
    unittest.main()
