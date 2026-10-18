"""Store admission (core/store_admission.py): shared for writers, exclusive for a sole owner.

Safety: LockFileEx byte-range locks on a fixture file in tmp_path; synthetic "store" files in
tmp_path; base-interpreter child processes (tests/fixtures/child_python.py) owned, waited with
bounds and killed through their own handles. No installer, runtime or real store is touched.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from core import store_admission
from core.store_admission import AdmissionError
from tests.fixtures.child_python import child_python

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows")

# A cooperating writer: admission first, only then the store; reports each step in a file.
WRITER = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core.store_admission import AdmissionError, admit_writer
data, log, go, stop = (Path(a) for a in sys.argv[2:6])
try:
    admission = admit_writer(data)
except AdmissionError as error:
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


def test_writers_share_admission_and_no_exclusive_admission_is_granted_while_one_is_inside(tmp_path):
    data = tmp_path / "data"
    first, second = store_admission.admit_writer(data), store_admission.admit_writer(data)
    try:
        with pytest.raises(AdmissionError) as refused:
            store_admission.admit_exclusive(data)
        assert refused.value.code == "writers_active"
    finally:
        first.release()
        second.release()
    store_admission.admit_exclusive(data).release()


def test_the_check_and_the_open_are_one_step_across_processes(tmp_path):
    """No check-to-open race: a writer is either admitted (and the exclusive admission refused) or refused (and
    never opens its store), whatever the interleaving."""
    data = tmp_path / "data"
    data.mkdir()
    store = data / "store.json"

    inside = Writer(tmp_path, data, "inside")
    try:
        assert inside.state() == "admitted"
        with pytest.raises(AdmissionError) as refused:
            store_admission.admit_exclusive(data)  # a writer is in (or about to open) its store
        assert refused.value.code == "writers_active"
    finally:
        inside.finish()
    assert store.read_text() == "written\n"

    exclusive = store_admission.admit_exclusive(data)
    late = Writer(tmp_path, data, "late")
    try:
        assert late.state() == "refused:busy"
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
    with pytest.raises(AdmissionError) as refused:
        store_admission.admit_writer(data)
    assert refused.value.code == "store"


def test_a_linked_data_root_is_refused(tmp_path):
    from tests.fixtures.owned_junction import owned_junction

    target = tmp_path / "target"
    target.mkdir()
    with owned_junction(target, tmp_path / "alias") as link:
        with pytest.raises(AdmissionError) as refused:
            store_admission.admit_writer(link / "data")
    assert refused.value.code == "store"
    assert not (target / "data").exists()
