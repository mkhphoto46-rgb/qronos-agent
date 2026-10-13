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
                              DEVICE_ACTION approval (typed secret when the policy requires it), audited;
* hand one exact .3mf to a  - only for a queue whose driver PROVES 3D capability (psk3d keywords in its
  proven 3D printer           PrintCapabilities, core/print3d_native.py): the Files read gate, the DEVICE_CONTROL
                              gate, always a single-use DEVICE_ACTION approval with the typed secret, the package
                              re-read and re-hashed at the answer, then the Windows 3D print path. The answer claims
                              only that Windows accepted the package for the queue, never that a part was printed.

Truthfully unsupported (shown as such, never as a capability): a queue that only LOOKS like a 3D printer by its
name or driver but whose driver does not prove it (no workflow, not even document printing), and every other
device class (scanners, cameras, other local devices). Desktop requests are typed ``qronos.device_hub`` actions
``{op, requestId, ...}``; answers are ``device_hub_result`` runtime events.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path
from ctypes import wintypes
from typing import Any, Callable, Protocol

from core.action_audit import ActionAuditLog, AuditEvent
from core.actions import ActionOutcome, ActionRequest
from core.approval_ledger import ApprovalKind, ApprovalLedger, StaleApproval
from core.power_wake import PowerWakeError, WakeCapability, default_power
from core.wia_imaging import (FORMATS as IMAGE_FORMATS, ImagingDevice, ImagingError, check_destination,
                              default_imaging, finalize_scan, new_staging, remove_staging)
from core.camera_capture import CameraDevice, CameraError, default_camera, save_frame
from core.print3d_native import MAX_PACKAGE_BYTES, Package3MF, Print3DError, Win3DPrinting, read_3mf
from core.print_workflow import PrinterState, PrintJobState, SpoolerUnavailable, WinSpooler, _is_link
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

OPS = frozenset({"devices.list", "device.jobs", "job.control", "print.request", "print3d.request", "wake.status",
                 "wake.schedule", "wake.cancel", "imaging.list", "image.capture", "camera.list", "camera.capture",
                 "decide",
                 "decline"})
READ_OPS = frozenset({"devices.list", "device.jobs", "wake.status", "imaging.list", "camera.list", "decline"})
_IMAGING_ID = re.compile(r"^img-[0-9a-f]{20}$")
#: Imaging devices WIA 2.0 lists but this path does not drive (truthful; never captured).
IMAGING_UNSUPPORTED = {"camera": "wia_camera_not_supported", "video": "webcam_not_supported",
                       "other": "not_supported"}
#: What a scan here is and is not (stable codes; Persian text in the desktop).
SCAN_LIMITS = ("scanners_only_via_wia", "windows_dialog_drives_the_scan", "one_page_one_new_file",
               "no_overwrite_no_links", "metadata_not_copied", "no_camera_webcam_or_phone_capture")
_CAMERA_ID = re.compile(r"^cam-[0-9a-f]{20}$")
#: What a camera capture here is and is not (stable codes; Persian text in the desktop).
CAMERA_LIMITS = ("cameras_windows_lists_only", "one_still_frame_no_video_no_audio", "camera_on_only_during_capture",
                 "one_new_file_no_overwrite_no_links", "no_recognition_no_model", "no_wia_still_cameras_or_unlisted_phones")

# Wake THIS PC from sleep (H3): a hardware wake timer, never a voice wake or a network packet.
WAKE_TARGET = "power:this-pc/wake-timer"
WAKE_EVENT = "device_hub_wake"
MIN_WAKE_LEAD_MINUTES = 2
MAX_WAKE_DAYS = 7
MAX_STAY_AWAKE_MINUTES = 120
#: What a wake timer cannot do, shown with every status and approval (stable codes; Persian text in the desktop).
WAKE_LIMITS = ("qronos_must_keep_running", "no_shutdown_hibernate_or_sign_out", "power_plan_allows_wake_timers",
               "modern_standby_keeps_display_off", "no_voice_or_brain_while_asleep", "fired_is_not_proof_of_wake")

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
    {"kind": "camera", "reason": "not_supported_yet",
     "detail": "دوربین عکاسی WIA 1.0/MTP و گوشی یا دوربین شبکه‌ای که ویندوز آن را دوربین فهرست نکند پشتیبانی نمی‌شوند؛ وب‌کم‌ها فقط از بخش «یک عکس از دوربین»."},
    {"kind": "other_local_device", "reason": "not_supported_yet",
     "detail": "دستگاه‌های محلیِ دیگر هنوز در Device Hub پشتیبانی نمی‌شوند."},
)
#: A print queue whose name or driver says 3D/3MF but whose driver does not PROVE 3D capability is offered nothing:
#: not 3D printing (unproven) and not document printing (it may be a 3D printer).
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
    def current_machines(self) -> tuple[str, ...]: ...  # this computer's own names (NetBIOS, DNS)
    def capabilities_3d(self, printer: str) -> dict[str, Any] | None: ...
    def submit_3d(self, printer: str, job_name: str, package: Package3MF) -> None: ...


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

    def current_machines(self) -> tuple[str, ...]:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel.GetComputerNameExW
        function.argtypes = [ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        function.restype = wintypes.BOOL
        names = []
        for kind in (0, 1, 3):  # ComputerNameNetBIOS, ComputerNameDnsHostname, ComputerNameDnsFullyQualified
            size = wintypes.DWORD(256)
            buffer = ctypes.create_unicode_buffer(size.value)
            if function(kind, buffer, ctypes.byref(size)) and buffer.value:
                names.append(buffer.value)
        if not names:
            raise SpoolerUnavailable("GetComputerNameEx failed.")
        return tuple(names)

    def _native3d(self) -> Win3DPrinting:
        if getattr(self, "_win3d", None) is None:
            self._win3d = Win3DPrinting()
        return self._win3d

    def capabilities_3d(self, printer: str) -> dict[str, Any] | None:
        return self._native3d().capabilities(printer)

    def submit_3d(self, printer: str, job_name: str, package: Package3MF) -> None:
        self._native3d().submit(printer, job_name, package)


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


@dataclass(frozen=True)
class PendingPrint3D:
    """The payload of one DEVICE_ACTION approval to hand one exact .3mf to a proven 3D printer."""

    device_id: str
    printer: str
    path: Path
    sha256: str  # "" when the Files gate required approval before the package could be read
    job_name: str
    read_request: ActionRequest
    read_decision: PermissionDecision
    request: ActionRequest
    decision: PermissionDecision
    request_id: str = ""


@dataclass(frozen=True)
class PendingScan:
    """The payload of one DEVICE_ACTION approval to scan one page into one new image file."""

    device_id: str
    wia_id: str
    name: str
    destination: Path
    fmt: str
    device_request: ActionRequest
    device_decision: PermissionDecision
    file_request: ActionRequest
    file_decision: PermissionDecision
    request_id: str = ""


@dataclass(frozen=True)
class PendingCamera:
    """The payload of one DEVICE_ACTION approval to take ONE still frame into one new image file."""

    device_id: str
    symbolic_link: str
    name: str
    destination: Path
    fmt: str
    device_request: ActionRequest
    device_decision: PermissionDecision
    file_request: ActionRequest
    file_decision: PermissionDecision
    request_id: str = ""


@dataclass(frozen=True)
class PendingWake:
    """The payload of one DEVICE_ACTION approval to arm a wake timer on this PC."""

    due: datetime
    stay: int
    request: ActionRequest
    decision: PermissionDecision
    request_id: str = ""


@dataclass(frozen=True)
class PendingWakeCancel:
    wake_id: str
    request: ActionRequest
    decision: PermissionDecision
    request_id: str = ""


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
        power: Callable[[], Any] = default_power,
        imaging: Callable[[], Any] = default_imaging,
        camera: Callable[[], Any] = default_camera,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
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
        self._power_factory, self._power, self._now = power, None, now
        self._imaging_factory, self._imaging = imaging, None
        self._camera_factory, self._camera = camera, None
        self._wake_lock = threading.Lock()
        self._armed: Any = None
        self._wake_info: dict[str, Any] | None = None
        self._wake_epoch = 0  # bumped by cancel_wake (Pause/Stop/shutdown): an answer begun before it never arms

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
        if op == "print3d.request":
            device_id_ = _device_id(data)
            path = data.get("path")
            if not isinstance(path, str) or not path.strip() or len(path) > MAX_PATH_CHARS or "\0" in path \
                    or not Path(path).is_absolute() or Path(path).suffix.casefold() != ".3mf":
                raise DeviceHubError("bad_request", "path must be one exact absolute .3mf file path")
            return lambda: self._prepare_print3d(device_id_, Path(path), data["requestId"])
        if op == "imaging.list":
            return self._imaging_list
        if op == "image.capture":
            value = data.get("deviceId")
            if not isinstance(value, str) or not _IMAGING_ID.match(value):
                raise DeviceHubError("bad_request", "deviceId is malformed")
            path = data.get("destination")
            if not isinstance(path, str) or not path.strip() or len(path) > MAX_PATH_CHARS or "\0" in path \
                    or not Path(path).is_absolute():
                raise DeviceHubError("bad_request", "destination must be one exact absolute image path")
            return lambda: self._prepare_scan(value, Path(path), data["requestId"])
        if op == "camera.list":
            return self._camera_list
        if op == "camera.capture":
            value = data.get("deviceId")
            if not isinstance(value, str) or not _CAMERA_ID.match(value):
                raise DeviceHubError("bad_request", "deviceId is malformed")
            path = data.get("destination")
            if not isinstance(path, str) or not path.strip() or len(path) > MAX_PATH_CHARS or "\0" in path \
                    or not Path(path).is_absolute():
                raise DeviceHubError("bad_request", "destination must be one exact absolute image path")
            return lambda: self._prepare_camera(value, Path(path), data["requestId"])
        if op == "wake.status":
            return self._wake_status
        if op == "wake.schedule":
            due = _wake_time(data)
            stay = _int(data, "stayAwakeMinutes", 0, MAX_STAY_AWAKE_MINUTES, default=10)
            return lambda: self._prepare_wake(due, stay, data["requestId"])
        if op == "wake.cancel":
            return lambda: self._prepare_wake_cancel(data["requestId"])
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
            try:
                proven_3d = source.capabilities_3d(printer.name)
            except Exception:  # noqa: BLE001 - an unreadable 3D capability is not proof
                proven_3d = None
            if proven_3d:  # the driver itself declares the 3D manufacturing keywords
                devices.append(Device(device_id(printer), printer, "3d_printer", dict(proven_3d),
                                      ("print_3d", "jobs_view", "job_control_own")))
                continue
            if _LOOKS_3D.search(printer.name) or _LOOKS_3D.search(printer.driver):
                devices.append(Device(device_id(printer), printer, "3d_printer", {}, (), "3d_capability_unproven"))
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
        is_own = _owner_check(source, device)
        jobs = [{"jobId": job.job_id, "document": job.document[:200], "own": is_own(job),
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
        is_own = _owner_check(source, device)
        for job in source.jobs(device.printer.name):
            if job.job_id == job_id:
                if not is_own(job):
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
        with self._wake_lock:
            epoch = self._wake_epoch  # captured BEFORE the ticket is taken (B1)
        try:
            ticket = self._ledger.take(approval_id, kinds=(ApprovalKind.DEVICE_ACTION,))
        except StaleApproval as stale:
            return {"ok": False, "code": "stale_approval", "detail": stale.reason}
        pending = ticket.payload
        if isinstance(pending, PendingPrint3D):
            return self._decide_print3d(pending, secret)
        if isinstance(pending, PendingWake):
            return self._decide_wake(pending, secret, epoch)
        if isinstance(pending, PendingWakeCancel):
            return self._decide_wake_cancel(pending, secret)
        if isinstance(pending, PendingScan):
            return self._decide_scan(pending, secret, epoch)
        if isinstance(pending, PendingCamera):
            return self._decide_camera(pending, secret, epoch)
        if not isinstance(pending, PendingJobControl):
            return {"ok": False, "code": "stale_approval", "detail": "not a device approval"}
        verdict = self._gate(pending.request)  # the CURRENT policy decides again
        if verdict.refused:
            self._record_policy_refusal(pending.request, pending.decision)
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
        if isinstance(ticket.payload, (PendingJobControl, PendingPrint3D, PendingWake, PendingWakeCancel)):
            self._record(AuditEvent.DENIED_BY_USER, ticket.payload.request)
        elif isinstance(ticket.payload, (PendingScan, PendingCamera)):
            self._record(AuditEvent.DENIED_BY_USER, ticket.payload.device_request)
        return {"ok": True, "declined": True}

    def _run_job_control(self, device: Device, job: PrintJobState, command: str, request: ActionRequest) -> dict:
        try:
            self._source().control_own_job(device.printer.name, job.job_id, JOB_COMMANDS[command])
        except Exception as error:  # noqa: BLE001 - reported, audited as failed
            self._record(AuditEvent.COMPLETED, request, outcome=ActionOutcome.FAILED)
            return {"ok": False, "code": "spooler_refused", "detail": type(error).__name__}
        self._record(AuditEvent.COMPLETED, request, outcome=ActionOutcome.SUCCEEDED)
        return {"ok": True, "status": "done", "deviceId": device.id, "jobId": job.job_id, "command": command}

    # ------------------------------------------------------------ 3D

    def _read_package(self, path: Path, device: Device) -> Package3MF:
        _checked_3mf(path)
        try:
            return read_3mf(path, device.capabilities)
        except Print3DError as error:
            raise DeviceHubError(error.code, str(error)) from None
        except OSError as error:
            raise DeviceHubError("file_unreadable", type(error).__name__) from None

    def _prepare_print3d(self, device_id_: str, path: Path, request_id: str) -> dict:
        device = self._device(device_id_)
        if "print_3d" not in device.workflows:
            raise DeviceHubError("unsupported", device.unsupported or "this device is not a proven 3D printer")
        if self._verify_secret is None:
            raise DeviceHubError("secret_unavailable", "3D printing needs the typed approval secret")
        _checked_3mf(path)
        read_request = ActionRequest(
            category=ActionCategory.READ_NON_SENSITIVE, target=str(path),
            summary="Read one exact 3MF package to hand to a 3D printer.",
            parameters={"deviceId": device.id, "reads": "3mf_package"},
        )
        read_verdict = self._gate(read_request)  # the Files policy decides before any byte is read
        if read_verdict.refused:
            return {"ok": False, "code": "denied", "detail": str(read_verdict.reason)[:200]}
        package = self._read_package(path, device) if read_verdict.allowed else None
        request = ActionRequest(
            category=ActionCategory.DEVICE_CONTROL, target=f"printer:{device.printer.name}/3d",
            summary="Hand one exact 3MF package to a 3D printer's Windows driver.",
            parameters={"deviceId": device.id, "file": str(path), "sha256": package.sha256 if package else ""},
        )
        verdict = self._gate(request)
        if verdict.refused:
            return {"ok": False, "code": "denied", "detail": str(verdict.reason)[:200]}
        # Physical fabrication: always a single-use approval with the typed secret, whatever the policy allows.
        job_name = "Qronos 3D " + secrets.token_hex(6)
        ticket = self._ledger.issue(ApprovalKind.DEVICE_ACTION, f"3D print {path.name}", payload=PendingPrint3D(
            device.id, device.printer.name, path, package.sha256 if package else "", job_name,
            read_request, read_verdict.decision, request, verdict.decision, request_id))
        preview: dict[str, Any] = {"device": device.printer.name, "file": str(path)[:400], "jobName": job_name,
                                   "buildVolumeMicrons": device.capabilities.get("buildVolumeMicrons")}
        if package is not None:
            preview.update({"size": package.size, "sha256": package.sha256, "textures": len(package.textures),
                            "threeMfVersion": package.model_namespace[:200]})
        else:
            preview["readAfterApproval"] = True
        return {"ok": True, "status": "approval_required", "approvalId": ticket.approval_id, "needsSecret": True,
                "preview": preview}

    def _decide_print3d(self, pending: PendingPrint3D, secret: str | None) -> dict:
        read_verdict, verdict = self._gate(pending.read_request), self._gate(pending.request)  # CURRENT policies
        if read_verdict.refused or verdict.refused:
            refused = verdict if verdict.refused else read_verdict
            self._record_policy_refusal(pending.request, PermissionDecision.REQUIRE_TYPED_SECRET)
            return {"ok": False, "code": "denied", "detail": str(refused.reason)[:200]}
        try:
            verified = self._verify_secret is not None and bool(self._verify_secret(secret or ""))
        except Exception:  # noqa: BLE001 - an unverifiable secret is a refusal
            verified = False
        if not verified:  # the single-use identity is spent; a new request is needed
            return {"ok": False, "code": "secret_rejected"}
        self._record(AuditEvent.APPROVED, pending.read_request, decision=read_verdict.decision)
        self._record(AuditEvent.APPROVED, pending.request, decision=verdict.decision)
        device = self._device(pending.device_id)
        if "print_3d" not in device.workflows:  # the driver no longer proves 3D capability
            return {"ok": False, "code": "capability_gone", "detail": "the device no longer reports 3D printing"}
        package = self._read_package(pending.path, device)  # read once more: these exact bytes are handed over
        if pending.sha256 and package.sha256 != pending.sha256:
            return {"ok": False, "code": "file_changed", "detail": "the file is not the one that was approved"}
        source = self._source()
        try:
            source.submit_3d(device.printer.name, pending.job_name, package)
        except Exception as error:  # noqa: BLE001 - reported, audited as failed
            self._record(AuditEvent.COMPLETED, pending.request, outcome=ActionOutcome.FAILED)
            code = error.code if isinstance(error, Print3DError) else "spooler_refused"
            return {"ok": False, "code": code, "detail": str(error)[:200] if isinstance(error, Print3DError)
                    else type(error).__name__}
        self._record(AuditEvent.COMPLETED, pending.request, outcome=ActionOutcome.SUCCEEDED)
        try:  # the spooler's own job id, when it lists the unique job name; nothing more is claimed
            job_id = next((job.job_id for job in source.jobs(device.printer.name)
                           if job.document == pending.job_name), None)
        except Exception:  # noqa: BLE001 - the handoff happened; the id is optional
            job_id = None
        return {"ok": True, "status": "handed_to_spooler", "deviceId": device.id, "jobName": pending.job_name,
                "jobId": job_id, "sha256": package.sha256, "size": package.size}

    # ------------------------------------------------------------ wake THIS PC from sleep (H3, core/power_wake.py)

    def _power_layer(self) -> Any:
        if self._power is None:
            try:
                self._power = self._power_factory()
            except PowerWakeError as error:
                raise DeviceHubError("wake_" + error.code, str(error)) from None
        return self._power

    def _wake_capability(self) -> WakeCapability:
        try:
            return self._power_layer().capability()
        except PowerWakeError as error:
            raise DeviceHubError("wake_" + error.code, str(error)) from None
        except OSError as error:
            raise DeviceHubError("wake_capability_unreadable", type(error).__name__) from None

    def _wake_snapshot(self) -> dict[str, Any] | None:
        with self._wake_lock:
            if self._wake_info and self._wake_info.get("state") in ("armed", "fired") and self._armed is not None \
                    and not self._armed.active:
                self._wake_info["state"] = "ended"
            return dict(self._wake_info) if self._wake_info else None

    def _wake_armed(self) -> bool:
        """A wake is live while its timer thread is: armed, or fired and still holding the system awake (B2)."""
        with self._wake_lock:
            return self._armed is not None and bool(self._armed.active)

    def _wake_status(self) -> dict:
        return {"ok": True, "capability": self._wake_capability().to_json(), "armed": self._wake_snapshot(),
                "limits": list(WAKE_LIMITS)}

    def _wake_due_check(self, due: datetime) -> None:
        now = self._now()
        if due < now + timedelta(minutes=MIN_WAKE_LEAD_MINUTES) or due > now + timedelta(days=MAX_WAKE_DAYS):
            raise DeviceHubError("bad_time", "the wake time must be 2 minutes to 7 days from now")

    @staticmethod
    def _wake_request(summary: str, parameters: dict[str, Any]) -> ActionRequest:
        return ActionRequest(category=ActionCategory.DEVICE_CONTROL, target=WAKE_TARGET, summary=summary,
                             parameters=parameters)

    @staticmethod
    def _wake_blocked(capability: WakeCapability) -> dict | None:
        verdict, reason = capability.verdict()
        if verdict in ("supported", "unknown"):  # "unknown": the setting is unreadable; Windows' answer decides
            return None
        return {"ok": False, "code": reason, "capability": capability.to_json()}

    def _prepare_wake(self, due: datetime, stay: int, request_id: str) -> dict:
        if self._verify_secret is None:
            raise DeviceHubError("secret_unavailable", "waking this PC needs the typed approval secret")
        self._wake_due_check(due)
        if self._wake_armed():
            raise DeviceHubError("already_armed", "a wake time is already set; cancel it first")
        capability = self._wake_capability()
        blocked = self._wake_blocked(capability)
        if blocked is not None:
            return blocked
        due_text = due.astimezone(timezone.utc).isoformat(timespec="seconds")
        request = self._wake_request("Wake this PC from sleep at a set time with a hardware wake timer.",
                                     {"dueUtc": due_text, "stayAwakeMinutes": stay})
        verdict = self._gate(request)
        if verdict.refused:
            return {"ok": False, "code": "denied", "detail": str(verdict.reason)[:200]}
        # A power action: always a single-use approval with the typed secret, whatever the policy allows.
        ticket = self._ledger.issue(ApprovalKind.DEVICE_ACTION, f"wake this PC at {due_text}",
                                    payload=PendingWake(due, stay, request, verdict.decision, request_id))
        return {"ok": True, "status": "approval_required", "approvalId": ticket.approval_id, "needsSecret": True,
                "preview": {"action": "wake_schedule", "dueUtc": due_text, "stayAwakeMinutes": stay,
                            "capability": capability.to_json(), "limits": list(WAKE_LIMITS)}}

    def _decide_wake(self, pending: "PendingWake", secret: str | None, epoch: int) -> dict:
        verdict = self._gate(pending.request)  # the CURRENT policy decides again
        if verdict.refused:
            self._record_policy_refusal(pending.request, PermissionDecision.REQUIRE_TYPED_SECRET)
            return {"ok": False, "code": "denied", "detail": str(verdict.reason)[:200]}
        if not self._secret_ok(secret):
            return {"ok": False, "code": "secret_rejected"}
        self._record(AuditEvent.APPROVED, pending.request, decision=verdict.decision)
        self._wake_due_check(pending.due)
        if self._wake_armed():
            return {"ok": False, "code": "already_armed"}
        capability = self._wake_capability()  # read again: the power plan may have changed meanwhile
        blocked = self._wake_blocked(capability)
        if blocked is not None:
            return blocked
        if self._wake_stopped_since(epoch):  # Pause/Stop/shutdown came after the request was taken: arm nothing
            self._record(AuditEvent.COMPLETED, pending.request, outcome=ActionOutcome.REFUSED)
            return {"ok": False, "code": "paused", "detail": "Qronos was paused or stopped; no wake was set"}
        try:
            armed, result = self._power_layer().arm(pending.due, pending.stay, self._on_wake_event)
        except PowerWakeError as error:
            self._record(AuditEvent.COMPLETED, pending.request, outcome=ActionOutcome.FAILED)
            return {"ok": False, "code": "wake_" + error.code, "detail": str(error)[:200]}
        if result.state != "armed":  # Windows accepted the call but says this system cannot resume on a timer
            self._record(AuditEvent.COMPLETED, pending.request, outcome=ActionOutcome.FAILED)
            return {"ok": False, "code": "wake_not_supported",
                    "detail": "Windows reported that this PC cannot resume on a timer"}
        info = {"wakeId": result.wake_id, "dueUtc": result.due_utc, "stayAwakeMinutes": pending.stay,
                "state": "armed", "armedAtUtc": self._now().isoformat(timespec="seconds")}
        paused = bool(self._paused())  # read outside _wake_lock (Pause sets it before cancel_wake bumps the epoch)
        with self._wake_lock:  # stored only if no Pause/Stop/shutdown came meanwhile (atomic with cancel_wake)
            stale = paused or self._wake_epoch != epoch
            if not stale:
                self._armed, self._wake_info = armed, info
        if stale:
            armed.cancel()  # Windows had accepted it: cancel it now, so the PC is not woken while paused
            self._record(AuditEvent.COMPLETED, pending.request, outcome=ActionOutcome.REFUSED)
            return {"ok": False, "code": "paused",
                    "detail": "Qronos was paused or stopped while the wake was being set; it was cancelled"}
        self._record(AuditEvent.COMPLETED, pending.request, outcome=ActionOutcome.SUCCEEDED)
        return {"ok": True, "status": "armed", "claim": "windows_accepted_wake_timer", **info}

    def _prepare_wake_cancel(self, request_id: str) -> dict:
        snapshot = self._wake_snapshot()
        if not snapshot or snapshot.get("state") not in ("armed", "fired") or not self._wake_armed():
            return {"ok": True, "status": "none"}  # a fired wake still holding the system awake can be ended
        request = self._wake_request("Cancel the wake time set for this PC.", {"wakeId": snapshot["wakeId"]})
        verdict = self._gate(request)
        if verdict.refused:
            return {"ok": False, "code": "denied", "detail": str(verdict.reason)[:200]}
        if verdict.allowed:
            return self._run_wake_cancel(snapshot["wakeId"], request)
        ticket = self._ledger.issue(ApprovalKind.DEVICE_ACTION, "cancel the wake time", payload=PendingWakeCancel(
            snapshot["wakeId"], request, verdict.decision, request_id))
        return {"ok": True, "status": "approval_required", "approvalId": ticket.approval_id,
                "needsSecret": verdict.decision is PermissionDecision.REQUIRE_TYPED_SECRET,
                "preview": {"action": "wake_cancel", "dueUtc": snapshot["dueUtc"]}}

    def _decide_wake_cancel(self, pending: "PendingWakeCancel", secret: str | None) -> dict:
        verdict = self._gate(pending.request)
        if verdict.refused:
            self._record_policy_refusal(pending.request, pending.decision)
            return {"ok": False, "code": "denied", "detail": str(verdict.reason)[:200]}
        if PermissionDecision.REQUIRE_TYPED_SECRET in (pending.decision, verdict.decision) \
                and not self._secret_ok(secret):
            return {"ok": False, "code": "secret_rejected"}
        self._record(AuditEvent.APPROVED, pending.request, decision=verdict.decision)
        return self._run_wake_cancel(pending.wake_id, pending.request)

    def _run_wake_cancel(self, wake_id: str, request: ActionRequest) -> dict:
        with self._wake_lock:
            armed = self._armed if self._wake_info and self._wake_info.get("wakeId") == wake_id else None
            if armed is not None:
                self._armed, self._wake_info = None, None
        if armed is None:
            return {"ok": True, "status": "none"}
        armed.cancel()
        self._record(AuditEvent.COMPLETED, request, outcome=ActionOutcome.SUCCEEDED)
        return {"ok": True, "status": "cancelled", "wakeId": wake_id}

    def cancel_wake(self, reason: str) -> bool:
        """Pause, Stop and shutdown: an armed wake timer is cancelled (no gate: it only removes a pending action)."""
        with self._wake_lock:
            armed, info = self._armed, self._wake_info
            self._armed, self._wake_info = None, None
            self._wake_epoch += 1  # an answer still in flight will not arm (B1)
        if armed is None:
            return False
        armed.cancel()
        self._emit(WAKE_EVENT, "ready", {"event": "wake_cancelled", "reason": str(reason)[:64],
                                         "wakeId": (info or {}).get("wakeId")})
        return True

    def _on_wake_event(self, name: str, payload: dict[str, Any]) -> None:
        with self._wake_lock:
            if name == "wake_timer_fired" and self._wake_info and self._wake_info.get("wakeId") == payload.get("wakeId"):
                self._wake_info.update({"state": "fired", "firedAtUtc": payload.get("firedAtUtc")})
        self._emit(WAKE_EVENT, "ready", {"event": name, **{k: payload[k] for k in ("wakeId", "firedAtUtc",
                                                                                  "stayAwakeMinutes") if k in payload}})

    # ------------------------------------------------------------ scan to ONE exact new image file (H1, WIA 2.0)

    def _imaging_layer(self) -> Any:
        if self._imaging is None:
            try:
                self._imaging = self._imaging_factory()
            except ImagingError as error:
                raise DeviceHubError("scan_" + error.code, str(error)) from None
        return self._imaging

    def _imaging_devices(self) -> list[ImagingDevice]:
        try:
            return list(self._imaging_layer().list_devices())[:MAX_DEVICES]
        except ImagingError as error:
            raise DeviceHubError("scan_" + error.code, str(error)) from None

    def _imaging_list(self) -> dict:
        devices = []
        for device in self._imaging_devices():
            unsupported = "" if device.kind == "scanner" else IMAGING_UNSUPPORTED.get(device.kind, "not_supported")
            devices.append({"id": device.device_id, "name": device.name[:200], "vendor": device.vendor[:200],
                            "description": device.description[:200], "kind": device.kind,
                            "workflows": ["scan_to_file"] if not unsupported else [], "unsupported": unsupported})
        return {"ok": True, "devices": devices, "formats": sorted(set(IMAGE_FORMATS)), "limits": list(SCAN_LIMITS)}

    def _scanner(self, device_id_: str) -> ImagingDevice:
        found = [d for d in self._imaging_devices() if d.device_id == device_id_]
        if not found:
            raise DeviceHubError("device_gone", "the scanner is no longer listed by Windows")
        if found[0].kind != "scanner":
            raise DeviceHubError("unsupported", IMAGING_UNSUPPORTED.get(found[0].kind, "not_supported"))
        return found[0]

    @staticmethod
    def _checked_destination(destination: Path) -> str:
        try:
            return check_destination(destination)
        except ImagingError as error:
            raise DeviceHubError(error.code, str(error)) from None

    def _prepare_scan(self, device_id_: str, destination: Path, request_id: str) -> dict:
        if self._verify_secret is None:
            raise DeviceHubError("secret_unavailable", "scanning needs the typed approval secret")
        fmt = self._checked_destination(destination)
        scanner = self._scanner(device_id_)
        device_request = ActionRequest(
            category=ActionCategory.DEVICE_CONTROL, target=f"imaging:{scanner.name[:120]}/scan",
            summary="Scan one page with a Windows WIA scanner (its own dialog) into one new image file.",
            parameters={"deviceId": scanner.device_id})
        file_request = ActionRequest(
            category=ActionCategory.CREATE_OR_EDIT_FILE, target=str(destination),
            summary="Create one new image file from a scan.", parameters={"writes": "scanned_image", "format": fmt})
        device_verdict, file_verdict = self._gate(device_request), self._gate(file_request)
        refused = device_verdict if device_verdict.refused else file_verdict if file_verdict.refused else None
        if refused is not None:
            return {"ok": False, "code": "denied", "detail": str(refused.reason)[:200]}
        # A device that captures what is on the glass, plus a new file: always single use + the typed secret.
        ticket = self._ledger.issue(ApprovalKind.DEVICE_ACTION, f"scan to {destination.name}", payload=PendingScan(
            scanner.device_id, scanner.wia_id, scanner.name, destination, fmt, device_request, device_verdict.decision,
            file_request, file_verdict.decision, request_id))
        return {"ok": True, "status": "approval_required", "approvalId": ticket.approval_id, "needsSecret": True,
                "preview": {"action": "scan", "device": scanner.name[:200], "file": str(destination)[:400],
                            "format": fmt, "limits": list(SCAN_LIMITS)}}

    def _decide_scan(self, pending: "PendingScan", secret: str | None, epoch: int) -> dict:
        device_verdict, file_verdict = self._gate(pending.device_request), self._gate(pending.file_request)
        if device_verdict.refused or file_verdict.refused:
            refused = device_verdict if device_verdict.refused else file_verdict
            self._record_policy_refusal(pending.device_request, PermissionDecision.REQUIRE_TYPED_SECRET)
            return {"ok": False, "code": "denied", "detail": str(refused.reason)[:200]}
        if not self._secret_ok(secret):
            return {"ok": False, "code": "secret_rejected"}
        self._record(AuditEvent.APPROVED, pending.device_request, decision=device_verdict.decision)
        self._record(AuditEvent.APPROVED, pending.file_request, decision=file_verdict.decision)
        if self._wake_stopped_since(epoch):  # Pause/Stop/shutdown came after the request was taken
            self._record(AuditEvent.COMPLETED, pending.device_request, outcome=ActionOutcome.REFUSED)
            return {"ok": False, "code": "paused", "detail": "Qronos was paused or stopped; nothing was scanned"}
        scanner = self._scanner(pending.device_id)
        if scanner.wia_id != pending.wia_id:
            return {"ok": False, "code": "device_gone", "detail": "the scanner is not the one that was approved"}
        self._checked_destination(pending.destination)
        if self._wake_stopped_since(epoch):  # checked again right before Windows' dialog would open
            self._record(AuditEvent.COMPLETED, pending.device_request, outcome=ActionOutcome.REFUSED)
            return {"ok": False, "code": "paused", "detail": "Qronos was paused or stopped; nothing was scanned"}
        staging = new_staging()
        try:
            try:
                staged = self._imaging_layer().acquire(pending.wia_id, staging)  # Windows' own WIA dialog
            except ImagingError as error:
                self._record(AuditEvent.COMPLETED, pending.device_request, outcome=ActionOutcome.FAILED)
                return {"ok": False, "code": "scan_" + error.code, "detail": str(error)[:200]}
            if not staged:
                self._record(AuditEvent.COMPLETED, pending.device_request, outcome=ActionOutcome.REFUSED)
                return {"ok": False, "code": "scan_cancelled", "detail": "the scan was cancelled; nothing was written"}
            if self._wake_stopped_since(epoch):  # Pause/Stop came while the dialog was open: keep nothing
                self._record(AuditEvent.COMPLETED, pending.device_request, outcome=ActionOutcome.REFUSED)
                return {"ok": False, "code": "paused",
                        "detail": "Qronos was paused or stopped during the scan; nothing was written"}
            try:
                saved = finalize_scan(staged, staging, pending.destination)
            except ImagingError as error:
                self._record(AuditEvent.COMPLETED, pending.file_request, outcome=ActionOutcome.FAILED)
                return {"ok": False, "code": error.code, "detail": str(error)[:200]}
        finally:
            remove_staging(staging)
        self._record(AuditEvent.COMPLETED, pending.device_request, outcome=ActionOutcome.SUCCEEDED)
        self._record(AuditEvent.COMPLETED, pending.file_request, outcome=ActionOutcome.SUCCEEDED)
        return {"ok": True, "status": "saved", "deviceId": pending.device_id, **saved}

    # ------------------------------------------------------------ ONE still frame from a camera (H1, Media Foundation)

    def _camera_layer(self) -> Any:
        if self._camera is None:
            try:
                self._camera = self._camera_factory()
            except CameraError as error:
                raise DeviceHubError(error.code if error.code.startswith("camera_") else "camera_" + error.code,
                                     str(error)) from None
        return self._camera

    def _camera_devices(self) -> list[CameraDevice]:
        try:
            return list(self._camera_layer().list_devices())[:MAX_DEVICES]
        except CameraError as error:
            raise DeviceHubError(error.code if error.code.startswith("camera_") else "camera_" + error.code,
                                 str(error)) from None

    def _camera_list(self) -> dict:
        devices = [{"id": device.device_id, "name": device.name[:200] or "دوربین", "workflows": ["still_to_file"]}
                   for device in self._camera_devices()]
        return {"ok": True, "devices": devices, "formats": sorted(set(IMAGE_FORMATS)), "limits": list(CAMERA_LIMITS)}

    def _camera_device(self, device_id_: str) -> CameraDevice:
        found = [d for d in self._camera_devices() if d.device_id == device_id_]
        if not found:
            raise DeviceHubError("device_gone", "the camera is no longer listed by Windows")
        return found[0]

    def _prepare_camera(self, device_id_: str, destination: Path, request_id: str) -> dict:
        if self._verify_secret is None:
            raise DeviceHubError("secret_unavailable", "a camera capture needs the typed approval secret")
        fmt = self._checked_destination(destination)
        camera = self._camera_device(device_id_)
        device_request = ActionRequest(
            category=ActionCategory.DEVICE_CONTROL, target=f"camera:{camera.name[:120]}/capture",
            summary="Turn on a local camera briefly and keep ONE still frame (no video, no audio, no preview).",
            parameters={"deviceId": camera.device_id})
        file_request = ActionRequest(
            category=ActionCategory.CREATE_OR_EDIT_FILE, target=str(destination),
            summary="Create one new image file from one camera frame.",
            parameters={"writes": "camera_still", "format": fmt})
        device_verdict, file_verdict = self._gate(device_request), self._gate(file_request)
        refused = device_verdict if device_verdict.refused else file_verdict if file_verdict.refused else None
        if refused is not None:
            return {"ok": False, "code": "denied", "detail": str(refused.reason)[:200]}
        # A camera sees the room and the person: always single use + the typed secret.
        ticket = self._ledger.issue(ApprovalKind.DEVICE_ACTION, f"camera to {destination.name}", payload=PendingCamera(
            camera.device_id, camera.symbolic_link, camera.name, destination, fmt, device_request,
            device_verdict.decision, file_request, file_verdict.decision, request_id))
        return {"ok": True, "status": "approval_required", "approvalId": ticket.approval_id, "needsSecret": True,
                "preview": {"action": "camera", "device": camera.name[:200], "file": str(destination)[:400],
                            "format": fmt, "limits": list(CAMERA_LIMITS)}}

    def _decide_camera(self, pending: "PendingCamera", secret: str | None, epoch: int) -> dict:
        device_verdict, file_verdict = self._gate(pending.device_request), self._gate(pending.file_request)
        if device_verdict.refused or file_verdict.refused:
            refused = device_verdict if device_verdict.refused else file_verdict
            self._record_policy_refusal(pending.device_request, PermissionDecision.REQUIRE_TYPED_SECRET)
            return {"ok": False, "code": "denied", "detail": str(refused.reason)[:200]}
        if not self._secret_ok(secret):
            return {"ok": False, "code": "secret_rejected"}
        self._record(AuditEvent.APPROVED, pending.device_request, decision=device_verdict.decision)
        self._record(AuditEvent.APPROVED, pending.file_request, decision=file_verdict.decision)
        stopped = {"ok": False, "code": "paused", "detail": "Qronos was paused or stopped; the camera was not used"}
        if self._wake_stopped_since(epoch):  # Pause/Stop/shutdown came after the request was taken
            self._record(AuditEvent.COMPLETED, pending.device_request, outcome=ActionOutcome.REFUSED)
            return stopped
        camera = self._camera_device(pending.device_id)
        if camera.symbolic_link.casefold() != pending.symbolic_link.casefold():
            self._record(AuditEvent.COMPLETED, pending.device_request, outcome=ActionOutcome.REFUSED)
            return {"ok": False, "code": "device_gone", "detail": "the camera is not the one that was approved"}
        self._checked_destination(pending.destination)
        if self._wake_stopped_since(epoch):  # checked again right before the camera would turn on
            self._record(AuditEvent.COMPLETED, pending.device_request, outcome=ActionOutcome.REFUSED)
            return stopped
        try:  # Pause/Stop/shutdown is polled during the capture and stops the camera
            frame = self._camera_layer().capture(pending.symbolic_link, lambda: self._wake_stopped_since(epoch))
        except CameraError as error:
            cancelled = error.code == "cancelled"
            self._record(AuditEvent.COMPLETED, pending.device_request,
                         outcome=ActionOutcome.REFUSED if cancelled else ActionOutcome.FAILED)
            if cancelled:
                return {"ok": False, "code": "paused",
                        "detail": "Qronos was paused or stopped during the capture; nothing was written"}
            return {"ok": False, "code": error.code, "detail": str(error)[:200]}
        if self._wake_stopped_since(epoch):  # Pause/Stop came as the frame arrived: keep nothing
            self._record(AuditEvent.COMPLETED, pending.device_request, outcome=ActionOutcome.REFUSED)
            return {"ok": False, "code": "paused",
                    "detail": "Qronos was paused or stopped during the capture; nothing was written"}
        self._record(AuditEvent.COMPLETED, pending.device_request, outcome=ActionOutcome.SUCCEEDED)
        try:
            saved = save_frame(frame, pending.destination, check_destination)
        except (CameraError, ImagingError) as error:
            self._record(AuditEvent.COMPLETED, pending.file_request, outcome=ActionOutcome.FAILED)
            return {"ok": False, "code": error.code, "detail": str(error)[:200]}
        self._record(AuditEvent.COMPLETED, pending.file_request, outcome=ActionOutcome.SUCCEEDED)
        return {"ok": True, "status": "saved", "deviceId": pending.device_id, **saved}

    def _wake_stopped_since(self, epoch: int) -> bool:
        if self._paused():
            return True
        with self._wake_lock:
            return self._wake_epoch != epoch

    def _secret_ok(self, secret: str | None) -> bool:
        try:
            return self._verify_secret is not None and bool(self._verify_secret(secret or ""))
        except Exception:  # noqa: BLE001 - an unverifiable secret is a refusal
            return False

    def _record_policy_refusal(self, request: ActionRequest, asked: PermissionDecision) -> None:
        """The person said yes but the CURRENT policy refused at the answer (the gate recorded its own refused
        verdict): never DENIED_BY_USER. APPROVED only when no typed secret was asked, since an unverified secret
        proves nobody; then COMPLETED with nothing done."""
        if asked is not PermissionDecision.REQUIRE_TYPED_SECRET:
            self._record(AuditEvent.APPROVED, request, decision=asked)
        self._record(AuditEvent.COMPLETED, request, outcome=ActionOutcome.REFUSED)

    def _record(self, event: AuditEvent, request: ActionRequest, *, outcome=None, decision=None) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(event, request, outcome=outcome, decision=decision)
        except Exception:  # noqa: BLE001 - auditing never hides the result
            pass


def _machine(name: str) -> str:
    return name.strip().lstrip("\\").rstrip(".").casefold()


def _owner_check(source: DeviceSource, device: Device) -> Callable[[PrintJobState], bool]:
    """A job is the caller's own only when its user is the caller AND it came from this computer: the origin
    equals one of this computer's explicit names exactly (no substring). An empty origin counts only on a local,
    unshared queue, where nothing else can submit; on a network or shared queue it is not this computer."""
    user = source.current_user().casefold()
    here = {_machine(name) for name in source.current_machines() if _machine(name)}
    attributes = device.printer.attributes
    local_only = not attributes & (PRINTER_ATTRIBUTE_NETWORK | PRINTER_ATTRIBUTE_SHARED)

    def is_own(job: PrintJobState) -> bool:
        if job.user.casefold() != user:
            return False
        origin = _machine(job.machine)
        return origin in here if origin else local_only

    return is_own


def _device_id(data: dict) -> str:
    value = data.get("deviceId")
    if not isinstance(value, str) or not _DEVICE_ID.match(value):
        raise DeviceHubError("bad_request", "deviceId is malformed")
    return value


def _checked_3mf(path: Path) -> None:
    """One exact regular local .3mf file, not through a link, within the package bound."""
    if not path.is_absolute() or path.suffix.casefold() != ".3mf":
        raise DeviceHubError("bad_request", "one exact absolute .3mf file is required")
    if not path.exists() and not path.is_symlink():
        raise DeviceHubError("file_missing", "the file does not exist")
    if _is_link(path) or any(_is_link(parent) for parent in path.parents if parent.exists()):
        raise DeviceHubError("link_refused", "the file or one of its folders is a link")
    if not path.is_file():
        raise DeviceHubError("file_missing", "the file does not exist")
    size = path.stat().st_size
    if size == 0:
        raise DeviceHubError("file_empty", "the file is empty")
    if size > MAX_PACKAGE_BYTES:
        raise DeviceHubError("too_large", "the package is too large")


def _wake_time(data: dict) -> datetime:
    """An exact ISO-8601 time WITH its UTC offset (the desktop sends the person's chosen local time so)."""
    value = data.get("at")
    if not isinstance(value, str) or not 16 <= len(value) <= 40:
        raise DeviceHubError("bad_request", "at must be an ISO-8601 time with its offset")
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise DeviceHubError("bad_request", "at must be an ISO-8601 time with its offset") from None
    if when.tzinfo is None:
        raise DeviceHubError("bad_request", "at must carry its UTC offset")
    return when.replace(microsecond=0)


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
