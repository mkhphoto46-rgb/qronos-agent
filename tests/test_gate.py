"""
The chokepoint every action has to pass through.

The interesting tests here are not the happy ones. They are the ones that fail
open: a category nobody wrote a policy for, a permission decision added after
this code was written, an executor that reads a verdict carelessly. Each of
those turns "Qronos asks before it acts" into "Qronos acts", and none of them
looks like a bug at the call site.
"""

from __future__ import annotations

import unittest
from enum import Enum

from core.actions import ActionOutcome, ActionRequest
from security import gate
from security.gate import ActionRefused, Verdict, evaluate, require
from security.permissions import (
    ActionCategory,
    PermissionDecision,
    PermissionLevel,
    get_permission_policy,
)


def a_request(category: ActionCategory) -> ActionRequest:
    return ActionRequest(
        category=category,
        target="something",
        summary="do something",
    )


def category_at(level: PermissionLevel) -> ActionCategory:
    """Any category whose policy sits at the given level."""
    for category in ActionCategory:
        if get_permission_policy(category).level is level:
            return category

    raise AssertionError(f"No category is policed at {level}.")


class TestEveryCategoryIsCovered(unittest.TestCase):
    def test_no_category_is_missing_a_policy(self) -> None:
        # Deny-by-default is the safety net, not the plan. A category with no
        # policy is refused, which means a real capability silently stops
        # working — better than the alternative, still a bug.
        self.assertEqual(gate.uncovered_categories(), ())

    def test_every_category_reaches_a_verdict(self) -> None:
        for category in ActionCategory:
            with self.subTest(category=category):
                verdict = evaluate(a_request(category))

                self.assertIsInstance(verdict, Verdict)
                self.assertTrue(verdict.reason)


class TestDecisionsMapToOutcomes(unittest.TestCase):
    def test_auto_allow_runs(self) -> None:
        verdict = evaluate(a_request(category_at(PermissionLevel.AUTO_ALLOW)))

        self.assertTrue(verdict.allowed)
        self.assertFalse(verdict.needs_approval)
        self.assertFalse(verdict.refused)

    def test_forbidden_is_refused(self) -> None:
        verdict = evaluate(a_request(category_at(PermissionLevel.FORBIDDEN)))

        self.assertTrue(verdict.refused)
        self.assertFalse(verdict.allowed)

    def test_every_confirmation_level_awaits_approval_not_allows(self) -> None:
        # The distinction the whole gate rests on. "A human should confirm" is
        # not a weaker kind of yes, and it must not be reachable through a
        # truthy check on the verdict.
        for level in (
            PermissionLevel.VOICE_CONFIRMATION,
            PermissionLevel.UI_CONFIRMATION,
            PermissionLevel.TYPED_SECRET,
        ):
            with self.subTest(level=level):
                verdict = evaluate(a_request(category_at(level)))

                self.assertTrue(verdict.needs_approval)
                self.assertFalse(verdict.allowed)

    def test_the_three_states_are_mutually_exclusive(self) -> None:
        for category in ActionCategory:
            with self.subTest(category=category):
                verdict = evaluate(a_request(category))
                flags = [
                    verdict.allowed,
                    verdict.needs_approval,
                    verdict.refused,
                ]

                self.assertEqual(sum(flags), 1)


class TestUnknownDecisionsFailClosed(unittest.TestCase):
    def test_every_decision_the_engine_can_return_is_mapped(self) -> None:
        # Fails the day a sixth decision is added to the permission engine
        # without deciding what the gate should do with it — which is exactly
        # how the link layer ended up granting two new decisions by accident.
        for decision in PermissionDecision:
            with self.subTest(decision=decision):
                self.assertIn(decision, gate._OUTCOME_FOR_DECISION)

    def test_an_unmapped_decision_is_refused(self) -> None:
        class FutureDecision(Enum):
            REQUIRE_FINGERPRINT = "require_fingerprint"

        original = gate.evaluate_action
        gate.evaluate_action = lambda category: FutureDecision.REQUIRE_FINGERPRINT  # type: ignore[assignment]

        try:
            verdict = evaluate(
                a_request(category_at(PermissionLevel.AUTO_ALLOW))
            )
        finally:
            gate.evaluate_action = original  # type: ignore[assignment]

        self.assertTrue(verdict.refused)
        self.assertIn("does not recognise", verdict.reason)

    def test_a_category_with_no_policy_is_refused(self) -> None:
        class FutureCategory(Enum):
            MAKE_COFFEE = "make_coffee"

        # Bypasses ActionRequest's own validation on purpose: the question is
        # what the gate does when handed a category it has never policed.
        request = a_request(category_at(PermissionLevel.AUTO_ALLOW))
        object.__setattr__(request, "category", FutureCategory.MAKE_COFFEE)

        verdict = evaluate(request)

        self.assertTrue(verdict.refused)
        self.assertIsNone(verdict.policy)


class TestReversibilityIsNotAssumed(unittest.TestCase):
    def test_an_unknown_category_is_treated_as_irreversible(self) -> None:
        # Guessing the other way would tell the undo journal it can roll back
        # something it cannot.
        request = a_request(category_at(PermissionLevel.AUTO_ALLOW))
        object.__setattr__(request, "category", _FakeCategory.UNKNOWN)

        self.assertFalse(evaluate(request).reversible)

    def test_reversibility_comes_from_the_policy(self) -> None:
        category = ActionCategory.IRREVERSIBLE_DESTRUCTION
        expected = get_permission_policy(category).reversible

        self.assertEqual(evaluate(a_request(category)).reversible, expected)


class _FakeCategory(Enum):
    UNKNOWN = "unknown"


class TestRequire(unittest.TestCase):
    def test_an_allowed_action_passes_through(self) -> None:
        verdict = require(a_request(category_at(PermissionLevel.AUTO_ALLOW)))

        self.assertTrue(verdict.allowed)

    def test_a_refused_action_raises(self) -> None:
        with self.assertRaises(ActionRefused) as caught:
            require(a_request(category_at(PermissionLevel.FORBIDDEN)))

        self.assertTrue(caught.exception.verdict.refused)

    def test_an_action_awaiting_approval_raises(self) -> None:
        # require() is for callers with no path for "ask a human". Letting an
        # awaiting-approval verdict through would run the action without one.
        with self.assertRaises(ActionRefused):
            require(a_request(category_at(PermissionLevel.UI_CONFIRMATION)))


class TestAuditSink(unittest.TestCase):
    def test_every_decision_is_offered_to_the_sink(self) -> None:
        seen: list[Verdict] = []

        for category in ActionCategory:
            evaluate(a_request(category), audit=seen.append)

        self.assertEqual(len(seen), len(list(ActionCategory)))

    def test_refusals_are_audited_too(self) -> None:
        # The denied action is the more interesting audit entry: it is the one
        # that says something tried.
        seen: list[Verdict] = []

        evaluate(
            a_request(category_at(PermissionLevel.FORBIDDEN)),
            audit=seen.append,
        )

        self.assertTrue(seen[0].refused)

    def test_the_strict_form_audits_before_raising(self) -> None:
        seen: list[Verdict] = []

        with self.assertRaises(ActionRefused):
            require(
                a_request(category_at(PermissionLevel.FORBIDDEN)),
                audit=seen.append,
            )

        self.assertEqual(len(seen), 1)


class TestTheDefaultSink(unittest.TestCase):
    """
    Recording must not depend on every caller remembering an argument.

    Driving a real action through the seam showed the hole: a call made
    without the audit argument produced no record, and the absence looked
    exactly like a call that was never made. For an audit trail that is the
    worst possible failure, because it is invisible in the artefact whose whole
    job is to be the record.
    """

    def setUp(self) -> None:
        self.seen: list[Verdict] = []
        self.previous = gate.set_default_audit_sink(self.seen.append)

    def tearDown(self) -> None:
        gate.set_default_audit_sink(self.previous)

    def test_a_call_with_no_sink_is_still_recorded(self) -> None:
        evaluate(a_request(ActionCategory.CONVERSATION))

        self.assertEqual(len(self.seen), 1)

    def test_the_strict_form_is_recorded_too(self) -> None:
        with self.assertRaises(ActionRefused):
            require(a_request(category_at(PermissionLevel.FORBIDDEN)))

        self.assertEqual(len(self.seen), 1)

    def test_an_explicit_sink_wins(self) -> None:
        # A caller that wants its own trail gets it, and does not also write to
        # the default, which would double every record.
        mine: list[Verdict] = []

        evaluate(a_request(ActionCategory.CONVERSATION), audit=mine.append)

        self.assertEqual(len(mine), 1)
        self.assertEqual(self.seen, [])

    def test_setting_none_turns_it_off_again(self) -> None:
        gate.set_default_audit_sink(None)

        evaluate(a_request(ActionCategory.CONVERSATION))

        self.assertEqual(self.seen, [])

    def test_the_previous_sink_is_returned_for_restoring(self) -> None:
        # Compared by equality rather than identity: attribute access on a
        # bound method builds a new object every time, so `is` would fail
        # against the very sink that was installed.
        replaced = gate.set_default_audit_sink(None)

        self.assertEqual(replaced, self.seen.append)

    def test_the_default_is_nothing_until_it_is_set(self) -> None:
        # Nothing is assumed at import about where records should go. The
        # application says, and until it does the behaviour is the old one.
        gate.set_default_audit_sink(None)

        self.assertIsNone(gate._sink_for(None))


class TestVerdictShape(unittest.TestCase):
    def test_a_verdict_carries_the_request_it_judged(self) -> None:
        request = a_request(ActionCategory.CONVERSATION)

        self.assertIs(evaluate(request).request, request)

    def test_a_non_running_verdict_becomes_a_result(self) -> None:
        verdict = evaluate(a_request(category_at(PermissionLevel.FORBIDDEN)))
        result = verdict.as_result()

        self.assertEqual(result.outcome, ActionOutcome.REFUSED)
        self.assertFalse(result.ran)

    def test_describe_names_the_action_and_the_outcome(self) -> None:
        description = evaluate(
            a_request(category_at(PermissionLevel.FORBIDDEN))
        ).describe()

        self.assertIn("refused", description)


if __name__ == "__main__":
    unittest.main()
