"""
Runtime coordination for the application workflows.

Turns one recognised request into a short sequence of approval prompts and
executions, each with its own single-use identity from the shared
:class:`core.approval_ledger.ApprovalLedger`:

    Telegram  inspect (voice/click) -> download (on-screen confirmation)
    Print     submit (typed approval secret)

Each answer is honoured only by presenting the identity of the prompt it
answers. Stop, Pause and shutdown revoke every open prompt (``discard``) and
cancel the running step through the shared ActiveActionRegistry, so a late
"yes" can never start or continue a workflow. Every execution runs on a
worker thread inside one cancellable active action.

The desktop sees four events (JSON messages): ``app_workflow_approval_required``,
``app_workflow_clarification``, ``app_workflow_finished`` and
``app_workflow_stale``. User-facing text is Persian; the English technical
detail is kept separately for the audit trail. The approval secret for
printing is verified here and never echoed, logged or stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import threading
from typing import Any, Callable

from core.action_audit import ActionAuditLog, AuditEvent
from core.action_cancellation import ActiveActionRegistry
from core.actions import ActionOutcome, ActionRequest
from core.app_workflow import (
    ApprovalMismatch,
    DuplicateSideEffect,
    JournalUnavailable,
    WorkflowAmbiguous,
    WorkflowApproval,
    WorkflowError,
    WorkflowRefused,
    WorkflowReport,
    WorkflowStopped,
)
from core.app_workflow_intents import (
    PrintIntent,
    TelegramDownloadIntent,
    WorkflowClarification,
    parse_workflow_intent,
)
from core.approval_ledger import ApprovalKind, ApprovalLedger, StaleApproval
from core.print_workflow import PreparedPrint, PrintRequest, PrintWorkflow
from core.telegram_workflow import (
    PreparedTelegramDownload,
    TelegramDownloadRequest,
    TelegramDownloadWorkflow,
)
from security.permissions import ActionCategory, PermissionDecision


APP_WORKFLOW_DECISION_ACTION = "qronos.app_workflow_decision"

PHASE_TELEGRAM_INSPECT = "telegram_inspect"
PHASE_TELEGRAM_DOWNLOAD = "telegram_download"
PHASE_PRINT = "print"

_PERMISSION_FOR_PHASE = {
    PHASE_TELEGRAM_INSPECT: PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
    PHASE_TELEGRAM_DOWNLOAD: PermissionDecision.REQUIRE_UI_CONFIRMATION,
    PHASE_PRINT: PermissionDecision.REQUIRE_TYPED_SECRET,
}

# Persian outcome headlines shown to the person.
_FINISHED_FA = {
    "succeeded": "انجام شد.",
    "submitted": "کار چاپ در صف چاپگر است و هنوز تمام نشده است.",
    "needs_attention": "کار چاپ در صف است اما چاپگر به رسیدگی نیاز دارد. کرونوس دوباره چاپ نمی‌کند.",
    "uncertain": "نتیجه قطعی نیست. کرونوس این کار را تکرار نمی‌کند؛ لطفاً خودتان بررسی کنید.",
    "refused": "انجام نشد؛ هیچ کاری روی سیستم انجام نشد.",
    "stopped": "متوقف شد؛ هیچ کار تازه‌ای شروع نشد.",
    "failed": "انجام نشد.",
}


@dataclass(frozen=True)
class PendingWorkflowStep:
    request_id: str
    phase: str
    telegram_request: TelegramDownloadRequest | None = None
    telegram_prepared: PreparedTelegramDownload | None = None
    print_prepared: PreparedPrint | None = None
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
        telegram_workflow: Callable[[], TelegramDownloadWorkflow],
        print_workflow: Callable[[], PrintWorkflow],
        resolve_file: Callable[[str], Path],
        default_printer: Callable[[], str | None],
        verify_secret: Callable[[str], bool] | None,
        paused: Callable[[], bool],
        run_async: Callable[[Callable[[], None]], None] = _start_thread,
        audit: ActionAuditLog | None = None,
    ) -> None:
        self._audit = audit
        self._ledger = ledger
        self._notify = notify
        self._actions = actions
        self._telegram = telegram_workflow
        self._print = print_workflow
        self._resolve_file = resolve_file
        self._default_printer = default_printer
        self._verify_secret = verify_secret
        self._paused = paused
        self._run_async = run_async
        self._lock = threading.Lock()

    # ------------------------------------------------------------ entry

    def try_begin(self, text: str, request_id: str) -> str | None:
        """Handle an application-workflow request.

        Returns the Persian reply that ends the chat turn, or None when the
        text is not a workflow request. Prompts and results follow as events.
        """
        intent = parse_workflow_intent(text)
        if intent is None:
            return None
        if isinstance(intent, WorkflowClarification):
            self._finish(request_id, "clarification", "refused", intent.question, "clarification needed")
            return intent.question
        if self._paused():
            message = "کرونوس متوقف است؛ کاری شروع نشد."
            self._finish(request_id, "workflow", "stopped", message, "runtime paused")
            return message
        if isinstance(intent, TelegramDownloadIntent):
            try:
                request = TelegramDownloadRequest(intent.contact)
            except ValueError as error:
                message = "نام مخاطب معتبر نیست."
                self._finish(request_id, "telegram", "refused", message, str(error))
                return message
            self._prompt(PendingWorkflowStep(
                request_id=request_id,
                phase=PHASE_TELEGRAM_INSPECT,
                telegram_request=request,
                details={"contact": request.contact},
            ), f"پیدا کردن گفتگوی دقیق «{request.contact}» در تلگرام و خواندن پیام‌های اخیر آن، بدون ارسال پیام.")
            return "برای ادامه، درخواست تأیید را ببینید. بدون تأیید شما هیچ کاری در تلگرام انجام نمی‌شود."
        if isinstance(intent, PrintIntent):
            if self._verify_secret is None:
                # Printing needs the typed approval secret, and this process is
                # not one of the reviewed paths allowed to check it. Refuse
                # before showing a prompt that could never be approved.
                message = (
                    "چاپ به رمز تأیید نیاز دارد و این مسیر هنوز اجازهٔ بررسی رمز را ندارد؛ "
                    "چیزی چاپ نشد."
                )
                self._finish(request_id, PHASE_PRINT, "refused", message, "typed-secret path unavailable")
                return message
            self._run_async(lambda: self._prepare_print(intent, request_id))
            return "فایل و چاپگر بررسی می‌شوند؛ پیش از چاپ، جزئیات برای تأیید نشان داده می‌شود."
        return None

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

    def resolve(self, action_token: str, *, approved: bool, secret: str | None = None) -> None:
        token = str(action_token or "").strip()
        if self._paused():
            self._stale(token, "runtime_paused")
            return
        step = self._open_step(token)
        if step is None:
            self._stale(token, "unknown_or_used")
            return
        needed = _PERMISSION_FOR_PHASE[step.phase]
        if approved and needed is PermissionDecision.REQUIRE_TYPED_SECRET:
            try:
                verified = self._verify_secret is not None and bool(self._verify_secret(secret or ""))
            except Exception:  # noqa: BLE001 - locked or unavailable secret store
                verified = False
            if not verified:
                # The prompt stays open; only a correct secret consumes it.
                self._notify("app_workflow_secret_rejected", "approval_required", _json({
                    "actionToken": token,
                    "requestId": step.request_id,
                    "message": "رمز تأیید درست نیست یا موقتاً قفل است. چیزی چاپ نشد.",
                }))
                return
        try:
            ticket = self._ledger.take(token, kinds=(ApprovalKind.APP_WORKFLOW,))
        except StaleApproval as stale:
            self._stale(token, stale.reason)
            return
        step = ticket.payload
        self._record(step, AuditEvent.APPROVED if approved else AuditEvent.DENIED_BY_USER, decision=needed)
        if not approved:
            self._finish_step(step, "refused", "لغو شد؛ هیچ کاری انجام نشد.", "declined")
            return
        approval = (ticket.approval_id, needed)
        if step.phase == PHASE_TELEGRAM_INSPECT:
            self._run_async(lambda: self._telegram_inspect(step, needed))
        elif step.phase == PHASE_TELEGRAM_DOWNLOAD:
            self._run_async(lambda: self._telegram_download(step, approval))
        elif step.phase == PHASE_PRINT:
            self._run_async(lambda: self._print_submit(step, approval))

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

    def _telegram_inspect(self, step: PendingWorkflowStep, decision: PermissionDecision) -> None:
        def run(token):
            prepared = self._telegram().inspect(
                step.telegram_request, approved=decision, token=token, approval_valid=self._approval_valid,
            )
            message = (
                f"جدیدترین فایل دریافتی از «{prepared.request.contact}»: «{prepared.message.file_name}» "
                f"({prepared.message.size_text or 'اندازه نامشخص'}) در پوشه «{prepared.destination}» ذخیره شود؟"
            )
            self._prompt(PendingWorkflowStep(
                request_id=step.request_id,
                phase=PHASE_TELEGRAM_DOWNLOAD,
                telegram_prepared=prepared,
                details={
                    "contact": prepared.request.contact,
                    "fileName": prepared.message.file_name,
                    "sizeText": prepared.message.size_text,
                    "destination": str(prepared.destination),
                },
            ), message)

        self._execute(step, "qronos-telegram-inspect", run)

    def _telegram_download(self, step: PendingWorkflowStep, approval) -> None:
        approval_id, decision = approval
        prepared = step.telegram_prepared

        def run(token):
            report = self._telegram().download(
                prepared,
                WorkflowApproval(
                    approval_id=approval_id, plan_digest=prepared.digest, decision=decision,
                    evidence_identity_token=prepared.evidence.identity_token,
                ),
                token=token, approval_valid=self._approval_valid,
            )
            self._report(step, report)

        self._execute(step, "qronos-telegram-download", run)

    def _prepare_print(self, intent: PrintIntent, request_id: str) -> None:
        try:
            path = self._resolve_file(intent.file_reference)
        except Exception as error:  # noqa: BLE001 - not found, ambiguous or search limit
            self._finish(request_id, PHASE_PRINT, "refused",
                         "فایل دقیق پیدا نشد یا بیش از یک فایل با این نام هست؛ چیزی چاپ نشد.",
                         type(error).__name__)
            return
        try:
            printer = intent.printer or self._default_printer()
            if not printer:
                self._finish(request_id, PHASE_PRINT, "refused",
                             "چاپگر پیش‌فرضی پیدا نشد. نام دقیق چاپگر را بگویید.", "no default printer")
                return
            prepared = self._print().prepare(PrintRequest(path, printer, copies=intent.copies))
        except WorkflowError as error:
            self._finish(request_id, PHASE_PRINT, "refused", _refusal_fa(error), str(error))
            return
        except (ValueError, LookupError) as error:
            self._finish(request_id, PHASE_PRINT, "refused",
                         "فایل یا چاپگر دقیق مشخص نشد؛ چیزی چاپ نشد.", str(error))
            return
        default_note = "" if intent.printer else " (چاپگر پیش‌فرض)"
        self._prompt(PendingWorkflowStep(
            request_id=request_id,
            phase=PHASE_PRINT,
            print_prepared=prepared,
            details={
                "file": str(prepared.file_path),
                "size": prepared.size,
                "sha256": prepared.sha256,
                "printer": prepared.printer.name + default_note,
                "copies": 1,
                "color": "پیش‌فرض چاپگر",
                "pages": "همه صفحه‌ها",
                "handler": prepared.handler,
                "warnings": list(prepared.warnings),
            },
        ), (
            f"فایل «{prepared.file_path.name}» یک بار با چاپگر «{prepared.printer.name}»{default_note} "
            "چاپ شود؟ (یک نسخه، رنگ پیش‌فرض چاپگر، همه صفحه‌ها) برای تأیید، رمز تأیید را وارد کنید."
        ))

    def _print_submit(self, step: PendingWorkflowStep, approval) -> None:
        approval_id, decision = approval
        prepared = step.print_prepared

        def run(token):
            report = self._print().submit(
                prepared,
                WorkflowApproval(approval_id=approval_id, plan_digest=prepared.digest, decision=decision),
                token=token, approval_valid=self._approval_valid,
            )
            self._report(step, report)

        self._execute(step, "qronos-print", run)

    def _execute(self, step: PendingWorkflowStep, label: str, run) -> None:
        try:
            with self._actions.begin(label) as token:
                run(token)
        except WorkflowStopped as stopped:
            self._finish_step(step, "stopped", _FINISHED_FA["stopped"], str(stopped))
        except (DuplicateSideEffect, JournalUnavailable) as blocked:
            self._finish_step(step, "refused", (
                "این کار قبلاً انجام شده یا نتیجهٔ تلاش قبلی هنوز روشن نیست؛ کرونوس آن را تکرار نمی‌کند."
            ), str(blocked))
        except (WorkflowAmbiguous, WorkflowRefused, ApprovalMismatch) as refused:
            self._finish_step(step, "refused", _refusal_fa(refused), str(refused))
        except Exception as error:  # noqa: BLE001 - report, never retry
            self._finish_step(step, "failed", _FINISHED_FA["failed"], type(error).__name__)

    def _report(self, step: PendingWorkflowStep, report: WorkflowReport) -> None:
        headline = _FINISHED_FA.get(report.outcome, _FINISHED_FA["failed"])
        if report.outcome == "succeeded" and "path" in report.result:
            headline = f"فایل دانلود شد: {report.result['path']}"
        elif report.outcome == "succeeded" and "jobId" in report.result:
            headline = f"چاپ انجام شد (کار شماره {report.result['jobId']} در «{report.result['printer']}»)."
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
    # The job reached the printer queue: the side effect happened.
    "submitted": ActionOutcome.SUCCEEDED,
    "refused": ActionOutcome.REFUSED,
    "stopped": ActionOutcome.CANCELLED,
    "uncertain": ActionOutcome.FAILED,
    "needs_attention": ActionOutcome.FAILED,
    "failed": ActionOutcome.FAILED,
}


def _audit_request(step: PendingWorkflowStep) -> ActionRequest:
    """The typed action a prompt asks about, for the audit trail."""
    if step.phase == PHASE_TELEGRAM_INSPECT:
        return ActionRequest(
            category=ActionCategory.GUI_AUTOMATION,
            target="app:telegram",
            summary="Find one exact Telegram chat and read its recent messages.",
            parameters={"contact": step.telegram_request.contact},
        )
    if step.phase == PHASE_TELEGRAM_DOWNLOAD:
        prepared = step.telegram_prepared
        return ActionRequest(
            category=ActionCategory.CREATE_OR_EDIT_FILE,
            target=str(prepared.destination / prepared.message.file_name),
            summary="Download one exact Telegram attachment.",
            parameters={"contact": prepared.request.contact, "fileName": prepared.message.file_name},
        )
    prepared = step.print_prepared
    return ActionRequest(
        category=ActionCategory.DEVICE_CONTROL,
        target=f"printer:{prepared.printer.name}",
        summary="Print one exact file once.",
        parameters={"file": str(prepared.file_path), "sha256": prepared.sha256},
    )
