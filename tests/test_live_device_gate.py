"""
The default-off live-device gate itself.

Nothing here touches a device. Device primitives are exercised only through
their stand-ins; blocked processes and connections are refused before they are
created; the marked acceptance tests are inspected by collection and import,
never run.
"""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import textwrap
import unittest

import pytest

import core.runtime_bridge as runtime_bridge
from tests import conftest
from tests.live_device import (
    LIVE_DEVICE_ENV,
    LIVE_DEVICE_MARKER,
    LiveDeviceBlocked,
    live_device_enabled,
    requires_live_device,
)

ROOT = Path(__file__).resolve().parent.parent

# Every live-device acceptance node in the suite. Adding one means marking it
# with requires_live_device and listing it here; removing a marker fails.
EXPECTED_LIVE_NODES = frozenset({
    # Runs the real nvidia-smi.
    "tests/test_resource_guard.py::TestResourceGuard::test_gpu_status",
}) | frozenset(
    f"tests/test_screen_capture.py::TestTheRealScreen::{name}"
    for name in (
        "test_a_real_capture_is_not_a_blank_rectangle",
        "test_a_real_capture_matches_the_logical_resolution",
        "test_a_real_window_capture_is_not_a_black_rectangle",
        "test_a_window_capture_is_smaller_than_the_whole_screen",
        "test_capturing_repeatedly_does_not_leak_handles",
        "test_the_display_reports_a_plausible_size",
        "test_there_is_a_window_in_front_of_something",
    )
)


def _environment(opt_in: str | None) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key != LIVE_DEVICE_ENV}
    if opt_in is not None:
        environment[LIVE_DEVICE_ENV] = opt_in
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


# A collection-only pytest run that reports each item and whether the gate
# added a skip marker. --collect-only never runs a test body.
_COLLECT = textwrap.dedent(
    """
    import json, sys
    import pytest

    class Report:
        def __init__(self):
            self.items = {}
        def pytest_collection_finish(self, session):
            for item in session.items:
                self.items[item.nodeid] = {
                    "live": item.get_closest_marker("live_device") is not None,
                    "skipped": item.get_closest_marker("skip") is not None,
                }

    report = Report()
    code = pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", *sys.argv[1:]], plugins=[report])
    print("REPORT" + json.dumps({"code": int(code), "items": report.items}))
    """
)


def _collect(opt_in: str | None, *targets: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _COLLECT, *targets],
        cwd=ROOT, env=_environment(opt_in), capture_output=True, text=True, timeout=180,
    )
    line = next((l for l in result.stdout.splitlines() if l.startswith("REPORT")), None)
    assert line is not None, result.stdout[-2000:] + result.stderr[-2000:]
    report = json.loads(line[len("REPORT"):])
    assert report["code"] == 0, result.stdout[-2000:]
    return report["items"]


# ------------------------------------------------------------- the selector


@pytest.mark.parametrize("value, enabled", [
    (None, False), ("", False), ("0", False), ("true", False), ("yes", False),
    (" 1", False), ("1 ", False), ("01", False), ("1", True),
])
def test_only_the_exact_value_one_opts_in(value, enabled):
    environ = {} if value is None else {LIVE_DEVICE_ENV: value}
    assert live_device_enabled(environ) is enabled


def test_the_marker_is_registered_with_pytest():
    parser = configparser.ConfigParser()
    parser.read(ROOT / "pytest.ini", encoding="utf-8")
    markers = parser["pytest"]["markers"]
    assert any(line.strip().startswith(f"{LIVE_DEVICE_MARKER}:") for line in markers.splitlines())


def test_the_decorator_marks_and_skips_for_unittest_by_default(monkeypatch):
    monkeypatch.delenv(LIVE_DEVICE_ENV, raising=False)

    @requires_live_device
    class Acceptance(unittest.TestCase):
        def test_device(self):  # pragma: no cover - must never run
            raise AssertionError("ran without opt-in")

    assert Acceptance.__unittest_skip__ is True
    assert any(mark.name == LIVE_DEVICE_MARKER for mark in Acceptance.pytestmark)
    result = unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(Acceptance).run(result)
    assert (result.testsRun, len(result.skipped), result.failures, result.errors) == (1, 1, [], [])


def test_the_decorator_leaves_an_opted_in_test_runnable(monkeypatch):
    monkeypatch.setenv(LIVE_DEVICE_ENV, "1")

    @requires_live_device
    def acceptance():
        return "would run"

    assert not getattr(acceptance, "__unittest_skip__", False)
    assert any(mark.name == LIVE_DEVICE_MARKER for mark in acceptance.pytestmark)


# ------------------------------------------- the marked acceptance nodes


@pytest.mark.parametrize("opt_in", [None, "0"])
def test_by_default_every_live_node_is_collected_and_skipped(opt_in):
    items = _collect(opt_in, "tests")
    live = {node for node, info in items.items() if info["live"]}
    assert live == EXPECTED_LIVE_NODES
    assert all(items[node]["skipped"] for node in live)
    # The software tests in the same file stay enabled.
    software = [node for node, info in items.items() if not info["live"]]
    assert software and not any(items[node]["skipped"] for node in software)


def test_with_the_opt_in_the_live_nodes_are_not_skipped():
    items = _collect("1", "tests")
    live = {node for node, info in items.items() if info["live"]}
    assert live == EXPECTED_LIVE_NODES
    assert not any(items[node]["skipped"] for node in live)


def test_direct_unittest_import_is_gated_too():
    probe = (
        "import tests.test_screen_capture as m;"
        "print(bool(getattr(m.TestTheRealScreen, '__unittest_skip__', False)))"
    )
    for opt_in, expected in ((None, "True"), ("1", "False")):
        result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=_environment(opt_in),
                                capture_output=True, text=True, timeout=120)
        assert result.stdout.strip().splitlines()[-1] == expected, result.stderr[-1000:]


def test_live_diagnostic_scripts_are_never_collected():
    parser = configparser.ConfigParser()
    parser.read(ROOT / "pytest.ini", encoding="utf-8")
    assert parser["pytest"]["testpaths"].split() == ["tests"]
    import ast
    for script in sorted((ROOT / "tools").rglob("*.py")):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
        assert not [n for n in names if n.startswith(("test", "Test"))], script.name


# ------------------------------------------------------------- the tripwire


def test_every_device_primitive_is_a_refusing_stand_in_in_a_software_test():
    for module, name, what in conftest.LIVE_DEVICE_FUNCTIONS:
        stand_in = getattr(module, name)
        assert getattr(stand_in, "live_device_stand_in", None) == what, f"{module.__name__}.{name}"
        with pytest.raises(LiveDeviceBlocked):
            stand_in()


def test_names_bound_elsewhere_in_core_are_replaced_too():
    # runtime_bridge imports foreground_window by name.
    assert getattr(runtime_bridge.foreground_window, "live_device_stand_in", None) == "reading the real foreground window"
    with pytest.raises(LiveDeviceBlocked):
        runtime_bridge.foreground_window()


@pytest.mark.parametrize("args", [
    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv"],
    [r"C:\runtime\whisper\bin\Release\whisper-cli.exe", "-m", "model.bin"],
    "ollama run anything",
    [r"D:\tools\crispasr.EXE", "--server"],
])
def test_gpu_and_inference_processes_are_refused_before_they_start(args):
    with pytest.raises(LiveDeviceBlocked):
        subprocess.Popen(args)
    with pytest.raises(LiveDeviceBlocked):
        subprocess.run(args)


def test_ordinary_child_processes_still_run():
    result = subprocess.run([sys.executable, "-c", "print('fixture process')"], capture_output=True, text=True)
    assert result.stdout.strip() == "fixture process"


def test_the_local_model_port_is_refused_before_connecting():
    for host in ("127.0.0.1", "localhost"):
        with socket.socket() as probe:
            with pytest.raises(LiveDeviceBlocked):
                probe.connect((host, 11434))
        with socket.socket() as probe:
            with pytest.raises(LiveDeviceBlocked):
                probe.connect_ex((host, 11434))


def test_loopback_fixture_servers_still_work():
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            peer, _ = server.accept()
            with peer:
                client.sendall(b"ok")
                assert peer.recv(2) == b"ok"


def test_only_an_opted_in_marked_test_is_allowed_real_devices(monkeypatch):
    class Node:
        def __init__(self, marked):
            self.marked = marked

        def get_closest_marker(self, name):
            return object() if (self.marked and name == LIVE_DEVICE_MARKER) else None

    monkeypatch.delenv(LIVE_DEVICE_ENV, raising=False)
    assert not conftest.live_devices_allowed(Node(True))
    assert not conftest.live_devices_allowed(Node(False))
    monkeypatch.setenv(LIVE_DEVICE_ENV, "1")
    assert conftest.live_devices_allowed(Node(True))
    assert not conftest.live_devices_allowed(Node(False))
    monkeypatch.setenv(LIVE_DEVICE_ENV, "true")
    assert not conftest.live_devices_allowed(Node(True))
