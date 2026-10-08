from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path

import pytest

from tests.live_device import LiveDeviceBlocked, requires_live_device
from core.action_cancellation import CancellationToken
from core.app_workflow import (
    ApprovalMismatch,
    DuplicateSideEffect,
    SideEffectJournal,
    SideEffectState,
    WorkflowAmbiguous,
    WorkflowApproval,
    WorkflowRefused,
    WorkflowStopped,
    operation_key,
)
from core.print_workflow import (
    JOB_CONTROL_RESUME,
    JOB_STATUS_ERROR,
    JOB_STATUS_PAPEROUT,
    JOB_STATUS_PAUSED,
    JOB_STATUS_PRINTED,
    PRINTER_ATTRIBUTE_WORK_OFFLINE,
    PRINTER_STATUS_OFFLINE,
    PRINTER_STATUS_PAPER_OUT,
    PRINTER_STATUS_PAUSED,
    PRINTER_STATUS_TONER_LOW,
    PrintJobState,
    PrintNotSubmitted,
    PrintRequest,
    PrintWorkflow,
    PrinterState,
    SpoolerUnavailable,
)
from security.permissions import PermissionDecision

SECRET = PermissionDecision.REQUIRE_TYPED_SECRET


class Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


class FakeSpooler:
    """Test boundary standing in for the Windows print spooler."""

    def __init__(self) -> None:
        self.printer_list = [
            PrinterState(name="Office Laser", port="USB001", driver="Laser Driver", status=0, attributes=0x40, jobs=0, is_default=True),
            PrinterState(name="Microsoft Print to PDF", port="PORTPROMPT:", driver="PDF", status=0, attributes=0x40, jobs=0),
        ]
        self.queue: dict[str, list[PrintJobState]] = {"Office Laser": [PrintJobState(3, "other.docx", "someone", 0, 0, 1)]}
        self.controls: list[tuple[str, int, int]] = []
        self.unavailable = 0
        self.after_submit = None  # callable mutating queue on next jobs() call
        # Jobs the change-notification watcher reports, including ones that
        # already left the queue.
        self.notified: dict[str, list[PrintJobState]] = {}
        self.watchers: list["FakeWatcher"] = []
        self.watch_available = True

    def printers(self):
        if self.unavailable:
            self.unavailable -= 1
            raise SpoolerUnavailable("RPC server unavailable")
        return tuple(self.printer_list)

    def jobs(self, printer):
        if self.after_submit is not None:
            change, self.after_submit = self.after_submit, None
            change(self)
        return tuple(self.queue.get(printer, []))

    def control_job(self, printer, job_id, command):
        self.controls.append((printer, job_id, command))
        jobs = self.queue[printer]
        for index, job in enumerate(jobs):
            if job.job_id == job_id and command == JOB_CONTROL_RESUME:
                jobs[index] = replace(job, status=JOB_STATUS_PRINTED)

    def current_user(self):
        return "Test User"

    def watch(self, printer):
        if not self.watch_available:
            raise SpoolerUnavailable("notifications unavailable")
        watcher = FakeWatcher(self, printer)
        self.watchers.append(watcher)
        return watcher


class FakeWatcher:
    """Test boundary standing in for printer change notifications."""

    def __init__(self, spooler, printer) -> None:
        self.spooler, self.printer, self.closed = spooler, printer, False

    def seen_jobs(self):
        return tuple(self.spooler.notified.get(self.printer, []))

    def close(self):
        self.closed = True


class FakeHandler:
    """Test boundary standing in for the started print handler process."""

    def __init__(self, done=True, code=0) -> None:
        self.done, self.code, self.closed, self.polls = done, code, False, 0

    def finished(self):
        self.polls += 1
        return self.done

    def exit_code(self):
        return self.code

    def close(self):
        self.closed = True


class FakeSubmitter:
    """Test boundary standing in for the Windows printto handler."""

    def __init__(self, spooler: FakeSpooler) -> None:
        self.spooler = spooler
        self.submissions: list[tuple[str, str]] = []
        self.handler = r"D:\Programs\Viewer\viewer.exe"
        self.job_status = JOB_STATUS_PRINTED
        self.effect = "job"

    def handler_for(self, path):
        return self.handler

    def start(self, path, printer):
        self.submissions.append((path.name, printer))
        if self.effect == "raise":
            raise OSError("handler crashed")
        if self.effect == "not_started":
            raise PrintNotSubmitted("ShellExecuteEx refused")
        job = PrintJobState(41, path.name, "test user", self.job_status, 0, 2)
        if self.effect == "job":
            self.spooler.queue.setdefault(printer, []).append(job)
            self.spooler.notified.setdefault(printer, []).append(job)
        if self.effect == "fast":
            # Printed and gone from the queue before Qronos could poll it.
            self.spooler.notified.setdefault(printer, []).append(replace(job, status=JOB_STATUS_PRINTED))
        if self.effect == "two":
            for job_id in (41, 42):
                self.spooler.queue.setdefault(printer, []).append(
                    PrintJobState(job_id, path.name, "Test User", 0, 0, 2))
        self.handler_process = FakeHandler(done=getattr(self, "handler_done", True))
        return self.handler_process


@pytest.fixture
def setup(tmp_path):
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.7 fake test bytes")
    spooler = FakeSpooler()
    submitter = FakeSubmitter(spooler)
    clock = Clock()

    def sleep(token, seconds):
        if token is not None:
            token.raise_if_cancelled()
        clock.now += seconds

    journal = SideEffectJournal(tmp_path / "journal.json")
    workflow = PrintWorkflow(spooler=spooler, submitter=submitter, journal=journal, clock=clock,
                             sleep=sleep, appear_timeout=5.0, monitor_timeout=10.0, poll_seconds=0.25)
    return workflow, spooler, submitter, document, journal, clock


def approve(prepared, approval_id="print-approval-1", decision=SECRET):
    return WorkflowApproval(approval_id=approval_id, plan_digest=prepared.digest, decision=decision)


def submit(workflow, prepared, approval=None, token=None, valid=lambda: True):
    return workflow.submit(prepared, approval or approve(prepared), token=token, approval_valid=valid)


def test_prints_once_and_confirms_the_job_in_the_queue(setup):
    workflow, spooler, submitter, document, journal, _ = setup
    prepared = workflow.prepare(PrintRequest(document, "office laser"))
    assert prepared.plan["printer"] == "Office Laser"
    assert prepared.plan["copies"] == 1 and prepared.plan["pages"] == "all"
    assert submitter.submissions == []
    report = submit(workflow, prepared)
    assert report.outcome == "succeeded"
    assert report.result == {"jobId": 41, "printer": "Office Laser"}
    assert submitter.submissions == [("report.pdf", "Office Laser")]
    assert journal.get(operation_key("print-approval-1", prepared.digest)).state is SideEffectState.CONFIRMED


def test_exact_printer_names_only(setup):
    workflow, spooler, _, document, *_ = setup
    with pytest.raises(WorkflowRefused, match="exactly that name"):
        workflow.prepare(PrintRequest(document, "Office"))
    spooler.printer_list.append(replace(spooler.printer_list[0], port="USB002"))
    with pytest.raises(WorkflowAmbiguous):
        workflow.prepare(PrintRequest(document, "Office Laser"))


@pytest.mark.parametrize("options", [{"copies": 2}, {"color": "monochrome"}, {"pages": "1-2"}])
def test_options_the_handler_cannot_apply_are_refused_not_ignored(setup, options):
    workflow, _, submitter, document, *_ = setup
    with pytest.raises(WorkflowRefused, match="refuses instead of ignoring"):
        workflow.prepare(PrintRequest(document, "Office Laser", **options))
    assert submitter.submissions == []


def test_files_that_cannot_be_printed_safely_are_refused(setup, tmp_path):
    workflow, _, submitter, document, *_ = setup
    with pytest.raises(WorkflowRefused, match="absolute"):
        workflow.prepare(PrintRequest(os.path.relpath(document), "Office Laser"))
    script = tmp_path / "run.bat"
    script.write_text("echo hi", encoding="utf-8")
    with pytest.raises(WorkflowRefused, match="file type"):
        workflow.prepare(PrintRequest(script, "Office Laser"))
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    with pytest.raises(WorkflowRefused, match="empty"):
        workflow.prepare(PrintRequest(empty, "Office Laser"))
    with pytest.raises(WorkflowRefused, match="does not exist"):
        workflow.prepare(PrintRequest(tmp_path / "missing.pdf", "Office Laser"))
    submitter.handler = None
    with pytest.raises(WorkflowRefused, match="no print handler"):
        workflow.prepare(PrintRequest(document, "Office Laser"))


@pytest.mark.parametrize("status, attributes, reason", [
    (PRINTER_STATUS_OFFLINE, 0x40, "offline"),
    (PRINTER_STATUS_PAUSED, 0x40, "paused"),
    (PRINTER_STATUS_PAPER_OUT, 0x40, "out of paper"),
    (0, 0x40 | PRINTER_ATTRIBUTE_WORK_OFFLINE, "use this printer offline"),
])
def test_printer_problems_stop_before_anything_is_submitted(setup, status, attributes, reason):
    workflow, spooler, submitter, document, *_ = setup
    spooler.printer_list[0] = replace(spooler.printer_list[0], status=status, attributes=attributes)
    with pytest.raises(WorkflowRefused, match=reason):
        workflow.prepare(PrintRequest(document, "Office Laser"))
    assert submitter.submissions == []


def test_low_toner_is_a_warning_not_a_refusal(setup):
    workflow, spooler, _, document, *_ = setup
    spooler.printer_list[0] = replace(spooler.printer_list[0], status=PRINTER_STATUS_TONER_LOW)
    assert workflow.prepare(PrintRequest(document, "Office Laser")).warnings == ("toner or ink is low",)


def test_printing_needs_typed_secret_for_this_plan(setup):
    workflow, _, submitter, document, *_ = setup
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    with pytest.raises(ApprovalMismatch):
        submit(workflow, prepared, approve(prepared, decision=PermissionDecision.REQUIRE_UI_CONFIRMATION))
    with pytest.raises(ApprovalMismatch):
        submit(workflow, prepared, replace(approve(prepared), plan_digest="f" * 64))
    assert submitter.submissions == []


def test_file_changed_after_approval_is_not_printed(setup):
    workflow, _, submitter, document, *_ = setup
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    document.write_bytes(b"%PDF-1.7 different content")
    with pytest.raises(WorkflowRefused, match="changed after it was approved"):
        submit(workflow, prepared)
    assert submitter.submissions == []


def test_printer_changes_after_approval_stop_submission(setup):
    workflow, spooler, submitter, document, *_ = setup
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    spooler.printer_list[0] = replace(spooler.printer_list[0], port="WSD-1234")
    with pytest.raises(WorkflowRefused, match="port or driver"):
        submit(workflow, prepared)
    spooler.printer_list[0] = replace(spooler.printer_list[0], port="USB001", status=PRINTER_STATUS_OFFLINE)
    with pytest.raises(WorkflowRefused, match="offline"):
        submit(workflow, prepared)
    spooler.printer_list[0] = replace(spooler.printer_list[0], status=0)
    submitter.handler = r"D:\Other\viewer.exe"
    with pytest.raises(WorkflowRefused, match="handler changed"):
        submit(workflow, prepared)
    assert submitter.submissions == []


def test_revocation_and_stop_prevent_submission(setup):
    workflow, _, submitter, document, journal, _ = setup
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    with pytest.raises(WorkflowStopped):
        submit(workflow, prepared, valid=lambda: False)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(WorkflowStopped):
        submit(workflow, prepared, approve(prepared, "print-approval-2"), token=token)
    assert submitter.submissions == []
    assert journal.get(operation_key("print-approval-1", prepared.digest)) is None


def test_handler_failing_to_start_is_not_submitted_and_releases_the_file(setup):
    workflow, _, submitter, document, journal, _ = setup
    submitter.effect = "not_started"
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    with pytest.raises(PrintNotSubmitted):
        submit(workflow, prepared)
    assert journal.get(operation_key("print-approval-1", prepared.digest)).state is SideEffectState.NOT_SUBMITTED
    submitter.effect = "job"
    assert submit(workflow, prepared, approve(prepared, "print-approval-2")).outcome == "succeeded"


def test_crashing_handler_is_uncertain_and_blocks_reprinting(setup):
    workflow, _, submitter, document, *_ = setup
    submitter.effect = "raise"
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    assert submit(workflow, prepared).outcome == "uncertain"
    submitter.effect = "job"
    again = workflow.prepare(PrintRequest(document, "Office Laser"))
    with pytest.raises(DuplicateSideEffect):
        submit(workflow, again, approve(again, "print-approval-2"))
    with pytest.raises(DuplicateSideEffect):
        submit(workflow, prepared)
    assert len(submitter.submissions) == 1


def test_job_never_seen_is_uncertain_and_not_retried(setup):
    workflow, _, submitter, document, journal, _ = setup
    submitter.effect = "nothing"
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    report = submit(workflow, prepared)
    assert report.outcome == "uncertain"
    assert len(submitter.submissions) == 1
    assert journal.get(operation_key("print-approval-1", prepared.digest)).state is SideEffectState.UNCERTAIN


def test_other_users_or_old_jobs_do_not_count_as_ours(setup):
    workflow, spooler, submitter, document, *_ = setup
    spooler.queue["Office Laser"].append(PrintJobState(7, "report.pdf", "Test User", 0, 0, 1))
    submitter.effect = "nothing"
    spooler.after_submit = None
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))

    def someone_else(spooler_):
        spooler_.queue["Office Laser"].append(PrintJobState(50, "report.pdf", "Another User", 0, 0, 1))

    original = submitter.start

    def submit_then_foreign_job(path, printer):
        result = original(path, printer)
        spooler.after_submit = someone_else
        return result

    submitter.start = submit_then_foreign_job
    assert submit(workflow, prepared).outcome == "uncertain"


def test_two_new_matching_jobs_are_not_guessed(setup):
    workflow, _, submitter, document, *_ = setup
    submitter.effect = "two"
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    assert submit(workflow, prepared).outcome == "uncertain"


def test_paused_job_is_resumed_once_and_then_completes(setup):
    workflow, spooler, submitter, document, *_ = setup
    submitter.job_status = JOB_STATUS_PAUSED
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    report = submit(workflow, prepared)
    assert report.outcome == "succeeded"
    assert spooler.controls == [("Office Laser", 41, JOB_CONTROL_RESUME)]
    assert len(submitter.submissions) == 1


def test_errored_job_is_never_restarted_and_needs_a_person(setup):
    workflow, spooler, submitter, document, *_ = setup
    submitter.job_status = JOB_STATUS_ERROR | JOB_STATUS_PAPEROUT
    spooler_printer = spooler.printer_list[0]
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    # Printer problems are checked again just before submission, so the paper
    # runs out only after the job was accepted.
    original = submitter.start

    def submit_then_paper_out(path, printer):
        result = original(path, printer)
        spooler.printer_list[0] = replace(spooler_printer, status=PRINTER_STATUS_PAPER_OUT)
        return result

    submitter.start = submit_then_paper_out
    report = submit(workflow, prepared)
    assert report.outcome == "needs_attention"
    assert "out of paper" in report.detail
    assert spooler.controls == []
    assert len(submitter.submissions) == 1


def test_job_still_queued_at_the_end_is_reported_as_submitted(setup):
    workflow, spooler, submitter, document, *_ = setup
    submitter.job_status = 0x10  # printing
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    report = submit(workflow, prepared)
    assert report.outcome == "submitted"
    assert report.result["jobId"] == 41


def test_spooler_queries_retry_a_bounded_number_of_times(setup):
    workflow, spooler, submitter, document, *_ = setup
    spooler.unavailable = 2
    assert workflow.prepare(PrintRequest(document, "Office Laser")).printer.name == "Office Laser"
    spooler.unavailable = 5
    with pytest.raises(WorkflowRefused, match="spooler is not responding"):
        workflow.prepare(PrintRequest(document, "Office Laser"))
    assert submitter.submissions == []


@requires_live_device
@pytest.mark.skipif(os.name != "nt", reason="Reads the Windows print spooler.")
def test_windows_spooler_reads_printers_without_side_effects():
    """Live acceptance: reads this computer's real printer list."""
    from core.print_workflow import WinSpooler

    spooler = WinSpooler()
    printers = spooler.printers()
    assert all(isinstance(item.name, str) and item.name for item in printers)
    assert sum(item.is_default for item in printers) <= 1
    assert spooler.current_user()


def test_file_reached_through_a_linked_folder_is_refused(setup, tmp_path):
    workflow, _, submitter, document, *_ = setup
    real = tmp_path / "real"
    real.mkdir()
    (real / "report.pdf").write_bytes(document.read_bytes())
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("Directory links are unavailable here.")
        import _winapi
        _winapi.CreateJunction(str(real), str(linked))
    with pytest.raises(WorkflowRefused, match="is a link"):
        workflow.prepare(PrintRequest(linked / "report.pdf", "Office Laser"))
    assert submitter.submissions == []


def test_an_expired_printing_grant_submits_nothing(setup, monkeypatch):
    from security.gate import ActionGrant

    workflow, _, submitter, document, journal, _ = setup
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    monkeypatch.setattr(ActionGrant, "active", lambda self: False)
    with pytest.raises(WorkflowStopped):
        submit(workflow, prepared)
    assert submitter.submissions == []
    assert journal.get(operation_key("print-approval-1", prepared.digest)) is None


def test_a_queue_failure_after_submission_is_uncertain_not_raised(setup):
    workflow, spooler, submitter, document, journal, _ = setup
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    original = submitter.start

    def submit_then_break(path, printer):
        result = original(path, printer)
        spooler.jobs = lambda printer: (_ for _ in ()).throw(RuntimeError("spooler crashed"))
        return result

    submitter.start = submit_then_break
    report = submit(workflow, prepared)
    assert report.outcome == "uncertain"
    assert journal.get(operation_key("print-approval-1", prepared.digest)).state is SideEffectState.UNCERTAIN


def test_a_failure_while_following_a_confirmed_job_reports_submitted(setup, monkeypatch):
    workflow, spooler, submitter, document, journal, _ = setup
    submitter.job_status = 0x10  # printing
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    monkeypatch.setattr(workflow, "_monitor", lambda *args: (_ for _ in ()).throw(RuntimeError("monitor broke")))
    report = submit(workflow, prepared)
    assert report.outcome == "submitted" and report.result["jobId"] == 41
    assert journal.get(operation_key("print-approval-1", prepared.digest)).state is SideEffectState.CONFIRMED


# ------------------------------------------------ non-blocking submission (P1)


def test_a_job_that_printed_before_any_poll_is_confirmed_through_notifications(setup):
    workflow, spooler, submitter, document, journal, _ = setup
    submitter.effect = "fast"
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    report = submit(workflow, prepared)
    assert report.outcome == "succeeded" and report.result["jobId"] == 41
    assert len(submitter.submissions) == 1
    assert all(watcher.closed for watcher in spooler.watchers)


def test_without_notifications_a_vanished_fast_job_is_uncertain_not_reprinted(setup):
    workflow, spooler, submitter, document, journal, _ = setup
    spooler.watch_available = False
    submitter.effect = "fast"
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    assert submit(workflow, prepared).outcome == "uncertain"
    assert len(submitter.submissions) == 1


def test_stop_while_a_handler_stays_open_returns_promptly_and_never_kills_it(setup):
    workflow, spooler, submitter, document, journal, clock = setup
    submitter.effect = "nothing"
    submitter.handler_done = False
    token = CancellationToken()
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    started_at = clock.now
    original = submitter.start

    def start_then_stop(path, printer):
        handler = original(path, printer)
        token.cancel()
        return handler

    submitter.start = start_then_stop
    report = submit(workflow, prepared, token=token)
    assert report.outcome == "uncertain" and "Stopped while waiting" in report.detail
    assert clock.now - started_at < 1.0, "Stop is observed within one short polling slice"
    assert submitter.handler_process.closed and submitter.handler_process.done is False
    assert all(watcher.closed for watcher in spooler.watchers)
    assert journal.get(operation_key("print-approval-1", prepared.digest)).state is SideEffectState.UNCERTAIN


def test_a_handler_that_never_prints_is_bounded_by_the_appear_timeout(setup):
    workflow, spooler, submitter, document, journal, clock = setup
    submitter.effect = "nothing"
    submitter.handler_done = False
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    started_at = clock.now
    assert submit(workflow, prepared).outcome == "uncertain"
    assert 5.0 <= clock.now - started_at <= 5.5
    assert submitter.handler_process.polls > 10, "the handler is polled, never waited on"


def test_a_handler_that_exits_without_a_job_stops_waiting_after_the_grace_period(setup):
    from core.print_workflow import HANDLER_EXIT_GRACE

    workflow, spooler, submitter, document, journal, clock = setup
    workflow._appear_timeout = 60.0
    submitter.effect = "nothing"
    prepared = workflow.prepare(PrintRequest(document, "Office Laser"))
    started_at = clock.now
    assert submit(workflow, prepared).outcome == "uncertain"
    assert clock.now - started_at <= HANDLER_EXIT_GRACE + 0.5


@pytest.mark.parametrize("name", ['Office "Laser"', 'Office Laser"'])
def test_printer_names_with_quotes_are_refused_not_stripped(name):
    with pytest.raises(ValueError, match="double quote"):
        PrintRequest(Path("D:/report.pdf"), name)


@pytest.mark.skipif(os.name != "nt", reason="ShellExecuteEx is a Windows facility.")
def test_shell_submitter_refuses_quoted_printer_names_before_launching(tmp_path):
    from core.print_workflow import ShellPrintToSubmitter

    with pytest.raises(PrintNotSubmitted, match="double quote"):
        ShellPrintToSubmitter().start(tmp_path / "never-launched.pdf", 'Office "Laser"')


def test_software_tests_cannot_reach_a_real_printer(tmp_path):
    """The live-device boundary covers every printer primitive (G1)."""
    from core import print_renderer as pr
    from core import print_workflow as pw
    from core.print_renderer import PrintOptions

    attempts = [
        lambda: pw.WinSpooler(),
        lambda: pw._shell_execute_printto(None, None),
        lambda: pr.printer_capabilities("Office Laser"),
        lambda: pr.job_devmode("Office Laser", PrintOptions()),
        lambda: pr._create_printer_dc("Office Laser", None),
    ]
    for attempt in attempts:
        with pytest.raises(LiveDeviceBlocked):
            attempt()


@pytest.mark.skipif(os.name != "nt", reason="Printer notification structures are Windows layouts.")
def test_notification_blocks_are_parsed_into_jobs():
    import ctypes
    from core import print_workflow as pw

    strings = {
        pw.JOB_NOTIFY_FIELD_DOCUMENT: ctypes.create_unicode_buffer("report.pdf"),
        pw.JOB_NOTIFY_FIELD_USER_NAME: ctypes.create_unicode_buffer("Test User"),
    }
    count = 4

    class Block(ctypes.Structure):
        _fields_ = [
            ("Version", ctypes.c_uint32), ("Flags", ctypes.c_uint32), ("Count", ctypes.c_uint32),
            ("aData", pw._PRINTER_NOTIFY_INFO_DATA * count),
        ]

    block = Block(2, pw.PRINTER_NOTIFY_INFO_DISCARDED, count)
    for index, field in enumerate((pw.JOB_NOTIFY_FIELD_DOCUMENT, pw.JOB_NOTIFY_FIELD_USER_NAME)):
        entry = block.aData[index]
        entry.Type, entry.Field, entry.Id = pw.JOB_NOTIFY_TYPE, field, 7
        entry.NotifyData.Data.cbBuf = ctypes.sizeof(strings[field])
        entry.NotifyData.Data.pBuf = ctypes.cast(strings[field], ctypes.c_void_p)
    status = block.aData[2]
    status.Type, status.Field, status.Id = pw.JOB_NOTIFY_TYPE, pw.JOB_NOTIFY_FIELD_STATUS, 7
    status.NotifyData.adwData[0] = JOB_STATUS_PRINTED
    other = block.aData[3]
    other.Type, other.Field, other.Id = 0x00, pw.JOB_NOTIFY_FIELD_STATUS, 99  # a printer-type entry
    discarded, jobs = pw.parse_notify_info(ctypes.addressof(block))
    assert discarded is True
    assert jobs == {7: {"document": "report.pdf", "user": "Test User", "status": JOB_STATUS_PRINTED}}


@pytest.mark.skipif(os.name != "nt", reason="Process handles are a Windows facility.")
def test_handler_process_is_polled_without_waiting():
    import ctypes
    import subprocess
    import sys
    import time
    from ctypes import wintypes
    from core.print_workflow import WinHandlerProcess

    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(1.0)"])
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, child.pid)  # SYNCHRONIZE | QUERY_LIMITED
    process = WinHandlerProcess(handle, kernel32)
    try:
        started = time.monotonic()
        assert process.finished() is False
        assert time.monotonic() - started < 0.2
        child.wait(timeout=30)
        assert process.finished() is True and process.exit_code() == 0
    finally:
        process.close()


# --------------------------------------------------- per-job options (renderer)


class FakeRenderer:
    """Test boundary standing in for core.print_renderer.RendererSubmitter."""

    IDENTITY = "Qronos renderer (test)"

    def __init__(self, spooler, *, max_copies=99, color=True, pdf_pages=3) -> None:
        self.spooler, self.max_copies, self.color, self.pdf_pages = spooler, max_copies, color, pdf_pages
        self.started = []
        self.outcome = "queued"  # queued | spooled_and_gone | not_submitted

    def handler_for(self, path):
        return self.IDENTITY if path.suffix.casefold() in {".txt", ".png", ".pdf"} else None

    def capabilities(self, printer, port=None):
        return self.max_copies, self.color

    def page_count(self, path):
        return self.pdf_pages

    def start(self, path, printer, options):
        from core.print_renderer import RenderedJob

        self.started.append((path.name, printer, options.to_plan()))
        job = RenderedJob()
        if self.outcome == "not_submitted":
            job.not_submitted_reason = "The printer refused these options."
        else:
            job.job_id = 77
            if self.outcome == "queued":
                # The renderer names the document after the file, but the match is by id.
                self.spooler.queue.setdefault(printer, []).append(
                    PrintJobState(77, "renamed by driver", "Test User", JOB_STATUS_PRINTED, 0, 2))
        job._done.set()
        return job


@pytest.fixture
def options_setup(setup, tmp_path):
    workflow, spooler, submitter, document, journal, clock = setup
    renderer = FakeRenderer(spooler)
    workflow._renderer = renderer
    pdf = tmp_path / "slides.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    text = tmp_path / "notes.txt"
    text.write_text("hello", encoding="utf-8")
    return workflow, spooler, submitter, renderer, pdf, text, journal


def test_approved_options_are_applied_by_the_renderer_and_matched_by_job_id(options_setup):
    workflow, spooler, submitter, renderer, pdf, text, journal = options_setup
    prepared = workflow.prepare(PrintRequest(pdf, "Office Laser", copies=2, color="monochrome", pages="1-2"))
    assert prepared.plan["route"] == "renderer"
    assert (prepared.plan["copies"], prepared.plan["color"], prepared.plan["pages"]) == (2, "monochrome", "1-2")
    report = submit(workflow, prepared)
    assert report.outcome == "succeeded" and report.result["jobId"] == 77
    assert renderer.started == [("slides.pdf", "Office Laser", {"copies": 2, "color": "monochrome", "pages": "1-2"})]
    assert submitter.submissions == [], "printto is not used when options are approved"


def test_a_job_that_spooled_and_left_the_queue_is_confirmed_by_its_exact_id(options_setup):
    workflow, spooler, submitter, renderer, pdf, text, journal = options_setup
    spooler.watch_available = False
    renderer.outcome = "spooled_and_gone"
    prepared = workflow.prepare(PrintRequest(text, "Office Laser", copies=3))
    report = submit(workflow, prepared)
    assert report.outcome == "succeeded" and report.result["jobId"] == 77
    assert journal.get(operation_key("print-approval-1", prepared.digest)).state is SideEffectState.CONFIRMED


def test_a_renderer_failure_before_the_job_started_is_not_submitted_and_releases_the_file(options_setup):
    workflow, spooler, submitter, renderer, pdf, text, journal = options_setup
    renderer.outcome = "not_submitted"
    prepared = workflow.prepare(PrintRequest(text, "Office Laser", copies=2))
    with pytest.raises(PrintNotSubmitted, match="refused these options"):
        submit(workflow, prepared)
    assert journal.get(operation_key("print-approval-1", prepared.digest)).state is SideEffectState.NOT_SUBMITTED
    renderer.outcome = "queued"
    assert submit(workflow, prepared, approve(prepared, "print-approval-2")).outcome == "succeeded"


@pytest.mark.parametrize("request_kwargs, renderer_kwargs, message", [
    ({"copies": 5}, {"max_copies": 3}, "at most 3 copies"),
    ({"color": "color"}, {"color": False}, "cannot print in colour"),
    ({"pages": "2-5"}, {"pdf_pages": 3}, "has 3 pages"),
    ({"pages": "4"}, {"pdf_pages": 3}, "has 3 pages"),
])
def test_options_the_printer_or_document_cannot_honour_are_refused_before_approval(
    options_setup, request_kwargs, renderer_kwargs, message,
):
    workflow, spooler, submitter, renderer, pdf, text, journal = options_setup
    for key, value in renderer_kwargs.items():
        setattr(renderer, key, value)
    with pytest.raises(WorkflowRefused, match=message):
        workflow.prepare(PrintRequest(pdf, "Office Laser", **request_kwargs))
    assert renderer.started == []


@pytest.mark.parametrize("kwargs", [{"copies": 0}, {"color": "sepia"}, {"pages": "3-1"}, {"pages": "1,1-2"}])
def test_malformed_options_are_refused(options_setup, kwargs):
    workflow, spooler, submitter, renderer, pdf, text, journal = options_setup
    with pytest.raises((WorkflowRefused, ValueError)):
        workflow.prepare(PrintRequest(pdf, "Office Laser", **kwargs))


def test_options_on_a_format_the_renderer_cannot_draw_are_refused(options_setup, tmp_path):
    workflow, spooler, submitter, renderer, pdf, text, journal = options_setup
    doc = tmp_path / "letter.docx"
    doc.write_bytes(b"PK fake docx")
    with pytest.raises(WorkflowRefused, match="plain text, images and PDF"):
        workflow.prepare(PrintRequest(doc, "Office Laser", copies=2))
    # Default options still print through the registered handler.
    assert workflow.prepare(PrintRequest(doc, "Office Laser")).plan["route"] == "printto"


def test_an_unclear_job_with_options_still_blocks_a_reprint_with_other_options(options_setup):
    workflow, spooler, submitter, renderer, pdf, text, journal = options_setup
    renderer.start = lambda *args: (_ for _ in ()).throw(OSError("driver crashed mid-job"))
    prepared = workflow.prepare(PrintRequest(text, "Office Laser", copies=2))
    assert submit(workflow, prepared).outcome == "uncertain"
    again = workflow.prepare(PrintRequest(text, "Office Laser"))
    with pytest.raises(DuplicateSideEffect):
        submit(workflow, again, approve(again, "print-approval-2"))
