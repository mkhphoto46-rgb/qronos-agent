from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.action_audit import (
    MAX_SUMMARY_CHARS,
    MAX_TARGET_CHARS,
    ActionAuditLog,
    AuditEvent,
)
from core.actions import ActionOutcome, ActionRequest, ActionResult
from security.gate import evaluate
from security.permissions import ActionCategory, PermissionDecision


class FakeClock:
    def __init__(self, initial_time: float = 1000.0) -> None:
        self.current_time = initial_time

    def __call__(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


def a_request(**overrides: object) -> ActionRequest:
    fields: dict = {
        "category": ActionCategory.OPEN_APPLICATION,
        "target": "Premiere",
        "summary": "Open Premiere Pro",
    }
    fields.update(overrides)

    return ActionRequest(**fields)  # type: ignore[arg-type]


class TestRecording(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.log = ActionAuditLog(path=None, clock=self.clock)

    def test_a_record_carries_the_action_identity(self) -> None:
        request = a_request()

        record = self.log.record(AuditEvent.DECIDED, request)

        self.assertEqual(record.action_id, request.action_id)
        self.assertEqual(record.category, "open_application")

    def test_records_are_kept_in_order(self) -> None:
        request = a_request()

        self.log.record(AuditEvent.DECIDED, request)
        self.clock.advance(5)
        self.log.record(AuditEvent.COMPLETED, request)

        self.assertEqual(
            [record.event for record in self.log.records()],
            [AuditEvent.DECIDED, AuditEvent.COMPLETED],
        )

    def test_one_action_can_be_followed_through_the_trail(self) -> None:
        first = a_request()
        second = a_request(target="Photoshop")

        self.log.record(AuditEvent.DECIDED, first)
        self.log.record(AuditEvent.DECIDED, second)
        self.log.record(AuditEvent.COMPLETED, first)

        self.assertEqual(len(self.log.for_action(first.action_id)), 2)

    def test_counting_by_event(self) -> None:
        self.log.record(AuditEvent.DECIDED, a_request())
        self.log.record(AuditEvent.DECIDED, a_request())

        self.assertEqual(self.log.count(AuditEvent.DECIDED), 2)


class TestFreeTextCannotReachTheLog(unittest.TestCase):
    """
    The rule the link audit log established, applied here.

    An audit file that accumulates user content becomes a second copy of the
    user's data, sitting outside every control that governs the real one. The
    fields are fixed by the action schema, and the two that look like they
    might take arbitrary text will not.
    """

    def setUp(self) -> None:
        self.log = ActionAuditLog(path=None, clock=FakeClock())

    def test_an_outcome_must_be_an_enum(self) -> None:
        with self.assertRaises(TypeError):
            self.log.record(
                AuditEvent.DECIDED,
                a_request(),
                outcome="the user said it was fine",  # type: ignore[arg-type]
            )

    def test_a_decision_must_be_an_enum(self) -> None:
        with self.assertRaises(TypeError):
            self.log.record(
                AuditEvent.DECIDED,
                a_request(),
                decision="probably allowed",  # type: ignore[arg-type]
            )

    def test_a_long_summary_is_clipped(self) -> None:
        record = self.log.record(
            AuditEvent.DECIDED,
            a_request(summary="x" * 5000),
        )

        self.assertLessEqual(len(record.summary), MAX_SUMMARY_CHARS)

    def test_a_long_target_is_clipped(self) -> None:
        # Targets are paths and URLs, which is exactly the kind of field that
        # turns a log into a record of what the user was working on.
        record = self.log.record(
            AuditEvent.DECIDED,
            a_request(target="C:/" + "deep/" * 500),
        )

        self.assertLessEqual(len(record.target), MAX_TARGET_CHARS)

    def test_newlines_cannot_split_a_record(self) -> None:
        record = self.log.record(
            AuditEvent.DECIDED,
            a_request(summary="first\nsecond"),
        )

        self.assertEqual(len(record.summary.splitlines()), 1)


class TestGateIntegration(unittest.TestCase):
    def test_the_log_can_be_the_gate_audit_sink(self) -> None:
        # Wiring the trail in should be one argument, not a wrapper at every
        # call site, or it will be forgotten at one of them.
        log = ActionAuditLog(path=None, clock=FakeClock())
        request = a_request(category=ActionCategory.CONVERSATION)

        evaluate(request, audit=log.record_verdict)

        record = log.records()[0]

        self.assertEqual(record.event, AuditEvent.DECIDED)
        self.assertEqual(record.decision, PermissionDecision.ALLOW.value)

    def test_a_refusal_is_recorded(self) -> None:
        log = ActionAuditLog(path=None, clock=FakeClock())

        evaluate(
            a_request(category=ActionCategory.CYBER_ATTACK),
            audit=log.record_verdict,
        )

        self.assertEqual(
            log.records()[0].outcome,
            ActionOutcome.REFUSED.value,
        )

    def test_a_result_can_be_recorded(self) -> None:
        log = ActionAuditLog(path=None, clock=FakeClock())
        request = a_request()

        log.record_result(
            request,
            ActionResult(
                action_id=request.action_id,
                outcome=ActionOutcome.SUCCEEDED,
            ),
        )

        self.assertEqual(log.records()[0].event, AuditEvent.COMPLETED)


class TestTheFile(unittest.TestCase):
    def test_records_reach_disk_as_one_line_each(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "action_audit.jsonl"
            log = ActionAuditLog(path=path, clock=FakeClock())

            log.record(AuditEvent.DECIDED, a_request())
            log.record(AuditEvent.COMPLETED, a_request())

            self.assertEqual(
                len(path.read_text(encoding="utf-8").splitlines()),
                2,
            )

    def test_an_unwritable_path_does_not_break_the_action(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            blocker = Path(name) / "blocked"
            blocker.write_text("i am a file", encoding="utf-8")

            log = ActionAuditLog(
                path=blocker / "audit.jsonl",
                clock=FakeClock(),
            )

            record = log.record(AuditEvent.DECIDED, a_request())

            self.assertTrue(record.action_id)
            self.assertEqual(len(log.records()), 1)


if __name__ == "__main__":
    unittest.main()
