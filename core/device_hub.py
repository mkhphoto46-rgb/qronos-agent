"""Device Hub: the devices Windows already has, their real capabilities, and the workflows they allow (H1/H2).

Only the local Windows print spooler and the device drivers behind it are used (requirement H2): a LAN printer
appears here when Windows has installed it (a printer connection), and it is reached through the spooler and its
driver exactly like a local one. There is no socket, HTTP, vendor cloud or smart-device API here.

What a device can do comes from its driver, read-only (DeviceCapabilities), and from its queue state; a workflow
is offered only when the device supports it:

* print one exact file      - the existing reviewed print step (AppWorkflowCoordinator.begin_print: prepare, the
                              DEVICE_CONTROL gate, single-use approval with the typed secret, one verified job);
* list a device's jobs      - read-only;
* pause / resume / cancel   - ONLY the caller's own job, through the DEVICE_CONTROL gate and a single-use
                              DEVICE_ACTION approval (typed secret when the policy requires it), audited.

Truthfully unsupported (shown as such, never as a capability): a queue that looks like a 3D printer (no 3D
printing workflow exists yet, so document printing is not offered to it either), and every other device class
(scanners, cameras, other local devices). Desktop requests are typed ``qronos.device_hub`` actions
``{op, requestId, ...}``; answers are ``device_hub_result`` runtime events.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from core.action_audit import ActionAuditLog, AuditEvent
from core.actions import ActionOutcome, ActionRequest
from core.approval_ledger import ApprovalKind, ApprovalLedger, StaleApproval
from core.print_workflow import PrinterState, PrintJobState, SpoolerUnavailable, WinSpooler
import security.gate as security_gate  # every job change reaches the permission gate
from security.permissions import ActionCategory, PermissionDecision

ACTION_ID = "qronos.device_hub"
RESULT_EVENT = "device_hub_result"

_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DEVICE_ID = re.compile(r"^prn-[0-9a-f]{20}$")
_APPROVAL_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_PAGES = re.compile(r"^(all|[0-9]{1,5}(-[0-9]{1,5})?(,[0-9]{1,5}(-[0-9]{1,5})?){0,31})$")
MAX_PATH_CHARS = 4096
MAX_SECRET_CHARS = 256
MAX_DEVICES = 64
MAX_JOBS = 100
MAX_PAPER_NAMES = 32

OPS = frozenset({"devices.list", "device.jobs", "job.control", "print.request", "decide", "decline"})
READ_OPS = frozenset({"devices.list", "device.jobs", "decline"})

# winspool / wingdi values (documented Windows constants)
PRINTER_ATTRIBUTE_SHARED = 0x8
PRINTER_ATTRIBUTE_NETWORK = 0x10
DC_DUPLEX, DC_PAPERNAMES, DC_COPIES, DC_COLLATE, DC_COLORDEVICE = 7, 16, 18, 22, 32
JOB_CONTROL_PAUSE, JOB_CONTROL_RESUME, JOB_CONTROL_DELETE = 1, 2, 5
JOB_COMMANDS = {"pause": JOB_CONTROL_PAUSE, "resume": JOB_CONTROL_RESUME, "cancel": JOB_CONTROL_DELETE}
_JOB_STATUS = {0x1: "paused", 0x2: "error", 0x4: "deleting", 0x8: "spooling", 0x10: "printing", 0x20: "offline",
               0x40: "paper_out", 0x80: "printed", 0x100: "deleted", 0x200: "blocked", 0x400: "user_intervention",
               0x800: "restarting", 0x1000: "complete"}

#: Device classes this Device Hub does not drive yet (truthful; no fake capability).
UNSUPPORTED_CLASSES: tuple[dict[str, str], ...] = (
    {"kind": "3d_printer", "reason": "3d_workflow_pending",
     "detail": "چاپ سه‌بعدی هنوز گردش کار محلیِ مبتنی بر درایور ندارد؛ چاپگر سه‌بعدی فقط شناسایی می‌شود."},
    {"kind": "scanner", "reason": "not_supported_yet", "detail": "اسکنر هنوز پشتیبانی نمی‌شود."},
    {"kind": "camera", "reason": "not_supported_yet", "detail": "دوربین از این بخش کنترل نمی‌شود."},
    {"kind": "other_local_device", "reason": "not_supported_yet",
     "detail": "دستگاه‌های محلیِ دیگر هنوز در Device Hub پشتیبانی نمی‌شوند."},
)
#: A print queue whose name or driver says it is a 3D printer is never offered document printing.
_LOOKS_3D = re.compile(r"(?<![0-9a-z])3d(?![0-9a-z])|\b3mf\b", re.IGNORECASE)


class DeviceHubError(ValueError):
    """A malformed or refused request; ``code`` is a stable word for the desktop."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


class DeviceSource(Protocol):
    """The local spooler and drivers (the real one is WinDeviceSpooler; tests use a recording fake)."""

    def printers(self) -> tuple[PrinterState, ...]: ...
    def jobs(self, printer: str) -> tuple[PrintJobState, ...]: ...
    def capabilities(self, printer: str, port: str) -> dict[str, Any]: ...
    def control_own_job(self, printer: str, job_id: int, command: int) -> None: ...
    def current_user(self) -> str: ...


class WinDeviceSpooler(WinSpooler):
    """The Windows spooler plus read-only driver capabilities and own-job pause/resume/cancel (SetJob)."""

    def __init__(self) -> None:
        super().__init__()
        w = self._winspool
        w.DeviceCapabilitiesW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ushort, ctypes.c_void_p,
                                          ctypes.c_void_p]
        w.DeviceCapabilitiesW.restype = ctypes.c_int

    def _capability(self, printer: str, port: str, which: int, output=None) -> int:
        return int(self._winspool.DeviceCapabilitiesW(printer, port, which, output, None))

    def capabilities(self, printer: str, port: str) -> dict[str, Any]:
        copies = self._capability(printer, port, DC_COPIES)
        names: list[str] = []
        count = self._capability(printer, port, DC_PAPERNAMES)
        if 0 < count <= 512:
            buffer = ctypes.create_unicode_buffer(64 * count)
            if self._capability(printer, port, DC_PAPERNAMES, buffer) == count:
                raw = ctypes.wstring_at(ctypes.addressof(buffer), 64 * count)
                names = [raw[i * 64:(i + 1) * 64].split("\0", 1)[0] for i in range(count)]
        return {"copiesMax": copies if copies > 0 else None,
                "color": self._capability(printer, port, DC_COLORDEVICE) == 1,
                "duplex": self._capability(printer, port, DC_DUPLEX) == 1,
                "collate": self._capability(printer, port, DC_COLLATE) == 1,
                "paperSizes": [name for name in names if name][:MAX_PAPER_NAMES]}

    def control_own_job(self, printer: str, job_id: int, command: int) -> None:
        if command not in JOB_COMMANDS.values():
            raise ValueError("unsupported job command")
        handle = self._open(printer)
        try:
            if not self._winspool.SetJobW(handle, int(job_id), 0, None, int(command)):
                raise SpoolerUnavailable(f"SetJob failed ({ctypes.get_last_error()}).")
        finally:
            self._winspool.ClosePrinter(handle)


def device_id(printer: PrinterState) -> str:
    """Stable while the Windows queue keeps its name, port and driver."""
    material = "\0".join((printer.name.casefold(), printer.port.casefold(), printer.driver.casefold()))
    return "prn-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class Device:
    id: str
    printer: PrinterState
    kind: str
    capabilities: dict[str, Any]
    workflows: tuple[str, ...]
    unsupported: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.printer.name, "kind": self.kind,
            "connection": "network" if self.printer.attributes & PRINTER_ATTRIBUTE_NETWORK else "local",
            "shared": bool(self.printer.attributes & PRINTER_ATTRIBUTE_SHARED),
            "isDefault": self.printer.is_default, "driver": self.printer.driver[:200],
            "status": {"problems": list(self.printer.problems), "warnings": list(self.printer.warnings),
                       "ready": not self.printer.problems},
            "queuedJobs": self.printer.jobs, "capabilities": self.capabilities,
            "workflows": list(self.workflows), "unsupported": self.unsupported,
        }


@dataclass(frozen=True)
class PendingJobControl:
    """The payload of one DEVICE_ACTION approval (only this module reads it)."""

    device_id: str
    printer: str
    job_id: int
    document: str
    command: str
    request: ActionRequest
    decision: PermissionDecision
    request_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="qronos-device-hub", daemon=True).start()


class DeviceHub:
    def __init__(
        self,
        *,
        source: Callable[[], DeviceSource],
        ledger: ApprovalLedger,
        emit: Callable[[str, str, dict[str, Any]], None],
        paused: Callable[[], bool],
        begin_print: Callable[[Any, str], str],
        verify_secret: Callable[[str], bool] | None,
        audit: ActionAuditLog | None = None,
        evaluate: Callable[..., Any] | None = None,
        run_async: Callable[[Callable[[], None]], None] = _start_thread,
    ) -> None:
        self._source = source
        self._ledger = ledger
        self._emit = emit
        self._paused = paused
        self._begin_print = begin_print
        self._verify_secret = verify_secret
        self._audit = audit
        self._evaluate = evaluate
        self._run_async = run_async
        self._lock = threading.Lock()  # one spooler conversation at a time

    # ------------------------------------------------------------ entry

    def handle(self, data: object) -> None:
        """One typed desktop request; every outcome (also a refusal) is answered once."""
        if not isinstance(data, dict):
            self._answer({"requestId": None, "op": None, "ok": False, "code": "bad_request"})
            return
        request_id, op = data.get("requestId"), data.get("op")
        base = {"requestId": request_id if isinstance(request_id, str) and _REQUEST_ID.match(request_id) else None,
                "op": op if op in OPS else None}
        try:
            if base["requestId"] is None:
                raise DeviceHubError("bad_request", "requestId is malformed")
            if base["op"] is None:
                raise DeviceHubError("unknown_op")
            if op not in READ_OPS and self._paused():
                raise DeviceHubError("paused", "Qronos is paused")
            run = self._operation(op, data)  # validates synchronously
        except DeviceHubError as error:
            self._answer({**base, "ok": False, "code": error.code, "detail": str(error)[:200]})
            return

        def work() -> None:
            try:
                with self._lock:
                    result = run()
            except DeviceHubError as error:
                result = {"ok": False, "code": error.code, "detail": str(error)[:200]}
            except SpoolerUnavailable as error:
                result = {"ok": False, "code": "spooler_unavailable", "detail": str(error)[:200]}
            except Exception as error:  # noqa: BLE001 - typed answer, never a crash
                result = {"ok": False, "code": "internal", "detail": type(error).__name__}
            self._answer({**base, **result})

        self._run_async(work)

    def _answer(self, payload: dict[str, Any]) -> None:
        payload.pop("secret", None)
        self._emit(RESULT_EVENT, "ready" if payload.get("ok") else "warning", payload)

    def _operation(self, op: str, data: dict) -> Callable[[], dict[str, Any]]:
        if op == "devices.list":
            return lambda: {"ok": True, "devices": [d.to_json() for d in self._devices()],
                            "unsupportedClasses": [dict(item) for item in UNSUPPORTED_CLASSES]}
        if op == "device.jobs":
            device_id_ = _device_id(data)
            return lambda: self._jobs(device_id_)
        if op == "job.control":
            device_id_, job_id = _device_id(data), _int(data, "jobId", 1, 2**31 - 1)
            command = data.get("command")
            if command not in JOB_COMMANDS:
                raise DeviceHubError("bad_request", "command must be pause, resume or cancel")
            return lambda: self._prepare_job_control(device_id_, job_id, command, data["requestId"])
        if op == "print.request":
            device_id_ = _device_id(data)
            path = data.get("path")
            if not isinstance(path, str) or not path.strip() or len(path) > MAX_PATH_CHARS or "\0" in path \
                    or not Path(path).is_absolute():
                raise DeviceHubError("bad_request", "path must be one exact absolute file path")
            copies = _int(data, "copies", 1, 99, default=1)
            color = data.get("color", "default")
            if color not in ("default", "color", "monochrome"):
                raise DeviceHubError("bad_request", "color must be default, color or monochrome")
            pages = data.get("pages", "all")
            if not isinstance(pages, str) or not _PAGES.match(pages):
                raise DeviceHubError("bad_request", "pages must be all or ranges such as 1-3,5")
            return lambda: self._print(device_id_, path, copies, color, pages, data["requestId"])
        if op == "decide":
            approval_id = _approval_id(data)
            secret = data.get("secret")
            if secret is not None and (not isinstance(secret, str) or len(secret) > MAX_SECRET_CHARS):
                raise DeviceHubError("bad_request", "secret must be a short string")  # never echoed
            if data.get("approved") is not True:
                raise DeviceHubError("bad_request", "use decline to refuse")
            return lambda: self._decide(approval_id, secret)
        approval_id = _approval_id(data)
        return lambda: self._decline(approval_id)

    # ------------------------------------------------------------ inventory

    def _devices(self) -> list[Device]:
        source = self._source()
        devices = []
        for printer in source.printers()[:MAX_DEVICES]:
            looks_3d = bool(_LOOKS_3D.search(printer.name) or _LOOKS_3D.search(printer.driver))
            if looks_3d:
                devices.append(Device(device_id(printer), printer, "3d_printer", {}, (), "3d_workflow_pending"))
                continue
            try:
                capabilities = source.capabilities(printer.name, printer.port)
            except Exception:  # noqa: BLE001 - an unreadable driver is reported, never guessed
                capabilities = {}
            workflows = ("jobs_view", "job_control_own")
            if capabilities:
                workflows = ("print_document",) + workflows
            devices.append(Device(device_id(printer), printer, "printer", capabilities, workflows,
                                  "" if capabilities else "driver_capabilities_unreadable"))
        return devices

    def _device(self, device_id_: str) -> Device:
        found = [device for device in self._devices() if device.id == device_id_]
        if not found:
            raise DeviceHubError("device_gone", "the device is no longer installed in Windows")
        return found[0]

    def _jobs(self, device_id_: str) -> dict[str, Any]:
        device = self._device(device_id_)
        source = self._source()
        user = source.current_user().casefold()
        jobs = [{"jobId": job.job_id, "document": job.document[:200], "own": job.user.casefold() == user,
                 "status": [text for bit, text in _JOB_STATUS.items() if job.status & bit],
                 "pagesPrinted": job.pages_printed, "totalPages": job.total_pages}
                for job in source.jobs(device.printer.name)[:MAX_JOBS]]
        return {"ok": True, "deviceId": device.id, "jobs": jobs}

    # ------------------------------------------------------------ workflows

    def _print(self, device_id_: str, path: str, copies: int, color: str, pages: str, request_id: str) -> dict:
        from core.app_workflow_intents import PrintIntent

        device = self._device(device_id_)
        if "print_document" not in device.workflows:
            raise DeviceHubError("unsupported", device.unsupported or "this device does not print documents")
        copies_max = device.capabilities.get("copiesMax")
        if copies_max and copies > copies_max:
            raise DeviceHubError("over_capability", f"this device prints at most {copies_max} copies")
        if color == "color" and device.capabilities.get("color") is False:
            raise DeviceHubError("over_capability", "this device does not print in colour")
        # The reviewed print step: exact file, exact queue, its own gate/approval/secret/one verified job.
        message = self._begin_print(PrintIntent(path, device.printer.name, copies=copies, color=color, pages=pages),
                                    request_id)
        return {"ok": True, "status": "print_requested", "deviceId": device.id, "message": message}

    def _own_job(self, device: Device, job_id: int) -> PrintJobState:
        source = self._source()
        user = source.current_user().casefold()
        for job in source.jobs(device.printer.name):
            if job.job_id == job_id:
                if job.user.casefold() != user:
                    raise DeviceHubError("not_own_job", "only your own print jobs can be changed")
                return job
        raise DeviceHubError("job_gone", "the job is no longer in the queue")

    def _prepare_job_control(self, device_id_: str, job_id: int, command: str, request_id: str) -> dict:
        device = self._device(device_id_)
        if "job_control_own" not in device.workflows:
            raise DeviceHubError("unsupported", device.unsupported or "jobs cannot be changed on this device")
        job = self._own_job(device, job_id)
        request = ActionRequest(
            category=ActionCategory.DEVICE_CONTROL, target=f"printer:{device.printer.name}/job:{job_id}",
            summary=f"{command.capitalize()} one of your own print jobs.",
            parameters={"deviceId": device.id, "jobId": job_id, "command": command},
        )
        verdict = self._gate(request)
        if verdict.refused:
            return {"ok": False, "code": "denied", "detail": str(verdict.reason)[:200]}
        if verdict.allowed:
            return self._run_job_control(device, job, command, request)
        ticket = self._ledger.issue(ApprovalKind.DEVICE_ACTION, f"{command} job {job_id}", payload=PendingJobControl(
            device.id, device.printer.name, job_id, job.document, command, request, verdict.decision, request_id))
        return {"ok": True, "status": "approval_required", "approvalId": ticket.approval_id,
                "needsSecret": verdict.decision is PermissionDecision.REQUIRE_TYPED_SECRET,
                "preview": {"device": device.printer.name, "document": job.document[:200], "command": command}}

    def _gate(self, request: ActionRequest):
        evaluate = self._evaluate or security_gate.evaluate  # looked up per call: the CURRENT policy decides
        return evaluate(request, audit=self._audit.record_verdict if self._audit is not None else None)

    def _decide(self, approval_id: str, secret: str | None) -> dict:
        try:
            ticket = self._ledger.take(approval_id, kinds=(ApprovalKind.DEVICE_ACTION,))
        except StaleApproval as stale:
            return {"ok": False, "code": "stale_approval", "detail": stale.reason}
        pending = ticket.payload
        if not isinstance(pending, PendingJobControl):
            return {"ok": False, "code": "stale_approval", "detail": "not a device approval"}
        verdict = self._gate(pending.request)  # the CURRENT policy decides again
        if verdict.refused:
            self._record(AuditEvent.DENIED_BY_USER, pending.request, decision=verdict.decision)
            return {"ok": False, "code": "denied", "detail": str(verdict.reason)[:200]}
        needs_secret = PermissionDecision.REQUIRE_TYPED_SECRET in (pending.decision, verdict.decision)
        if needs_secret:
            try:
                verified = self._verify_secret is not None and bool(self._verify_secret(secret or ""))
            except Exception:  # noqa: BLE001 - an unverifiable secret is a refusal
                verified = False
            if not verified:  # the single-use identity is spent; a new request is needed
                return {"ok": False, "code": "secret_rejected"}
        self._record(AuditEvent.APPROVED, pending.request, decision=verdict.decision)
        device = self._device(pending.device_id)
        job = self._own_job(device, pending.job_id)
        if job.document != pending.document:
            return {"ok": False, "code": "job_changed", "detail": "the job is not the one that was approved"}
        return self._run_job_control(device, job, pending.command, pending.request)

    def _decline(self, approval_id: str) -> dict:
        try:
            ticket = self._ledger.take(approval_id, kinds=(ApprovalKind.DEVICE_ACTION,))
        except StaleApproval as stale:
            return {"ok": False, "code": "stale_approval", "detail": stale.reason}
        if isinstance(ticket.payload, PendingJobControl):
            self._record(AuditEvent.DENIED_BY_USER, ticket.payload.request)
        return {"ok": True, "declined": True}

    def _run_job_control(self, device: Device, job: PrintJobState, command: str, request: ActionRequest) -> dict:
        try:
            self._source().control_own_job(device.printer.name, job.job_id, JOB_COMMANDS[command])
        except Exception as error:  # noqa: BLE001 - reported, audited as failed
            self._record(AuditEvent.COMPLETED, request, outcome=ActionOutcome.FAILED)
            return {"ok": False, "code": "spooler_refused", "detail": type(error).__name__}
        self._record(AuditEvent.COMPLETED, request, outcome=ActionOutcome.SUCCEEDED)
        return {"ok": True, "status": "done", "deviceId": device.id, "jobId": job.job_id, "command": command}

    def _record(self, event: AuditEvent, request: ActionRequest, *, outcome=None, decision=None) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(event, request, outcome=outcome, decision=decision)
        except Exception:  # noqa: BLE001 - auditing never hides the result
            pass


def _device_id(data: dict) -> str:
    value = data.get("deviceId")
    if not isinstance(value, str) or not _DEVICE_ID.match(value):
        raise DeviceHubError("bad_request", "deviceId is malformed")
    return value


def _approval_id(data: dict) -> str:
    value = data.get("approvalId")
    if not isinstance(value, str) or not _APPROVAL_ID.match(value):
        raise DeviceHubError("bad_request", "approvalId is malformed")
    return value


def _int(data: dict, name: str, low: int, high: int, *, default: int | None = None) -> int:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not (low <= value <= high):
        raise DeviceHubError("bad_request", f"{name} must be an integer in {low}..{high}")
    return value


def default_source() -> DeviceSource:
    if os.name != "nt":
        raise SpoolerUnavailable("The Windows print spooler is only available on Windows.")
    return WinDeviceSpooler()
