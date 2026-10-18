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
import tests.test_screen_capture as screen_capture_tests
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
    # TelemetryCache's default reader: the real nvidia-smi, CPU and RAM.
    "tests/test_telemetry_cache.py::TestTheDefaultReaderIsCheap::test_a_real_burst_costs_one_read",
    "tests/test_telemetry_cache.py::TestTheDefaultReaderIsCheap::test_invalidating_does_not_pay_for_accuracy_again",
    "tests/test_telemetry_cache.py::TestTheDefaultReaderIsCheap::test_the_first_real_reading_is_structurally_valid",
    # The production supervisor entry with the real bridge and production
    # session mutex/Job names; its child gets no pytest primitive stand-ins.
    "tests/test_supervisor_relay.py::test_the_production_entry_supervises_the_real_bridge_end_to_end",
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
        tree = ast.parse(script.read_text(encoding="utf-8-sig"))
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


class _NeverStarted:
    """Fake Popen delegate. Records entry and does nothing, so a broken guard
    in these tests can never start a real program."""

    entered: list = []

    def __init__(self, args, *pargs, **kwargs):
        _NeverStarted.entered.append(args)


@pytest.fixture
def fake_popen():
    _NeverStarted.entered = []
    return conftest.guarded_popen_class(_NeverStarted)


BLOCKED_PROGRAMS = [
    # Sequences: the first item is one whole path, spaces included.
    (["E:/Local AI/whisper/bin/Release/whisper-cli.exe", "-m", "E:/Local AI/models/x.bin"], {}),
    ([r"E:\Local AI\whisper\bin\Release\whisper-server.exe", "--port", "8178"], {}),
    ([r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe", "--query-gpu=memory.used"], {}),
    (["C:/Program Files/Ollama/ollama.exe", "serve"], {}),
    ([r"D:\tools\crispasr.EXE", "--server"], {}),
    (["nvidia-smi"], {}),
    ([Path("E:/Local AI/llama/llama-server.exe")], {}),
    # String commands, quoted or not.
    ('"C:\\Program Files\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe" --query-gpu=name', {}),
    ("C:\\Program Files\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe --query-gpu=name", {}),
    ("ollama run anything", {}),
    # Through a shell, shell=True, or an explicit executable.
    (["cmd", "/c", "E:/Local AI/whisper/whisper-cli.exe", "-h"], {}),
    (["powershell.exe", "-Command", "& 'C:\\Program Files\\Ollama\\ollama.exe' serve"], {}),
    (["echo ready & whisper-cli.exe -h"], {"shell": True}),
    (["tool"], {"executable": r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"}),
]


@pytest.mark.parametrize("args, kwargs", BLOCKED_PROGRAMS)
def test_gpu_and_inference_programs_are_refused_and_the_delegate_never_entered(fake_popen, args, kwargs):
    with pytest.raises(LiveDeviceBlocked):
        fake_popen(args, **kwargs)
    assert _NeverStarted.entered == []


@pytest.mark.parametrize("args", [
    [sys.executable, "-c", "print('fixture')"],
    ["C:/Program Files/Python/python.exe", "-m", "pytest"],
    "git status --short",
    ["powershell.exe", "-NoProfile", "-Command", "[Windows.UI.Automation]"],
    [r"E:\Local AI\tools\not-nvidia-smi-helper.exe"],
    ["cmd", "/c", "echo", "whisper-client-notes.txt"],
])
def test_ordinary_programs_reach_the_delegate(fake_popen, args):
    fake_popen(args)
    assert _NeverStarted.entered == [args]


def test_ordinary_child_processes_still_run():
    result = subprocess.run([sys.executable, "-c", "print('fixture process')"], capture_output=True, text=True)
    assert result.stdout.strip() == "fixture process"


def test_the_boundary_is_installed_in_this_software_test():
    assert getattr(subprocess.Popen, "live_device_guard", False) is True
    assert getattr(socket.socket.connect, "live_device_guard", False) is True
    assert getattr(socket.socket.connect_ex, "live_device_guard", False) is True


@pytest.mark.parametrize("address", [
    ("127.0.0.1", 11434), ("localhost", 11434), ("::1", 11434), ("0.0.0.0", 11434),
    ("127.0.0.1", 11435), ("LOCALHOST", 11435), ("127.0.0.2", 11435),
    ("127.0.0.1", 8178), ("[::1]", 8178),
])
def test_real_local_inference_endpoints_are_refused_and_the_delegate_never_entered(address):
    entered = []
    connect = conftest.guarded_connect(lambda sock, addr: entered.append(addr))
    with pytest.raises(LiveDeviceBlocked):
        connect(None, address)
    assert entered == []


@pytest.mark.parametrize("address", [
    ("127.0.0.1", 0), ("127.0.0.1", 49152), ("127.0.0.1", 11436), ("192.0.2.1", 11434), "/tmp/socket",
])
def test_other_addresses_reach_the_connect_delegate(address):
    entered = []
    connect = conftest.guarded_connect(lambda sock, addr: entered.append(addr))
    connect(None, address)
    assert entered == [address]


def test_the_endpoint_ports_match_the_product_defaults():
    from core.memory_runtime import MEMORY_SIDECAR_PORT
    from core.ollama_controller import OllamaController
    from core.whisper_server_runtime import DEFAULT_PORT

    assert {MEMORY_SIDECAR_PORT, DEFAULT_PORT} <= conftest.LIVE_MODEL_PORTS
    assert OllamaController().base_url.rsplit(":", 1)[-1].rstrip("/") == "11434"
    assert 11434 in conftest.LIVE_MODEL_PORTS


def test_primitive_aliases_in_test_modules_are_replaced_too():
    # tests/test_screen_capture.py imports foreground_window by name; this
    # module imported it at collection, before the boundary was installed.
    assert getattr(screen_capture_tests.foreground_window, "live_device_stand_in", None) == (
        "reading the real foreground window"
    )


def test_the_only_alias_exemption_is_the_platform_forced_unit_test():
    assert conftest.REAL_ALIAS_EXEMPT_NODES == {
        "tests/test_screen_capture.py::TestOffWindows::test_there_is_no_foreground_window_without_windows",
    }
    import inspect
    from tests.test_screen_capture import TestOffWindows
    source = inspect.getsource(TestOffWindows.test_there_is_no_foreground_window_without_windows)
    assert source.index('screen_capture.sys.platform = "linux"') < source.index("foreground_window()")


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


_ASSET_ROOT_CHILD = textwrap.dedent(
    """
    import json
    import os
    from pathlib import Path

    from core.config import resolve_paths


    def test_reports_the_packaged_asset_root(tmp_path):
        # Metadata only: the path is resolved from the environment; nothing under it is opened.
        assets = resolve_paths(packaged=True).assets
        Path(os.environ["QRONOS_ASSET_ROOT_REPORT"]).write_text(
            json.dumps({"assets": str(assets), "tmp": str(tmp_path)}), encoding="utf-8")
    """
)


def test_software_profile_redirects_an_inherited_asset_root(tmp_path):
    # A user-level QRONOS_ASSET_ROOT (an installed asset folder) is inherited by every test process. The repo
    # conftest, loaded as a plugin into a child pytest, must point the packaged asset root into that test's own
    # profile; the inherited value here is a fake sentinel folder, never real assets.
    sentinel = tmp_path / "inherited-asset-root-sentinel"
    sentinel.mkdir()
    child = tmp_path / "child"
    child.mkdir()
    (child / "test_asset_root_child.py").write_text(_ASSET_ROOT_CHILD, encoding="utf-8")
    report = tmp_path / "report.json"
    environment = _environment(None)
    environment.update({"QRONOS_ASSET_ROOT": str(sentinel), "QRONOS_ASSET_ROOT_REPORT": str(report),
                        "QRONOS_OLLAMA_AUTO_START": "0"})
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(child / "test_asset_root_child.py"), "-q", "-p", "no:cacheprovider",
         "-p", "tests.conftest", f"--rootdir={child}", f"--basetemp={tmp_path / 'pytest-child'}"],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=25,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    seen = json.loads(report.read_text(encoding="utf-8"))
    assets, child_tmp = Path(seen["assets"]), Path(seen["tmp"]).resolve()
    assert assets != sentinel.resolve() and sentinel.resolve() not in assets.parents
    assert child_tmp in assets.parents
    assert list(sentinel.iterdir()) == []
