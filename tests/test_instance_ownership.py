"""
R1/R2/N1: singletons are OWNED (wait-acquired on a holder thread) (core/instance_ownership.py and
its users).

Native boundary of these fixtures (audited):
- real CreateMutexW / WaitForSingleObject / ReleaseMutex / CloseHandle, only on PRIVATE
  names "Local\\QronosR9Test-<uuid>-<tag>";
- a competing owner is tests/fixtures/mutex_owner_child.py, a base-interpreter child
  (tests/fixtures/child_python.py, with -B) that owns the private mutex and either waits
  for a tmp go-file (60 s at most) or ends while owning it; it is waited with bounds and
  killed only through its own Popen handle.
No production mutex, store, data root, device, model or network.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

import pytest

from core import instance_ownership as io_
from core.instance_ownership import (
    AlreadyOwned,
    MutexApi,
    OwnedMutex,
    OwnershipUnavailable,
)
from tests.fixtures.child_python import child_python

windows = pytest.mark.skipif(os.name != "nt", reason="Windows named mutexes")
CHILD = Path(__file__).resolve().parent / "fixtures" / "mutex_owner_child.py"


def private(tag: str) -> str:
    return f"Local\\QronosR9Test-{uuid.uuid4().hex}-{tag}"


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel


class ExistingHandle:
    """A handle to a private mutex that this process keeps open WITHOUT owning it."""

    def __init__(self, name: str) -> None:
        self.kernel = _kernel()
        self.handle = self.kernel.CreateMutexW(None, False, name)
        assert self.handle

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class ChildOwner:
    """A base-interpreter child that owns the private mutex (fixtures/mutex_owner_child.py)."""

    def __init__(self, tmp: Path, name: str, mode: str) -> None:
        tag = uuid.uuid4().hex[:8]
        self.ready, self.go = tmp / f"ready-{tag}", tmp / f"go-{tag}"
        executable, environment = child_python()
        self.process = subprocess.Popen([executable, "-B", str(CHILD), name, mode, str(self.ready), str(self.go)],
                                        env=environment)
        deadline = time.monotonic() + 30
        while not (self.ready.exists() and self.ready.read_text()) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.ready.read_text() in ("0", "128"), "the child did not own the mutex"

    def let_go(self) -> None:
        self.go.write_text("1")
        assert self.process.wait(30) == 0

    def wait_exit(self) -> None:
        self.process.wait(30)

    def reap(self) -> None:
        if self.process.poll() is None:
            self.go.write_text("1")
            try:
                self.process.wait(30)
            except subprocess.TimeoutExpired:
                self.process.kill()  # exactly this test's own child, by its own handle
                self.process.wait(10)


@pytest.fixture
def owners(tmp_path):
    made: list[ChildOwner] = []

    def make(name: str, mode: str = "hold") -> ChildOwner:
        owner = ChildOwner(tmp_path, name, mode)
        made.append(owner)
        return owner

    yield make
    for owner in made:
        owner.reap()
    assert all(owner.process.poll() is not None for owner in made)


# ------------------------------------------------------------------ real ownership


@windows
def test_a_stale_unowned_mutex_is_acquired_existence_is_not_ownership():
    name = private("stale")
    stale = ExistingHandle(name)  # exists, nobody owns it (what the old existence check called "running")
    try:
        owned = OwnedMutex.acquire(name)
        assert owned.owned and not owned.abandoned
        owned.release()
    finally:
        stale.close()


@windows
def test_an_active_competing_owner_is_refused_until_it_lets_go(owners):
    name = private("busy")
    owner = owners(name)
    with pytest.raises(AlreadyOwned):
        OwnedMutex.acquire(name)
    owner.let_go()
    owned = OwnedMutex.acquire(name)
    assert owned.owned and not owned.abandoned
    owned.release()


@windows
def test_an_owner_that_died_holding_it_leaves_it_abandoned_and_that_is_reported(owners):
    name = private("abandoned")
    keeper = ExistingHandle(name)  # keeps the mutex object alive across the owner's death
    try:
        owners(name, "abandon").wait_exit()
        owned = OwnedMutex.acquire(name)
        assert owned.owned and owned.abandoned
        owned.release()
    finally:
        keeper.close()


@windows
def test_release_runs_on_the_holder_thread_once_even_when_called_from_another_thread(owners, tmp_path):
    name = private("release")
    owned = OwnedMutex.acquire(name)
    caller = threading.Thread(target=owned.release)  # not the thread that called acquire
    caller.start()
    caller.join(10)
    owned.release()  # a second release does nothing
    assert not owned.owned
    owner = owners(name)  # another process can now own it: the release really happened
    owner.let_go()


@windows
def test_a_second_owner_in_the_same_process_is_refused_too():
    name = private("twice")
    first = OwnedMutex.acquire(name)
    try:
        with pytest.raises(AlreadyOwned):
            OwnedMutex.acquire(name)  # another holder thread: Windows grants it only to the owner thread
    finally:
        first.release()


# ---------------------------------------------------------------- holder thread unit


def fake_api(results: dict, calls: list) -> MutexApi:
    def create(name):
        calls.append(("create", threading.get_ident()))
        return results.get("handle", 77)

    def wait(handle, ms):
        calls.append(("wait", threading.get_ident()))
        return results["wait"]

    def release(handle):
        calls.append(("release", threading.get_ident()))
        return True

    def close(handle):
        calls.append(("close", threading.get_ident()))
        return True

    return MutexApi(create, wait, release, close, lambda: 5)


def test_release_and_close_happen_once_on_the_acquiring_thread():
    calls: list = []
    owned = OwnedMutex.acquire("Local\\QronosR9Test-unit", api=fake_api({"wait": 0}, calls))
    owned.release()
    owned.release()
    kinds = [kind for kind, _ in calls]
    assert kinds == ["create", "wait", "release", "close"]
    assert len({thread for _, thread in calls}) == 1 and calls[0][1] != threading.get_ident()


@pytest.mark.parametrize("results,error", [({"wait": 0x102}, AlreadyOwned), ({"wait": 0xFFFFFFFF}, OwnershipUnavailable),
                                           ({"wait": 0, "handle": 0}, OwnershipUnavailable)])
def test_anything_but_a_granted_wait_is_not_ownership_and_closes_the_handle(results, error):
    calls: list = []
    with pytest.raises(error):
        OwnedMutex.acquire("Local\\QronosR9Test-unit", api=fake_api(results, calls))
    kinds = [kind for kind, _ in calls]
    assert "release" not in kinds  # never released what was not owned
    if results.get("handle", 77):
        assert kinds[-1] == "close"


# ------------------------------------------------------------------- the three guards


@windows
def test_the_sidecar_guard_owns_refuses_a_live_owner_and_reports_an_abandoned_slot(owners):
    from core.windows_sidecar_guard import SidecarAlreadyRunning, WindowsSidecarGuard

    name = private("sidecar")
    owner = owners(name)
    with pytest.raises(SidecarAlreadyRunning):
        WindowsSidecarGuard.acquire(name)
    owner.let_go()

    keeper = ExistingHandle(name)  # so the dead owner's mutex is really ABANDONED, not recreated
    try:
        owners(name, "abandon").wait_exit()
        guard = WindowsSidecarGuard.acquire(name)
        assert guard.abandoned
        guard.close()
        guard.close()
    finally:
        keeper.close()
    owners(name).let_go()  # released for real


@windows
def test_the_supervisor_singleton_is_owned_and_a_second_supervisor_is_refused(owners):
    from core.supervisor import SupervisorAlreadyRunning, SupervisorInstanceGuard

    name = private("supervisor")
    first = SupervisorInstanceGuard.acquire(name=name)
    assert first.owned
    with pytest.raises(SupervisorAlreadyRunning):
        SupervisorInstanceGuard.acquire(name=name)
    first.release()
    owners(name).let_go()  # nothing of ours still owns it


@windows
def test_the_bridge_singleton_is_owned_for_life_and_a_second_bridge_is_refused(owners):
    from core.runtime_instance_guard import RuntimeAlreadyRunning, RuntimeInstanceGuard

    name = private("bridge")
    bridge = RuntimeInstanceGuard.acquire(name=name)
    assert bridge.owned
    with pytest.raises(RuntimeAlreadyRunning):
        RuntimeInstanceGuard.acquire(name=name)
    bridge.close()
    owners(name).let_go()  # released for real


# ------------------------------------------- unconfirmed release


@pytest.mark.parametrize("release_ok,close_ok", [(True, True), (False, True), (True, False)])
def test_an_unconfirmed_native_release_is_never_reported_as_clean(release_ok, close_ok):
    api = MutexApi(create=lambda name: 9, wait=lambda handle, ms: 0, release=lambda handle: release_ok,
                   close=lambda handle: close_ok, last_error=lambda: 0)
    owned = OwnedMutex.acquire("Local\\QronosR9Test-unit", api=api)
    assert owned.release() is (release_ok and close_ok)
    assert owned.release_confirmed is (release_ok and close_ok) and not owned.owned


def test_a_holder_that_does_not_finish_its_release_is_still_reported_owned(monkeypatch):
    monkeypatch.setattr(io_, "_REPORT_SECONDS", 0.5)
    stuck = threading.Event()
    api = MutexApi(create=lambda name: 9, wait=lambda handle, ms: 0, release=lambda handle: stuck.wait(30) or True,
                   close=lambda handle: True, last_error=lambda: 0)
    owned = OwnedMutex.acquire("Local\\QronosR9Test-unit", api=api)
    try:
        assert owned.release() is False  # not confirmed: the holder is still inside ReleaseMutex
        assert owned.owned  # and so it is NOT reported as released
    finally:
        stuck.set()
