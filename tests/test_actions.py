from __future__ import annotations

import unittest

from core.actions import (
    ActionOutcome,
    ActionRequest,
    ActionResult,
    InvalidAction,
    from_line,
    new_action_id,
    to_line,
)
from security.permissions import ActionCategory


def a_request(**overrides: object) -> ActionRequest:
    fields: dict = {
        "category": ActionCategory.OPEN_APPLICATION,
        "target": "Premiere",
        "summary": "Open Premiere Pro",
    }
    fields.update(overrides)

    return ActionRequest(**fields)  # type: ignore[arg-type]


class TestActionRequestValidation(unittest.TestCase):
    def test_a_well_formed_action_is_accepted(self) -> None:
        request = a_request()

        self.assertEqual(request.category, ActionCategory.OPEN_APPLICATION)
        self.assertTrue(request.action_id)

    def test_a_category_is_required_to_be_a_category(self) -> None:
        # A string here would sail past the gate's policy lookup and land in
        # the "no policy covers this" branch, which reads like a missing
        # policy rather than a caller mistake.
        with self.assertRaises(InvalidAction):
            ActionRequest(
                category="open_application",  # type: ignore[arg-type]
                target="Premiere",
                summary="Open Premiere Pro",
            )

    def test_a_target_is_required(self) -> None:
        for target in ("", "   "):
            with self.subTest(target=target):
                with self.assertRaises(InvalidAction):
                    a_request(target=target)

    def test_a_summary_is_required(self) -> None:
        # The summary is what a person is shown when asked to approve. An
        # action nobody can describe is an action nobody can consent to.
        with self.assertRaises(InvalidAction):
            a_request(summary="  ")

    def test_ids_are_distinct(self) -> None:
        self.assertNotEqual(new_action_id(), new_action_id())

    def test_an_action_is_immutable(self) -> None:
        # It is logged before it runs. If it could change afterwards the audit
        # trail would describe something other than what happened.
        request = a_request()

        with self.assertRaises(Exception):
            request.target = "Photoshop"  # type: ignore[misc]


class TestParameterValidation(unittest.TestCase):
    def test_flat_json_safe_values_are_kept(self) -> None:
        request = a_request(
            parameters={"path": "C:/x.txt", "count": 3, "force": False}
        )

        self.assertEqual(request.parameters["count"], 3)

    def test_a_nested_value_is_refused(self) -> None:
        # Refused at construction rather than at serialisation. An action that
        # cannot be written to the audit trail must not exist, or the failure
        # arrives after the decision to run it has been taken.
        for value in ({"a": 1}, [1, 2], object()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(InvalidAction):
                    a_request(parameters={"bad": value})

    def test_a_non_string_name_is_refused(self) -> None:
        with self.assertRaises(InvalidAction):
            a_request(parameters={1: "one"})


class TestRoundTrip(unittest.TestCase):
    def test_an_action_survives_a_round_trip(self) -> None:
        original = a_request(parameters={"path": "C:/x.txt"})

        restored = from_line(to_line(original))

        self.assertEqual(restored, original)

    def test_persian_survives_a_round_trip(self) -> None:
        original = a_request(summary="پریمیر را باز کن", target="پریمیر")

        self.assertEqual(from_line(to_line(original)), original)

    def test_a_wrong_schema_version_is_refused(self) -> None:
        # Refusing beats guessing. A journal written by a future version holds
        # fields this one would silently drop.
        data = a_request().to_json()
        data["schemaVersion"] = 99

        with self.assertRaises(InvalidAction):
            ActionRequest.from_json(data)

    def test_an_unknown_category_is_refused(self) -> None:
        data = a_request().to_json()
        data["category"] = "make_coffee"

        with self.assertRaises(InvalidAction):
            ActionRequest.from_json(data)

    def test_a_missing_field_is_refused(self) -> None:
        data = a_request().to_json()
        del data["target"]

        with self.assertRaises(InvalidAction):
            ActionRequest.from_json(data)

    def test_malformed_json_is_refused(self) -> None:
        with self.assertRaises(InvalidAction):
            from_line("{not json")

    def test_a_bare_value_is_not_an_action(self) -> None:
        with self.assertRaises(InvalidAction):
            from_line('"open premiere"')

    def test_a_line_stays_one_line(self) -> None:
        # The journal is line-oriented. A summary containing a newline would
        # otherwise split one entry into two, the second unparseable.
        line = to_line(a_request(summary="first\nsecond"))

        self.assertEqual(len(line.splitlines()), 1)


class TestActionResult(unittest.TestCase):
    def test_only_a_run_action_counts_as_having_run(self) -> None:
        for outcome, expected in (
            (ActionOutcome.SUCCEEDED, True),
            (ActionOutcome.FAILED, True),
            (ActionOutcome.REFUSED, False),
            (ActionOutcome.AWAITING_APPROVAL, False),
        ):
            with self.subTest(outcome=outcome):
                result = ActionResult(action_id="a", outcome=outcome)

                self.assertEqual(result.ran, expected)

    def test_a_result_survives_a_round_trip(self) -> None:
        original = ActionResult(
            action_id="abc",
            outcome=ActionOutcome.FAILED,
            detail="the application did not start",
        )

        self.assertEqual(
            ActionResult.from_json(original.to_json()),
            original,
        )

    def test_a_malformed_result_is_refused(self) -> None:
        with self.assertRaises(InvalidAction):
            ActionResult.from_json(
                {"schemaVersion": 1, "actionId": "a", "outcome": "maybe"}
            )


if __name__ == "__main__":
    unittest.main()
