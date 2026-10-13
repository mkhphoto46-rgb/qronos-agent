from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

import pytest

import core.audio_input as audio_input
import core.audio_output as audio_output
import core.computer_executor as computer_executor
import core.device_hub as device_hub
import core.print3d_native as print3d_native
import core.print_renderer as print_renderer
import core.print_workflow as print_workflow
import core.screen_capture as screen_capture
import core.window_binding as window_binding
import core.windows_accessibility as accessibility_reader
import core.windows_accessibility_mutation as accessibility_mutation
from tests.live_device import (
    LIVE_DEVICE_MARKER,
    SKIP_REASON,
    LiveDeviceBlocked,
    live_device_enabled,
)


# ------------------------------------------------------------ live devices
#
# Real device primitives. Every test that is not an opted-in live-device test
# gets a stand-in that raises before the device is touched. They are replaced
# wherever a loaded core module or test module bound them by name
# (runtime_bridge imports foreground_window directly), matched by identity.
#
# What this boundary does NOT promise: it covers the primitives listed here,
# subprocess.Popen and socket.socket.connect/connect_ex. Code that reaches a
# device another way (ctypes/WinRT calls not listed, os.system, asyncio's
# Windows proactor connects, a module bound before this fixture ran through a
# closure, or a new adapter) is not intercepted. The marker inventory and the
# direct unittest skips are the primary gate; this is a second layer.
LIVE_DEVICE_FUNCTIONS = (
    (screen_capture, "_grab_with_gdi", "capturing real screen or window pixels"),
    (screen_capture, "foreground_window", "reading the real foreground window"),
    (window_binding, "enumerate_windows", "enumerating real desktop windows"),
    (audio_input, "_get_sounddevice", "opening the real audio input stack (microphone)"),
    (audio_output, "_get_sounddevice", "opening the real audio output stack (speakers)"),
    (accessibility_reader, "_run_uia_script", "reading the real UI Automation tree"),
    (accessibility_reader, "focused_element", "reading the real focused UI element"),
    (print_workflow, "WinSpooler", "reading or driving the real print spooler"),
    (print_workflow, "_shell_execute_printto", "starting a real print handler"),
    (print_renderer, "printer_capabilities", "reading a real printer's capabilities"),
    (print_renderer, "job_devmode", "opening a real printer's settings"),
    (print_renderer, "_create_printer_dc", "opening a real printer device context"),
    (device_hub, "WinDeviceSpooler", "reading or driving the real print spooler and drivers (Device Hub)"),
    (print3d_native, "Win3DPrinting", "reading or driving a real 3D printer's driver"),
)
# Child processes that read the GPU or run local inference.
LIVE_DEVICE_EXECUTABLES = frozenset({
    "nvidia-smi", "whisper-cli", "whisper-server", "crispasr", "ollama", "llama-server",
})
# Programs that run another command line given to them.
_SHELLS = frozenset({"cmd", "powershell", "pwsh", "bash", "sh", "wsl", "start"})
# Real local inference endpoints a test may not reach:
#   11434 Ollama (core/ollama_controller.py, core/user_memory.py)
#   11435 BGE-M3 CPU embedding sidecar (core/memory_runtime.py MEMORY_SIDECAR_PORT)
#   8178  whisper-server default (core/whisper_server_runtime.py DEFAULT_PORT)
# Chatterbox picks its port at run time; its crispasr process is blocked above.
LIVE_MODEL_PORTS = frozenset({11434, 11435, 8178})
_TESTS_DIR = Path(__file__).resolve().parent
# Test modules that keep their own by-name binding of a primitive because the
# test forces the non-Windows branch before calling it (no OS call is made).
REAL_ALIAS_EXEMPT_NODES = frozenset({
    "tests/test_screen_capture.py::TestOffWindows::test_there_is_no_foreground_window_without_windows",
})
_TOOL_IN_COMMAND = re.compile(
    r"(?:^|[\s\"'\\/=&|;(])(" + "|".join(re.escape(name) for name in sorted(LIVE_DEVICE_EXECUTABLES))
    + r")(?:\.exe)?(?=$|[\s\"'&|;)])",
    re.IGNORECASE,
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if live_device_enabled():
        return
    skip = pytest.mark.skip(reason=SKIP_REASON)
    for item in items:
        if item.get_closest_marker(LIVE_DEVICE_MARKER) is not None:
            item.add_marker(skip)


def _blocked(what: str):
    def refuse(*args, **kwargs):
        raise LiveDeviceBlocked(
            f"A test attempted {what}. Live-device tests must be marked with "
            "tests.live_device.requires_live_device and run only with "
            "QRONOS_RUN_LIVE_DEVICE_TESTS=1; software tests must inject a fake."
        )

    refuse.live_device_stand_in = what
    return refuse


def _program_name(path) -> str:
    """Base name of one whole executable path, without splitting on spaces."""
    text = os.fsdecode(path) if isinstance(path, (bytes, os.PathLike)) else str(path)
    name = text.strip().strip("\"'").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def blocked_program(args, kwargs: dict | None = None) -> str | None:
    """The GPU/inference program this Popen call would start, if any.

    A sequence's first item is one path, spaces and all ("E:/Local AI/.../
    whisper-cli.exe"). A string command, a shell=True call, an explicit
    executable= and a shell launcher's arguments are all searched in full.
    """
    kwargs = kwargs or {}
    names: list[str] = []
    if kwargs.get("executable"):
        names.append(_program_name(kwargs["executable"]))
    if isinstance(args, (str, bytes)):
        command = os.fsdecode(args)
    elif isinstance(args, os.PathLike):
        command = None
        names.append(_program_name(args))
    else:
        items = [os.fsdecode(item) if isinstance(item, (bytes, os.PathLike)) else str(item) for item in args]
        command = None
        if items:
            names.append(_program_name(items[0]))
            if kwargs.get("shell") or _program_name(items[0]) in _SHELLS:
                command = subprocess.list2cmdline(items)
    for name in names:
        if name in LIVE_DEVICE_EXECUTABLES:
            return name
    if command is not None:
        match = _TOOL_IN_COMMAND.search(command)
        if match:
            return match.group(1).lower()
    return None


def _is_loopback_host(host) -> bool:
    text = str(host).strip("[]").lower()
    if text in {"localhost", ""}:
        return True
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def refused_model_endpoint(address) -> str | None:
    """'host:port' when the address is a real local inference endpoint."""
    if isinstance(address, tuple) and len(address) >= 2 and isinstance(address[1], int) \
            and address[1] in LIVE_MODEL_PORTS and _is_loopback_host(address[0]):
        return f"{address[0]}:{address[1]}"
    return None


def guarded_popen_class(delegate: type) -> type:
    """A Popen subclass that refuses GPU/inference programs before delegating."""

    class GuardedPopen(delegate):  # type: ignore[misc, valid-type]
        live_device_guard = True

        def __init__(self, args, *pargs, **kwargs):
            program = blocked_program(args, kwargs)
            if program is not None:
                raise LiveDeviceBlocked(
                    f"A test attempted to start {program} (GPU or local inference). "
                    "Mark it as a live-device test or inject a fake."
                )
            super().__init__(args, *pargs, **kwargs)

    return GuardedPopen


def guarded_connect(delegate):
    """A socket connect method that refuses real local inference endpoints before delegating."""

    def connect(self, address):
        endpoint = refused_model_endpoint(address)
        if endpoint is not None:
            raise LiveDeviceBlocked(
                f"A test attempted to reach a real local inference endpoint at {endpoint}. "
                "Mark it as a live-device test or use a fixture server on another port."
            )
        return delegate(self, address)

    connect.live_device_guard = True
    return connect


def _alias_modules(nodeid: str | None):
    for loaded_name, loaded in list(sys.modules.items()):
        if loaded is None:
            continue
        if loaded_name == "core" or loaded_name.startswith("core."):
            yield loaded
            continue
        file = getattr(loaded, "__file__", None)
        if file and nodeid not in REAL_ALIAS_EXEMPT_NODES:
            try:
                Path(file).resolve().relative_to(_TESTS_DIR)
            except (ValueError, OSError):
                continue
            yield loaded


def install_live_device_boundary(monkeypatch: pytest.MonkeyPatch, nodeid: str | None = None) -> None:
    """Replace every listed real device primitive with a stand-in that refuses."""
    for module, name, what in LIVE_DEVICE_FUNCTIONS:
        real = getattr(module, name)
        if getattr(real, "live_device_stand_in", None):
            continue
        stand_in = _blocked(what)
        for loaded in _alias_modules(nodeid):
            for attribute, value in list(vars(loaded).items()):
                if value is real:
                    monkeypatch.setattr(loaded, attribute, stand_in)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen_class(subprocess.Popen))
    monkeypatch.setattr(socket.socket, "connect", guarded_connect(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect(socket.socket.connect_ex))


@pytest.fixture(autouse=True)
def block_live_devices_unless_opted_in(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a marked live-device test, run with the explicit opt-in, reaches real devices."""
    if not live_devices_allowed(request.node):
        install_live_device_boundary(monkeypatch, request.node.nodeid)


def live_devices_allowed(node) -> bool:
    """Both the explicit opt-in and the live-device marker; neither alone is enough."""
    return live_device_enabled() and node.get_closest_marker(LIVE_DEVICE_MARKER) is not None


@pytest.fixture(autouse=True)
def isolate_qronos_storage_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy storage defaults must not read or write the real user profile."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("QRONOS_CONVERSATION_HISTORY_PATH", str(tmp_path / "history.sqlite3"))
    monkeypatch.setenv("QRONOS_SETTINGS_PATH", str(tmp_path / "settings.json"))


@pytest.fixture(autouse=True)
def isolate_known_local_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The start-up order resolves the known-folder ``<Local AppData>\\Qronos`` whenever a store
    override is set (core/startup_admission.installed_store_target), and every test sets
    overrides above. Point that known folder at a private folder of this test, so no test ever
    queries or resolves the real user's Local AppData (child processes do their own seam).
    """
    from core import startup_admission

    monkeypatch.setattr(startup_admission, "_known_local_appdata", lambda: tmp_path / "known-local-appdata")


@pytest.fixture(autouse=True)
def isolate_qronos_permission_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Prevent the test suite from ever reading or mutating the real user's
    persisted Qronos permission state.

    Every test receives its own isolated permission-policy and audit files.
    Tests that intentionally override these environment variables remain free
    to do so for the duration of that test.
    """

    monkeypatch.setenv(
        "QRONOS_PERMISSION_POLICY_PATH",
        str(tmp_path / "permissions_v2.json"),
    )

    monkeypatch.setenv(
        "QRONOS_PERMISSION_AUDIT_PATH",
        str(tmp_path / "action_audit.jsonl"),
    )

    monkeypatch.setenv(
        "QRONOS_APPROVAL_SECRET_PATH",
        str(tmp_path / "approval_secret.sqlite3"),
    )


@pytest.fixture(autouse=True)
def isolate_qronos_conversation_and_settings_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Keep test turns out of the real user's history, settings and queue.

    ``QronosRuntime()`` opens the canonical conversation history and settings
    at their default locations under the user's profile. Any test that runs a
    text turn, or queues one, records messages there. Every test gets its own
    throwaway files instead; tests that set these variables themselves still
    win for their own duration.
    """

    monkeypatch.setenv(
        "QRONOS_CONVERSATION_HISTORY_PATH",
        str(tmp_path / "conversation_history.sqlite3"),
    )

    monkeypatch.setenv(
        "QRONOS_SETTINGS_PATH",
        str(tmp_path / "settings.json"),
    )

    # The Smart Queue's durable record lives under the user's profile too.
    monkeypatch.setenv(
        "QRONOS_QUEUE_STATE_PATH",
        str(tmp_path / "queue_state.json"),
    )

    # Profile isolation is environment-based above, so child CLI processes
    # and their parent resolve the same storage roots. Patching just Python
    # helpers here would hide persisted approvals from those children.


@pytest.fixture(autouse=True)
def block_real_windows_computer_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Hard safety boundary for the automated test suite.

    Normal unit/integration tests must never launch, close, focus, minimize,
    maximize, or otherwise interact with applications installed on the real
    developer machine.

    Tests for computer-control behavior must inject fake launchers/openers/
    window actions explicitly.

    Without this boundary, Qronos dynamic application discovery can find a
    real installed application such as Adobe Premiere Pro and a test can
    accidentally launch it.
    """

    def blocked_side_effect(*args, **kwargs):
        raise AssertionError(
            "Pytest attempted a real Windows computer side effect. "
            "This test must inject a fake launcher/opener/window/GUI boundary."
        )

    # UIA mutations are native side effects too; reader/actuator tests must
    # replace this subprocess boundary explicitly before exercising success.
    monkeypatch.setattr(accessibility_mutation, "_run_uia_script", blocked_side_effect)

    # Static executable launch, such as Notepad.
    monkeypatch.setattr(
        computer_executor,
        "_default_launcher",
        blocked_side_effect,
    )

    # Dynamically discovered executable launch, such as Premiere/Photoshop.
    monkeypatch.setattr(
        computer_executor,
        "_launch_discovered_executable",
        blocked_side_effect,
    )

    # Dynamically discovered Start Menu shortcut launch.
    monkeypatch.setattr(
        computer_executor,
        "_launch_trusted_shortcut",
        blocked_side_effect,
    )

    # Packaged application activation, such as Microsoft Paint.
    monkeypatch.setattr(
        computer_executor,
        "activate_application",
        blocked_side_effect,
    )

    # Opening real Windows Explorer locations.
    monkeypatch.setattr(
        computer_executor,
        "_default_open_location",
        blocked_side_effect,
    )

    # Real close/focus/minimize/maximize window operations.
    monkeypatch.setattr(
        computer_executor,
        "_default_window_action",
        blocked_side_effect,
    )

    # Real mouse/keyboard/GUI automation.
    monkeypatch.setattr(
        computer_executor,
        "_default_gui_action",
        blocked_side_effect,
    )

    # The Elevated Action Broker's native adapters: the real UAC prompt
    # (ShellExecuteExW "runas") and the real Service Control Manager. Tests
    # inject fake Win32 calls; resolving the real ones is refused.
    from core import spooler_service, uac_launch

    monkeypatch.setattr(uac_launch, "_real_win32_calls", blocked_side_effect)
    monkeypatch.setattr(spooler_service, "_real_advapi32", blocked_side_effect)
    # The broker's installed profile is a real known folder, not an
    # environment value, so the isolated test profile cannot redirect it:
    # tests must inject where it is.
    from core import action_broker

    monkeypatch.setattr(action_broker, "_known_local_appdata", blocked_side_effect)
