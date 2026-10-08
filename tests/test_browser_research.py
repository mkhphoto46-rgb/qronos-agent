from __future__ import annotations

from dataclasses import replace
import json

import pytest

from core.action_cancellation import CancellationToken
from core.app_workflow import ApprovalMismatch, WorkflowApproval, WorkflowRefused, WorkflowStopped
from core.browser_research import (
    BrowserNavigation,
    BrowserResearchWorkflow,
    NavigationClarification,
    PageReading,
    UiaPageReader,
    plan_navigation,
)
from core.visible_browser import BrowserIdentity
from core.window_binding import WindowBinder, WindowRect, WindowSnapshot
from security.permissions import PermissionDecision

VOICE = PermissionDecision.REQUIRE_VOICE_CONFIRMATION


# ---------------------------------------------------------------- planning


@pytest.mark.parametrize("text, url", [
    ("open https://example.org/docs", "https://example.org/docs"),
    ("سایت example.org را باز کن", "https://example.org"),
    ("go to wikipedia.org/wiki/Tehran", "https://wikipedia.org/wiki/Tehran"),
])
def test_explicit_public_links_become_one_exact_url(text, url):
    plan = plan_navigation(text)
    assert isinstance(plan, BrowserNavigation) and plan.kind == "open" and plan.url == url


@pytest.mark.parametrize("text", [
    "open http://localhost:8080/",
    "open https://user:pass@example.org/",
    "open http://192.168.1.1/",
    "open https://a.example.org and https://b.example.org",
])
def test_unsafe_or_multiple_links_ask_instead(text):
    assert isinstance(plan_navigation(text), NavigationClarification)


def test_search_requests_become_a_visible_results_page_for_the_users_words_only():
    plan = plan_navigation("search the web for solar panel efficiency")
    assert isinstance(plan, BrowserNavigation) and plan.kind == "search"
    assert plan.url.startswith("https://duckduckgo.com/?q=") and "solar" in plan.url
    assert plan.host == "duckduckgo.com"


def test_ordinary_text_is_not_navigation():
    assert plan_navigation("سلام، حالت چطوره؟") is None
    assert plan_navigation("open Paint") is None


# --------------------------------------------------------------- workflow


def browser_window(**changes) -> WindowSnapshot:
    base = WindowSnapshot(
        hwnd=900, pid=77, thread_id=78, title="Browser", process_path=r"D:\Browser\msedge.exe",
        process_start_time_100ns=99, window_rect=WindowRect(0, 0, 1200, 900),
        client_rect=WindowRect(0, 0, 1200, 900), dpi=96, visible=True, minimized=False, foreground=True,
    )
    return replace(base, **changes)


class FakeLauncher:
    """Test boundary standing in for the visible browser launcher."""

    def __init__(self) -> None:
        self.opened: list[str] = []

    def launch(self, url):
        self.opened.append(url)
        return BrowserIdentity(executable=r"D:\Browser\msedge.exe")


class FakeReader:
    """Test boundary standing in for UI Automation page reading."""

    def __init__(self, pages) -> None:
        self.pages = list(pages)
        self.reads = 0

    def read(self, binding):
        self.reads += 1
        return self.pages.pop(0) if self.pages else None


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make(pages, *, foreground=None):
    state = {"window": browser_window() if foreground is None else foreground}
    binder = WindowBinder(enumerate_fn=lambda: (state["window"],), snapshot_fn=lambda hwnd: state["window"])
    clock = Clock()
    launcher, reader = FakeLauncher(), FakeReader(pages)

    def sleep(token, seconds):
        if token is not None:
            token.raise_if_cancelled()
        clock.now += seconds

    workflow = BrowserResearchWorkflow(
        launcher=launcher, reader=reader, binder=binder, foreground_fn=lambda: state["window"],
        clock=clock, sleep=sleep, load_timeout=3.0, poll_seconds=0.5,
    )
    return workflow, launcher, reader, state


def approve(navigation, decision=VOICE):
    from core.app_workflow import plan_digest

    return WorkflowApproval(approval_id="nav-1", plan_digest=plan_digest(navigation.plan), decision=decision)


NAV = BrowserNavigation(kind="open", url="https://example.org/docs", host="example.org")
PAGE = PageReading(url="https://www.example.org/docs", title="Docs", text="Hello world",
                   links=(("Next", "https://example.org/next"),))


def test_opens_the_approved_url_and_reads_the_visible_page(monkeypatch):
    workflow, launcher, reader, _ = make([None, PAGE])
    report = workflow.run(NAV, approve(NAV), token=None, approval_valid=lambda: True)
    assert launcher.opened == ["https://example.org/docs"]
    assert report.outcome == "succeeded"
    assert report.result["title"] == "Docs" and report.result["text"] == "Hello world"
    assert report.result["untrusted"] is True
    assert report.result["links"] == [["Next", "https://example.org/next"]]


def test_a_page_on_another_host_is_never_read_as_the_answer():
    other = replace(PAGE, url="https://evil.example.net/phish")
    workflow, launcher, _, _ = make([other] * 20)
    report = workflow.run(NAV, approve(NAV), token=None, approval_valid=lambda: True)
    assert report.outcome == "uncertain"
    assert "text" not in report.result and report.result["seenHost"] == "evil.example.net"


def test_lookalike_host_suffix_is_not_accepted():
    lookalike = replace(PAGE, url="https://example.org.attacker.net/docs")
    workflow, *_ = make([lookalike] * 20)
    assert workflow.run(NAV, approve(NAV), token=None, approval_valid=lambda: True).outcome == "uncertain"


def test_navigation_needs_consent_for_this_exact_url():
    workflow, launcher, *_ = make([PAGE])
    other = BrowserNavigation(kind="open", url="https://example.org/other", host="example.org")
    with pytest.raises(ApprovalMismatch):
        workflow.run(NAV, approve(other), token=None, approval_valid=lambda: True)
    with pytest.raises(ApprovalMismatch):
        workflow.run(NAV, approve(NAV, decision=PermissionDecision.ALLOW), token=None, approval_valid=lambda: True)
    assert launcher.opened == []


def test_revoked_or_stopped_navigation_opens_nothing():
    workflow, launcher, *_ = make([PAGE])
    with pytest.raises(WorkflowStopped):
        workflow.run(NAV, approve(NAV), token=None, approval_valid=lambda: False)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(WorkflowStopped):
        workflow.run(NAV, approve(NAV), token=token, approval_valid=lambda: True)
    assert launcher.opened == []


def test_a_browser_that_is_not_in_front_is_not_read():
    workflow, launcher, reader, _ = make([PAGE], foreground=browser_window(foreground=False))
    with pytest.raises(WorkflowRefused, match="not in front"):
        workflow.run(NAV, approve(NAV), token=None, approval_valid=lambda: True)
    assert reader.reads == 0


def test_another_application_in_front_is_not_read():
    workflow, _, reader, _ = make([PAGE], foreground=browser_window(process_path=r"C:\Windows\notepad.exe"))
    with pytest.raises(WorkflowRefused):
        workflow.run(NAV, approve(NAV), token=None, approval_valid=lambda: True)
    assert reader.reads == 0


def test_stop_while_waiting_for_the_page_stops_reading():
    workflow, _, reader, state = make([None] * 10)
    token = CancellationToken()
    original = reader.read

    def read_then_stop(binding):
        token.cancel()
        return original(binding)

    reader.read = read_then_stop
    with pytest.raises(WorkflowStopped):
        workflow.run(NAV, approve(NAV), token=token, approval_valid=lambda: True)


def test_browser_window_replaced_while_loading_stops():
    workflow, _, reader, state = make([None] * 10)
    original = reader.read

    def read_then_replace(binding):
        state["window"] = browser_window(pid=4242)
        return original(binding)

    reader.read = read_then_replace
    with pytest.raises(WorkflowStopped):
        workflow.run(NAV, approve(NAV), token=None, approval_valid=lambda: True)


# --------------------------------------------------------- UIA page reader


def reader_with(payload):
    def run(script, *, timeout):
        assert "Hyperlink" in script and "__HWND__" not in script
        return True, json.dumps(payload)

    return UiaPageReader(run_script=run)


def binding():
    snapshot = browser_window()
    binder = WindowBinder(enumerate_fn=lambda: (snapshot,), snapshot_fn=lambda hwnd: snapshot)
    return binder.bind_snapshot(snapshot, app_id="browser", display_name="msedge.exe")


def test_reader_keeps_only_public_links_and_bounds_text():
    page = reader_with({
        "ok": True, "url": "https://example.org/a", "title": "A", "text": "x" * 30000,
        "links": [["Good", "https://example.org/b"], ["Local", "http://127.0.0.1/"],
                  ["Script", "javascript:alert(1)"], ["Bad shape"]],
    }).read(binding())
    assert page.links == (("Good", "https://example.org/b"),)
    assert len(page.text) == 20000 and page.truncated


@pytest.mark.parametrize("payload", [
    {"ok": False},
    {"ok": True, "url": "file:///C:/secret.txt", "title": "t", "text": "", "links": []},
    {"ok": True, "url": "", "title": "t", "text": "", "links": []},
])
def test_reader_refuses_non_web_or_missing_documents(payload):
    assert reader_with(payload).read(binding()) is None


@pytest.mark.parametrize("text", [
    "search my files for the quarterly report",
    "آخرین فایل را پیدا کن",
    "find the latest news file in Downloads",
])
def test_local_searches_never_become_web_searches(text):
    assert plan_navigation(text) is None


@pytest.mark.parametrize("text, query", [
    ("در اینترنت دربارهٔ قیمت طلا جستجو کن", "قیمت طلا"),
    ("جستجو در اینترنت برای مدرسه موسیقی", "برای مدرسه موسیقی"),
    ("search the web for solar panel efficiency", "solar panel efficiency"),
])
def test_web_searches_keep_only_the_subject_words(text, query):
    plan = plan_navigation(text)
    assert isinstance(plan, BrowserNavigation) and plan.query == query


@pytest.mark.parametrize("text", ["search the web", "در اینترنت جستجو کن"])
def test_a_web_search_without_a_subject_asks_for_one(text):
    assert isinstance(plan_navigation(text), NavigationClarification)
