"""The sidecar guard's whole-contained tree proof (core/windows_sidecar_guard.py), with real private Job Objects.

Only owned, inert base-interpreter children run (tests/fixtures/contained_tree_child.py), each ending by itself on
its private "go" file. Mutex names are unique test names; no Qronos sidecar, model, server or port is involved.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from core import windows_sidecar_guard as guard_module
from core.windows_sidecar_guard import SidecarGuardUnavailable, WindowsSidecarGuard

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "contained_tree_child.py"
BASE_PYTHON = str(Path(getattr(sys, "_base_executable", sys.executable)))
NO_WINDOW = 0x08000000


@pytest.fixture
def tree(tmp_path):
    directory = tmp_path / "tree"
    directory.mkdir()
    yield directory
    (directory / "go").write_text("go", encoding="utf-8")  # every fixture process ends by itself
    time.sleep(0.3)


@pytest.fixture
def guard():
    owned = WindowsSidecarGuard.acquire(f"Local\\Qronos.Test.TreeProof.{uuid.uuid4().hex}")
    yield owned
    owned.close()


def argv(mode: str, directory: Path) -> list[str]:
    return [BASE_PYTHON, "-B", str(FIXTURE), mode, str(directory)]


def contained(guard, mode: str, directory: Path):
    return guard.spawn_contained(argv(mode, directory), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)


def wait_for(directory: Path, *names: str) -> None:
    deadline = time.monotonic() + 20
    while not all((directory / name).exists() for name in names):
        assert time.monotonic() < deadline, f"fixture never wrote {names}"
        time.sleep(0.02)


def release(directory: Path) -> None:
    (directory / "go").write_text("go", encoding="utf-8")


def members(guard) -> dict[int, str]:
    """The Job's member PIDs and names (test-only read of JobObjectBasicProcessIdList)."""
    import ctypes
    from ctypes import wintypes

    import psutil

    class IdList(ctypes.Structure):
        _fields_ = [("Assigned", wintypes.DWORD), ("InList", wintypes.DWORD), ("Ids", ctypes.c_size_t * 64)]

    info = IdList()
    assert guard_module._kernel32().QueryInformationJobObject(guard._job_handle, 3, ctypes.byref(info),
                                                              ctypes.sizeof(info), None)
    names = {}
    for pid in info.Ids[: info.InList]:
        try:
            names[int(pid)] = psutil.Process(int(pid)).name().lower()
        except psutil.Error:
            names[int(pid)] = "gone"
    return names


def pids(directory: Path, *roles: str) -> set[int]:
    return {int((directory / f"{role}.pid").read_text(encoding="utf-8")) for role in roles}


def only_fixture_and_console_hosts(found: dict[int, str], expected: set[int]) -> bool:
    """Exactly the fixture processes, plus the conhost.exe Windows attaches to each console member."""
    return expected <= set(found) and all(pid in expected or name == "conhost.exe" for pid, name in found.items())


def test_a_runner_started_by_the_first_statement_is_contained_and_only_its_end_proves(guard, tree):
    process = contained(guard, "spawn-first", tree)
    wait_for(tree, "child.pid", "runner.pid")
    found = members(guard)  # the runner started before anything else is a member
    assert only_fixture_and_console_hosts(found, pids(tree, "child", "runner")), found
    assert guard.active_processes() == len(found)
    assert guard.tree_state() == "running" and guard.breakaway_permitted() is False
    assert guard.wait_tree_exit(0.3) is False  # time alone proves nothing
    release(tree)
    assert guard.wait_tree_exit(15) is True
    process.wait(timeout=5)
    guard.close()
    assert guard.tree_state() == "tree_exited"  # observed before the handle closed: sticky


def test_a_dead_server_with_a_live_runner_is_running_never_exited(guard, tree):
    process = contained(guard, "exit-leaving-runner", tree)
    wait_for(tree, "runner.pid", "child.pid")
    process.wait(timeout=15)  # the server parent is gone
    assert process.poll() is not None
    found = members(guard)
    assert process.pid not in found and only_fixture_and_console_hosts(found, pids(tree, "runner")), found
    assert guard.active_processes() >= 1 and guard.tree_state() == "running"
    release(tree)
    assert guard.wait_tree_exit(15) is True


def test_every_nested_descendant_counts(guard, tree):
    contained(guard, "nested", tree)
    wait_for(tree, "child.pid", "middle.pid", "runner.pid")
    found = members(guard)
    assert only_fixture_and_console_hosts(found, pids(tree, "child", "middle", "runner")), found
    assert guard.tree_state() == "running"
    release(tree)
    assert guard.wait_tree_exit(15) is True


def test_a_member_cannot_break_away(guard, tree):
    contained(guard, "try-breakaway", tree)
    wait_for(tree, "breakaway.txt", "child.pid")
    assert (tree / "breakaway.txt").read_text(encoding="utf-8") == "refused"
    assert guard.breakaway_permitted() is False


def test_closing_the_job_before_observing_it_empty_never_proves(tree):
    guard = WindowsSidecarGuard.acquire(f"Local\\Qronos.Test.TreeProof.{uuid.uuid4().hex}")
    process = contained(guard, "wait-go", tree)
    wait_for(tree, "child.pid")
    guard.close()  # kill-on-close ends the tree, but nobody observed it end
    process.wait(timeout=10)
    assert guard.tree_state() == "unknown" and guard.active_processes() is None
    assert guard.end_contained_tree(1) is False and guard.wait_tree_exit(0.2) is False


def test_an_unanswerable_query_is_unknown_and_proves_nothing(guard, tree, monkeypatch):
    contained(guard, "wait-go", tree)
    wait_for(tree, "child.pid")
    real = guard_module._kernel32()

    class Refusing:
        def __getattr__(self, name):
            return getattr(real, name)

        @staticmethod
        def QueryInformationJobObject(*args):
            return 0

    monkeypatch.setattr(guard_module, "_kernel32", lambda: Refusing())
    assert guard.active_processes() is None and guard.tree_state() == "unknown"
    assert guard.wait_tree_exit(0.2) is False
    monkeypatch.undo()
    assert guard.tree_state() == "running"


def test_the_owned_backstop_ends_only_its_own_tree_and_then_proves(guard, tree, tmp_path):
    contained(guard, "spawn-first", tree)
    wait_for(tree, "child.pid", "runner.pid")
    outside = tmp_path / "outside"
    outside.mkdir()
    unrelated = subprocess.Popen(argv("wait-go", outside), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        wait_for(outside, "child.pid")
        assert guard.end_contained_tree(10) is True and guard.tree_state() == "tree_exited"
        assert unrelated.poll() is None, "a process outside the guard's Job must never be ended"
    finally:
        release(outside)
        unrelated.wait(timeout=15)


def test_one_tree_per_guard_and_a_closed_guard_starts_nothing(guard, tree):
    contained(guard, "wait-go", tree)
    wait_for(tree, "child.pid")
    with pytest.raises(SidecarGuardUnavailable):
        contained(guard, "wait-go", tree)
    closed = WindowsSidecarGuard.acquire(f"Local\\Qronos.Test.TreeProof.{uuid.uuid4().hex}")
    closed.close()
    before = set(tree.iterdir())
    with pytest.raises(SidecarGuardUnavailable):
        contained(closed, "wait-go", tree)
    time.sleep(0.3)
    assert set(tree.iterdir()) == before  # nothing was started


def test_a_process_that_cannot_be_contained_is_ended_before_it_ever_runs(guard, tree, monkeypatch):
    real = guard_module._kernel32()
    started = []
    real_popen = subprocess.Popen

    class Recording(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self)

    class Refusing:
        def __getattr__(self, name):
            return getattr(real, name)

        @staticmethod
        def AssignProcessToJobObject(*args):
            return 0

    monkeypatch.setattr(subprocess, "Popen", Recording)
    monkeypatch.setattr(guard_module, "_kernel32", lambda: Refusing())
    with pytest.raises(SidecarGuardUnavailable):
        contained(guard, "spawn-first", tree)
    monkeypatch.undo()
    [process] = started
    assert process.poll() is not None  # ended and reaped through its own handle
    time.sleep(0.3)
    assert not (tree / "child.pid").exists() and not (tree / "runner.pid").exists()  # it never ran
    assert guard.tree_state() == "unknown"
