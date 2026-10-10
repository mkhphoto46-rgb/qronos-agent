"""
R1/R2/N1: singletons are OWNED (wait-acquired on a holder thread), and the update
fence is checked AFTER every acquisition (core/instance_ownership.py and its users).

Native boundary of these fixtures (audited):
- real CreateMutexW / WaitForSingleObject / ReleaseMutex / CloseHandle, only on PRIVATE
  names "Local\\QronosR9Test-<uuid>-<tag>"; a private "fence" is just such a mutex kept
  open by the test (core.store_admission.update_in_progress opens it read-only);
- a competing owner is tests/fixtures/mutex_owner_child.py, a base-interpreter child
  (tests/fixtures/child_python.py, with -B) that owns the private mutex and either waits
  for a tmp go-file (60 s at most) or ends while owning it; it is waited with bounds and
  killed only through its own Popen handle;
- the Supervisor test uses a fake launcher/child and a real private ChannelServer pipe.
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
    UpdateInProgress,
    acquire_unless_updating,
)
from core.store_admission import update_in_progress
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
    """A handle to a private mutex that this process keeps open WITHOUT owning it (or a held fence)."""

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


def no_fence() -> bool:
    return False


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


def capture_acquisitions(monkeypatch) -> list:
    """Records every OwnedMutex that acquire_unless_updating obtains (to see abandonment it then refused)."""
    seen: list = []
    real = OwnedMutex.acquire.__func__

    def recording(cls, name, **options):
        owned = real(cls, name, **options)
        seen.append(owned)
        return owned

    monkeypatch.setattr(OwnedMutex, "acquire", classmethod(recording))
    return seen


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


# ------------------------------------------------------------ fence after acquisition


@windows
def test_a_held_fence_refuses_after_acquisition_and_releases_what_was_acquired():
    name, fence_name = private("guard"), private("fence")
    fence = ExistingHandle(fence_name)
    try:
        with pytest.raises(UpdateInProgress):
            acquire_unless_updating(name, fence_exists=lambda: update_in_progress(fence_name))
        again = OwnedMutex.acquire(name)  # it was given back
        again.release()
    finally:
        fence.close()
    owned = acquire_unless_updating(name, fence_exists=lambda: update_in_progress(fence_name))
    owned.release()


@windows
def test_an_abandoned_acquisition_with_the_fence_held_is_still_refused(owners, monkeypatch):
    name, fence_name = private("abandoned-guard"), private("fence")
    keeper = ExistingHandle(name)
    fence = ExistingHandle(fence_name)
    seen = capture_acquisitions(monkeypatch)
    try:
        owners(name, "abandon").wait_exit()
        with pytest.raises(UpdateInProgress):
            acquire_unless_updating(name, fence_exists=lambda: update_in_progress(fence_name))
        assert len(seen) == 1 and seen[0].abandoned and not seen[0].owned  # abandoned, refused, given back
        again = OwnedMutex.acquire(name)
        assert not again.abandoned  # the refused acquisition was released, not abandoned again
        again.release()
    finally:
        fence.close()
        keeper.close()


@windows
def test_a_fence_that_appears_at_the_acquisition_seam_refuses():
    name = private("seam")
    checks = []

    def fence_appears() -> bool:  # absent before acquiring would not matter: it is asked only after
        checks.append(True)
        return True

    with pytest.raises(UpdateInProgress):
        acquire_unless_updating(name, fence_exists=fence_appears)
    assert checks == [True]
    OwnedMutex.acquire(name).release()


@windows
def test_entry_retry_waits_for_an_update_to_end_but_not_for_a_live_owner(owners):
    name, fence_name = private("retry"), private("fence")
    fence = ExistingHandle(fence_name)
    threading.Timer(0.6, fence.close).start()  # the installer ends shortly
    started = time.monotonic()
    owned = acquire_unless_updating(name, retry_seconds=10, step_seconds=0.1,
                                    fence_exists=lambda: update_in_progress(fence_name))
    assert 0.5 <= time.monotonic() - started < 5
    owned.release()

    busy = private("busy-no-update")
    owner = owners(busy)
    started = time.monotonic()
    with pytest.raises(AlreadyOwned):
        acquire_unless_updating(busy, retry_seconds=10, fence_exists=no_fence)
    assert time.monotonic() - started < 2  # no update: refused at once
    owner.let_go()


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
def test_the_sidecar_guard_owns_refuses_a_live_owner_and_refuses_an_abandoned_slot_during_an_update(owners,
                                                                                                    monkeypatch):
    from core.windows_sidecar_guard import SidecarAlreadyRunning, SidecarUpdateInProgress, WindowsSidecarGuard

    name, fence_name = private("sidecar"), private("fence")
    owner = owners(name)
    with pytest.raises(SidecarAlreadyRunning):
        WindowsSidecarGuard.acquire(name, fence_exists=no_fence)
    owner.let_go()

    keeper = ExistingHandle(name)  # so the dead owner's mutex is really ABANDONED, not recreated
    seen = capture_acquisitions(monkeypatch)
    owners(name, "abandon").wait_exit()
    fence = ExistingHandle(fence_name)
    try:
        with pytest.raises(SidecarUpdateInProgress):
            WindowsSidecarGuard.acquire(name, fence_exists=lambda: update_in_progress(fence_name))
        assert seen and seen[-1].abandoned
    finally:
        fence.close()
        keeper.close()
    monkeypatch.undo()
    guard = WindowsSidecarGuard.acquire(name, fence_exists=lambda: update_in_progress(fence_name))
    assert not guard.abandoned  # the refused acquisition gave it back cleanly
    guard.close()
    guard.close()
    owners(name).let_go()  # released for real


@windows
def test_the_supervisor_singleton_is_owned_and_a_second_supervisor_is_refused(owners):
    from core.supervisor import SupervisorAlreadyRunning, SupervisorInstanceGuard

    name, fence_name = private("supervisor"), private("fence")
    first = SupervisorInstanceGuard.acquire(name=name, fence_exists=no_fence, retry_seconds=0)
    assert first.owned
    with pytest.raises(SupervisorAlreadyRunning):
        SupervisorInstanceGuard.acquire(name=name, fence_exists=no_fence, retry_seconds=0)
    first.release()
    fence = ExistingHandle(fence_name)
    try:
        with pytest.raises(UpdateInProgress):
            SupervisorInstanceGuard.acquire(name=name, fence_exists=lambda: update_in_progress(fence_name),
                                            retry_seconds=0)
    finally:
        fence.close()
    owners(name).let_go()  # nothing of ours still owns it


@windows
def test_the_bridge_singleton_is_owned_for_life_and_refused_while_an_update_runs():
    from core.runtime_instance_guard import RuntimeAlreadyRunning, RuntimeInstanceGuard

    name, fence_name = private("bridge"), private("fence")
    bridge = RuntimeInstanceGuard.acquire(name=name, fence_exists=no_fence)
    assert bridge.owned
    with pytest.raises(RuntimeAlreadyRunning):
        RuntimeInstanceGuard.acquire(name=name, fence_exists=no_fence)
    bridge.close()
    fence = ExistingHandle(fence_name)
    try:
        with pytest.raises(UpdateInProgress):
            RuntimeInstanceGuard.acquire(name=name, fence_exists=lambda: update_in_progress(fence_name))
    finally:
        fence.close()


@windows
def test_the_updater_quiescence_is_refused_while_an_owned_singleton_lives():
    """The updater's create-then-wait hold (imported core.update_service.MutexHold) cannot take an owned name."""
    from core.local_update import UpdateError
    from core.update_service import MutexHold

    name = private("runtime")
    owned = OwnedMutex.acquire(name)
    try:
        with pytest.raises(UpdateError) as refused:
            MutexHold((private("fence"), name))
        assert refused.value.code == "runtime_running"
    finally:
        owned.release()
    MutexHold((private("fence"), name)).release()  # free once the owner let go


# ------------------------------------------- unknown fence, unconfirmed release, real race


@windows
def test_a_fence_check_that_fails_releases_the_singleton_and_refuses(owners):
    name = private("fence-error")

    def broken_fence() -> bool:
        raise OSError("fixture: Windows did not answer")

    with pytest.raises(OwnershipUnavailable):
        acquire_unless_updating(name, fence_exists=broken_fence)
    owners(name).let_go()  # a real next owner gets it: nothing of ours kept it


@pytest.mark.parametrize("release_ok,close_ok", [(True, True), (False, True), (True, False)])
def test_an_unconfirmed_native_release_is_never_reported_as_clean(release_ok, close_ok):
    api = MutexApi(create=lambda name: 9, wait=lambda handle, ms: 0, release=lambda handle: release_ok,
                   close=lambda handle: close_ok, last_error=lambda: 0)
    owned = OwnedMutex.acquire("Local\\QronosR9Test-unit", api=api)
    assert owned.release() is (release_ok and close_ok)
    assert owned.release_confirmed is (release_ok and close_ok) and not owned.owned


@windows
def test_the_real_updater_order_fence_first_then_singletons_never_lets_both_proceed():
    """core.update_service.MutexHold creates the fence FIRST, then waits for each runtime singleton."""
    singleton, fence_name = private("singleton"), private("fence")
    # Interleaving 1: the updater has created (owns) the fence but not yet taken the singleton.
    updater_fence = OwnedMutex.acquire(fence_name)
    with pytest.raises(UpdateInProgress):
        acquire_unless_updating(singleton, fence_exists=lambda: update_in_progress(fence_name))
    updater_singleton = OwnedMutex.acquire(singleton)  # the updater's next step succeeds: it was released
    updater_singleton.release()
    updater_fence.release()
    # Interleaving 2: the runtime checked before the fence existed and owns its singleton; the updater's
    # later wait on that name fails, so the updater gives up, fence included.
    runtime = acquire_unless_updating(singleton, fence_exists=lambda: update_in_progress(fence_name))
    updater_fence = OwnedMutex.acquire(fence_name)
    with pytest.raises(AlreadyOwned):
        OwnedMutex.acquire(singleton)
    updater_fence.release()
    runtime.release()


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
