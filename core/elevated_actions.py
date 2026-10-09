"""
Elevated actions from the user runtime: ask, approve, UAC, broker — once.

The only elevated action is ``printer.spooler_restart`` (see
``core/action_broker.py`` and ``core/spooler_service.py``). It is reached
from the person's own words, through the same prompt protocol as the other
application workflows (``app_workflow_approval_required`` with phase
``printer_spooler_restart``; decisions arrive as ``qronos.app_workflow_decision``).

Approval layers, all of which must agree, in order:

    1. The person asked for it in so many words (``parse_spooler_restart``);
       a model's output is never parsed into this action.
    2. The permission gate for ``device_control`` must not refuse it.
    3. The typed approval secret, ALWAYS. This is a floor: even if the
       person's permission settings lowered device control to "allow" or "ask",
       an elevated action still needs the secret. A UAC dialog is Windows'
       consent to elevation, not Qronos's approval of the exact action, so it
       never replaces this step.
    4. A short-lived gate grant for this exact request, turned by
       ``BrokerSession`` into an HMAC'd, single-use approval envelope for the
       exact typed action, with nothing for the caller to choose: the kind is
       fixed and it has no parameters.
    5. Windows' UAC prompt, which the person can decline.
    6. The broker re-checks the envelope, freshness, digest, registry,
       category/target and gate, and claims the nonce once.

Outcomes are reported, never retried: declined or unavailable means nothing
was done; a lost answer after the request left means the outcome is unknown,
and the person is told to check the service. One elevated action runs at a
time. Prompts expire after ``APPROVAL_TTL_SECONDS`` and are revoked by Stop,
Pause and shutdown. Remote (Device Link) text never reaches this coordinator:
the bridge only routes the desktop's own chat turns here.

Elevation exists only in the installed (frozen) Qronos, whose bundled
executable is the broker; from source it is refused before any prompt.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import security.gate as gate
from core.action_broker import PRODUCTION_ACTIONS
from core.action_broker_client import BrokerOutcomeUnknown, BrokerSession, BrokerUnavailable, TypedAction
from core.actions import ActionRequest
from core.approval_ledger import ApprovalKind, ApprovalLedger, StaleApproval
from security.permissions import PermissionDecision

KIND = "printer.spooler_restart"
PHASE = "printer_spooler_restart"
#: Long enough to read the prompt, short enough not to linger.
APPROVAL_TTL_SECONDS = 120.0
#: The grant must outlive the UAC prompt; the broker caps the envelope at 180 s.
GRANT_TTL_SECONDS = 170.0
MAX_TEXT_CHARS = 300

_SPOOLER_WORDS = ("print spooler", "spooler", "printing service", "print service",
                  "سرویس چاپ", "اسپولر", "سرویس پرینت", "سرویس پرینتر", "سرویس چاپگر")
_RESTART_WORDS = ("restart", "re-start", "ری استارت", "ریاستارت", "ریستارت",
                  "راه اندازی مجدد", "دوباره راه اندازی", "مجددا راه اندازی", "مجدداً راه اندازی")
_NEGATIONS = re.compile(r"(\bdon'?t\b|\bdo not\b|\bnever\b|\bnot\b|نکن|نشود|نباید|نمی ?خواهم|نمیخوام|نه )")

FA = {
    "paused": "کرونوس متوقف است؛ کاری شروع نشد.",
    "busy": "یک کار مدیریتی دیگر در حال انجام است؛ کار تازه‌ای شروع نشد.",
    "no_secret_path": "این کار به رمز تأیید نیاز دارد و این مسیر اجازهٔ بررسی رمز را ندارد؛ کاری انجام نشد.",
    "secret_not_configured": "این کار به رمز تأیید نیاز دارد و هنوز رمزی تعیین نشده است؛ کاری انجام نشد.",
    "secret_locked": "رمز تأیید موقتاً قفل است؛ کاری انجام نشد.",
    "secret_unavailable": "رمز تأیید در دسترس نیست؛ کاری انجام نشد.",
    "forbidden": "کنترل دستگاه در تنظیمات مجوزها ممنوع است؛ کاری انجام نشد.",
    "prompt": (
        "راه‌اندازی دوبارهٔ سرویس چاپ ویندوز (Spooler): سرویس متوقف و دوباره روشن می‌شود و کار دیگری انجام نمی‌شود. "
        "پس از تأیید با رمز، ویندوز اجازهٔ مدیر (UAC) را می‌پرسد."
    ),
    "asked": "برای ادامه، درخواست تأیید را ببینید. بدون تأیید شما و اجازهٔ ویندوز کاری انجام نمی‌شود.",
    "secret_rejected": "رمز تأیید درست نیست یا موقتاً قفل است. کاری انجام نشد.",
    "declined": "لغو شد؛ کاری انجام نشد.",
    "uac_declined": "اجازهٔ مدیر ویندوز (UAC) داده نشد؛ کاری انجام نشد.",
    "not_installed": "این کار فقط در نسخهٔ نصب‌شدهٔ کرونوس ممکن است؛ کاری انجام نشد.",
    "unavailable": "کمک‌رسان مدیریتی کرونوس اجرا نشد؛ کاری انجام نشد.",
    "unknown": (
        "پاسخ کمک‌رسان مدیریتی گم شد؛ معلوم نیست سرویس چاپ دوباره راه‌اندازی شده یا نه. "
        "دوباره به‌طور خودکار تلاش نمی‌شود؛ وضعیت سرویس چاپ را بررسی کنید."
    ),
    "done": "سرویس چاپ ویندوز دوباره راه‌اندازی شد.",
    "failed": "سرویس چاپ دوباره راه‌اندازی نشد. دوباره به‌طور خودکار تلاش نمی‌شود.",
    "refused": "کمک‌رسان مدیریتی این درخواست را نپذیرفت؛ کاری انجام نشد.",
}
_SECRET_STATES = {"not_configured": "secret_not_configured", "locked": "secret_locked"}


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("‌", " ").replace("‏", " ")
    text = text.replace("ي", "ی").replace("ك", "ک").lower()
    return re.sub(r"\s+", " ", text).strip()


def parse_spooler_restart(text: object) -> bool:
    """True only for a short, plain request to restart the print spooler."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        return False
    normal = _normalise(text)
    if _NEGATIONS.search(normal + " "):
        return False
    return any(word in normal for word in _SPOOLER_WORDS) and any(word in normal for word in _RESTART_WORDS)


class ElevationUnavailable(RuntimeError):
    """This process cannot start the broker (for example, it runs from source)."""


def bundled_launcher():
    """The UAC launcher for the installed Qronos executable; refused from source."""
    if not getattr(sys, "frozen", False):
        raise ElevationUnavailable("Elevated actions need the installed Qronos executable.")
    from core.uac_launch import UacBrokerLauncher

    return UacBrokerLauncher(Path(sys.executable))


@dataclass(frozen=True)
class PendingElevation:
    request_id: str
    request: ActionRequest


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="qronos-elevated-action", daemon=True).start()


class ElevatedActionCoordinator:
    def __init__(
        self,
        *,
        notify: Callable[[str, str, str], None],
        verify_secret: Optional[Callable[[str], bool]],
        secret_status: Optional[Callable[[], str]],
        paused: Callable[[], bool],
        launcher_factory: Callable[[], object] = bundled_launcher,
        session_factory: Callable[[object], BrokerSession] = BrokerSession,
        gate_check: Callable[[ActionRequest], gate.Verdict] = gate.evaluate,
        run_async: Callable[[Callable[[], None]], None] = _start_thread,
        ledger: Optional[ApprovalLedger] = None,
    ) -> None:
        self._notify = notify
        self._verify_secret = verify_secret
        self._secret_status = secret_status
        self._paused = paused
        self._launcher_factory = launcher_factory
        self._session_factory = session_factory
        self._gate = gate_check
        self._run_async = run_async
        self._ledger = ledger if ledger is not None else ApprovalLedger(ttl_seconds=APPROVAL_TTL_SECONDS)
        self._busy = threading.Lock()
        spec = PRODUCTION_ACTIONS.get(KIND)
        assert spec is not None, "the spooler restart must be a registered broker action"
        self._spec = spec

    # ------------------------------------------------------------ entry

    def try_begin(self, text: str, request_id: str) -> Optional[str]:
        """The Persian reply that ends the chat turn, or None if not this request."""
        if not parse_spooler_restart(text):
            return None
        refusal = self._refusal_before_prompt()
        if refusal is not None:
            self._finish(request_id, "refused", FA[refusal], refusal)
            return FA[refusal]

        request = ActionRequest(
            category=self._spec.category,
            target=self._spec.target({}),
            summary=self._spec.summary,
            action_id=f"elevated-{uuid.uuid4().hex}",
        )
        if self._gate(request).refused:
            self._finish(request_id, "refused", FA["forbidden"], "gate refused")
            return FA["forbidden"]

        ticket = self._ledger.issue(ApprovalKind.APP_WORKFLOW, request_id,
                                    payload=PendingElevation(request_id, request))
        self._notify("app_workflow_approval_required", "approval_required", _json({
            "actionToken": ticket.approval_id,
            "requestId": request_id,
            "phase": PHASE,
            "permission": PermissionDecision.REQUIRE_TYPED_SECRET.value,
            "details": {"service": "Spooler", "elevation": "uac"},
            "message": FA["prompt"],
        }))
        return FA["asked"]

    def _refusal_before_prompt(self) -> Optional[str]:
        if self._paused():
            return "paused"
        if self._busy.locked():
            return "busy"
        if self._verify_secret is None:
            return "no_secret_path"
        state = "ready"
        if self._secret_status is not None:
            try:
                state = str(self._secret_status())
            except Exception:  # noqa: BLE001 - an unreadable store never allows elevation
                state = "unavailable"
        if state != "ready":
            return _SECRET_STATES.get(state, "secret_unavailable")
        return None

    # ---------------------------------------------------------- decisions

    def owns(self, action_token: str) -> bool:
        token = str(action_token or "")
        return any(ticket.approval_id == token for ticket in self._ledger.open_tickets())

    def resolve(self, action_token: str, *, approved: bool, secret: Optional[str] = None) -> None:
        token = str(action_token or "").strip()
        if self._paused():
            self._stale(token, "runtime_paused")
            return
        pending = next((t.payload for t in self._ledger.open_tickets() if t.approval_id == token), None)
        if pending is None:
            self._stale(token, "unknown_or_used")
            return
        if approved:
            try:
                verified = self._verify_secret is not None and bool(self._verify_secret(secret or ""))
            except Exception:  # noqa: BLE001 - locked or unavailable store
                verified = False
            if not verified:
                # The prompt stays open; only a correct secret consumes it.
                self._notify("app_workflow_secret_rejected", "approval_required", _json({
                    "actionToken": token, "requestId": pending.request_id, "message": FA["secret_rejected"],
                }))
                return
        try:
            ticket = self._ledger.take(token, kinds=(ApprovalKind.APP_WORKFLOW,))
        except StaleApproval as stale:
            self._stale(token, stale.reason)
            return
        pending = ticket.payload
        if not approved:
            self._finish(pending.request_id, "refused", FA["declined"], "declined")
            return
        self._run_async(lambda: self._perform(pending))

    def discard(self, reason: str) -> int:
        """Revoke every open elevation prompt (Stop/Pause/shutdown). Idempotent."""
        revoked = self._ledger.revoke_kinds((ApprovalKind.APP_WORKFLOW,), reason)
        for ticket in revoked:
            self._notify("app_workflow_stale", "ready", _json({
                "actionToken": ticket.approval_id, "requestId": ticket.payload.request_id, "reason": reason,
            }))
        return len(revoked)

    # --------------------------------------------------------- execution

    def _perform(self, pending: PendingElevation) -> None:
        if not self._busy.acquire(blocking=False):
            self._finish(pending.request_id, "refused", FA["busy"], "another elevated action is running")
            return
        try:
            outcome, message, detail = self._run(pending)
        finally:
            self._busy.release()
        self._finish(pending.request_id, outcome, message, detail)

    def _run(self, pending: PendingElevation) -> tuple[str, str, str]:
        if self._paused():
            return "stopped", FA["paused"], "runtime paused"

        verdict = self._gate(pending.request)  # fresh: settings may have changed
        if verdict.refused:
            return "refused", FA["forbidden"], "gate refused"
        try:
            grant = gate.issue_grant(
                pending.request, verdict,
                approval=verdict.decision if verdict.needs_approval else None,
                ttl_seconds=GRANT_TTL_SECONDS,
            )
        except gate.ActionGrantUnavailable as error:
            return "refused", FA["forbidden"], str(error)[:200]

        try:
            launcher = self._launcher_factory()
        except ElevationUnavailable:
            return "refused", FA["not_installed"], "not the installed executable"

        try:
            result = self._session_factory(launcher).perform(TypedAction(KIND, {}), pending.request, grant)
        except BrokerOutcomeUnknown as error:
            return "uncertain", FA["unknown"], str(error)[:200]
        except BrokerUnavailable as error:
            from core.uac_launch import UacDeclined

            if isinstance(error.__cause__, UacDeclined):
                return "refused", FA["uac_declined"], "UAC declined"
            return "refused", FA["unavailable"], str(error)[:200]

        if result.ok:
            return "succeeded", FA["done"], result.detail[:500]
        if result.outcome == "failed":
            return "failed", FA["failed"], result.detail[:500]
        return "refused", FA["refused"], f"{result.outcome}: {result.detail}"[:500]

    # ------------------------------------------------------------ events

    def _stale(self, token: str, reason: str) -> None:
        self._notify("app_workflow_stale", "ready", _json({"actionToken": token, "reason": reason}))

    def _finish(self, request_id: str, outcome: str, message: str, detail: str) -> None:
        self._notify("app_workflow_finished", "ready", _json({
            "requestId": request_id,
            "workflow": PHASE,
            "outcome": outcome,
            "message": message,
            "detail": detail,
            "result": {},
        }))
