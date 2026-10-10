"""The memory sidecar manager's whole-contained lifecycle proof, with the REAL guard and private Job Objects.

The manager's fixed launch ``[executable, "serve"]`` (cwd = project root) runs an inert base-interpreter stand-in:
the executable is the base interpreter and ``serve`` is a private script in this test's tmp project root that
starts a "runner" child, as a model server does. No Ollama, model, port or request: the API/listener checks are
faked, the profile is unchanged. Mutex names are unique test names; every fixture process ends on its "go" file.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

from core import windows_sidecar_guard as guard_module
from core.memory_runtime import MemorySidecarManager, SidecarLifecycle
from core.windows_sidecar_guard import SidecarAlreadyRunning, WindowsSidecarGuard

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "contained_tree_child.py"
BASE_PYTHON = str(Path(getattr(sys, "_base_executable", sys.executable)))


@pytest.fixture
def world(tmp_path, monkeypatch):
    project = tmp_path / "project"
    tree = tmp_path / "tree"
    project.mkdir()
    tree.mkdir()
    (project / "serve").write_text(
        "import runpy, sys\n"
        f"sys.argv = [{str(FIXTURE)!r}, 'serve', {str(tree)!r}]\n"
        f"runpy.run_path({str(FIXTURE)!r}, run_name='__main__')\n", encoding="utf-8")
    name = f"Local\\Qronos.Test.MemorySidecar.{uuid.uuid4().hex}"
    manager = MemorySidecarManager(project_root=project,
                                   sidecar_guard_factory=lambda: WindowsSidecarGuard.acquire(name))
    monkeypatch.setattr(manager, "_ollama_executable", lambda: BASE_PYTHON)
    monkeypatch.setattr(manager, "_api_ready", lambda timeout=0.25: manager._process is not None
                        and manager._process.poll() is None and (tree / "child.pid").exists())
    monkeypatch.setattr(manager, "_port_open", lambda: False)
    monkeypatch.setattr("core.memory_runtime.process_owns_listener", Mock(return_value=True))
    monkeypatch.setattr("core.memory_runtime.MEMORY_SIDECAR_TREE_GRACE_SECONDS", 0.5)
    monkeypatch.setattr("core.memory_runtime.MEMORY_SIDECAR_TREE_BACKSTOP_SECONDS", 5.0)
    yield manager, tree, name
    (tree / "go").write_text("go", encoding="utf-8")
    try:
        manager.shutdown()
    finally:
        time.sleep(0.3)


def started(manager, tree) -> None:
    deadline = time.monotonic() + 20
    while not manager.ensure_server():
        assert time.monotonic() < deadline, "the stand-in server never became ready"
        time.sleep(0.05)
    while not (tree / "runner.pid").exists():
        assert time.monotonic() < deadline, "the stand-in runner never started"
        time.sleep(0.02)


def mutex_held(name: str) -> bool:
    try:
        WindowsSidecarGuard.acquire(name).close()
        return False
    except SidecarAlreadyRunning:
        return True


def test_the_server_and_its_runner_are_contained_and_stop_releases_only_after_the_observed_end(world):
    manager, tree, name = world
    started(manager, tree)
    lifecycle = manager.lifecycle
    assert isinstance(lifecycle, SidecarLifecycle) and lifecycle.generation == 1
    assert lifecycle.server_pid == manager._process.pid and lifecycle.state() == "running"
    guard = manager._sidecar_guard
    assert guard.contains_tree and guard.active_processes() >= 2 and guard.breakaway_permitted() is False
    with pytest.raises(AttributeError):
        lifecycle._guard = None  # immutable capability
    manager.shutdown()  # exact server ended; the runner waits for "go": the owned Job backstop ends it
    assert lifecycle.state() == "tree_exited"
    assert manager._sidecar_guard is None and guard.is_closed and not mutex_held(name)
    assert manager.owned_process is None


def test_a_dead_server_with_a_live_runner_keeps_ownership_until_a_retire_proves_the_tree_ended(world):
    manager, tree, name = world
    started(manager, tree)
    first = manager.lifecycle
    owner, captured = manager.owned_generation()
    assert captured is first and owner is manager.owned_process
    manager._process.kill()  # the server parent dies (the test ends its own fixture child, exact handle)
    manager._process.wait(timeout=10)
    assert manager.owned_generation() is None  # never a capability without its exact live owner
    assert manager.owned_process is None  # no connection owner any more ...
    assert first.state() == "running"  # ... but its runner still holds the model: not tree_exited
    assert mutex_held(name)  # singleton still owned: no second sidecar can start elsewhere
    (tree / "child.pid").unlink()
    (tree / "runner.pid").unlink()
    started(manager, tree)  # retire: owned backstop + observation, then a NEW generation
    assert first.state() == "tree_exited"  # sticky proof for the old generation only
    second = manager.lifecycle
    assert second.generation == 2 and second.state() == "running" and second is not first
    owner, captured = manager.owned_generation()  # one read: the owner and ITS generation's capability
    assert captured is second and owner.pid == second.server_pid != first.server_pid
    assert first.state() == "tree_exited" and second.state() == "running"  # a later generation never answers
                                                                             # for (or blurs) an earlier one


def test_an_unobservable_stop_retains_the_guard_and_no_second_server_starts(world, monkeypatch):
    manager, tree, name = world
    started(manager, tree)
    lifecycle = manager.lifecycle
    guard = manager._sidecar_guard
    real = guard_module._kernel32()

    class Unanswerable:
        def __getattr__(self, attribute):
            return getattr(real, attribute)

        @staticmethod
        def QueryInformationJobObject(*args):
            return 0  # Windows cannot say whether the Job is empty

    monkeypatch.setattr(guard_module, "_kernel32", lambda: Unanswerable())
    manager.shutdown()
    assert lifecycle.state() == "unknown"  # terminate was issued, but nothing was observed
    assert manager._sidecar_guard is guard and not guard.is_closed and mutex_held(name)
    spawned = Mock(side_effect=AssertionError("no second server while the first is unproved"))
    monkeypatch.setattr(guard, "spawn_contained", spawned)
    assert manager.ensure_server() is False and manager.lifecycle is lifecycle
    monkeypatch.setattr(guard_module, "_kernel32", lambda: real)  # the query answers again
    monkeypatch.undo()
    manager.shutdown()  # recheck: now observed empty while the handle was still held
    assert lifecycle.state() == "tree_exited" and manager._sidecar_guard is None and not mutex_held(name)
