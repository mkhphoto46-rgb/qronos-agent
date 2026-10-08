"""
Print one exact approved file on one chosen local printer, once.

    1. ``prepare`` (read-only): the file must be one existing regular file (not
       a link), of a type whose registered Windows "printto" handler exists;
       its SHA-256 and size are recorded. The printer must match exactly one
       installed printer by name (case-insensitive equality, never similarity).
       Its spooler status, queue and handler are read so the person can see
       the exact file, printer, handler and options before approving.

    2. ``submit`` (device control, typed-secret approval bound to that plan):
       the file is hashed again and the printer re-read; anything changed,
       offline, paused, in error or out of paper refuses before submission.
       The attempt is journalled, the file is handed once to the Windows shell
       "printto" verb for that exact printer, and Qronos waits for exactly one
       new job from this user whose document name matches the file to appear
       in that printer's queue.

    3. Bounded recovery touches only that job: a paused job is resumed once.
       An errored job is never restarted, because a restart reprints from the
       first page. Printer level problems (offline, paused queue, paper, toner, door, jam, user
       intervention) are reported for a person; Qronos never changes printer
       settings. Spooler queries (never submissions) are retried a bounded
       number of times while the spooler is unavailable.

Nothing is ever printed twice: an approved attempt runs once, and an
attempt whose job never became visible is reported as uncertain and blocks
printing the same file on the same printer again until a person has checked.

Options: the shell "printto" verb prints with the printer's current defaults
(one copy, the printer's colour mode, all pages). Any other option is refused
before approval rather than silently ignored; applying per-job options needs
a renderer that is not part of this module.

Only Windows facilities are used (winspool.drv, shell32 ShellExecuteExW,
shlwapi AssocQueryStringW); no vendor or cloud printing service is called.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ctypes
from ctypes import wintypes
import hashlib
import ntpath
import os
from pathlib import Path
import time
from typing import Callable, Protocol

from core.action_cancellation import ActionCancelled, CancellationToken
from core.actions import ActionRequest
from core.app_workflow import (
    SideEffectJournal,
    grant_for,
    SideEffectState,
    StepGuard,
    WorkflowAmbiguous,
    WorkflowApproval,
    WorkflowRefused,
    WorkflowReport,
    WorkflowStopped,
    content_key,
    operation_key,
    plan_digest,
)
from security.gate import ActionGrant
from security.permissions import ActionCategory, PermissionDecision


PRINTABLE_EXTENSIONS = frozenset({
    ".pdf", ".txt", ".rtf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".xps", ".oxps",
})
MAX_PRINT_BYTES = 512 * 1024 * 1024
DEFAULT_APPEAR_TIMEOUT = 45.0
DEFAULT_MONITOR_TIMEOUT = 120.0
DEFAULT_POLL_SECONDS = 0.1
HANDLER_EXIT_GRACE = 5.0
SPOOLER_QUERY_ATTEMPTS = 3

# Printer status bits (PRINTER_INFO_2.Status) and attribute bits.
PRINTER_STATUS_PAUSED = 0x1
PRINTER_STATUS_ERROR = 0x2
PRINTER_STATUS_PENDING_DELETION = 0x4
PRINTER_STATUS_PAPER_JAM = 0x8
PRINTER_STATUS_PAPER_OUT = 0x10
PRINTER_STATUS_MANUAL_FEED = 0x20
PRINTER_STATUS_PAPER_PROBLEM = 0x40
PRINTER_STATUS_OFFLINE = 0x80
PRINTER_STATUS_OUTPUT_BIN_FULL = 0x800
PRINTER_STATUS_NOT_AVAILABLE = 0x1000
PRINTER_STATUS_TONER_LOW = 0x20000
PRINTER_STATUS_NO_TONER = 0x40000
PRINTER_STATUS_USER_INTERVENTION = 0x100000
PRINTER_STATUS_OUT_OF_MEMORY = 0x200000
PRINTER_STATUS_DOOR_OPEN = 0x400000
PRINTER_STATUS_SERVER_UNKNOWN = 0x800000
PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x400

# Job status bits (JOB_INFO_1.Status) and SetJob commands.
JOB_STATUS_PAUSED = 0x1
JOB_STATUS_ERROR = 0x2
JOB_STATUS_DELETING = 0x4
JOB_STATUS_OFFLINE = 0x20
JOB_STATUS_PAPEROUT = 0x40
JOB_STATUS_PRINTED = 0x80
JOB_STATUS_DELETED = 0x100
JOB_STATUS_BLOCKED_DEVQ = 0x200
JOB_STATUS_USER_INTERVENTION = 0x400
JOB_STATUS_COMPLETE = 0x1000
JOB_CONTROL_RESUME = 2

# Problems that block submission and need a person or the device.
_BLOCKING_PRINTER = {
    PRINTER_STATUS_PAUSED: "the printer queue is paused in Windows",
    PRINTER_STATUS_ERROR: "the printer reports an error",
    PRINTER_STATUS_PENDING_DELETION: "the printer is being removed",
    PRINTER_STATUS_PAPER_JAM: "paper is jammed",
    PRINTER_STATUS_PAPER_OUT: "the printer is out of paper",
    PRINTER_STATUS_PAPER_PROBLEM: "the printer reports a paper problem",
    PRINTER_STATUS_OFFLINE: "the printer is offline",
    PRINTER_STATUS_NOT_AVAILABLE: "the printer is not available",
    PRINTER_STATUS_NO_TONER: "the printer is out of toner or ink",
    PRINTER_STATUS_USER_INTERVENTION: "the printer needs attention",
    PRINTER_STATUS_DOOR_OPEN: "a printer door is open",
    PRINTER_STATUS_OUT_OF_MEMORY: "the printer is out of memory",
    PRINTER_STATUS_SERVER_UNKNOWN: "the print server is unknown",
    PRINTER_STATUS_OUTPUT_BIN_FULL: "the output tray is full",
}
_WARNING_PRINTER = {
    PRINTER_STATUS_TONER_LOW: "toner or ink is low",
    PRINTER_STATUS_MANUAL_FEED: "the printer is waiting for manual paper feed",
}


class SpoolerUnavailable(RuntimeError):
    """The Windows print spooler could not be queried."""


class PrintNotSubmitted(WorkflowRefused):
    """The handler was never started; nothing was printed."""


@dataclass(frozen=True)
class PrinterState:
    name: str
    port: str
    driver: str
    status: int
    attributes: int
    jobs: int
    is_default: bool = False

    @property
    def problems(self) -> tuple[str, ...]:
        found = [text for bit, text in _BLOCKING_PRINTER.items() if self.status & bit]
        if self.attributes & PRINTER_ATTRIBUTE_WORK_OFFLINE:
            found.append("Windows is set to use this printer offline")
        return tuple(found)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(text for bit, text in _WARNING_PRINTER.items() if self.status & bit)


@dataclass(frozen=True)
class PrintJobState:
    job_id: int
    document: str
    user: str
    status: int
    pages_printed: int
    total_pages: int


class JobWatcher(Protocol):
    """Jobs added to one printer since the watcher was created.

    Created before submission so a job that prints and leaves the queue
    before the first poll is still seen (Windows printer change
    notifications report it with its document, user and status).
    """

    def seen_jobs(self) -> tuple[PrintJobState, ...]: ...
    def close(self) -> None: ...


class Spooler(Protocol):
    def printers(self) -> tuple[PrinterState, ...]: ...
    def jobs(self, printer: str) -> tuple[PrintJobState, ...]: ...
    def control_job(self, printer: str, job_id: int, command: int) -> None: ...
    def current_user(self) -> str: ...
    def watch(self, printer: str) -> JobWatcher: ...


class HandlerProcess(Protocol):
    """The started print handler. Polled, never waited on and never killed."""

    def finished(self) -> bool: ...
    def exit_code(self) -> int | None: ...
    def close(self) -> None: ...


class PrintSubmitter(Protocol):
    def handler_for(self, path: Path) -> str | None: ...
    def start(self, path: Path, printer: str) -> HandlerProcess:
        """Start the registered printto handler once and return immediately.

        Raises PrintNotSubmitted only when Windows reports a documented
        failure before anything was launched.
        """
        ...


@dataclass(frozen=True)
class PrintRequest:
    file_path: Path
    printer: str
    copies: int = 1
    color: str = "default"
    pages: str = "all"

    def __post_init__(self) -> None:
        object.__setattr__(self, "file_path", Path(self.file_path))
        name = str(self.printer or "").strip()
        if not name or len(name) > 220 or any(ord(ch) < 32 for ch in name):
            raise ValueError("A print request needs one exact printer name.")
        if '"' in name:
            # Never strip it: a stripped name could be a different printer.
            raise ValueError("A printer name with a double quote cannot be passed safely.")
        object.__setattr__(self, "printer", name)
        if int(self.copies) != self.copies or int(self.copies) < 1:
            raise ValueError("Copies must be a positive whole number.")


@dataclass(frozen=True)
class PreparedPrint:
    request: PrintRequest
    file_path: Path
    sha256: str
    size: int
    printer: PrinterState
    handler: str
    plan: dict
    digest: str
    warnings: tuple[str, ...] = ()


def _is_link(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return path.is_symlink() or bool(attributes & 0x400)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _document_matches(document: str, file_name: str) -> bool:
    """Handlers name the job after the file, sometimes with a prefix or path."""
    shown = ntpath.basename(str(document or "")).casefold().strip()
    wanted = file_name.casefold()
    stem = os.path.splitext(wanted)[0]
    return bool(shown) and (shown == wanted or shown.endswith(" - " + wanted) or shown == stem
                            or shown.startswith(wanted) or wanted in shown)


class PrintWorkflow:
    def __init__(
        self,
        *,
        spooler: Spooler,
        submitter: PrintSubmitter,
        journal: SideEffectJournal,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[CancellationToken | None, float], None] | None = None,
        appear_timeout: float = DEFAULT_APPEAR_TIMEOUT,
        monitor_timeout: float = DEFAULT_MONITOR_TIMEOUT,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._spooler = spooler
        self._submitter = submitter
        self._journal = journal
        self._clock = clock
        self._sleep = sleep or _cancellable_sleep
        self._appear_timeout = float(appear_timeout)
        self._monitor_timeout = float(monitor_timeout)
        self._poll = float(poll_seconds)

    # ------------------------------------------------------- read helpers

    def _query(self, function, *args):
        """Bounded retry of read-only spooler queries while it restarts."""
        last: Exception | None = None
        for attempt in range(SPOOLER_QUERY_ATTEMPTS):
            try:
                return function(*args)
            except SpoolerUnavailable as error:
                last = error
                if attempt + 1 < SPOOLER_QUERY_ATTEMPTS:
                    self._sleep(None, 1.0 + attempt)
        raise WorkflowRefused(f"The Windows print spooler is not responding ({last}).")

    def _printer(self, name: str) -> PrinterState:
        printers = self._query(self._spooler.printers)
        exact = [item for item in printers if item.name.casefold() == name.casefold()]
        if not exact:
            raise WorkflowRefused("No installed printer has exactly that name.")
        if len(exact) > 1:
            raise WorkflowAmbiguous("More than one printer has exactly that name; Qronos will not choose.")
        return exact[0]

    def _checked_file(self, path: Path) -> tuple[Path, str, int]:
        if not path.is_absolute():
            raise WorkflowRefused("The file to print must be an exact, absolute path.")
        if not path.exists() and not path.is_symlink():
            raise WorkflowRefused("The file to print does not exist.")
        if _is_link(path) or any(_is_link(parent) for parent in path.parents if parent.exists()):
            raise WorkflowRefused("The file or one of its folders is a link; it is not printed.")
        if not path.is_file():
            raise WorkflowRefused("The file to print does not exist.")
        if path.suffix.casefold() not in PRINTABLE_EXTENSIONS:
            raise WorkflowRefused("Qronos does not print this file type.")
        sha256, size = _hash_file(path)
        if size == 0:
            raise WorkflowRefused("The file is empty.")
        if size > MAX_PRINT_BYTES:
            raise WorkflowRefused("The file is too large to print safely.")
        return path, sha256, size

    # ------------------------------------------------------------ prepare

    def prepare(self, request: PrintRequest) -> PreparedPrint:
        if request.copies != 1 or request.color != "default" or request.pages != "all":
            raise WorkflowRefused(
                "This file is printed by its registered Windows handler, which uses the printer's "
                "defaults (one copy, default colour, all pages). Other options are not applied, "
                "so Qronos refuses instead of ignoring them."
            )
        path, sha256, size = self._checked_file(request.file_path)
        printer = self._printer(request.printer)
        if printer.problems:
            raise WorkflowRefused("Cannot print now: " + "; ".join(printer.problems) + ".")
        handler = self._submitter.handler_for(path)
        if not handler:
            raise WorkflowRefused("Windows has no print handler registered for this file type.")
        plan = {
            "workflow": "print",
            "file": str(path),
            "sha256": sha256,
            "size": size,
            "printer": printer.name,
            "port": printer.port,
            "driver": printer.driver,
            "handler": handler,
            "copies": 1,
            "color": "printer default",
            "pages": "all",
        }
        return PreparedPrint(
            request=request, file_path=path, sha256=sha256, size=size, printer=printer,
            handler=handler, plan=plan, digest=plan_digest(plan), warnings=printer.warnings,
        )

    # ------------------------------------------------------------- submit

    def submit(
        self,
        prepared: PreparedPrint,
        approval: WorkflowApproval,
        *,
        token: CancellationToken | None,
        approval_valid: Callable[[], bool],
    ) -> WorkflowReport:
        approval.require_for(plan=prepared.plan, required=PermissionDecision.REQUIRE_TYPED_SECRET)
        grant: ActionGrant = grant_for(
            ActionRequest(
                category=ActionCategory.DEVICE_CONTROL,
                target=f"printer:{prepared.printer.name}",
                summary="Print one exact file once.",
                parameters={"file": str(prepared.file_path), "sha256": prepared.sha256},
            ),
            approval.decision,
        )
        guard = StepGuard(token=token, approval_valid=approval_valid, clock=self._clock)

        guard.checkpoint("re-check file")
        _, sha256, size = self._checked_file(prepared.file_path)
        if (sha256, size) != (prepared.sha256, prepared.size):
            raise WorkflowRefused("The file changed after it was approved; nothing was printed.")
        guard.checkpoint("re-check printer")
        printer = self._printer(prepared.printer.name)
        if (printer.port, printer.driver) != (prepared.printer.port, prepared.printer.driver):
            raise WorkflowRefused("The printer's port or driver changed after approval; nothing was printed.")
        if printer.problems:
            raise WorkflowRefused("Cannot print now: " + "; ".join(printer.problems) + ".")
        if self._submitter.handler_for(prepared.file_path) != prepared.handler:
            raise WorkflowRefused("The file's print handler changed after approval; nothing was printed.")

        user = self._spooler.current_user()
        before = {job.job_id for job in self._query(self._spooler.jobs, printer.name)}
        try:
            watcher = self._spooler.watch(printer.name)
        except SpoolerUnavailable:
            watcher = None  # queue polling alone; a very fast job may then be uncertain
        op = operation_key(approval.approval_id, prepared.digest)
        content = content_key("print", prepared.sha256, printer.name.casefold())
        guard.checkpoint("submit")
        try:
            if not grant.active():
                raise WorkflowStopped("grant_expired", "The printing permission expired before submission.")
            self._journal.begin(op, content, f"print {prepared.file_path.name} on {printer.name}")
        except BaseException:
            if watcher is not None:
                watcher.close()
            raise
        handler = None
        exit_code = None
        try:
            try:
                guard.checkpoint("submit")
                handler = self._submitter.start(prepared.file_path, printer.name)
            except (WorkflowStopped, PrintNotSubmitted) as not_started:
                self._journal.settle(op, SideEffectState.NOT_SUBMITTED, str(not_started))
                raise
            except Exception as error:  # noqa: BLE001 - the handler may have run
                self._journal.settle(op, SideEffectState.UNCERTAIN, type(error).__name__)
                return self._report("uncertain", (
                    "Windows was asked to print but the result could not be confirmed. "
                    "Qronos will not print this file again; check the printer queue."
                ), guard)
            try:
                job = self._await_job(
                    printer.name, prepared.file_path.name, user, before, token, watcher, handler,
                )
            except Exception as error:  # noqa: BLE001 - the handler may already have printed
                self._journal.settle_if_unresolved(op, SideEffectState.UNCERTAIN, type(error).__name__)
                return self._report("uncertain", (
                    "Windows was asked to print but the queue could not be checked. "
                    "Qronos will not print this file again; check the printer."
                ), guard)
            if handler.finished():
                exit_code = handler.exit_code()
        finally:
            if handler is not None:
                handler.close()
            if watcher is not None:
                watcher.close()
        if job == "stopped":
            self._journal.settle(op, SideEffectState.UNCERTAIN, "stopped while waiting for the job")
            return self._report("uncertain", (
                "Stopped while waiting for the print job. The print handler may still print; "
                "Qronos will not print this file again. Check the printer queue."
            ), guard)
        if job is None:
            self._journal.settle(op, SideEffectState.UNCERTAIN, "no matching job seen")
            return self._report("uncertain", (
                "No matching print job appeared in the queue. It may have printed very quickly "
                "or the handler did not print. Qronos will not print it again; check the printer."
            ), guard, {"handlerExitCode": exit_code})
        if job == "several":
            self._journal.settle(op, SideEffectState.UNCERTAIN, "several matching jobs")
            return self._report("uncertain", (
                "Several matching jobs appeared; Qronos cannot tell which one is this request."
            ), guard)

        self._journal.settle(op, SideEffectState.CONFIRMED, f"job {job.job_id}")
        try:
            return self._monitor(printer.name, job, token, guard)
        except Exception as error:  # noqa: BLE001 - the job is already in the queue
            return self._report("submitted", (
                "The job is in the printer queue; following it further failed "
                f"({type(error).__name__}). Qronos will not print it again."
            ), guard, {"jobId": job.job_id, "printer": printer.name})

    def _await_job(self, printer, file_name, user, before, token, watcher, handler):
        """Find our one new job while the handler runs, in short cancellable slices.

        Sources: the change-notification watcher (catches jobs that finish
        before any poll) and the live queue. The handler is only polled.
        """
        deadline = self._clock() + self._appear_timeout
        handler_done_at = None
        while True:
            found: dict[int, PrintJobState] = {}
            if watcher is not None:
                for job in watcher.seen_jobs():
                    found[job.job_id] = job
            try:
                for job in self._spooler.jobs(printer):
                    found[job.job_id] = job  # the live queue has the latest fields
            except SpoolerUnavailable:
                pass
            new = [
                job for job in found.values()
                if job.job_id not in before
                and job.user.casefold() == user.casefold()
                and _document_matches(job.document, file_name)
            ]
            if len(new) == 1:
                return new[0]
            if len(new) > 1:
                return "several"
            now = self._clock()
            if handler_done_at is None and handler.finished():
                handler_done_at = now
            # Once the handler has exited, a job that has not appeared within a
            # short grace period will not appear.
            if now >= deadline or (
                handler_done_at is not None and now >= handler_done_at + HANDLER_EXIT_GRACE
            ):
                return None
            try:
                self._sleep(token, self._poll)
            except ActionCancelled:
                return "stopped"

    def _monitor(self, printer: str, job: PrintJobState, token, guard) -> WorkflowReport:
        """Follow our one job; bounded own-job recovery; never resubmit."""
        resumed = False
        deadline = self._clock() + self._monitor_timeout
        current = job
        while True:
            if current.status & (JOB_STATUS_PRINTED | JOB_STATUS_COMPLETE):
                return self._report("succeeded", "The file was printed.", guard,
                                    {"jobId": job.job_id, "printer": printer})
            if current.status & JOB_STATUS_PAUSED and not current.status & JOB_STATUS_ERROR and not resumed:
                # Resuming continues the same job; it cannot print anything twice.
                # An errored job is never restarted: restart reprints from page one.
                resumed = True
                try:
                    self._spooler.control_job(printer, job.job_id, JOB_CONTROL_RESUME)
                except SpoolerUnavailable:
                    pass
            elif current.status & (JOB_STATUS_PAPEROUT | JOB_STATUS_OFFLINE | JOB_STATUS_USER_INTERVENTION
                                   | JOB_STATUS_BLOCKED_DEVQ | JOB_STATUS_ERROR | JOB_STATUS_PAUSED):
                state = self._printer_or_none(printer)
                needs = list(state.problems) if state else []
                return self._report("needs_attention", (
                    "The job is in the queue but cannot finish without a person: "
                    + ("; ".join(needs) if needs else "the printer reports a problem")
                    + ". Qronos will not print it again."
                ), guard, {"jobId": job.job_id, "printer": printer})
            if self._clock() >= deadline:
                return self._report("submitted", (
                    "The job is in the printer queue and has not finished yet."
                ), guard, {"jobId": job.job_id, "printer": printer})
            try:
                self._sleep(token, max(self._poll, 1.0))
            except ActionCancelled:
                return self._report("submitted", (
                    "Stopped while following the job; it stays in the printer queue."
                ), guard, {"jobId": job.job_id, "printer": printer})
            try:
                jobs = self._query(self._spooler.jobs, printer)
            except WorkflowRefused:
                continue
            found = [item for item in jobs if item.job_id == job.job_id]
            if not found:
                # Spoolers drop finished jobs unless told to keep them.
                return self._report("succeeded", "The job left the queue after printing.", guard,
                                    {"jobId": job.job_id, "printer": printer})
            current = found[0]
            if current.status & JOB_STATUS_DELETING or current.status & JOB_STATUS_DELETED:
                return self._report("needs_attention", "The print job was cancelled in Windows.", guard,
                                    {"jobId": job.job_id, "printer": printer})

    def _printer_or_none(self, name: str) -> PrinterState | None:
        try:
            return self._printer(name)
        except WorkflowRefused:
            return None

    @staticmethod
    def _report(outcome, detail, guard, result=None) -> WorkflowReport:
        return WorkflowReport(outcome=outcome, detail=detail, steps=guard.steps, result=result or {})


def _cancellable_sleep(token: CancellationToken | None, seconds: float) -> None:
    if token is None:
        time.sleep(seconds)
    else:
        token.sleep(seconds)


# ============================================================ Windows

PRINTER_ENUM_LOCAL = 0x2
PRINTER_ENUM_CONNECTIONS = 0x4
PRINTER_ACCESS_USE = 0x8
ERROR_INSUFFICIENT_BUFFER = 122


class _PRINTER_INFO_2W(ctypes.Structure):
    _fields_ = [
        ("pServerName", wintypes.LPWSTR), ("pPrinterName", wintypes.LPWSTR),
        ("pShareName", wintypes.LPWSTR), ("pPortName", wintypes.LPWSTR),
        ("pDriverName", wintypes.LPWSTR), ("pComment", wintypes.LPWSTR),
        ("pLocation", wintypes.LPWSTR), ("pDevMode", ctypes.c_void_p),
        ("pSepFile", wintypes.LPWSTR), ("pPrintProcessor", wintypes.LPWSTR),
        ("pDatatype", wintypes.LPWSTR), ("pParameters", wintypes.LPWSTR),
        ("pSecurityDescriptor", ctypes.c_void_p), ("Attributes", wintypes.DWORD),
        ("Priority", wintypes.DWORD), ("DefaultPriority", wintypes.DWORD),
        ("StartTime", wintypes.DWORD), ("UntilTime", wintypes.DWORD),
        ("Status", wintypes.DWORD), ("cJobs", wintypes.DWORD), ("AveragePPM", wintypes.DWORD),
    ]


class _JOB_INFO_1W(ctypes.Structure):
    _fields_ = [
        ("JobId", wintypes.DWORD), ("pPrinterName", wintypes.LPWSTR),
        ("pMachineName", wintypes.LPWSTR), ("pUserName", wintypes.LPWSTR),
        ("pDocument", wintypes.LPWSTR), ("pDatatype", wintypes.LPWSTR),
        ("pStatus", wintypes.LPWSTR), ("Status", wintypes.DWORD),
        ("Priority", wintypes.DWORD), ("Position", wintypes.DWORD),
        ("TotalPages", wintypes.DWORD), ("PagesPrinted", wintypes.DWORD),
        ("Submitted", wintypes.WORD * 8),
    ]


class _PRINTER_DEFAULTSW(ctypes.Structure):
    _fields_ = [("pDatatype", wintypes.LPWSTR), ("pDevMode", ctypes.c_void_p), ("DesiredAccess", wintypes.DWORD)]


class WinSpooler:
    """Read the Windows print spooler and control only a caller's own job."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise SpoolerUnavailable("The Windows print spooler is only available on Windows.")
        self._winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
        self._advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        w = self._winspool
        w.EnumPrintersW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
        w.EnumPrintersW.restype = wintypes.BOOL
        w.GetDefaultPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        w.GetDefaultPrinterW.restype = wintypes.BOOL
        w.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(_PRINTER_DEFAULTSW)]
        w.OpenPrinterW.restype = wintypes.BOOL
        w.ClosePrinter.argtypes = [wintypes.HANDLE]
        w.ClosePrinter.restype = wintypes.BOOL
        w.EnumJobsW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
        w.EnumJobsW.restype = wintypes.BOOL
        w.SetJobW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        w.SetJobW.restype = wintypes.BOOL

    def _default_name(self) -> str:
        size = wintypes.DWORD(0)
        self._winspool.GetDefaultPrinterW(None, ctypes.byref(size))
        if size.value == 0:
            return ""
        buffer = ctypes.create_unicode_buffer(size.value)
        if not self._winspool.GetDefaultPrinterW(buffer, ctypes.byref(size)):
            return ""
        return buffer.value

    def printers(self) -> tuple[PrinterState, ...]:
        flags = PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
        needed, returned = wintypes.DWORD(0), wintypes.DWORD(0)
        self._winspool.EnumPrintersW(flags, None, 2, None, 0, ctypes.byref(needed), ctypes.byref(returned))
        if needed.value == 0:
            error = ctypes.get_last_error()
            if error not in (0, ERROR_INSUFFICIENT_BUFFER):
                raise SpoolerUnavailable(f"EnumPrinters failed ({error}).")
            return ()
        buffer = ctypes.create_string_buffer(needed.value)
        if not self._winspool.EnumPrintersW(flags, None, 2, buffer, needed, ctypes.byref(needed), ctypes.byref(returned)):
            raise SpoolerUnavailable(f"EnumPrinters failed ({ctypes.get_last_error()}).")
        items = ctypes.cast(buffer, ctypes.POINTER(_PRINTER_INFO_2W))
        default = self._default_name().casefold()
        return tuple(
            PrinterState(
                name=items[i].pPrinterName or "", port=items[i].pPortName or "",
                driver=items[i].pDriverName or "", status=int(items[i].Status),
                attributes=int(items[i].Attributes), jobs=int(items[i].cJobs),
                is_default=(items[i].pPrinterName or "").casefold() == default,
            )
            for i in range(returned.value)
        )

    def _open(self, printer: str) -> wintypes.HANDLE:
        handle = wintypes.HANDLE()
        defaults = _PRINTER_DEFAULTSW(None, None, PRINTER_ACCESS_USE)
        if not self._winspool.OpenPrinterW(printer, ctypes.byref(handle), ctypes.byref(defaults)):
            raise SpoolerUnavailable(f"OpenPrinter failed ({ctypes.get_last_error()}).")
        return handle

    def jobs(self, printer: str) -> tuple[PrintJobState, ...]:
        handle = self._open(printer)
        try:
            needed, returned = wintypes.DWORD(0), wintypes.DWORD(0)
            self._winspool.EnumJobsW(handle, 0, 0xFFFFFFFF, 1, None, 0, ctypes.byref(needed), ctypes.byref(returned))
            if needed.value == 0:
                return ()
            buffer = ctypes.create_string_buffer(needed.value)
            if not self._winspool.EnumJobsW(handle, 0, 0xFFFFFFFF, 1, buffer, needed,
                                            ctypes.byref(needed), ctypes.byref(returned)):
                raise SpoolerUnavailable(f"EnumJobs failed ({ctypes.get_last_error()}).")
            items = ctypes.cast(buffer, ctypes.POINTER(_JOB_INFO_1W))
            return tuple(
                PrintJobState(
                    job_id=int(items[i].JobId), document=items[i].pDocument or "",
                    user=items[i].pUserName or "", status=int(items[i].Status),
                    pages_printed=int(items[i].PagesPrinted), total_pages=int(items[i].TotalPages),
                )
                for i in range(returned.value)
            )
        finally:
            self._winspool.ClosePrinter(handle)

    def control_job(self, printer: str, job_id: int, command: int) -> None:
        if command != JOB_CONTROL_RESUME:
            raise ValueError("Only resuming one's own paused job is used for recovery.")
        handle = self._open(printer)
        try:
            if not self._winspool.SetJobW(handle, int(job_id), 0, None, int(command)):
                raise SpoolerUnavailable(f"SetJob failed ({ctypes.get_last_error()}).")
        finally:
            self._winspool.ClosePrinter(handle)

    def watch(self, printer: str) -> "WinJobWatcher":
        return WinJobWatcher(self, printer)

    def current_user(self) -> str:
        size = wintypes.DWORD(257)
        buffer = ctypes.create_unicode_buffer(size.value)
        function = self._advapi.GetUserNameW
        function.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        function.restype = wintypes.BOOL
        if not function(buffer, ctypes.byref(size)):
            raise SpoolerUnavailable("GetUserName failed.")
        return buffer.value


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    ]


SEE_MASK_NOCLOSEPROCESS = 0x40
SEE_MASK_NOASYNC = 0x100
SEE_MASK_FLAG_NO_UI = 0x400
SW_SHOWMINNOACTIVE = 7
ASSOCF_NOTRUNCATE = 0x20
ASSOCF_INIT_IGNOREUNKNOWN = 0x400
ASSOCSTR_EXECUTABLE = 2


class ShellPrintToSubmitter:
    """Hand one file to its registered Windows "printto" handler for one printer."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise PrintNotSubmitted("Printing through the Windows shell needs Windows.")
        self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._shlwapi = ctypes.WinDLL("shlwapi", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def handler_for(self, path: Path) -> str | None:
        function = self._shlwapi.AssocQueryStringW
        function.argtypes = [ctypes.c_uint, ctypes.c_uint, wintypes.LPCWSTR, wintypes.LPCWSTR,
                             wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        function.restype = ctypes.c_long
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        result = function(ASSOCF_NOTRUNCATE | ASSOCF_INIT_IGNOREUNKNOWN, ASSOCSTR_EXECUTABLE,
                          path.suffix, "printto", buffer, ctypes.byref(size))
        if result != 0 or not buffer.value:
            return None
        executable = buffer.value
        name = ntpath.basename(executable).casefold()
        if name in {"cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe"}:
            return None
        return executable

    def start(self, path: Path, printer: str) -> "WinHandlerProcess":
        if '"' in printer:
            raise PrintNotSubmitted("A printer name with a double quote cannot be passed safely.")
        info = _SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI
        info.lpVerb = "printto"
        info.lpFile = str(path)
        info.lpParameters = '"' + printer + '"'
        info.nShow = SW_SHOWMINNOACTIVE
        execute = self._shell32.ShellExecuteExW
        execute.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
        execute.restype = wintypes.BOOL
        if not execute(ctypes.byref(info)):
            error = ctypes.get_last_error()
            if error in _PRE_LAUNCH_ERRORS:
                raise PrintNotSubmitted(f"Windows could not start the print handler ({error}).")
            # DDE and other failures can happen after the handler started.
            raise OSError(f"ShellExecuteEx failed after possibly starting the handler ({error}).")
        return WinHandlerProcess(int(info.hProcess or 0), self._kernel32)


# ShellExecuteEx errors that mean nothing was launched: file/path not found,
# access denied, out of memory, bad format, no association, cancelled.
_PRE_LAUNCH_ERRORS = frozenset({2, 3, 5, 8, 11, 1155, 1223})
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102


class WinHandlerProcess:
    """A started handler process, polled with zero-timeout waits.

    ``close`` releases only Qronos's handle: the user's application keeps
    running and finishes printing on its own.
    """

    def __init__(self, handle: int, kernel32) -> None:
        self._handle = int(handle)
        self._kernel32 = kernel32
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self._kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def finished(self) -> bool:
        if not self._handle:
            # Started through DDE or an existing instance: no process to watch.
            return True
        return self._kernel32.WaitForSingleObject(self._handle, 0) == WAIT_OBJECT_0

    def exit_code(self) -> int | None:
        if not self._handle:
            return None
        code = wintypes.DWORD()
        if self._kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            return int(code.value)
        return None

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = 0


# ----------------------------------------------- printer change notifications

JOB_NOTIFY_TYPE = 0x01
JOB_NOTIFY_FIELD_USER_NAME = 0x03
JOB_NOTIFY_FIELD_STATUS = 0x0A
JOB_NOTIFY_FIELD_DOCUMENT = 0x0D
JOB_NOTIFY_FIELD_TOTAL_PAGES = 0x14
JOB_NOTIFY_FIELD_PAGES_PRINTED = 0x15
PRINTER_CHANGE_JOB = 0x0000FF00
PRINTER_NOTIFY_OPTIONS_REFRESH = 0x01
PRINTER_NOTIFY_INFO_DISCARDED = 0x01
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _PRINTER_NOTIFY_OPTIONS_TYPE(ctypes.Structure):
    _fields_ = [
        ("Type", wintypes.WORD), ("Reserved0", wintypes.WORD), ("Reserved1", wintypes.DWORD),
        ("Reserved2", wintypes.DWORD), ("Count", wintypes.DWORD), ("pFields", ctypes.POINTER(wintypes.WORD)),
    ]


class _PRINTER_NOTIFY_OPTIONS(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.DWORD), ("Flags", wintypes.DWORD), ("Count", wintypes.DWORD),
        ("pTypes", ctypes.POINTER(_PRINTER_NOTIFY_OPTIONS_TYPE)),
    ]


class _NOTIFY_DATA(ctypes.Structure):
    _fields_ = [("cbBuf", wintypes.DWORD), ("pBuf", ctypes.c_void_p)]


class _NOTIFY_UNION(ctypes.Union):
    _fields_ = [("adwData", wintypes.DWORD * 2), ("Data", _NOTIFY_DATA)]


class _PRINTER_NOTIFY_INFO_DATA(ctypes.Structure):
    _fields_ = [
        ("Type", wintypes.WORD), ("Field", wintypes.WORD), ("Reserved", wintypes.DWORD),
        ("Id", wintypes.DWORD), ("NotifyData", _NOTIFY_UNION),
    ]


class _PRINTER_NOTIFY_INFO(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.DWORD), ("Flags", wintypes.DWORD), ("Count", wintypes.DWORD),
        ("aData", _PRINTER_NOTIFY_INFO_DATA * 1),
    ]


_STRING_FIELDS = {JOB_NOTIFY_FIELD_USER_NAME: "user", JOB_NOTIFY_FIELD_DOCUMENT: "document"}
_NUMBER_FIELDS = {
    JOB_NOTIFY_FIELD_STATUS: "status",
    JOB_NOTIFY_FIELD_TOTAL_PAGES: "total_pages",
    JOB_NOTIFY_FIELD_PAGES_PRINTED: "pages_printed",
}


def parse_notify_info(address: int) -> tuple[bool, dict[int, dict[str, object]]]:
    """Read a PRINTER_NOTIFY_INFO block: (discarded, {job id: fields})."""
    info = _PRINTER_NOTIFY_INFO.from_address(address)
    entries = (_PRINTER_NOTIFY_INFO_DATA * int(info.Count)).from_address(
        address + _PRINTER_NOTIFY_INFO.aData.offset
    )
    jobs: dict[int, dict[str, object]] = {}
    for item in entries:
        if item.Type != JOB_NOTIFY_TYPE:
            continue
        fields = jobs.setdefault(int(item.Id), {})
        if item.Field in _STRING_FIELDS:
            data = item.NotifyData.Data
            text = ctypes.wstring_at(data.pBuf, max(0, int(data.cbBuf) // 2)).split("\x00", 1)[0] if data.pBuf else ""
            fields[_STRING_FIELDS[item.Field]] = text
        elif item.Field in _NUMBER_FIELDS:
            fields[_NUMBER_FIELDS[item.Field]] = int(item.NotifyData.adwData[0])
    return bool(info.Flags & PRINTER_NOTIFY_INFO_DISCARDED), jobs


class WinJobWatcher:
    """Collect every job added to one printer, through printer change notifications."""

    _FIELDS = (
        JOB_NOTIFY_FIELD_USER_NAME, JOB_NOTIFY_FIELD_DOCUMENT, JOB_NOTIFY_FIELD_STATUS,
        JOB_NOTIFY_FIELD_TOTAL_PAGES, JOB_NOTIFY_FIELD_PAGES_PRINTED,
    )

    def __init__(self, spooler: "WinSpooler", printer: str) -> None:
        self._winspool = spooler._winspool
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        w = self._winspool
        w.FindFirstPrinterChangeNotification.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        w.FindFirstPrinterChangeNotification.restype = wintypes.HANDLE
        w.FindNextPrinterChangeNotification.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        w.FindNextPrinterChangeNotification.restype = wintypes.BOOL
        w.FreePrinterNotifyInfo.argtypes = [ctypes.c_void_p]
        w.FreePrinterNotifyInfo.restype = wintypes.BOOL
        w.FindClosePrinterChangeNotification.argtypes = [wintypes.HANDLE]
        w.FindClosePrinterChangeNotification.restype = wintypes.BOOL
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._jobs: dict[int, dict[str, object]] = {}
        self._fields = (wintypes.WORD * len(self._FIELDS))(*self._FIELDS)
        self._type = _PRINTER_NOTIFY_OPTIONS_TYPE(JOB_NOTIFY_TYPE, 0, 0, 0, len(self._FIELDS), self._fields)
        self._options = _PRINTER_NOTIFY_OPTIONS(2, 0, 1, ctypes.pointer(self._type))
        self._printer = spooler._open(printer)
        self._change = w.FindFirstPrinterChangeNotification(
            self._printer, PRINTER_CHANGE_JOB, 0, ctypes.byref(self._options),
        )
        if not self._change or self._change == _INVALID_HANDLE:
            spooler._winspool.ClosePrinter(self._printer)
            self._printer = None
            raise SpoolerUnavailable(f"Printer change notifications are unavailable ({ctypes.get_last_error()}).")

    def _drain(self) -> None:
        refresh = False
        while self._change and self._kernel32.WaitForSingleObject(self._change, 0) == WAIT_OBJECT_0:
            changed = wintypes.DWORD()
            info = ctypes.c_void_p()
            options = None
            if refresh:
                options = _PRINTER_NOTIFY_OPTIONS(2, PRINTER_NOTIFY_OPTIONS_REFRESH, 1, ctypes.pointer(self._type))
            if not self._winspool.FindNextPrinterChangeNotification(
                self._change, ctypes.byref(changed), ctypes.byref(options) if options else None, ctypes.byref(info),
            ):
                raise SpoolerUnavailable(f"Reading printer notifications failed ({ctypes.get_last_error()}).")
            if info.value:
                try:
                    discarded, jobs = parse_notify_info(info.value)
                finally:
                    self._winspool.FreePrinterNotifyInfo(info)
                for job_id, fields in jobs.items():
                    self._jobs.setdefault(job_id, {}).update(fields)
                refresh = discarded

    def seen_jobs(self) -> tuple[PrintJobState, ...]:
        self._drain()
        return tuple(
            PrintJobState(
                job_id=job_id, document=str(fields.get("document", "")), user=str(fields.get("user", "")),
                status=int(fields.get("status", 0)), pages_printed=int(fields.get("pages_printed", 0)),
                total_pages=int(fields.get("total_pages", 0)),
            )
            for job_id, fields in sorted(self._jobs.items())
        )

    def close(self) -> None:
        if self._change:
            self._winspool.FindClosePrinterChangeNotification(self._change)
            self._change = None
        if self._printer:
            self._winspool.ClosePrinter(self._printer)
            self._printer = None
