"""Bounded startup status hooks (core/runtime_hooks.py). In-process fakes only."""

from __future__ import annotations

import json
import sys
import threading
import types

import pytest

from core import runtime_hooks
from core.runtime_hooks import StartupStatusHook, run_startup_status_hooks, running_app_version


def install(monkeypatch, name: str, function) -> StartupStatusHook:
    module = types.ModuleType(f"core.{name}")
    module.startup_check = function
    monkeypatch.setitem(sys.modules, f"core.{name}", module)
    return StartupStatusHook("update_status", f"core.{name}", "startup_check")


def run(hooks, timeout=1.0, context=None):
    events = []
    thread = run_startup_status_hooks(lambda *event: events.append(event), hooks, context or {"appVersion": "0.1.0"}, timeout)
    thread.join(10)
    assert not thread.is_alive()
    return events


def test_there_are_no_hooks_until_a_reviewed_service_is_added():
    assert runtime_hooks.STARTUP_STATUS_HOOKS == ()
    assert run_startup_status_hooks(lambda *event: None) is None


def test_a_hook_reports_its_status_with_the_running_version(monkeypatch):
    seen = {}

    def check(context):
        seen.update(context)
        context["appVersion"] = "tampered"  # a copy: cannot change what others see
        return {"state": "idle"}

    hook = install(monkeypatch, "fake_status_ok", check)
    assert run([hook, hook]) == [("update_status", "ready", '{"state":"idle"}')] * 2
    assert seen == {"appVersion": "0.1.0"}  # the second call got a fresh copy


@pytest.mark.parametrize("result, reason", [
    (lambda context: 1 / 0, "ZeroDivisionError"),
    (lambda context: ["not", "an", "object"], "not_an_object"),
    (lambda context: {"x": float("nan")}, "not_json"),
    (lambda context: {"x": "y" * (runtime_hooks.MAX_STATUS_BYTES + 1)}, "too_large"),
])
def test_a_failing_hook_is_reported_unavailable_and_the_next_still_runs(monkeypatch, result, reason):
    bad = install(monkeypatch, "fake_status_bad", result)
    good = StartupStatusHook("library_status", "core.fake_status_good", "startup_check")
    install(monkeypatch, "fake_status_good", lambda context: {"ok": True})
    events = run([bad, good])
    assert events[0] == ("update_status", "unavailable", json.dumps({"reason": reason}))
    assert events[1] == ("library_status", "ready", '{"ok":true}')


def test_a_hook_that_never_returns_is_abandoned_at_its_deadline(monkeypatch):
    release = threading.Event()
    hook = install(monkeypatch, "fake_status_hang", lambda context: release.wait(30) or {})
    try:
        assert run([hook], timeout=0.3) == [("update_status", "unavailable", '{"reason": "timeout"}')]
    finally:
        release.set()


def test_a_missing_service_module_is_unavailable_not_a_crash():
    hook = StartupStatusHook("update_status", "core.no_such_service_module", "startup_check")
    assert run([hook])[0][:2] == ("update_status", "unavailable")


@pytest.mark.parametrize("event_type, module, function", [
    ("runtime_ready", "core.x", "f"),
    ("supervisor_state", "core.x", "f"),
    ("Update", "core.x", "f"),
    ("update_status", "os", "system"),
    ("update_status", "core.x.y", "f"),
    ("update_status", "core.x", "__import__"),
])
def test_hooks_are_closed_records(event_type, module, function):
    with pytest.raises(ValueError):
        StartupStatusHook(event_type, module, function)


def test_the_running_version_comes_only_from_the_desktop():
    assert running_app_version({"QRONOS_APP_VERSION": "0.1.0"}) == "0.1.0"
    for bad in ("", "0.1", "v0.1.0", "01.1.0", "0.1.0-beta", "0.1.0\n", "1" * 12 + ".0.0"):
        assert running_app_version({"QRONOS_APP_VERSION": bad}) == ""
    assert running_app_version({}) == ""
