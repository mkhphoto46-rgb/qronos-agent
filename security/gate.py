"""
The single place an action is allowed, or is not.

``security/permissions.py`` decides policy: which category needs which
authorization level. It has been correct and tested for a while, and it is not
what this module changes. What was missing is a chokepoint — one function every
executor must call, so that "no action bypasses permissions" is a property of
the code rather than a rule people remember.

Built before any executor exists, deliberately. Adding a gate to executors that
already work means auditing every call site and hoping none was missed; adding
executors to a gate that already exists means there is nowhere else for them to
go.

Three rules:

    Deny by default. A category the policy table does not cover is refused, not
    allowed. A category can only be added to Qronos by adding it to the policy,
    which is where somebody has to think about it.

    Approval is not permission. A decision that requires a human comes back as
    ``AWAITING_APPROVAL``, never as an allow with a note attached. The caller
    cannot proceed on it by forgetting to check a flag, because there is no
    flag: the outcome is a different value.

    Every decision is recorded, including the refusals. A denied action is the
    more interesting audit event, not the less. Recording is not left to each
    caller remembering an argument — see :func:`set_default_audit_sink`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core.actions import ActionOutcome, ActionRequest, ActionResult
from security.permissions import (
    ACTION_POLICIES,
    ActionCategory,
    PermissionDecision,
    PermissionPolicy,
    evaluate_action,
    get_permission_policy,
)


# What the gate does with each decision the permission engine can return.
# Written out rather than derived, so that a decision added later has no
# default and fails the exhaustiveness test below instead of quietly picking
# one. The link layer learned this the hard way: an else branch there turned
# two new decisions into "allowed".
_OUTCOME_FOR_DECISION: dict[PermissionDecision, ActionOutcome] = {
    PermissionDecision.ALLOW: ActionOutcome.SUCCEEDED,
    PermissionDecision.REQUIRE_VOICE_CONFIRMATION: (
        ActionOutcome.AWAITING_APPROVAL
    ),
    PermissionDecision.REQUIRE_UI_CONFIRMATION: (
        ActionOutcome.AWAITING_APPROVAL
    ),
    PermissionDecision.REQUIRE_TYPED_SECRET: ActionOutcome.AWAITING_APPROVAL,
    PermissionDecision.DENY: ActionOutcome.REFUSED,
}


# Called with the request and the verdict, for the audit trail. A plain
# callable rather than an interface so a caller can pass a bound method, a
# lambda in a test, or nothing at all.
AuditSink = Callable[["Verdict"], None]


#: Used when a caller passes no sink of its own. None means no default, which
#: is the state at import: nothing is assumed about where audit records should
#: go until the application says.
_default_audit_sink: AuditSink | None = None


def set_default_audit_sink(sink: AuditSink | None) -> AuditSink | None:
    """
    Install the sink used when a caller passes none. Returns the previous one.

    The audit argument on :func:`evaluate` started out optional, which made
    "every decision is recorded" a claim about discipline rather than about the
    code: an executor that forgot the argument produced no trail, silently, and
    the omission looked exactly like a call that was never made.

    Wiring it once at startup makes recording the default and forgetting the
    exception. It is a module-level setting rather than a hidden global because
    the previous value comes back, so a test can install a sink and restore
    what was there.
    """
    global _default_audit_sink

    previous = _default_audit_sink
    _default_audit_sink = sink

    return previous


def _sink_for(audit: AuditSink | None) -> AuditSink | None:
    """An explicit sink wins; otherwise whatever the application installed."""
    return audit if audit is not None else _default_audit_sink


class ActionRefused(Exception):
    """An action was not permitted to run."""

    def __init__(self, verdict: "Verdict") -> None:
        super().__init__(verdict.reason)
        self.verdict = verdict


@dataclass(frozen=True)
class Verdict:
    """What the gate decided about one action, and why."""

    request: ActionRequest
    decision: PermissionDecision
    outcome: ActionOutcome
    reason: str
    policy: PermissionPolicy | None = None

    @property
    def allowed(self) -> bool:
        """True only when the action may run right now, unaided."""
        return self.outcome is ActionOutcome.SUCCEEDED

    @property
    def needs_approval(self) -> bool:
        """True when a human could still let this through."""
        return self.outcome is ActionOutcome.AWAITING_APPROVAL

    @property
    def refused(self) -> bool:
        """True when no approval would help."""
        return self.outcome is ActionOutcome.REFUSED

    @property
    def reversible(self) -> bool:
        """
        Whether the policy considers this action undoable.

        Unknown categories are treated as irreversible. Guessing the other way
        would tell the undo journal it can roll something back that it cannot.
        """
        return self.policy is not None and self.policy.reversible

    def describe(self) -> str:
        return f"{self.request.describe()} -> {self.outcome.value}: {self.reason}"

    def as_result(self) -> ActionResult:
        """The result to record when the action is not going to run."""
        return ActionResult(
            action_id=self.request.action_id,
            outcome=self.outcome,
            detail=self.reason,
        )


def evaluate(
    request: ActionRequest,
    audit: AuditSink | None = None,
) -> Verdict:
    """
    Decide whether one action may run. This is the only sanctioned route.

    Returns a verdict rather than raising, because two of the three answers are
    not errors: an action awaiting approval is a normal step in a flow that
    continues once a person responds. Callers that want the strict form use
    :func:`require`.
    """
    policy = _policy_for(request.category)

    if policy is None:
        verdict = Verdict(
            request=request,
            decision=PermissionDecision.DENY,
            outcome=ActionOutcome.REFUSED,
            reason=(
                f"No policy covers {request.category.value}, "
                "so the action is refused."
            ),
            policy=None,
        )
    else:
        decision = evaluate_action(request.category)
        outcome = _OUTCOME_FOR_DECISION.get(decision)

        if outcome is None:
            # A decision the gate has never seen. Refusing is the only safe
            # answer to a value this module was not written against.
            verdict = Verdict(
                request=request,
                decision=decision,
                outcome=ActionOutcome.REFUSED,
                reason=(
                    f"The permission engine returned {decision.value}, "
                    "which the gate does not recognise."
                ),
                policy=policy,
            )
        else:
            verdict = Verdict(
                request=request,
                decision=decision,
                outcome=outcome,
                reason=_reason_for(policy, decision),
                policy=policy,
            )

    sink = _sink_for(audit)

    if sink is not None:
        sink(verdict)

    return verdict


def require(
    request: ActionRequest,
    audit: AuditSink | None = None,
) -> Verdict:
    """
    Allow the action or raise.

    For a caller that has no path for "ask a human" and must not proceed
    without one. Raising rather than returning means the executor cannot run on
    a verdict it forgot to inspect.
    """
    verdict = evaluate(request, audit=audit)

    if not verdict.allowed:
        raise ActionRefused(verdict)

    return verdict


def _policy_for(category: ActionCategory) -> PermissionPolicy | None:
    try:
        return get_permission_policy(category)
    except KeyError:
        return None


def _reason_for(
    policy: PermissionPolicy,
    decision: PermissionDecision,
) -> str:
    if decision is PermissionDecision.ALLOW:
        return f"{policy.category.value} is allowed without approval."

    if decision is PermissionDecision.DENY:
        return f"{policy.category.value} is forbidden by policy."

    return (
        f"{policy.category.value} requires "
        f"{decision.value.removeprefix('require_').replace('_', ' ')}."
    )


def uncovered_categories() -> tuple[ActionCategory, ...]:
    """
    Categories with no policy behind them.

    Should always be empty. It is a function rather than an assertion at import
    so the test suite can state the invariant, and so adding a category without
    a policy fails a test rather than the application.
    """
    return tuple(
        category
        for category in ActionCategory
        if category not in ACTION_POLICIES
    )
