"""Permission for the media index comes from the Qronos gate, not from callers.

Reading a location the user selected is ``READ_NON_SENSITIVE`` (the existing
"explicitly selected non-sensitive local information" category); writing a
report to a destination the user chose is ``CREATE_OR_EDIT_FILE``. Both are
decided by :func:`security.gate.evaluate` under the user's current Files &
Storage policy (ALLOW, ALWAYS_ASK, DENY or CUSTOM rules). No new permission
level and no new lifetime is invented here.

* **Allowed** by policy: the action runs; the selection is the explicit act.
* **Needs approval**: the exact prepared action (operation, path, depth or
  destination plus the exact content hash and bytes) is held under a
  single-use identity from :class:`core.approval_ledger.ApprovalLedger`.
  Approving that identity re-asks the gate for the *same* request, mints an
  :class:`security.gate.ActionGrant` for it, and runs exactly that action
  once. A second answer, a different path or different content cannot reuse
  it. Without an in-process ledger (the standalone CLI) nothing can wait for
  a person, so the request is refused.
* **Typed secret** (when the effective policy is that strong): approving also
  needs the user's approval secret, checked by
  :class:`core.approval_secret.ApprovalSecretStore`. Without a verifier the
  decision is refused. The secret is a call argument only; it is never put in
  a request, preview, audit record or report, and the CLI has no way to pass it.
* **Denied**: refused.

Background work re-asks the gate for every root on every run. It continues
only while the policy allows reading outright, or while the in-process grant
that approved that root is still active (the gate's own short lifetime) *and*
the root still has the kind and depth that grant approved.
A database row is a record of what happened, never a reason to proceed.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable, Protocol

from core.actions import ActionRequest
from core.approval_ledger import ApprovalKind, ApprovalLedger, StaleApproval
from core.approval_secret import ApprovalSecretError, ApprovalSecretStore, InvalidApprovalSecret
from security.gate import ActionGrant, ActionGrantUnavailable, AuditSink, Verdict, evaluate, issue_grant
from security.permissions import ActionCategory, PermissionDecision

ROOT_OPERATION = "media.add_root"
EXPORT_OPERATION = "media.export_report"


class MediaPermissionRefused(PermissionError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class Approval:
    """How a prepared action was authorised; stored for the record only."""

    action_id: str
    decision: str  # "allow" or "grant:<gate decision>"
    approved_at: float


@dataclass(frozen=True)
class Pending:
    """An action waiting for a person, identified by a single-use id."""

    approval_id: str
    decision: str
    preview: dict[str, Any]


class MediaPermissions(Protocol):
    def request_root(self, path: Path, kind: str, max_depth: int) -> Approval | Pending: ...
    def approve_root(self, approval_id: str, decision: PermissionDecision,
                     secret: str | None = None) -> tuple[dict[str, Any], Approval]: ...
    def recheck_root(self, path: Path, approval: Approval, kind: str, max_depth: int) -> None: ...
    def request_export(self, destination: Path, content: bytes, sha256: str,
                       details: dict[str, Any]) -> Approval | Pending: ...
    def approve_export(self, approval_id: str, decision: PermissionDecision,
                       secret: str | None = None) -> tuple[dict[str, Any], Approval]: ...


@dataclass(frozen=True)
class _RootGrant:
    grant: ActionGrant
    kind: str
    max_depth: int


class GatePermissions:
    """The production adapter: every answer comes from ``security.gate``."""

    def __init__(self, *, audit: AuditSink | None = None, ledger: ApprovalLedger | None = None,
                 secrets: ApprovalSecretStore | None = None, clock: Callable[[], float] = time.time) -> None:
        self.audit = audit
        self.ledger = ledger
        self.secrets = secrets
        self.clock = clock
        # Grants minted in this process for roots, by exact path, with the
        # scope they approved. They expire on the gate's own clock and die
        # with the process.
        self._root_grants: dict[str, _RootGrant] = {}

    # ------------------------------------------------------------ helpers

    def _prepare(self, request: ActionRequest, payload: dict[str, Any], question: str,
                 preview: dict[str, Any]) -> Approval | Pending:
        verdict = evaluate(request, audit=self.audit)
        if verdict.allowed:
            return Approval(request.action_id, "allow", self.clock())
        if verdict.refused:
            raise MediaPermissionRefused("denied", verdict.reason)
        if self.ledger is None:
            raise MediaPermissionRefused(
                "approval_required",
                f"{verdict.decision.value}: approve this in Qronos; a standalone process cannot wait for approval",
            )
        ticket = self.ledger.issue(ApprovalKind.FILE_ACTION, question,
                                   payload={**payload, "request": request})
        return Pending(ticket.approval_id, verdict.decision.value, preview)

    def _verify_secret(self, secret: str | None) -> None:
        if self.secrets is None:
            raise MediaPermissionRefused("typed_secret_unavailable", "no approval-secret verifier in this process")
        if not isinstance(secret, str) or not secret:
            raise MediaPermissionRefused("secret_required")
        try:
            verified = self.secrets.verify(secret)
        except InvalidApprovalSecret:
            verified = False
        except ApprovalSecretError as exc:
            raise MediaPermissionRefused("typed_secret_unavailable", type(exc).__name__) from None
        if not verified:
            raise MediaPermissionRefused("secret_rejected")

    def _take(self, approval_id: str, operation: str, decision: PermissionDecision,
              secret: str | None = None) -> tuple[dict, ActionGrant | None, Verdict]:
        if self.ledger is None:
            raise MediaPermissionRefused("approval_required", "no approval ledger in this process")
        try:
            ticket = self.ledger.take(approval_id, kinds=(ApprovalKind.FILE_ACTION,))
        except StaleApproval as exc:
            raise MediaPermissionRefused("stale_approval", exc.reason) from None
        payload = ticket.payload if isinstance(ticket.payload, dict) else {}
        request = payload.get("request")
        if payload.get("operation") != operation or not isinstance(request, ActionRequest):
            raise MediaPermissionRefused("stale_approval", "approval belongs to another action")
        # Re-ask the gate for the same request: the policy may have changed
        # while the question was on screen.
        verdict = evaluate(request, audit=self.audit)
        if verdict.refused:
            raise MediaPermissionRefused("denied", verdict.reason)
        if verdict.allowed:
            return payload, None, verdict
        if verdict.decision is PermissionDecision.REQUIRE_TYPED_SECRET:
            # issue_grant compares decisions only; the secret is checked here.
            # The identity is already spent, so a wrong secret needs a new request.
            self._verify_secret(secret)
        try:
            grant = issue_grant(request, verdict, approval=decision)
        except (ActionGrantUnavailable, ValueError) as exc:  # the decision does not match the verdict
            raise MediaPermissionRefused("approval_rejected", str(exc)) from None
        return payload, grant, verdict

    # --------------------------------------------------------------- roots

    def request_root(self, path: Path, kind: str, max_depth: int) -> Approval | Pending:
        request = ActionRequest(
            category=ActionCategory.READ_NON_SENSITIVE, target=str(path),
            summary="Index videos and subtitles in this selected location, locally only",
            parameters={"operation": ROOT_OPERATION, "kind": kind, "maxDepth": int(max_depth)},
        )
        payload = {"operation": ROOT_OPERATION, "path": str(path), "kind": kind, "maxDepth": int(max_depth)}
        return self._prepare(request, payload, f"Index videos in {path}?",
                             {"path": str(path), "kind": kind, "maxDepth": int(max_depth)})

    def approve_root(self, approval_id: str, decision: PermissionDecision,
                     secret: str | None = None) -> tuple[dict[str, Any], Approval]:
        payload, grant, verdict = self._take(approval_id, ROOT_OPERATION, decision, secret)
        request: ActionRequest = payload["request"]
        if grant is not None:
            self._root_grants[payload["path"]] = _RootGrant(grant, payload["kind"], int(payload["maxDepth"]))
        label = "allow" if grant is None else f"grant:{verdict.decision.value}"
        return payload, Approval(request.action_id, label, self.clock())

    def recheck_root(self, path: Path, approval: Approval, kind: str, max_depth: int) -> None:
        verdict = evaluate(ActionRequest(
            category=ActionCategory.READ_NON_SENSITIVE, target=str(path),
            summary="Continue indexing a previously selected location, locally only",
            parameters={"operation": "media.recheck_root"},
        ), audit=self.audit)
        if verdict.allowed:
            return
        if verdict.refused:
            raise MediaPermissionRefused("revoked", verdict.reason)
        held = self._root_grants.get(str(path))
        if (held is None or not held.grant.active() or held.grant.action_id != approval.action_id
                or (held.kind, held.max_depth) != (kind, int(max_depth))):
            self._root_grants.pop(str(path), None)
            raise MediaPermissionRefused("approval_required", "the Files policy asks before reading; approve again")

    # -------------------------------------------------------------- export

    def request_export(self, destination: Path, content: bytes, sha256: str,
                       details: dict[str, Any]) -> Approval | Pending:
        request = ActionRequest(
            category=ActionCategory.CREATE_OR_EDIT_FILE, target=str(destination),
            summary="Write a media report to this new file",
            parameters={"operation": EXPORT_OPERATION, "sha256": sha256, "bytes": len(content), **details},
        )
        payload = {"operation": EXPORT_OPERATION, "destination": str(destination), "content": bytes(content),
                   "sha256": sha256, **details}
        preview = {"destination": str(destination), "sha256": sha256, "bytes": len(content),
                   "head": content[:2048].decode("utf-8", "replace"), **details}
        return self._prepare(request, payload, f"Write a media report to {destination}?", preview)

    def approve_export(self, approval_id: str, decision: PermissionDecision,
                       secret: str | None = None) -> tuple[dict[str, Any], Approval]:
        payload, grant, verdict = self._take(approval_id, EXPORT_OPERATION, decision, secret)
        request: ActionRequest = payload["request"]
        label = "allow" if grant is None else f"grant:{verdict.decision.value}"
        return payload, Approval(request.action_id, label, self.clock())
