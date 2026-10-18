"""
Shared safety rails for multi-step application workflows.

A workflow such as "download the newest file from one Telegram contact" or
"print one exact file" is several observations followed by exactly one
external side effect. Two things go wrong in that shape, and this module is
where each one is stopped:

    The world changes between looking and acting. Every observation is kept as
    :class:`WindowEvidence` (exact HWND, process instance, geometry/DPI layout
    token, optional capture digest and its age). Before a side effect the
    evidence is revalidated; a different process instance, a moved or resized
    window, or evidence older than its limit stops the step.

    A yes arrives for something else. A person approves a *plan*: the exact
    contact, attachment, file, printer and options. :func:`plan_digest` makes
    that plan a stable identity and :class:`WorkflowApproval` binds the
    approval to it and to the window identity it was shown against. An
    approval for another plan, another window, or one that was revoked by
    Stop/Pause/shutdown is refused, however late it arrives.

Nothing here touches an application. Adapters call these rails between their
own observations and actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import time
from typing import Any, Callable, Mapping

from core.action_cancellation import (
    ActionCancelled,
    CancellationToken,
)
from core.window_binding import (
    WindowBinder,
    WindowBinding,
    WindowBindingStale,
    WindowSnapshot,
)
from core.actions import ActionRequest
from security.gate import ActionGrant, ActionGrantUnavailable, evaluate, issue_grant
from security.permissions import PermissionDecision


DEFAULT_MAX_EVIDENCE_AGE_SECONDS = 20.0

# Fixed vocabulary for why a workflow stopped; reported to the UI and audit.
STOP_CANCELLED = "cancelled"
STOP_APPROVAL_REVOKED = "approval_revoked"
STOP_WINDOW_CHANGED = "window_changed"
STOP_EVIDENCE_EXPIRED = "evidence_expired"


class WorkflowError(RuntimeError):
    """Base class for workflow failures."""


class WorkflowStopped(WorkflowError):
    """Stop, Pause, shutdown, revocation or a changed window ended the workflow.

    Raised only at a checkpoint, so nothing past that checkpoint was attempted.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(detail or reason)


class WorkflowRefused(WorkflowError):
    """A precondition failed. Nothing external was attempted."""


class WorkflowAmbiguous(WorkflowRefused):
    """More than one candidate matched; the workflow will not guess."""


class WorkflowUncertain(WorkflowError):
    """A side effect may have happened but could not be confirmed."""


class ApprovalMismatch(WorkflowError):
    """An approval does not belong to this exact plan and evidence."""


# --------------------------------------------------------------- evidence


@dataclass(frozen=True)
class WindowEvidence:
    """One timestamped observation of the exact window a workflow acts in."""

    app_id: str
    hwnd: int
    pid: int
    process_path: str
    process_start_time_100ns: int
    identity_token: str
    layout_token: str
    observed_at: float
    capture_sha256: str | None = None

    @classmethod
    def from_binding(
        cls,
        binding: WindowBinding,
        *,
        clock: Callable[[], float] = time.monotonic,
        capture_png: bytes | None = None,
    ) -> "WindowEvidence":
        if not isinstance(binding, WindowBinding):
            raise TypeError("Window evidence needs a WindowBinding.")
        snapshot = binding.snapshot
        return cls(
            app_id=binding.app_id,
            hwnd=int(snapshot.hwnd),
            pid=int(snapshot.pid),
            process_path=str(snapshot.process_path),
            process_start_time_100ns=int(snapshot.process_start_time_100ns),
            identity_token=binding.identity_token,
            layout_token=binding.layout_token,
            observed_at=float(clock()),
            capture_sha256=(
                hashlib.sha256(capture_png).hexdigest()
                if capture_png is not None
                else None
            ),
        )

    def age(self, clock: Callable[[], float] = time.monotonic) -> float:
        return max(0.0, float(clock()) - self.observed_at)

    def to_json(self) -> dict[str, Any]:
        """Identity only; never the capture bytes or the window title."""
        return {
            "appId": self.app_id,
            "hwnd": self.hwnd,
            "pid": self.pid,
            "identityToken": self.identity_token,
            "layoutToken": self.layout_token,
            "captureSha256": self.capture_sha256,
        }


def plan_digest(plan: Mapping[str, Any]) -> str:
    """Stable identity of an approved plan (canonical JSON, SHA-256)."""
    if not isinstance(plan, Mapping) or not plan:
        raise ValueError("A workflow plan must be a non-empty mapping.")
    try:
        raw = json.dumps(
            plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"A workflow plan must be plain JSON data: {error}.") from error
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- approval


@dataclass(frozen=True)
class WorkflowApproval:
    """A person's answer, bound to one plan and the window it was shown for."""

    approval_id: str
    plan_digest: str
    decision: PermissionDecision
    evidence_identity_token: str | None = None

    def require_for(
        self,
        *,
        plan: Mapping[str, Any],
        required: PermissionDecision,
        evidence: WindowEvidence | None = None,
    ) -> None:
        if not str(self.approval_id or "").strip():
            raise ApprovalMismatch("The approval has no identity.")
        if self.decision is not required:
            raise ApprovalMismatch(
                f"This step needs {required.value}; the answer was {self.decision.value}."
            )
        if self.plan_digest != plan_digest(plan):
            raise ApprovalMismatch("The approval was given for a different plan.")
        if evidence is not None:
            if self.evidence_identity_token != evidence.identity_token:
                raise ApprovalMismatch(
                    "The approval was given for a different window or process."
                )
        elif self.evidence_identity_token is not None:
            raise ApprovalMismatch("The approval names a window this step does not use.")


# Strength order for consent tiers: a stronger answer covers a weaker need.
_DECISION_RANK = {
    PermissionDecision.ALLOW: 0,
    PermissionDecision.REQUIRE_VOICE_CONFIRMATION: 1,
    PermissionDecision.REQUIRE_UI_CONFIRMATION: 2,
    PermissionDecision.REQUIRE_TYPED_SECRET: 3,
}


def grant_for(request: ActionRequest, approved: PermissionDecision) -> ActionGrant:
    """Issue a grant when the person's answer is at least as strong as needed."""
    verdict = evaluate(request)
    if verdict.refused:
        raise WorkflowRefused(verdict.reason)
    if verdict.allowed:
        return issue_grant(request, verdict)
    needed = _DECISION_RANK.get(verdict.decision)
    given = _DECISION_RANK.get(approved)
    if needed is None or given is None or given < needed:
        raise ApprovalMismatch(
            f"This step needs {verdict.decision.value}; the answer was {approved.value}."
        )
    try:
        return issue_grant(request, verdict, approval=verdict.decision)
    except ActionGrantUnavailable as error:
        raise WorkflowRefused(str(error)) from error


# ------------------------------------------------------------------ guard


@dataclass(frozen=True)
class StepRecord:
    step: str
    at: float
    detail: str = ""


class StepGuard:
    """
    Checks run before every observation-dependent step and every side effect.

    ``approval_valid`` answers whether the approval identity is still open
    (not revoked by Stop/Pause/shutdown). ``token`` is the action's
    cancellation token. ``binding``/``binder`` revalidate the exact window.
    """

    def __init__(
        self,
        *,
        token: CancellationToken | None,
        approval_valid: Callable[[], bool],
        binder: WindowBinder | None = None,
        binding: WindowBinding | None = None,
        evidence: WindowEvidence | None = None,
        max_evidence_age: float = DEFAULT_MAX_EVIDENCE_AGE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (binding is None) != (binder is None):
            raise ValueError("A window binding needs its binder, and the reverse.")
        age = float(max_evidence_age)
        if not math.isfinite(age) or age <= 0:
            raise ValueError("max_evidence_age must be a finite positive number.")
        self._token = token
        self._approval_valid = approval_valid
        self._binder = binder
        self._binding = binding
        self._evidence = evidence
        self._max_age = age
        self._clock = clock
        self._steps: list[StepRecord] = []

    @property
    def steps(self) -> tuple[StepRecord, ...]:
        return tuple(self._steps)

    def refresh_evidence(self, evidence: WindowEvidence) -> None:
        """Adopt a newer observation of the same window."""
        if self._evidence is not None and evidence.identity_token != self._evidence.identity_token:
            raise WorkflowStopped(
                STOP_WINDOW_CHANGED,
                "The new observation belongs to a different window or process.",
            )
        self._evidence = evidence

    def checkpoint(
        self,
        step: str,
        *,
        require_foreground: bool = False,
        require_same_layout: bool = False,
    ) -> WindowSnapshot | None:
        """Stop unless everything the next step relies on is still true."""
        try:
            if self._token is not None:
                self._token.raise_if_cancelled()
        except ActionCancelled as cancelled:
            raise WorkflowStopped(
                STOP_CANCELLED,
                f"Stopped before {step} ({getattr(cancelled, 'reason', 'cancelled')}).",
            ) from cancelled
        if not self._approval_valid():
            raise WorkflowStopped(
                STOP_APPROVAL_REVOKED,
                f"The approval was revoked before {step}.",
            )
        if self._evidence is not None and self._evidence.age(self._clock) > self._max_age:
            raise WorkflowStopped(
                STOP_EVIDENCE_EXPIRED,
                f"What Qronos observed is too old to act on before {step}.",
            )
        fresh: WindowSnapshot | None = None
        if self._binding is not None and self._binder is not None:
            try:
                fresh = self._binder.revalidate(
                    self._binding,
                    require_foreground=require_foreground,
                    require_same_layout=require_same_layout,
                )
            except WindowBindingStale as stale:
                raise WorkflowStopped(STOP_WINDOW_CHANGED, str(stale)) from stale
        self._steps.append(StepRecord(step=step, at=float(self._clock())))
        return fresh


@dataclass(frozen=True)
class WorkflowReport:
    """What a workflow did, for the person and the audit trail."""

    outcome: str  # "succeeded", "refused", "stopped", "uncertain", "failed"
    detail: str
    steps: tuple[StepRecord, ...] = field(default_factory=tuple)
    result: Mapping[str, Any] = field(default_factory=dict)
