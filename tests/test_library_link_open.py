"""
Saved web links reach a browser only through the gated, audited Library flow.

No real browser is ever opened: launchers are recording fakes, and
``webbrowser.open``/``os.startfile`` are replaced with failures for safety.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import sqlite3
import webbrowser

import pytest

from core import library_cli
from core.action_audit import ActionAuditLog, AuditEvent
from core.actions import ActionOutcome
from core.approval_secret import ApprovalSecretStore
from core.artifact_registry import ArtifactRegistry
from core.artifact_service import ArtifactProducerRegistry
from core.library_executor import BROWSER_UNAVAILABLE, LibraryActionRefused, LibraryExecutor
from core.link_policy import UnsafeLink, validate_web_url
from core.permission_control import set_custom_rule
from security.gate import Verdict
from security.permissions import PermissionDecision


ROOT = Path(__file__).resolve().parents[1]
SECRET = "correct horse battery"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/",
        "https://example.org/path/page?q=1&lang=fa#section",
        "http://example.com:8080/a",
        "https://sub.example.co.uk",
        "https://xn--exmple-cua.de/",
        "https://93.184.216.34/",
    ],
)
def test_public_web_links_are_accepted_unchanged(url: str) -> None:
    assert validate_web_url(f"  {url}  ") == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JAVASCRIPT:alert(1)",
        "file:///C:/Windows/System32/calc.exe",
        "data:text/html,<script>alert(1)</script>",
        "ms-settings:privacy",
        "mailto:someone@example.org",
        "about:blank",
        "chrome://settings",
        "ftp://example.org/file",
        "https://user:pass@example.org/",
        "https://example.org@evil.example/",
        "http://localhost:11434/api/tags",
        "http://printer.local/",
        "http://127.0.0.1:11434/",
        "http://[::1]/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://127.1/",
        "https://exa mple.org/",
        "https://example.org/\nline",
        "https:\\\\evil.example\\",
        "https://",
        "https:///path-only",
        "https:example.org",
        "http://example.org:99999/",
        "https://-bad-.example/",
        "https://example.org/" + "a" * 2100,
        "",
        None,
        42,
    ],
)
def test_unsafe_links_are_refused(url: object) -> None:
    with pytest.raises(UnsafeLink):
        validate_web_url(url)


class Links:
    def __init__(self, tmp_path: Path) -> None:
        self.registry = ArtifactRegistry(tmp_path / "db" / "library.sqlite3", tmp_path / "Artifacts")
        self.producer = ArtifactProducerRegistry(self.registry)
        self.audit = ActionAuditLog(tmp_path / "audit.jsonl")
        self.opened: list[str] = []
        self.secrets = ApprovalSecretStore(tmp_path / "secret.sqlite3", audit_path=None)

    def launcher(self, url: str) -> None:
        self.opened.append(url)

    def make(self, **overrides) -> LibraryExecutor:
        options = dict(
            audit=self.audit,
            browser=self.launcher,
            approval_secrets=self.secrets,
            protected_roots=(),
        )
        options.update(overrides)
        return LibraryExecutor(self.registry, **options)

    def link(self, url: str = "https://example.org/private-report?id=abc123"):
        return self.producer.register_link(url, name="Report")


@pytest.fixture
def links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Links:
    def never(*args, **kwargs):
        raise AssertionError("tests must never open a real browser or file")

    monkeypatch.setattr(webbrowser, "open", never)
    monkeypatch.setattr(os, "startfile", never, raising=False)
    return Links(tmp_path)


def test_unsafe_links_are_not_saved(links: Links) -> None:
    for url in ("javascript:alert(1)", "http://127.0.0.1:11434/", "https://u:p@example.org/"):
        with pytest.raises(UnsafeLink):
            links.link(url)


def test_registry_open_never_opens_links_directly(links: Links) -> None:
    item = links.link()
    with pytest.raises(PermissionError, match="approval flow"):
        links.registry.open(item.id)
    result = None
    with pytest.raises(PermissionError):
        result = library_cli.run("open", {"id": item.id}, registry=links.registry)
    assert result is None and links.opened == []


def test_without_a_reviewed_browser_workflow_opening_fails_closed(links: Links) -> None:
    item = links.link()
    executor = links.make(browser=None)

    with pytest.raises(LibraryActionRefused) as caught:
        executor.prepare("open_link", [item.id])

    assert str(caught.value) == BROWSER_UNAVAILABLE
    assert "COPY LINK" in BROWSER_UNAVAILABLE
    events = [(r.event, r.decision or r.outcome) for r in links.audit.records()]
    assert (AuditEvent.DECIDED, "require_voice_confirmation") in events
    assert (AuditEvent.COMPLETED, "refused") in events


def test_default_policy_needs_an_exact_click_approval(links: Links) -> None:
    item = links.link()
    executor = links.make()

    approval = executor.prepare("open_link", [item.id])
    assert approval["confirmation"] == "click"
    assert approval["decision"] == "require_voice_confirmation"
    assert approval["targets"][0]["pathOrUrl"] == item.path_or_url
    assert links.opened == [], "nothing opens before the person confirms"

    result = executor.commit(approval["token"], operation="open_link")

    assert result["complete"] is True
    assert links.opened == [item.path_or_url]
    with pytest.raises(LibraryActionRefused):
        executor.commit(approval["token"], operation="open_link")
    assert links.opened == [item.path_or_url], "single use"


def test_audit_never_contains_the_url(links: Links) -> None:
    item = links.link()
    executor = links.make()
    approval = executor.prepare("open_link", [item.id])
    executor.commit(approval["token"], operation="open_link")

    text = Path(links.audit.path).read_text(encoding="utf-8")
    assert "example.org" not in text and "abc123" not in text and "private-report" not in text
    assert "browser_navigation" in text and item.id in text


def test_deny_policy_blocks_the_click(links: Links) -> None:
    item = links.link()
    set_custom_rule("browser", "browser_navigation", "DENY")

    with pytest.raises(LibraryActionRefused, match="forbidden"):
        links.make().prepare("open_link", [item.id])
    assert links.opened == []


def test_deny_after_approval_blocks_the_commit(links: Links) -> None:
    item = links.link()
    executor = links.make()
    approval = executor.prepare("open_link", [item.id])
    set_custom_rule("browser", "browser_navigation", "DENY")

    result = executor.commit(approval["token"], operation="open_link")
    assert result["refused"] == 1 and links.opened == []


def _require_typed_secret(request, audit=None):
    verdict = Verdict(
        request=request,
        decision=PermissionDecision.REQUIRE_TYPED_SECRET,
        outcome=ActionOutcome.AWAITING_APPROVAL,
        reason="browser_navigation requires typed secret.",
    )
    if audit is not None:
        audit(verdict)
    return verdict


def test_a_stronger_requirement_is_never_satisfied_by_a_click(links: Links) -> None:
    item = links.link()
    executor = links.make(evaluate_fn=_require_typed_secret)

    with pytest.raises(LibraryActionRefused, match="APPROVAL SECRET"):
        executor.prepare("open_link", [item.id])

    links.secrets.set_secret(SECRET)
    approval = executor.prepare("open_link", [item.id])
    assert approval["confirmation"] == "secret"
    with pytest.raises(LibraryActionRefused):
        executor.commit(approval["token"], operation="open_link")
    with pytest.raises(LibraryActionRefused):
        executor.commit(approval["token"], operation="open_link", secret="not it at all")
    assert links.opened == []

    executor.commit(approval["token"], operation="open_link", secret=SECRET)
    assert links.opened == [item.path_or_url]


def test_a_url_changed_after_approval_is_not_opened(links: Links) -> None:
    item = links.link()
    executor = links.make()
    approval = executor.prepare("open_link", [item.id])
    with sqlite3.connect(links.registry.path) as c:
        c.execute("UPDATE artifacts SET path_or_url='https://evil.example/' WHERE id=?", (item.id,))

    result = executor.commit(approval["token"], operation="open_link")
    assert result["refused"] == 1 and links.opened == []


@pytest.mark.parametrize("stored", ["javascript:alert(1)", "file:///C:/x.bat", "http://127.0.0.1:11434/"])
def test_legacy_unsafe_stored_links_are_refused(links: Links, stored: str) -> None:
    item = links.link()
    with sqlite3.connect(links.registry.path) as c:
        c.execute("UPDATE artifacts SET path_or_url=? WHERE id=?", (stored, item.id))

    with pytest.raises(LibraryActionRefused):
        links.make().prepare("open_link", [item.id])
    assert links.opened == []


def test_open_link_only_applies_to_one_saved_link(links: Links, tmp_path: Path) -> None:
    a, b = links.link("https://example.org/a"), links.link("https://example.org/b")
    with pytest.raises(LibraryActionRefused, match="one link"):
        links.make().prepare("open_link", [a.id, b.id])

    path = tmp_path / "Artifacts" / "Documents" / "notes.md"
    path.write_text("x", encoding="utf-8")
    file_item = links.producer.register_file(path, origin="Generated by Qronos")
    with pytest.raises(LibraryActionRefused, match="saved web links"):
        links.make().prepare("open_link", [file_item.id])
    assert links.opened == []


def test_local_file_open_keeps_its_restrictions_and_exact_identity(
    links: Links, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda p: started.append(p), raising=False)
    path = tmp_path / "Artifacts" / "Documents" / "notes.md"
    path.write_text("original", encoding="utf-8")
    item = links.producer.register_file(path, origin="Generated by Qronos")

    links.registry.open(item.id)
    assert started == [str(path)]

    path.unlink()
    path.write_text("a different file at the same path", encoding="utf-8")
    with pytest.raises(PermissionError, match="no longer the artifact"):
        links.registry.open(item.id)

    script = tmp_path / "Artifacts" / "Documents" / "tool.js"
    script.write_text("WScript.Echo(1)", encoding="utf-8")
    with pytest.raises(PermissionError, match="executable"):
        links.registry.open(links.producer.register_file(script, origin="Generated by Qronos").id)
    assert started == [str(path)]


def test_no_product_module_imports_webbrowser() -> None:
    offenders = []
    for package in ("core", "security"):
        for path in (ROOT / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = (
                    [a.name for a in node.names] if isinstance(node, ast.Import)
                    else [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                )
                if "webbrowser" in names:
                    offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_library_view_routes_link_open_through_the_gated_flow() -> None:
    view = (ROOT / "desktop" / "src" / "components" / "LibraryView.tsx").read_text(encoding="utf-8")
    assert 'if(a.category==="link"){await requestAction("open_link",[a.id]);return;}' in view
