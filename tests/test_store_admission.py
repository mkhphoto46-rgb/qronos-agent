"""Store admission (core/store_admission.py) and the update launch's lifetime guard.

Safety: LockFileEx byte-range locks on a fixture file in tmp_path; synthetic "store" files in
tmp_path; base-interpreter child processes (tests/fixtures/child_python.py) owned, waited with
bounds and killed through their own handles; named mutexes only with unique test names. No
installer, runtime or real store is touched.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from core import store_admission, update_service
from core.local_update import UpdateError
from tests.fixtures.child_python import child_python
from tests.test_update_service import SECRET, Recorder, World, staged  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(os.name != "nt", reason="LockFileEx and named mutexes are Windows")

# A cooperating writer: admission first, only then the store; reports each step in a file.
WRITER = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core.local_update import UpdateError
from core.store_admission import admit_writer
data, log, go, stop = (Path(a) for a in sys.argv[2:6])
try:
    admission = admit_writer(data)
except UpdateError as error:
    log.write_text("refused:" + error.code)
    raise SystemExit(0)
log.write_text("admitted")
deadline = time.monotonic() + 60
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
with open(data / "store.json", "a", encoding="utf-8") as store:   # opened only after admission
    store.write("written\n")
while not stop.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
admission.release()
"""


class Writer:
    def __init__(self, tmp: Path, data: Path, name: str) -> None:
        self.log, self.go, self.stop = tmp / f"{name}.log", tmp / f"{name}.go", tmp / f"{name}.stop"
        executable, environment = child_python()
        self.process = subprocess.Popen(
            [executable, "-c", WRITER, str(ROOT), str(data), str(self.log), str(self.go), str(self.stop)],
            env=environment)

    def state(self, timeout: float = 30.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.log.exists() and self.log.read_text():
                return self.log.read_text()
            time.sleep(0.02)
        raise AssertionError("the writer child never reported")

    def finish(self) -> None:
        self.go.write_text("1")
        self.stop.write_text("1")
        try:
            self.process.wait(timeout=30)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=30)


def test_writers_share_admission_and_the_updater_cannot_take_it_while_one_is_inside(tmp_path):
    data = tmp_path / "data"
    first, second = store_admission.admit_writer(data), store_admission.admit_writer(data)
    try:
        with pytest.raises(UpdateError) as refused:
            store_admission.admit_exclusive(data)
        assert refused.value.code == "writers_active"
    finally:
        first.release()
        second.release()
    store_admission.admit_exclusive(data).release()


def test_the_check_and_the_open_are_one_step_across_processes(tmp_path):
    """No check-to-open race: a writer is either admitted (and the updater refused) or refused (and never
    opens its store), whatever the interleaving."""
    data = tmp_path / "data"
    data.mkdir()
    store = data / "store.json"

    inside = Writer(tmp_path, data, "inside")
    try:
        assert inside.state() == "admitted"
        with pytest.raises(UpdateError) as refused:
            store_admission.admit_exclusive(data)  # a writer is in (or about to open) its store
        assert refused.value.code == "writers_active"
    finally:
        inside.finish()
    assert store.read_text() == "written\n"

    exclusive = store_admission.admit_exclusive(data)
    late = Writer(tmp_path, data, "late")
    try:
        assert late.state() == "refused:update_in_progress"
    finally:
        late.finish()
        exclusive.release()
    assert store.read_text() == "written\n"  # the refused writer never opened the store


def test_the_admission_file_must_be_this_profile_s_own(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"")
    os.link(outside, data / store_admission.ADMISSION_FILE)
    with pytest.raises(UpdateError) as refused:
        store_admission.admit_writer(data)
    assert refused.value.code == "store"


def test_a_launch_hold_keeps_writers_out_until_the_installer_ends(tmp_path, world=None):
    """The launching process is the installer's lifetime guard: exclusion until the installer exits."""
    world = World(tmp_path)
    names = (f"Local\\QronosUpdateTest-guard-{uuid.uuid4().hex}",)
    installer_may_exit = tmp_path / "installer-may-exit"
    executable, _ = child_python()
    started: list = []

    def real_installer(path, arguments, handles):
        code = ("import os, sys, time\n"
                "deadline = time.monotonic() + 60\n"
                "while not os.path.exists(sys.argv[1]) and time.monotonic() < deadline:\n"
                "    time.sleep(0.05)\n")
        process = update_service.spawn_inheriting([executable, "-I", "-c", code, str(installer_may_exit)], handles)
        started.append(process)
        return process

    service = world.service(quiesce=lambda: update_service.LaunchHold(world.data, names), launcher=real_installer)
    _, approved = staged(service, *world.release("0.2.0"))
    announced = threading.Event()
    result: dict = {}

    def run():
        result["handoff"] = service.launch(approved["launchToken"], on_launched=lambda h: announced.set())

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert announced.wait(60), "the launch never announced the started installer"
        assert thread.is_alive()  # still guarding
        probe = Writer(tmp_path, world.data, "during-install")
        try:
            assert probe.state() == "refused:update_in_progress"
        finally:
            probe.finish()
        assert update_service.update_in_progress(names[0]) is True
        installer_may_exit.write_text("1")
        thread.join(60)
        assert not thread.is_alive() and result["handoff"]["version"] == "0.2.0"
    finally:
        installer_may_exit.write_text("1")
        for process in started:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)
        thread.join(30)
    after = Writer(tmp_path, world.data, "after-install")
    try:
        assert after.state() == "admitted"
    finally:
        after.finish()
    assert update_service.update_in_progress(names[0]) is False


def test_a_writer_inside_its_store_makes_the_launch_refuse(tmp_path):
    world = World(tmp_path)
    names = (f"Local\\QronosUpdateTest-busy-{uuid.uuid4().hex}",)
    service = world.service(quiesce=lambda: update_service.LaunchHold(world.data, names))
    _, approved = staged(service, *world.release("0.2.0"))
    inside = Writer(tmp_path, world.data, "inside")
    try:
        assert inside.state() == "admitted"
        with pytest.raises(UpdateError) as refused:
            service.launch(approved["launchToken"])
        assert refused.value.code == "writers_active"
    finally:
        inside.finish()
    assert world.launcher.calls == [] and service.status()["launchReady"] is not None
