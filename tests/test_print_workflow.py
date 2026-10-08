from __future__ import annotations

from dataclasses import replace
import os

import pytest

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

    def submit(self, path, printer):
        self.submissions.append((path.name, printer))
        if self.effect == "raise":
            raise OSError("handler crashed")
        if self.effect == "not_started":
            raise PrintNotSubmitted("ShellExecuteEx refused")
        if self.effect == "job":
            self.spooler.queue.setdefault(printer, []).append(
                PrintJobState(41, path.name, "test user", self.job_status, 0, 2))
        if self.effect == "two":
            for job_id in (41, 42):
                self.spooler.queue.setdefault(printer, []).append(
                    PrintJobState(job_id, path.name, "Test User", 0, 0, 2))
        return 0


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

    original = submitter.submit

    def submit_then_foreign_job(path, printer):
        result = original(path, printer)
        spooler.after_submit = someone_else
        return result

    submitter.submit = submit_then_foreign_job
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
    original = submitter.submit

    def submit_then_paper_out(path, printer):
        result = original(path, printer)
        spooler.printer_list[0] = replace(spooler_printer, status=PRINTER_STATUS_PAPER_OUT)
        return result

    submitter.submit = submit_then_paper_out
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


@pytest.mark.skipif(os.name != "nt", reason="The print spooler is a Windows facility.")
def test_windows_spooler_reads_printers_without_side_effects():
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
    original = submitter.submit

    def submit_then_break(path, printer):
        result = original(path, printer)
        spooler.jobs = lambda printer: (_ for _ in ()).throw(RuntimeError("spooler crashed"))
        return result

    submitter.submit = submit_then_break
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
