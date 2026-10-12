from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core.action_cancellation import ActionCancelled, CancellationToken
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
from core.telegram_workflow import (
    ContactCandidate,
    LocalDownloadFolder,
    MessageView,
    TelegramDownloadRequest,
    TelegramDownloadWorkflow,
    UiaTelegramUi,
    _matches_attachment,
    approximate_size,
)
from core.window_binding import WindowBinder, WindowRect, WindowSnapshot, WindowTarget
from core.windows_accessibility import ActionableUiElement
from security.permissions import PermissionDecision

UI = PermissionDecision.REQUIRE_UI_CONFIRMATION
VOICE = PermissionDecision.REQUIRE_VOICE_CONFIRMATION


def window(**changes) -> WindowSnapshot:
    base = WindowSnapshot(
        hwnd=777, pid=4000, thread_id=4001, title="Telegram",
        process_path=r"D:\Apps\Telegram\Telegram.exe",
        process_start_time_100ns=55555,
        window_rect=WindowRect(0, 0, 1200, 900), client_rect=WindowRect(0, 0, 1200, 900),
        dpi=96, visible=True, minimized=False, foreground=True,
    )
    return replace(base, **changes)


TARGET = WindowTarget(app_id="telegram", display_name="Telegram", process_names=("telegram.exe",))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def doc(element_id, name="report.pdf", size="2.0 KB", incoming=True, **extra) -> MessageView:
    return MessageView(element_id=element_id, incoming=incoming, kind="document", file_name=name,
                       size_text=size, download_element_id=f"dl-{element_id}", **extra)


class FakeTelegram:
    """Test boundary standing in for the Telegram app's UI."""

    def __init__(self, state, folder: Path, *, contacts=None, messages=None, title="Sara Ahmadi") -> None:
        self.state = state
        self.folder = folder
        self.contacts = contacts if contacts is not None else (ContactCandidate("c1", "Sara Ahmadi"),)
        self.messages = messages if messages is not None else (
            MessageView("m9", incoming=True, kind="text"),
            doc("m8", incoming=False, name="mine.pdf"),
            doc("m7"),
            doc("m6", name="older.pdf"),
        )
        self.title = title
        self.calls: list[str] = []
        self.download_effect = "write"
        self.binder = WindowBinder(
            enumerate_fn=lambda: (state["window"],),
            snapshot_fn=lambda hwnd: state["window"] if state["window"].hwnd == hwnd else None,
        )

    def bind(self):
        self.calls.append("bind")
        return self.binder.bind(TARGET)

    def search_contacts(self, binding, query, *, grant):
        self.calls.append(f"search:{query}")
        assert grant.target == "app:telegram"
        return self.contacts

    def open_chat(self, binding, candidate, *, grant):
        self.calls.append(f"open:{candidate.element_id}")

    def chat_title(self, binding):
        self.calls.append("title")
        if callable(self.title):
            return self.title()
        return self.title

    def recent_messages(self, binding, *, limit):
        self.calls.append("messages")
        return self.messages

    def start_download(self, binding, message, *, grant):
        self.calls.append(f"download:{message.element_id}")
        if self.download_effect == "write":
            (self.folder / message.file_name).write_bytes(b"x" * 2048)
        elif self.download_effect == "raise":
            raise RuntimeError("UI Automation pipe broke after the request")
        elif self.download_effect == "two":
            (self.folder / message.file_name).write_bytes(b"x" * 2048)
            stem, ext = message.file_name.rsplit(".", 1)
            (self.folder / f"{stem} (1).{ext}").write_bytes(b"x" * 2048)


@pytest.fixture
def setup(tmp_path):
    folder = tmp_path / "Telegram downloads"
    folder.mkdir()
    state = {"window": window()}
    ui = FakeTelegram(state, folder)
    clock = Clock()

    def sleep(token, seconds):
        if token is not None:
            token.raise_if_cancelled()
        clock.now += seconds

    journal = SideEffectJournal(tmp_path / "journal.json")
    workflow = TelegramDownloadWorkflow(
        ui, binder=ui.binder, journal=journal, folder=LocalDownloadFolder(folder),
        clock=clock, verify_timeout=10.0, poll_seconds=1.0, sleep=sleep,
    )
    return workflow, ui, state, folder, journal, clock


def approve(prepared, approval_id="approval-1", decision=UI):
    return WorkflowApproval(
        approval_id=approval_id, plan_digest=prepared.digest, decision=decision,
        evidence_identity_token=prepared.evidence.identity_token,
    )


def inspect(workflow, contact="Sara Ahmadi", **kwargs):
    return workflow.inspect(
        TelegramDownloadRequest(contact), approved=kwargs.pop("approved", VOICE),
        token=kwargs.pop("token", None), approval_valid=kwargs.pop("approval_valid", lambda: True),
    )


def test_downloads_only_the_newest_incoming_document_and_reports_its_path(setup):
    workflow, ui, state, folder, journal, _ = setup
    prepared = inspect(workflow)
    assert prepared.message.element_id == "m7"
    assert prepared.plan["fileName"] == "report.pdf"
    assert not any(call.startswith("download") for call in ui.calls)
    report = workflow.download(prepared, approve(prepared), token=None, approval_valid=lambda: True)
    assert report.outcome == "succeeded"
    assert report.result["path"] == str(folder / "report.pdf")
    assert [call for call in ui.calls if call.startswith("download")] == ["download:m7"]
    entry = journal.get(operation_key("approval-1", prepared.digest))
    assert entry.state is SideEffectState.CONFIRMED


def test_persian_letter_variants_match_exactly_but_similar_names_do_not(setup):
    workflow, ui, *_ = setup
    ui.contacts = (ContactCandidate("c1", "علي رضايي"),)  # Arabic yeh
    ui.title = "علی رضایی"
    assert inspect(workflow, "علی رضایی").plan["contact"] == "علی رضایی"
    ui.contacts = (ContactCandidate("c1", "Sara Ahmadi Work"), ContactCandidate("c2", "Sara Ahmady"))
    with pytest.raises(WorkflowRefused, match="exactly that name"):
        inspect(workflow, "Sara Ahmadi")
    assert not any(call.startswith("open") for call in ui.calls[-3:])


def test_duplicate_exact_names_stop_without_opening_a_chat(setup):
    workflow, ui, *_ = setup
    ui.contacts = (ContactCandidate("c1", "Sara Ahmadi"), ContactCandidate("c2", "sara  ahmadi"))
    with pytest.raises(WorkflowAmbiguous):
        inspect(workflow)
    assert not any(call.startswith("open") for call in ui.calls)


def test_wrong_chat_header_stops(setup):
    workflow, ui, *_ = setup
    ui.title = "Sara Ahmadi (Group)"
    with pytest.raises(WorkflowRefused, match="not the requested contact"):
        inspect(workflow)


@pytest.mark.parametrize("messages, expected", [
    ((MessageView("m1", incoming=True, kind="text"), doc("m2", incoming=False)), "No incoming file"),
    ((doc("m1", downloaded=True),), "already downloaded"),
    ((doc("m1", name="..\\evil.exe"),), "cannot save safely"),
    ((replace(doc("m1"), download_element_id=""),), "no download control"),
])
def test_unusable_attachments_are_refused(setup, messages, expected):
    workflow, ui, *_ = setup
    ui.messages = messages
    with pytest.raises(WorkflowRefused, match=expected):
        inspect(workflow)


def test_inspection_needs_at_least_voice_consent(setup):
    workflow, ui, *_ = setup
    with pytest.raises(ApprovalMismatch):
        inspect(workflow, approved=PermissionDecision.ALLOW)
    assert ui.calls == []


def test_download_needs_ui_confirmation_for_this_exact_plan(setup):
    workflow, ui, *_ = setup
    prepared = inspect(workflow)
    with pytest.raises(ApprovalMismatch):
        workflow.download(prepared, approve(prepared, decision=VOICE), token=None, approval_valid=lambda: True)
    other = replace(approve(prepared), plan_digest="0" * 64)
    with pytest.raises(ApprovalMismatch):
        workflow.download(prepared, other, token=None, approval_valid=lambda: True)
    assert not any(call.startswith("download") for call in ui.calls)


def test_revoked_or_stopped_approval_never_downloads(setup):
    workflow, ui, _, _, journal, _ = setup
    prepared = inspect(workflow)
    with pytest.raises(WorkflowStopped):
        workflow.download(prepared, approve(prepared), token=None, approval_valid=lambda: False)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(WorkflowStopped):
        workflow.download(prepared, approve(prepared, "approval-2"), token=token, approval_valid=lambda: True)
    assert not any(call.startswith("download") for call in ui.calls)
    assert journal.get(operation_key("approval-1", prepared.digest)) is None


def test_stop_arriving_after_journal_entry_settles_as_not_submitted(setup):
    workflow, ui, _, _, journal, _ = setup
    prepared = inspect(workflow)
    token = CancellationToken()
    checks = {"n": 0}
    real_title = ui.title

    def title():
        checks["n"] += 1
        return real_title

    ui.title = title
    original = workflow._journal.begin

    def begin_then_stop(*args, **kwargs):
        original(*args, **kwargs)
        token.cancel()

    workflow._journal.begin = begin_then_stop
    with pytest.raises(WorkflowStopped):
        workflow.download(prepared, approve(prepared), token=token, approval_valid=lambda: True)
    assert not any(call.startswith("download") for call in ui.calls)
    assert journal.get(operation_key("approval-1", prepared.digest)).state is SideEffectState.NOT_SUBMITTED


def test_newer_attachment_since_approval_refuses(setup):
    workflow, ui, *_ = setup
    prepared = inspect(workflow)
    ui.messages = (doc("m10", name="new.pdf"), *ui.messages)
    with pytest.raises(WorkflowRefused, match="changed since it was shown"):
        workflow.download(prepared, approve(prepared), token=None, approval_valid=lambda: True)
    assert not any(call.startswith("download") for call in ui.calls)


def test_replaced_telegram_process_refuses(setup):
    workflow, ui, state, *_ = setup
    prepared = inspect(workflow)
    state["window"] = window(pid=4999)
    with pytest.raises(WorkflowStopped):
        workflow.download(prepared, approve(prepared), token=None, approval_valid=lambda: True)
    assert not any(call.startswith("download") for call in ui.calls)


def test_unclear_ui_failure_is_uncertain_and_never_repeated(setup):
    workflow, ui, _, _, journal, _ = setup
    ui.download_effect = "raise"
    prepared = inspect(workflow)
    report = workflow.download(prepared, approve(prepared), token=None, approval_valid=lambda: True)
    assert report.outcome == "uncertain"
    second = inspect(workflow)
    with pytest.raises(DuplicateSideEffect):
        workflow.download(second, approve(second, "approval-2"), token=None, approval_valid=lambda: True)
    with pytest.raises(DuplicateSideEffect):
        workflow.download(prepared, approve(prepared), token=None, approval_valid=lambda: True)
    assert [call for call in ui.calls if call.startswith("download")] == ["download:m7"]


def test_missing_file_after_timeout_is_uncertain(setup):
    workflow, ui, _, _, journal, _ = setup
    ui.download_effect = "nothing"
    prepared = inspect(workflow)
    report = workflow.download(prepared, approve(prepared), token=None, approval_valid=lambda: True)
    assert report.outcome == "uncertain"
    assert journal.get(operation_key("approval-1", prepared.digest)).state is SideEffectState.UNCERTAIN


def test_stop_while_verifying_is_uncertain_not_retried(setup):
    workflow, ui, *_ = setup
    ui.download_effect = "nothing"
    token = CancellationToken()
    prepared = inspect(workflow)
    original = ui.start_download

    def start_then_stop(*args, **kwargs):
        original(*args, **kwargs)
        token.cancel()

    ui.start_download = start_then_stop
    report = workflow.download(prepared, approve(prepared), token=token, approval_valid=lambda: True)
    assert report.outcome == "uncertain"
    assert "will not start it again" in report.detail


def test_two_matching_new_files_are_not_guessed(setup):
    workflow, ui, *_ = setup
    ui.download_effect = "two"
    prepared = inspect(workflow)
    report = workflow.download(prepared, approve(prepared), token=None, approval_valid=lambda: True)
    assert report.outcome == "uncertain"
    assert report.result["candidates"] == ["report (1).pdf", "report.pdf"]


def test_implausible_size_is_not_accepted(setup):
    workflow, ui, *_ = setup
    ui.messages = (doc("m7", size="5 MB"),)
    prepared = inspect(workflow)
    report = workflow.download(prepared, approve(prepared), token=None, approval_valid=lambda: True)
    assert report.outcome == "uncertain"


def test_existing_same_name_file_is_not_mistaken_for_the_download(setup):
    workflow, ui, _, folder, *_ = setup
    (folder / "report.pdf").write_bytes(b"old" * 10)
    ui.download_effect = "nothing"
    prepared = inspect(workflow)
    assert workflow.download(prepared, approve(prepared), token=None, approval_valid=lambda: True).outcome == "uncertain"


def test_size_and_name_helpers():
    assert approximate_size("2.1 MB") == (2149580, 2254439)
    assert approximate_size("۲ مگابایت")[0] <= 2 * 1024 ** 2 <= approximate_size("۲ مگابایت")[1]
    assert approximate_size("soon") is None
    assert _matches_attachment("Report (2).PDF", "report.pdf")
    assert not _matches_attachment("report.pdf.exe", "report.pdf")
    assert not _matches_attachment("report copy.pdf", "report.pdf")


# --------------------------------------------- UIA adapter recognition


def element(runtime_id, name, control_type, left, top, width=200, height=30, **flags):
    defaults = dict(
        automation_id="", class_name="", value="", enabled=True, focusable=True, focused=False,
        value_pattern=False, value_read_only=None, text_pattern=False, invoke_pattern=False,
        expand_pattern=False,
    )
    defaults.update(flags)
    return ActionableUiElement(
        runtime_id=runtime_id, name=name, control_type=control_type,
        left=left, top=top, width=width, height=height, **defaults,
    )


def uia(elements, calls):
    state = {"window": window()}
    binder = WindowBinder(
        enumerate_fn=lambda: (state["window"],),
        snapshot_fn=lambda hwnd: state["window"],
    )

    from core.accessibility_action_executor import AccessibilityActionExecutor
    from core.accessibility_targeting import AccessibilityTargetLocator
    from core.windows_accessibility_mutation import UiInvokeReceipt

    by_id = {item.runtime_id: item for item in elements}

    # Only the native calls are test boundaries; the reviewed executor and its
    # identity, authorization and cancellation guards are real.
    def set_value(hwnd, runtime_id, value, *, binding, authorization, binder, input_authorization=None):
        assert input_authorization is not None, "typing needs an input authorization"
        calls.append(("set", runtime_id, value, tuple(sorted(authorization.allowed_runtime_ids))))
        return by_id[runtime_id]

    def invoke(hwnd, runtime_id, *, binding, authorization, binder):
        calls.append(("invoke", runtime_id, tuple(sorted(authorization.allowed_runtime_ids))))
        return UiInvokeReceipt(runtime_id=runtime_id)

    class ForegroundOnly:
        def acquire_foreground(self, binding, **_options):
            calls.append(("foreground",))

    actuator = AccessibilityActionExecutor(
        binder, ForegroundOnly(), AccessibilityTargetLocator(None, enumerate_fn=lambda hwnd: tuple(elements)),
        set_value_fn=set_value, invoke_fn=invoke,
        lookup_runtime_fn=lambda hwnd, runtime_id: by_id.get(runtime_id),
    )
    ui = UiaTelegramUi(binder=binder, target_fn=lambda: TARGET, actuator=actuator,
                       elements_fn=lambda hwnd: elements, settle_seconds=0)
    return ui, binder


def gui_grant():
    from core.actions import ActionRequest
    from core.telegram_workflow import grant_for
    from security.permissions import ActionCategory

    return grant_for(ActionRequest(category=ActionCategory.GUI_AUTOMATION, target="app:telegram",
                                   summary="test"), VOICE)


def test_uia_search_types_only_into_the_single_search_field():
    calls = []
    elements = [
        element("s", "Search", "ControlType.Edit", 10, 10, value_pattern=True, value_read_only=False),
        element("r1", "Sara Ahmadi", "ControlType.ListItem", 10, 60),
        element("r2", "Sara Ahmadi Work", "ControlType.ListItem", 10, 100),
    ]
    ui, binder = uia(elements, calls)
    binding = ui.bind()
    found = ui.search_contacts(binding, "Sara Ahmadi", grant=gui_grant())
    assert calls == [("foreground",), ("set", "s", "Sara Ahmadi", ("s",))], "typing happens only after the window is in front"
    assert [item.display_name for item in found] == ["Sara Ahmadi", "Sara Ahmadi Work"]


@pytest.mark.parametrize("fields", [0, 2])
def test_uia_search_refuses_without_exactly_one_search_field(fields):
    from core.telegram_workflow import TelegramActionNotStarted

    calls = []
    elements = [
        element(f"s{i}", "Search", "ControlType.Edit", 10, 10 + 40 * i, value_pattern=True, value_read_only=False)
        for i in range(fields)
    ]
    ui, _ = uia(elements, calls)
    with pytest.raises(TelegramActionNotStarted):
        ui.search_contacts(ui.bind(), "Sara Ahmadi", grant=gui_grant())
    assert calls == []


def test_uia_messages_read_direction_attachment_and_download_control():
    calls = []
    elements = [
        element("chat1", "Sara Ahmadi", "ControlType.ListItem", 0, 60, width=300),
        element("hdr", "Sara Ahmadi", "ControlType.Text", 320, 5, width=200, focusable=False),
        element("in", "report.pdf 2.0 KB", "ControlType.ListItem", 330, 400, width=300, height=60),
        element("dl", "Download", "ControlType.Button", 340, 410, width=30, height=30, invoke_pattern=True),
        element("out", "mine.pdf 1 KB", "ControlType.ListItem", 850, 500, width=300, height=60),
        element("open", "Open", "ControlType.Button", 860, 510, width=30, height=30, invoke_pattern=True),
    ]
    ui, _ = uia(elements, calls)
    binding = ui.bind()
    assert ui.chat_title(binding) == "Sara Ahmadi"
    messages = ui.recent_messages(binding, limit=10)
    newest, older = messages
    assert (newest.element_id, newest.incoming, newest.downloaded) == ("out", False, True)
    assert (older.element_id, older.incoming, older.kind, older.file_name, older.size_text, older.download_element_id) == (
        "in", True, "document", "report.pdf", "2.0 KB", "dl",
    )
    ui.start_download(binding, older, grant=gui_grant())
    assert calls == [("invoke", "dl", ("dl",))]


def test_uia_actions_refuse_an_element_whose_identity_changed():
    from core.accessibility_action_executor import AccessibilityTargetStale

    calls = []
    elements = [
        element("chat1", "Sara Ahmadi", "ControlType.ListItem", 0, 60, width=300),
        element("hdr", "Sara Ahmadi", "ControlType.Text", 320, 5, width=200, focusable=False),
        element("in", "report.pdf 2.0 KB", "ControlType.ListItem", 330, 400, width=300, height=60),
        element("dl", "Download", "ControlType.Button", 340, 410, width=30, height=30, invoke_pattern=True),
    ]
    ui, _ = uia(elements, calls)
    binding = ui.bind()
    newest = ui.recent_messages(binding, limit=10)[0]
    # Same RuntimeId, but the control now says something else.
    elements[3] = element("dl", "Cancel", "ControlType.Button", 340, 410, width=30, height=30, invoke_pattern=True)
    ui._actuator.lookup_runtime_fn = lambda hwnd, runtime_id: {e.runtime_id: e for e in elements}.get(runtime_id)
    with pytest.raises(AccessibilityTargetStale):
        ui.start_download(binding, newest, grant=gui_grant())
    assert not any(call[0] == "invoke" for call in calls)


def test_a_folder_error_after_the_download_started_is_uncertain_not_raised(setup):
    workflow, ui, _, _, journal, _ = setup
    prepared = inspect(workflow)
    calls = {"n": 0}
    original = workflow._folder.listing

    def failing_after_start():
        calls["n"] += 1
        if calls["n"] > 1:  # the first call is the snapshot before downloading
            raise PermissionError("folder became unreadable")
        return original()

    workflow._folder.listing = failing_after_start
    report = workflow.download(prepared, approve(prepared), token=None, approval_valid=lambda: True)
    assert report.outcome == "uncertain"
    assert journal.get(operation_key("approval-1", prepared.digest)).state is SideEffectState.UNCERTAIN
