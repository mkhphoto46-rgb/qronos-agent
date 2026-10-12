"""
Visible, permission-gated web research through the person's own browser.

Public web content reaches Qronos only through a browser window the person can
see. This module never sends an HTTP request, never fetches a page, never
submits a form, clicks a page element or types into a page. One approved
navigation does exactly this:

    1. ``plan_navigation`` turns the request into one exact URL: either an
       explicit public link (``core.link_policy.validate_web_url``) or a search
       results page for a query that passed ``core.web_query.build_query``'s
       privacy gate (user words only; no memory, file or profile context).
    2. The person approves that exact URL (browser navigation consent tier or
       stronger); the approval is bound to the plan digest.
    3. :class:`core.visible_browser.VisibleBrowserLauncher` opens it in the
       default browser, which must come to the front.
    4. The foreground browser window is bound by HWND and process identity,
       and its active document is read through Windows UI Automation: the
       address must be on the planned host, then the visible page title, text
       (bounded) and link list (public http(s) links only) are returned.

Page text is untrusted data. It is returned for the person and a local model
to read; nothing in it can trigger another action. Following a link is a new
plan with a new approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import time
from typing import Callable, Protocol, Sequence
from urllib.parse import quote_plus, urlsplit

from core.action_cancellation import ActionCancelled, CancellationToken
from core.actions import ActionRequest
from core.app_workflow import (
    STOP_CANCELLED,
    StepGuard,
    WorkflowApproval,
    WorkflowRefused,
    WorkflowReport,
    WorkflowStopped,
    grant_for,
    plan_digest,
)
from core.link_policy import UnsafeLink, validate_web_url
from core.persian_text import normalise
from core.web_query import PrivacyGateError, build_query
from core.window_binding import WindowBinder, WindowBinding, WindowSnapshot
from security.gate import ActionGrant
from security.permissions import ActionCategory, PermissionDecision


# The visible results page used for searches. A plain GET URL that the person
# sees in their own browser; Qronos does not call any search API.
SEARCH_PAGE = "https://duckduckgo.com/?q={query}"
SEARCH_HOST = "duckduckgo.com"
MAX_PAGE_CHARS = 20_000
MAX_LINKS = 40
DEFAULT_LOAD_TIMEOUT = 20.0
DEFAULT_POLL_SECONDS = 0.5

# A web search needs an explicit web context and a search verb, so a local
# request such as "search my files" never becomes a web search.
def _words(*alternatives: str) -> re.Pattern[str]:
    """Whole words or phrases only: never a fragment of a longer word."""
    return re.compile(
        r"(?<![\w‌])(?:" + "|".join(alternatives) + r")(?![\w‌])",
        re.IGNORECASE,
    )


_WEB_CONTEXT = _words("web", "internet", "online", "google", "اینترنت", "گوگل", "وب", "آنلاین")
_SEARCH_VERB = _words("search", "look up", "find", "جستجو", "جست‌وجو", "سرچ", "بگرد", "پیدا کن")
_COMMAND_WORDS = _words(
    "search", "look up", "find", "on", "in", "the", "web", "internet", "online", "google", "for",
    "about", "please", "جستجو", "جست‌وجو", "سرچ", "بگرد", "پیدا", "کن", "کنید", "در", "توی",
    "اینترنت", "گوگل", "وب", "آنلاین", "را", "درباره‌ی", "دربارهٔ", "درباره", "لطفاً", "لطفا",
)
_OPEN_WORDS = re.compile(r"(?:\bopen\b|\bgo to\b|\bvisit\b|باز کن|برو به|بازش کن|نشان بده)", re.IGNORECASE)
_URL = re.compile(r"\bhttps?://[^\s\"'«»<>]+", re.IGNORECASE)
_BARE_HOST = re.compile(r"(?<![\w@./-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(/[^\s\"'«»<>]*)?(?![\w@-])", re.IGNORECASE)


@dataclass(frozen=True)
class BrowserNavigation:
    kind: str  # "search" or "open"
    url: str
    host: str
    query: str = ""

    @property
    def plan(self) -> dict:
        return {"workflow": "browser_navigate", "kind": self.kind, "url": self.url, "query": self.query}


@dataclass(frozen=True)
class NavigationClarification:
    question: str  # Persian


@dataclass(frozen=True)
class PageReading:
    url: str
    title: str
    text: str
    links: tuple[tuple[str, str], ...] = ()
    truncated: bool = False


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").rstrip(".").casefold()
    return host[4:] if host.startswith("www.") else host


def plan_navigation(text: str) -> BrowserNavigation | NavigationClarification | None:
    """One exact URL for an explicit link or a privacy-gated search; else None."""
    raw = normalise(str(text or ""), for_search=False)
    if not raw:
        return None
    explicit = _URL.findall(raw)
    if not explicit and _OPEN_WORDS.search(raw):
        bare = _BARE_HOST.search(raw)
        if bare:
            explicit = ["https://" + bare.group(1) + (bare.group(2) or "")]
    if explicit:
        if len(explicit) > 1:
            return NavigationClarification("چند نشانی در درخواست هست؛ یکی را مشخص کنید.")
        candidate = explicit[0].rstrip(".,،؛)")
        try:
            url = validate_web_url(candidate)
        except UnsafeLink:
            return NavigationClarification(
                "این نشانی باز نمی‌شود؛ فقط نشانی‌های عمومی http یا https بدون نام کاربری و رمز مجاز است."
            )
        return BrowserNavigation(kind="open", url=url, host=_host(url))
    if _WEB_CONTEXT.search(raw) and _SEARCH_VERB.search(raw):
        subject = " ".join(_COMMAND_WORDS.sub(" ", raw).split()).strip(" .,،؟?!:")
        if not subject:
            return NavigationClarification("چه چیزی را در اینترنت جستجو کنم؟ لطفاً موضوع را دقیق بگویید.")
        try:
            query = build_query(subject)
        except PrivacyGateError:
            return NavigationClarification("چه چیزی را در اینترنت جستجو کنم؟ لطفاً موضوع را دقیق بگویید.")
        url = SEARCH_PAGE.format(query=quote_plus(query.text))
        return BrowserNavigation(kind="search", url=validate_web_url(url), host=SEARCH_HOST, query=query.text)
    return None


class PageReader(Protocol):
    def read(self, binding: WindowBinding) -> PageReading | None: ...


class BrowserLauncher(Protocol):
    def launch(self, url: str): ...


class BrowserResearchWorkflow:
    def __init__(
        self,
        *,
        launcher: BrowserLauncher,
        reader: PageReader,
        binder: WindowBinder,
        foreground_fn: Callable[[], WindowSnapshot | None],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[CancellationToken | None, float], None] | None = None,
        load_timeout: float = DEFAULT_LOAD_TIMEOUT,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._launcher = launcher
        self._reader = reader
        self._binder = binder
        self._foreground = foreground_fn
        self._clock = clock
        self._sleep = sleep or _cancellable_sleep
        self._load_timeout = float(load_timeout)
        self._poll = float(poll_seconds)

    def run(
        self,
        navigation: BrowserNavigation,
        approval: WorkflowApproval,
        *,
        token: CancellationToken | None,
        approval_valid: Callable[[], bool],
    ) -> WorkflowReport:
        approval.require_for(plan=navigation.plan, required=PermissionDecision.REQUIRE_VOICE_CONFIRMATION)
        grant: ActionGrant = grant_for(
            ActionRequest(
                category=ActionCategory.BROWSER_NAVIGATION,
                target=navigation.url,
                summary="Open one exact page in the visible browser and read what it shows.",
                parameters={"kind": navigation.kind, "query": navigation.query},
            ),
            approval.decision,
        )
        guard = StepGuard(token=token, approval_valid=approval_valid, clock=self._clock)
        guard.checkpoint("open browser")
        if not grant.active():
            raise WorkflowRefused("The browsing permission expired before the page was opened.")
        identity = self._launcher.launch(navigation.url)

        binding = self._bind_foreground(identity.process_name)
        bound = StepGuard(
            token=token, approval_valid=approval_valid, binder=self._binder, binding=binding, clock=self._clock,
        )
        deadline = self._clock() + self._load_timeout
        last_host = ""
        while True:
            bound.checkpoint("read page")
            reading = self._reader.read(binding)
            if reading is not None:
                last_host = _host(reading.url)
                if last_host == navigation.host or last_host.endswith("." + navigation.host):
                    return WorkflowReport(
                        outcome="succeeded",
                        detail="The page is open in the visible browser and was read from it.",
                        steps=guard.steps + bound.steps,
                        result={
                            "url": reading.url,
                            "title": reading.title,
                            "text": reading.text,
                            "links": [list(item) for item in reading.links],
                            "truncated": reading.truncated,
                            "untrusted": True,
                        },
                    )
            if self._clock() >= deadline:
                return WorkflowReport(
                    outcome="uncertain",
                    detail=(
                        "The browser is open, but the active page is not the approved address "
                        "(or did not finish loading), so nothing was read."
                    ),
                    steps=guard.steps + bound.steps,
                    result={"expectedHost": navigation.host, "seenHost": last_host},
                )
            try:
                self._sleep(token, self._poll)
            except ActionCancelled as cancelled:
                raise WorkflowStopped(STOP_CANCELLED, "Stopped while the page was loading.") from cancelled

    def _bind_foreground(self, process_name: str) -> WindowBinding:
        snapshot = self._foreground()
        if snapshot is None or snapshot.process_name != process_name or not snapshot.foreground:
            raise WorkflowRefused("The browser window is not in front; nothing was read.")
        return self._binder.bind_snapshot(snapshot, app_id="browser", display_name=process_name)


def _cancellable_sleep(token: CancellationToken | None, seconds: float) -> None:
    if token is None:
        time.sleep(seconds)
    else:
        token.sleep(seconds)


def foreground_window_snapshot() -> WindowSnapshot | None:
    from core.window_binding import enumerate_windows

    for snapshot in enumerate_windows():
        if snapshot.foreground:
            return snapshot
    return None


# ----------------------------------------------------- production reader


def _run_read_only_script(script: str, *, timeout: float) -> tuple[bool, str]:
    """Run one read-only UI Automation PowerShell script (no shell, bounded)."""
    import subprocess

    try:
        finished = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=float(timeout), encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, type(error).__name__
    if finished.returncode != 0:
        return False, "UI Automation read failed."
    return True, (finished.stdout or "").strip()


_READ_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]::new(__HWND__))
if ($null -eq $root) { '{"ok":false}'; exit 0 }
$documentType = [System.Windows.Automation.ControlType]::Document
$condition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty, $documentType)
$best = $null; $bestArea = 0
foreach ($candidate in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)) {
    if ($candidate.Current.IsOffscreen) { continue }
    $r = $candidate.Current.BoundingRectangle
    $area = $r.Width * $r.Height
    if ($area -gt $bestArea) { $best = $candidate; $bestArea = $area }
}
if ($null -eq $best) { '{"ok":false}'; exit 0 }
$url = ''
$valuePattern = $null
if ($best.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$valuePattern)) {
    $url = [string]$valuePattern.Current.Value
}
$text = ''
$textPattern = $null
if ($best.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$textPattern)) {
    $text = [string]$textPattern.DocumentRange.GetText(__MAX_CHARS__)
}
$links = @()
$linkCondition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Hyperlink)
foreach ($link in $best.FindAll([System.Windows.Automation.TreeScope]::Descendants, $linkCondition)) {
    if ($links.Count -ge __MAX_LINKS__) { break }
    $target = $null
    if ($link.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$target)) {
        $links += ,@([string]$link.Current.Name, [string]$target.Current.Value)
    }
}
[ordered]@{ ok = $true; url = $url; title = [string]$best.Current.Name; text = $text; links = $links } |
    ConvertTo-Json -Compress -Depth 4
"""


class UiaPageReader:
    """Read the active document of one bound browser window through UI Automation."""

    def __init__(self, *, run_script: Callable[..., tuple[bool, str]] | None = None, timeout: float = 15.0) -> None:
        self._run = run_script or _run_read_only_script
        self._timeout = float(timeout)

    def read(self, binding: WindowBinding) -> PageReading | None:
        script = (
            _READ_SCRIPT.replace("__HWND__", str(int(binding.hwnd)))
            .replace("__MAX_CHARS__", str(MAX_PAGE_CHARS + 1))
            .replace("__MAX_LINKS__", str(MAX_LINKS * 2))
        )
        ok, raw = self._run(script, timeout=self._timeout)
        if not ok or not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return None
        url = str(payload.get("url") or "")
        try:
            url = validate_web_url(url)
        except UnsafeLink:
            return None
        text = str(payload.get("text") or "")
        links = []
        for item in payload.get("links") or []:
            if not isinstance(item, list) or len(item) != 2:
                continue
            name, target = (str(value or "").strip() for value in item)
            try:
                links.append((name[:200], validate_web_url(target)))
            except UnsafeLink:
                continue
            if len(links) >= MAX_LINKS:
                break
        return PageReading(
            url=url,
            title=str(payload.get("title") or "")[:300],
            text=text[:MAX_PAGE_CHARS],
            links=tuple(links),
            truncated=len(text) > MAX_PAGE_CHARS,
        )
