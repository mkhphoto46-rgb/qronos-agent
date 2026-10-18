"""
Runtime coordination for the application workflows.

Turns one recognised request into an approval prompt and its execution,
each prompt with its own single-use identity from the shared
:class:`core.approval_ledger.ApprovalLedger`:

    Browser   navigate (voice/click) -> open the page and read what it shows

Each answer is honoured only by presenting the identity of the prompt it
answers. Stop, Pause and shutdown revoke every open prompt (``discard``) and
cancel the running step through the shared ActiveActionRegistry, so a late
"yes" can never start or continue a workflow. Every execution runs on a
worker thread inside one cancellable active action.

The desktop sees three events (JSON messages): ``app_workflow_approval_required``,
``app_workflow_finished`` and ``app_workflow_stale``. User-facing text is
Persian; the English technical detail is kept separately for the audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import threading
from typing import Any, Callable

from core.action_audit import ActionAuditLog, AuditEvent
from core.action_cancellation import ActiveActionRegistry
from core.actions import ActionOutcome, ActionRequest
from core.app_workflow import (
    ApprovalMismatch,
    WorkflowAmbiguous,
    WorkflowApproval,
    WorkflowRefused,
    WorkflowReport,
    WorkflowStopped,
    plan_digest,
)
from core.approval_ledger import ApprovalKind, ApprovalLedger, StaleApproval
from core.browser_research import (
    BrowserNavigation,
    BrowserResearchWorkflow,
    NavigationClarification,
    plan_navigation,
)
from security.permissions import ActionCategory, PermissionDecision


APP_WORKFLOW_DECISION_ACTION = "qronos.app_workflow_decision"

PHASE_BROWSER = "browser_navigate"
# Page text shown in the result event is bounded; it is untrusted content.
MAX_EVENT_PAGE_CHARS = 6000
MAX_EVENT_LINKS = 20

_PERMISSION_FOR_PHASE = {
    PHASE_BROWSER: PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
}

# Persian outcome headlines shown to the person.
_FINISHED_FA = {
    "succeeded": "انجام شد.",
    "uncertain": "نتیجه قطعی نیست. کرونوس این کار را تکرار نمی‌کند؛ لطفاً خودتان بررسی کنید.",
    "refused": "انجام نشد؛ هیچ کاری روی سیستم انجام نشد.",
    "stopped": "متوقف شد؛ هیچ کار تازه‌ای شروع نشد.",
    "failed": "انجام نشد.",
}


@dataclass(frozen=True)
class PendingWorkflowStep:
    request_id: str
    phase: str
    navigation: BrowserNavigation | None = None
    details: dict = field(default_factory=dict)
    audit_request: ActionRequest | None = None


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="qronos-app-workflow", daemon=True).start()


class AppWorkflowCoordinator:
    def __init__(
        self,
        *,
        ledger: ApprovalLedger,
        notify: Callable[[str, str, str], None],
        actions: ActiveActionRegistry,
        paused: Callable[[], bool],
        run_async: Callable[[Callable[[], None]], None] = _start_thread,
        audit: ActionAuditLog | None = None,
        browser_workflow: Callable[[], BrowserResearchWorkflow] | None = None,
    ) -> None:
        self._browser = browser_workflow
        self._audit = audit
        self._ledger = ledger
        self._notify = notify
        self._actions = actions
        self._paused = paused
        self._run_async = run_async
        self._lock = threading.Lock()

    # ------------------------------------------------------------ entry

    def try_begin(self, text: str, request_id: str) -> str | None:
        """Handle an application-workflow request.

        Returns the Persian reply that ends the chat turn, or None when the
        text is not a workflow request. Prompts and results follow as events.
        """
        navigation = plan_navigation(text)
        if navigation is None or self._browser is None:
            return None
        if isinstance(navigation, NavigationClarification):
            self._finish(request_id, PHASE_BROWSER, "refused", navigation.question, "clarification needed")
            return navigation.question
        if self._paused():
            message = "کرونوس متوقف است؛ کاری شروع نشد."
            self._finish(request_id, PHASE_BROWSER, "stopped", message, "runtime paused")
            return message
        what = (
            f"جستجوی «{navigation.query}» در مرورگر شما"
            if navigation.kind == "search"
            else "باز کردن این نشانی در مرورگر شما"
        )
        self._prompt(PendingWorkflowStep(
            request_id=request_id,
            phase=PHASE_BROWSER,
            navigation=navigation,
            details={"url": navigation.url, "kind": navigation.kind, "query": navigation.query},
        ), f"{what}: {navigation.url} — سپس متن همان صفحهٔ باز را می‌خوانم. چیزی ارسال یا کلیک نمی‌شود.")
        return "برای ادامه، درخواست تأیید را ببینید. بدون تأیید شما هیچ صفحه‌ای باز نمی‌شود."

    # ----------------------------------------------------------- prompts

    def _prompt(self, step: PendingWorkflowStep, message: str) -> None:
        step = replace(step, audit_request=_audit_request(step))
        ticket = self._ledger.issue(ApprovalKind.APP_WORKFLOW, step.request_id, payload=step)
        self._notify("app_workflow_approval_required", "approval_required", _json({
            "actionToken": ticket.approval_id,
            "requestId": step.request_id,
            "phase": step.phase,
            "permission": _PERMISSION_FOR_PHASE[step.phase].value,
            "details": step.details,
            "message": message,
        }))

    def discard(self, reason: str) -> int:
        """Revoke every open workflow prompt (Stop/Pause/shutdown). Idempotent."""
        revoked = self._ledger.revoke_kinds((ApprovalKind.APP_WORKFLOW,), reason)
        for ticket in revoked:
            step: PendingWorkflowStep = ticket.payload
            self._notify("app_workflow_stale", "ready", _json({
                "actionToken": ticket.approval_id,
                "requestId": step.request_id,
                "reason": reason,
            }))
        return len(revoked)

    # ---------------------------------------------------------- decisions

    def resolve(self, action_token: str, *, approved: bool) -> None:
        token = str(action_token or "").strip()
        if self._paused():
            self._stale(token, "runtime_paused")
            return
        step = self._open_step(token)
        if step is None:
            self._stale(token, "unknown_or_used")
            return
        try:
            ticket = self._ledger.take(token, kinds=(ApprovalKind.APP_WORKFLOW,))
        except StaleApproval as stale:
            self._stale(token, stale.reason)
            return
        step = ticket.payload
        needed = _PERMISSION_FOR_PHASE[step.phase]
        self._record(step, AuditEvent.APPROVED if approved else AuditEvent.DENIED_BY_USER, decision=needed)
        if not approved:
            self._finish_step(step, "refused", "لغو شد؛ هیچ کاری انجام نشد.", "declined")
            return
        approval = (ticket.approval_id, needed)
        if step.phase == PHASE_BROWSER:
            self._run_async(lambda: self._browser_navigate(step, approval))

    def _open_step(self, token: str) -> PendingWorkflowStep | None:
        for ticket in self._ledger.open_tickets():
            if ticket.approval_id == token and ticket.kind is ApprovalKind.APP_WORKFLOW:
                return ticket.payload
        return None

    def _stale(self, token: str, reason: str) -> None:
        self._notify("app_workflow_stale", "ready", _json({"actionToken": token, "reason": reason}))

    # --------------------------------------------------------- execution

    def _approval_valid(self) -> bool:
        return not self._paused() and not self._ledger.closed

    def _browser_navigate(self, step: PendingWorkflowStep, approval) -> None:
        approval_id, decision = approval
        navigation = step.navigation

        def run(token):
            report = self._browser().run(
                navigation,
                WorkflowApproval(approval_id=approval_id, plan_digest=plan_digest(navigation.plan), decision=decision),
                token=token, approval_valid=self._approval_valid,
            )
            if report.outcome != "succeeded":
                self._report(step, report)
                return
            page = report.result
            result = {
                "url": page["url"],
                "title": page["title"],
                # Untrusted page content: shown to the person, never acted on.
                "text": str(page["text"])[:MAX_EVENT_PAGE_CHARS],
                "links": list(page["links"])[:MAX_EVENT_LINKS],
                "truncated": bool(page["truncated"]) or len(str(page["text"])) > MAX_EVENT_PAGE_CHARS,
                "untrusted": True,
            }
            self._finish_step(step, "succeeded", f"صفحه در مرورگر باز شد: {page['title'] or page['url']}",
                              report.detail, result)

        self._execute(step, "qronos-browser-navigate", run)

    def _execute(self, step: PendingWorkflowStep, label: str, run) -> None:
        try:
            with self._actions.begin(label) as token:
                run(token)
        except WorkflowStopped as stopped:
            self._finish_step(step, "stopped", _FINISHED_FA["stopped"], str(stopped))
        except (WorkflowAmbiguous, WorkflowRefused, ApprovalMismatch) as refused:
            self._finish_step(step, "refused", _refusal_fa(refused), str(refused))
        except Exception as error:  # noqa: BLE001 - report, never retry
            self._finish_step(step, "failed", _FINISHED_FA["failed"], type(error).__name__)

    def _report(self, step: PendingWorkflowStep, report: WorkflowReport) -> None:
        headline = _FINISHED_FA.get(report.outcome, _FINISHED_FA["failed"])
        self._finish_step(step, report.outcome, headline, report.detail, dict(report.result))

    def _record(self, step: PendingWorkflowStep, event: AuditEvent, *, outcome=None, decision=None) -> None:
        if self._audit is None or step.audit_request is None:
            return
        try:
            self._audit.record(event, step.audit_request, outcome=outcome, decision=decision)
        except Exception as error:  # noqa: BLE001 - auditing must not hide the result
            self._notify("runtime_warning", "warning", f"Could not audit a workflow step: {type(error).__name__}")

    def _finish_step(self, step: PendingWorkflowStep, outcome, message, detail, result=None) -> None:
        self._record(step, AuditEvent.COMPLETED, outcome=_AUDIT_OUTCOME.get(outcome, ActionOutcome.FAILED))
        self._finish(step.request_id, step.phase, outcome, message, detail, result)

    def _finish(self, request_id, workflow, outcome, message, detail, result=None) -> None:
        self._notify("app_workflow_finished", "ready", _json({
            "requestId": request_id,
            "workflow": workflow,
            "outcome": outcome,
            "message": message,
            "detail": detail,
            "result": result or {},
        }))


def _refusal_fa(error: Exception) -> str:
    if isinstance(error, WorkflowAmbiguous):
        return "بیش از یک گزینه دقیقاً با این نام پیدا شد؛ کرونوس حدس نمی‌زند. لطفاً دقیق‌تر بگویید."
    if isinstance(error, ApprovalMismatch):
        return "تأیید با این کار مطابقت نداشت؛ هیچ کاری انجام نشد."
    return "انجام نشد؛ شرایط لازم برقرار نبود و هیچ کاری روی سیستم انجام نشد."


_AUDIT_OUTCOME = {
    "succeeded": ActionOutcome.SUCCEEDED,
    "refused": ActionOutcome.REFUSED,
    "stopped": ActionOutcome.CANCELLED,
    "uncertain": ActionOutcome.FAILED,
    "failed": ActionOutcome.FAILED,
}


def _audit_request(step: PendingWorkflowStep) -> ActionRequest:
    """The typed action a prompt asks about, for the audit trail."""
    return ActionRequest(
        category=ActionCategory.BROWSER_NAVIGATION,
        target=step.navigation.url,
        summary="Open one exact page in the visible browser and read what it shows.",
        parameters={"kind": step.navigation.kind, "query": step.navigation.query},
    )
