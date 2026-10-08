"""
Shared safety rails for multi-step application workflows.

A workflow such as "download the newest file from one Telegram contact" or
"print one exact file" is several observations followed by exactly one
external side effect. Three things go wrong in that shape, and this module is
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

    A retry repeats something that already happened. :class:`SideEffectJournal`
    records the intent to perform a side effect durably *before* it happens.
    Once recorded the same side effect is never attempted again, and an
    uncertain outcome blocks any repeat of the same content (same file to the
    same printer, same attachment to the same folder) until a person has
    looked, instead of being "fixed" by trying again.

Nothing here touches an application. Adapters call these rails between their
own observations and actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from core.action_cancellation import (
    ActionCancelled,
    CancellationToken,
)
from core.json_store import JsonStore
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


class DuplicateSideEffect(WorkflowRefused):
    """The same side effect was already attempted or is unresolved."""


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


# ---------------------------------------------------------------- journal


class SideEffectState(str, Enum):
    # Recorded before the side effect starts. If a crash happens now, the
    # outcome is unknown and is treated exactly like UNCERTAIN.
    SUBMITTING = "submitting"
    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"
    # The side effect provably did not start (refused before submission).
    NOT_SUBMITTED = "not_submitted"
    # A person looked at an uncertain outcome and released the content key.
    ACKNOWLEDGED = "acknowledged"


_UNRESOLVED = {SideEffectState.SUBMITTING, SideEffectState.UNCERTAIN}
_JOURNAL_VERSION = 1
_MAX_JOURNAL_ENTRIES = 2048


@dataclass(frozen=True)
class SideEffectEntry:
    operation_key: str
    content_key: str
    state: SideEffectState
    detail: str
    updated_at: float


class SideEffectJournal:
    """
    Durable, single-shot record of external side effects.

    ``operation_key`` identifies one approved attempt (approval identity plus
    plan digest); it can be begun once, ever. ``content_key`` identifies what
    the side effect does (for example the file hash and printer); while any
    attempt for that content is SUBMITTING or UNCERTAIN, a new attempt for the
    same content is refused so an unclear print or download is not repeated.
    """

    def __init__(
        self,
        path: str | Path | None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = JsonStore(path, _JOURNAL_VERSION)
        self._clock = clock
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        payload = self._store.load_payload({"entries": {}})
        entries = payload.get("entries", {})
        if not isinstance(entries, dict):
            raise WorkflowError("The side-effect journal is malformed.")
        return entries

    def _save(self, entries: dict[str, dict[str, Any]]) -> None:
        if len(entries) > _MAX_JOURNAL_ENTRIES:
            # Drop the oldest settled entries; unresolved ones are never dropped.
            settled = sorted(
                (
                    (value.get("updatedAt", 0.0), key)
                    for key, value in entries.items()
                    if SideEffectState(value["state"]) not in _UNRESOLVED
                ),
            )
            for _, key in settled[: len(entries) - _MAX_JOURNAL_ENTRIES]:
                entries.pop(key, None)
        self._store.save({"entries": entries})

    @staticmethod
    def _entry(key: str, value: Mapping[str, Any]) -> SideEffectEntry:
        return SideEffectEntry(
            operation_key=key,
            content_key=str(value["contentKey"]),
            state=SideEffectState(value["state"]),
            detail=str(value.get("detail", "")),
            updated_at=float(value.get("updatedAt", 0.0)),
        )

    def get(self, operation_key: str) -> SideEffectEntry | None:
        with self._lock:
            value = self._load().get(operation_key)
            return None if value is None else self._entry(operation_key, value)

    def unresolved_for(self, content_key: str) -> tuple[SideEffectEntry, ...]:
        with self._lock:
            return tuple(
                self._entry(key, value)
                for key, value in sorted(self._load().items())
                if value.get("contentKey") == content_key
                and SideEffectState(value["state"]) in _UNRESOLVED
            )

    def begin(self, operation_key: str, content_key: str, detail: str = "") -> None:
        """Record SUBMITTING durably, or refuse a repeat."""
        if not operation_key or not content_key:
            raise ValueError("Side effects need operation and content keys.")
        with self._lock:
            entries = self._load()
            if operation_key in entries:
                raise DuplicateSideEffect(
                    "This approved action was already attempted; it is not repeated."
                )
            for key, value in entries.items():
                if (
                    value.get("contentKey") == content_key
                    and SideEffectState(value["state"]) in _UNRESOLVED
                ):
                    raise DuplicateSideEffect(
                        "An earlier attempt with the same content has an unconfirmed "
                        "outcome; it must be checked before trying again."
                    )
            entries[operation_key] = {
                "contentKey": content_key,
                "state": SideEffectState.SUBMITTING.value,
                "detail": detail[:500],
                "updatedAt": float(self._clock()),
            }
            self._save(entries)

    def settle(self, operation_key: str, state: SideEffectState, detail: str = "") -> None:
        if state is SideEffectState.SUBMITTING:
            raise ValueError("SUBMITTING is recorded by begin().")
        with self._lock:
            entries = self._load()
            value = entries.get(operation_key)
            if value is None:
                raise WorkflowError("Cannot settle a side effect that was never begun.")
            current = SideEffectState(value["state"])
            if current not in _UNRESOLVED:
                raise WorkflowError("This side effect is already settled.")
            if state is SideEffectState.ACKNOWLEDGED:
                raise ValueError("Use acknowledge() to release an uncertain outcome.")
            if current is SideEffectState.UNCERTAIN and state is not SideEffectState.CONFIRMED:
                # Later evidence may confirm an unclear outcome, but nothing may
                # turn "it might have happened" into "it did not happen".
                raise WorkflowError("An uncertain side effect can only be confirmed or acknowledged.")
            value["state"] = state.value
            value["detail"] = detail[:500]
            value["updatedAt"] = float(self._clock())
            self._save(entries)

    def acknowledge(self, operation_key: str, detail: str = "") -> None:
        """A person checked an uncertain outcome; the content may be tried again."""
        with self._lock:
            entries = self._load()
            value = entries.get(operation_key)
            if value is None or SideEffectState(value["state"]) is not SideEffectState.UNCERTAIN:
                raise WorkflowError("Only an uncertain outcome can be acknowledged.")
            value["state"] = SideEffectState.ACKNOWLEDGED.value
            value["detail"] = detail[:500]
            value["updatedAt"] = float(self._clock())
            self._save(entries)


def operation_key(approval_id: str, digest: str) -> str:
    """One approved attempt: the approval identity and the plan it approved."""
    raw = f"{approval_id}\0{digest}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def content_key(kind: str, *parts: str) -> str:
    raw = "\0".join((kind, *parts))
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class WorkflowReport:
    """What a workflow did, for the person and the audit trail."""

    outcome: str  # "succeeded", "refused", "stopped", "uncertain", "failed"
    detail: str
    steps: tuple[StepRecord, ...] = field(default_factory=tuple)
    result: Mapping[str, Any] = field(default_factory=dict)
