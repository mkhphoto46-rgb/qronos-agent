"""An application the person opens leaves Qronos' Jobs, so it is not ended when Qronos stops or restarts.

The launch functions are captured before the autouse conftest fixture blocks them, and every real process start is
replaced by a recorder: nothing is launched.
"""
from __future__ import annotations

import os
import uuid

import pytest

from core import computer_executor
from core.computer_executor import (
    KNOWN_APPS,
    _default_launcher as real_default_launcher,
    _default_open_location as real_default_open_location,
    _launch_user_app as real_launch_user_app,
)

CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


class _Recorded:
    pid = 4242


def test_a_user_app_is_started_outside_the_runtime_jobs(monkeypatch):
    calls = []

    def popen(argv, **options):
        calls.append((argv, options))
        return _Recorded()

    monkeypatch.setattr(computer_executor.subprocess, "Popen", popen)
    real_launch_user_app(["notepad.exe"])
    (argv, options), = calls
    assert argv == ["notepad.exe"] and options["shell"] is False
    assert options["creationflags"] & CREATE_BREAKAWAY_FROM_JOB
    assert options["creationflags"] & CREATE_NEW_PROCESS_GROUP


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
def test_the_static_launcher_and_explorer_go_through_the_user_app_launch(monkeypatch, tmp_path):
    launched = []
    monkeypatch.setattr(computer_executor, "_launch_user_app", lambda argv: launched.append(argv) or _Recorded())
    executable = next(iter(KNOWN_APPS.values())).executable
    real_default_launcher((executable,))
    real_default_open_location(str(tmp_path))
    assert launched == [[executable], ["explorer.exe", str(tmp_path.resolve())]]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
@pytest.mark.parametrize("allow", [False, True])
def test_only_a_guard_that_asks_for_it_permits_breakaway(allow):
    from core.windows_sidecar_guard import WindowsSidecarGuard

    guard = WindowsSidecarGuard.acquire(f"Local\\Qronos.Test.Breakaway.{uuid.uuid4().hex}", allow_breakaway=allow)
    try:
        assert guard.breakaway_permitted() is allow
    finally:
        guard.close()
