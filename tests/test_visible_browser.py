from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.visible_browser import (
    BrowserUnavailable,
    BrowserVisibilityUnconfirmed,
    VisibleBrowserLauncher,
    resolve_default_browser,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def browser(tmp_path) -> Path:
    executable = tmp_path / "Browser" / "msedge.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"MZ test stand-in, never executed")
    return executable


def launcher(executable, *, visible=True, popen_error=None, calls=None):
    calls = calls if calls is not None else []
    clock = Clock()

    def popen(arguments, **options):
        calls.append((arguments, options))
        if popen_error:
            raise popen_error
        return object()

    def sleep(seconds):
        clock.now += seconds

    return VisibleBrowserLauncher(
        handler_fn=lambda: str(executable),
        popen_fn=popen,
        visible_fn=lambda name: visible if not callable(visible) else visible(name),
        clock=clock,
        sleep=sleep,
        visible_timeout=2.0,
    ), calls


def test_opens_one_public_link_as_a_single_argument_without_a_shell(browser):
    launch, calls = launcher(browser)
    launch("https://example.org/a?b=c")
    assert len(calls) == 1
    arguments, options = calls[0]
    assert arguments == [str(browser), "https://example.org/a?b=c"]
    assert options["shell"] is False


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/win.ini",
    "javascript:alert(1)",
    "ms-settings:privacy",
    "http://localhost:8080/",
    "http://192.168.1.10/",
    "https://user:pass@example.org/",
    "https://example.org/ --disable-web-security",
    "https://example.org/\"--flag",
    "http://2130706433/",
    "",
])
def test_unsafe_links_never_reach_the_browser(browser, url):
    launch, calls = launcher(browser)
    with pytest.raises(BrowserUnavailable):
        launch(url)
    assert calls == []


@pytest.mark.parametrize("name", ["notepad.exe", "cmd.exe", "powershell.exe", "unknownbrowser.exe"])
def test_default_handler_that_is_not_a_known_browser_is_refused(tmp_path, name):
    executable = tmp_path / name
    executable.write_bytes(b"stand-in")
    launch, calls = launcher(executable)
    with pytest.raises(BrowserUnavailable, match="not a known web browser"):
        launch("https://example.org/")
    assert calls == []


def test_relative_missing_or_absent_handlers_are_refused(tmp_path):
    with pytest.raises(BrowserUnavailable):
        resolve_default_browser(lambda: None)
    with pytest.raises(BrowserUnavailable, match="full path"):
        resolve_default_browser(lambda: "msedge.exe")
    with pytest.raises(BrowserUnavailable, match="missing"):
        resolve_default_browser(lambda: str(tmp_path / "msedge.exe"))


def test_linked_browser_executable_is_refused(tmp_path, browser):
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(browser.parent, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("Directory links are unavailable here.")
        import _winapi
        _winapi.CreateJunction(str(browser.parent), str(linked))
    # A regular executable reached through a junction folder is still refused.
    with pytest.raises(BrowserUnavailable, match="through a link"):
        resolve_default_browser(lambda: str(linked / "msedge.exe"))
    assert resolve_default_browser(lambda: str(browser)).process_name == "msedge.exe"


def test_success_needs_a_visible_browser_window_in_front(browser):
    states = iter([False, False, True])
    launch, calls = launcher(browser, visible=lambda name: next(states))
    launch("https://example.org/")
    assert len(calls) == 1


def test_no_visible_window_is_reported_not_retried(browser):
    launch, calls = launcher(browser, visible=False)
    with pytest.raises(BrowserVisibilityUnconfirmed):
        launch("https://example.org/")
    assert len(calls) == 1


def test_start_failure_is_reported(browser):
    launch, calls = launcher(browser, popen_error=FileNotFoundError("gone"))
    with pytest.raises(BrowserUnavailable, match="could not be started"):
        launch("https://example.org/")


def test_library_gated_open_link_flow_uses_the_launcher_once_after_approval(tmp_path, browser):
    from core.action_audit import ActionAuditLog
    from core.approval_secret import ApprovalSecretStore
    from core.artifact_registry import ArtifactRegistry
    from core.artifact_service import ArtifactProducerRegistry
    from core.library_executor import LibraryActionRefused, LibraryExecutor

    registry = ArtifactRegistry(tmp_path / "db" / "library.sqlite3", tmp_path / "Artifacts")
    item = ArtifactProducerRegistry(registry).register_link("https://example.org/report", name="Report")
    launch, calls = launcher(browser)
    executor = LibraryExecutor(
        registry,
        audit=ActionAuditLog(tmp_path / "audit.jsonl"),
        browser=launch,
        approval_secrets=ApprovalSecretStore(tmp_path / "secret.sqlite3", audit_path=None),
        protected_roots=(),
    )
    approval = executor.prepare("open_link", [item.id])
    assert calls == [], "nothing opens before the person approves"
    assert executor.commit(approval["token"], operation="open_link")["complete"] is True
    assert [arguments for arguments, _ in calls] == [[str(browser), "https://example.org/report"]]
    with pytest.raises(LibraryActionRefused):
        executor.commit(approval["token"], operation="open_link")
    assert len(calls) == 1
