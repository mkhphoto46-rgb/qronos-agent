"""
The storage-locator lock under real cross-process behaviour.

Every test uses a disposable profile under tmp_path and real child Python
processes that this test starts, tracks and - only if they overrun - kills.
All waits are bounded, so a broken lock fails a test instead of hanging it.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

from core import storage_locations as sl
from core.storage_locations import StorageLocationError, change_storage_location, read_storage_state


ROOT = Path(__file__).resolve().parents[1]
CHILD_LIMIT_SECONDS = 60.0

HOLDER = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core import storage_locations as sl
locator, signals, hold = Path(sys.argv[2]), Path(sys.argv[3]), float(sys.argv[4])
with sl._locator_lock(locator):
    (signals / "entered").write_text("1")
    end = time.monotonic() + hold
    while time.monotonic() < end and not (signals / "release").exists():
        time.sleep(0.01)
    (signals / "leaving").write_text("1")
(signals / "left").write_text("1")
"""

READER = r"""
import sys, time
from pathlib import Path
locator, signals, hold = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
with open(locator, "r", encoding="utf-8") as handle:   # a plain lock-free reader
    handle.read()
    (signals / "open").write_text("1")
    end = time.monotonic() + hold
    while time.monotonic() < end and not (signals / "release").exists():
        time.sleep(0.01)
(signals / "closed").write_text("1")
"""

CHANGER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from core.storage_locations import change_storage_location
change_storage_location(sys.argv[2], sys.argv[3])
"""


class Profile:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.local = tmp_path / "Local"
        self.custom = tmp_path / "Custom"
        self.cwd = tmp_path / "cwd"
        for folder in (self.local, tmp_path / "Roaming", tmp_path / "Home", self.custom, self.cwd):
            folder.mkdir()
        self.procs: list[subprocess.Popen] = []
        self.signal_count = 0

    @property
    def locator(self) -> Path:
        return self.local / "Qronos" / "storage_locations.json"

    @property
    def lock(self) -> Path:
        return sl.locator_lock_path(self.locator)

    def signals(self) -> Path:
        self.signal_count += 1
        folder = self.tmp / f"signals-{self.signal_count}"
        folder.mkdir()
        return folder

    def spawn(self, code: str, *args: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", code, *args],
            env={**os.environ}, cwd=self.cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.procs.append(proc)
        return proc

    def holder(self, hold: float = 30.0) -> Path:
        signals = self.signals()
        self.spawn(HOLDER, str(ROOT), str(self.locator), str(signals), str(hold))
        return signals

    def finish(self, proc: subprocess.Popen) -> None:
        out, err = proc.communicate(timeout=CHILD_LIMIT_SECONDS)
        assert proc.returncode == 0, err.decode("utf-8", "replace")

    def cleanup(self) -> None:
        for proc in self.procs:  # only processes this test started
            if proc.poll() is None:
                proc.kill()
            try:
                proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                pass


def wait_for(path: Path, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < end, f"timed out waiting for {path.name}"
        time.sleep(0.01)


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Profile:
    p = Profile(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(p.local))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "Home"))
    monkeypatch.setenv("HOME", str(tmp_path / "Home"))
    monkeypatch.delenv("QRONOS_PACKAGED_RUNTIME", raising=False)
    monkeypatch.chdir(p.cwd)
    p.locator.parent.mkdir(parents=True)
    try:
        yield p
    finally:
        p.cleanup()


def _stored(profile: Profile) -> dict[str, object]:
    return json.loads(profile.locator.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- concurrency


def test_repeated_concurrent_updates_preserve_every_key(profile: Profile) -> None:
    keys = ("historyRoot", "memoryRoot", "cacheRoot", "backupRoot")
    for round_ in range(3):
        procs = [
            profile.spawn(CHANGER, str(ROOT), key, str(profile.custom / f"{key}-{round_}"))
            for key in keys
        ]
        for proc in procs:
            profile.finish(proc)
        stored = _stored(profile)
        for key in keys:
            assert stored[key] == str(profile.custom / f"{key}-{round_}")
    assert [p.name for p in profile.locator.parent.glob("*.tmp")] == []


def test_a_slow_owner_is_never_treated_as_stale(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    signals = profile.holder()
    wait_for(signals / "entered")
    ancient = time.time() - 24 * 3600
    os.utime(profile.lock, (ancient, ancient))  # however old the file looks
    monkeypatch.setattr(sl, "LOCK_TIMEOUT_SECONDS", 0.5)

    with pytest.raises(StorageLocationError) as caught:
        change_storage_location("historyRoot", str(profile.custom / "intruder"))

    assert caught.value.code == "busy"
    assert not (signals / "leaving").exists(), "the owner was still inside: no concurrent entry"
    assert not profile.locator.exists() or "intruder" not in profile.locator.read_text(encoding="utf-8")

    (signals / "release").write_text("1")
    wait_for(signals / "left")
    change_storage_location("historyRoot", str(profile.custom / "after"))
    assert _stored(profile)["historyRoot"] == str(profile.custom / "after")


def test_an_owner_killed_inside_the_lock_releases_it(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    signals = profile.holder()
    wait_for(signals / "entered")
    victim = profile.procs[-1]
    victim.kill()  # abrupt exit of our own child, no clean-up code runs
    victim.wait(timeout=30)
    assert not (signals / "left").exists()
    monkeypatch.setattr(sl, "LOCK_TIMEOUT_SECONDS", 5.0)

    started = time.monotonic()
    change_storage_location("historyRoot", str(profile.custom / "recovered"))

    assert time.monotonic() - started < 5.0
    assert _stored(profile)["historyRoot"] == str(profile.custom / "recovered")


def test_contention_ends_in_a_bounded_truthful_busy(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    change_storage_location("historyRoot", str(profile.custom / "before"))
    before = profile.locator.read_bytes()
    signals = profile.holder()
    wait_for(signals / "entered")
    monkeypatch.setattr(sl, "LOCK_TIMEOUT_SECONDS", 0.4)

    started = time.monotonic()
    with pytest.raises(StorageLocationError) as caught:
        change_storage_location("historyRoot", str(profile.custom / "blocked"))
    elapsed = time.monotonic() - started

    assert caught.value.code == "busy" and "another Qronos process" in str(caught.value)
    assert 0.4 <= elapsed < 5.0
    assert profile.locator.read_bytes() == before
    (signals / "release").write_text("1")


def test_an_old_owner_never_releases_a_successors_lock(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    with sl._locator_lock(profile.locator):
        signals = profile.holder()  # starts waiting for the lock we hold
        time.sleep(0.3)
        assert not (signals / "entered").exists()
    original = os.stat(profile.lock)
    wait_for(signals / "entered")  # the successor now owns it

    monkeypatch.setattr(sl, "LOCK_TIMEOUT_SECONDS", 0.4)
    with pytest.raises(StorageLocationError) as caught:
        with sl._locator_lock(profile.locator):
            pass
    assert caught.value.code == "busy", "the successor's lock must still be exclusive"
    current = os.stat(profile.lock)
    assert (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino), "lock file never replaced"
    (signals / "release").write_text("1")
    wait_for(signals / "left")


# ---------------------------------------------------------------- release on errors


def test_an_exception_inside_the_lock_releases_it(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        with sl._locator_lock(profile.locator):
            raise ValueError("boom")

    def broken_write(path: Path, document: dict[str, object]) -> None:
        raise OSError(errno.EIO, "simulated write failure")

    monkeypatch.setattr(sl, "_write_locator", broken_write)
    with pytest.raises(OSError):
        change_storage_location("historyRoot", str(profile.custom / "never"))
    monkeypatch.undo()

    signals = profile.holder(hold=0.0)  # another process gets in at once
    wait_for(signals / "entered", timeout=5.0)
    wait_for(signals / "left")


# ---------------------------------------------------------------- errors are not contention


def test_a_lock_path_that_is_a_folder_is_refused_at_once(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    profile.lock.mkdir()
    monkeypatch.setattr(sl, "LOCK_TIMEOUT_SECONDS", 5.0)

    started = time.monotonic()
    with pytest.raises(StorageLocationError) as caught:
        change_storage_location("historyRoot", str(profile.custom / "x"))

    assert caught.value.code == "lock_invalid"
    assert time.monotonic() - started < 1.0
    assert profile.lock.is_dir()


def test_a_lock_path_that_is_a_link_is_refused(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    target = profile.tmp / "elsewhere"
    if os.name == "nt":
        import _winapi

        target.mkdir()
        _winapi.CreateJunction(str(target), str(profile.lock))
    else:
        target.write_text("not a lock", encoding="utf-8")
        os.symlink(target, profile.lock)
    monkeypatch.setattr(sl, "LOCK_TIMEOUT_SECONDS", 5.0)
    try:
        with pytest.raises(StorageLocationError) as caught:
            change_storage_location("historyRoot", str(profile.custom / "x"))
        assert caught.value.code == "lock_invalid"
    finally:
        if os.name == "nt":
            os.rmdir(profile.lock)
        else:
            profile.lock.unlink()
    if os.name != "nt":
        assert target.read_text(encoding="utf-8") == "not a lock"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes")
def test_permission_denied_is_reported_not_waited_out(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    profile.lock.write_bytes(b"")
    os.chmod(profile.lock, stat.S_IREAD)
    monkeypatch.setattr(sl, "LOCK_TIMEOUT_SECONDS", 5.0)
    try:
        started = time.monotonic()
        with pytest.raises(StorageLocationError) as caught:
            change_storage_location("historyRoot", str(profile.custom / "x"))
        assert caught.value.code == "lock_denied"
        assert time.monotonic() - started < 1.0
        assert isinstance(caught.value.__cause__, PermissionError)
    finally:
        os.chmod(profile.lock, stat.S_IREAD | stat.S_IWRITE)


def test_unexpected_lock_errors_are_reported_and_leak_nothing(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(fd: int) -> bool:
        raise OSError(errno.EIO, "simulated device error")

    monkeypatch.setattr(sl, "_try_lock", failing)
    monkeypatch.setattr(sl, "LOCK_TIMEOUT_SECONDS", 5.0)
    started = time.monotonic()
    with pytest.raises(StorageLocationError) as caught:
        change_storage_location("historyRoot", str(profile.custom / "x"))
    assert caught.value.code == "lock_unavailable"
    assert time.monotonic() - started < 1.0
    monkeypatch.undo()

    signals = profile.holder(hold=0.0)
    wait_for(signals / "entered", timeout=5.0)
    wait_for(signals / "left")


# ---------------------------------------------------------------- lock-free readers


def test_a_writer_waits_out_a_reader_holding_the_locator(profile: Profile) -> None:
    change_storage_location("historyRoot", str(profile.custom / "before"))
    signals = profile.signals()
    profile.spawn(READER, str(profile.locator), str(signals), "0.6")
    wait_for(signals / "open")

    change_storage_location("historyRoot", str(profile.custom / "after"))

    assert _stored(profile)["historyRoot"] == str(profile.custom / "after")
    assert [p.name for p in profile.locator.parent.glob("*.tmp")] == []


@pytest.mark.skipif(os.name != "nt", reason="POSIX lets a rename replace an open file")
def test_a_reader_that_never_lets_go_ends_in_a_clear_failure(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    change_storage_location("historyRoot", str(profile.custom / "before"))
    before = profile.locator.read_bytes()
    signals = profile.signals()
    profile.spawn(READER, str(profile.locator), str(signals), "30")
    wait_for(signals / "open")
    monkeypatch.setattr(sl, "REPLACE_TIMEOUT_SECONDS", 0.3)
    try:
        with pytest.raises(StorageLocationError) as caught:
            change_storage_location("historyRoot", str(profile.custom / "after"))
        assert caught.value.code == "locator_write_failed"
        assert profile.locator.read_bytes() == before
        assert [p.name for p in profile.locator.parent.glob("*.tmp")] == []
    finally:
        (signals / "release").write_text("1")
        wait_for(signals / "closed")


@pytest.mark.skipif(os.name != "nt", reason="the transient denial is a Windows replace behaviour")
def test_a_reader_does_not_mistake_a_replace_for_corruption(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    change_storage_location("historyRoot", str(profile.custom / "kept"))
    real_read = Path.read_text
    denials = {"left": 2}

    def briefly_denied(self: Path, *args: object, **kwargs: object) -> str:
        if self == profile.locator and denials["left"]:
            denials["left"] -= 1
            raise PermissionError(errno.EACCES, "in use by a writer")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", briefly_denied)
    state = read_storage_state()

    assert state.locator_status == "ok"
    assert state.locations.history_root == str(profile.custom / "kept")


def test_a_persistently_unreadable_locator_is_still_reported(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    change_storage_location("historyRoot", str(profile.custom / "kept"))
    real_read = Path.read_text

    def always_denied(self: Path, *args: object, **kwargs: object) -> str:
        if self == profile.locator:
            raise PermissionError(errno.EACCES, "access denied")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", always_denied)
    monkeypatch.setattr(sl, "READ_RETRY_SECONDS", 0.2)
    started = time.monotonic()
    state = read_storage_state()

    assert state.locator_status == "corrupt"
    assert time.monotonic() - started < 5.0
