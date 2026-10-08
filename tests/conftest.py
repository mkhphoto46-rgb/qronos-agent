from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

import core.audio_input as audio_input
import core.audio_output as audio_output
import core.computer_executor as computer_executor
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
# wherever a loaded core module bound them by name (runtime_bridge imports
# foreground_window directly), matched by identity.
LIVE_DEVICE_FUNCTIONS = (
    (screen_capture, "_grab_with_gdi", "capturing real screen or window pixels"),
    (screen_capture, "foreground_window", "reading the real foreground window"),
    (window_binding, "enumerate_windows", "enumerating real desktop windows"),
    (audio_input, "_get_sounddevice", "opening the real audio input stack (microphone)"),
    (audio_output, "_get_sounddevice", "opening the real audio output stack (speakers)"),
    (accessibility_reader, "_run_uia_script", "reading the real UI Automation tree"),
    (accessibility_reader, "focused_element", "reading the real focused UI element"),
)
# Child processes that read the GPU or run local inference.
LIVE_DEVICE_EXECUTABLES = frozenset({
    "nvidia-smi", "whisper-cli", "whisper-server", "crispasr", "ollama", "llama-server",
})
# The local model server's default port: a test may not reach a real model.
LIVE_MODEL_PORTS = frozenset({11434})
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


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


def _executable_name(args) -> str:
    first = args if isinstance(args, (str, bytes, os.PathLike)) else (next(iter(args), "") if args else "")
    text = os.fsdecode(first) if isinstance(first, (bytes, os.PathLike)) else str(first)
    token = text.strip().split()[0] if text.strip() else ""
    name = token.strip('"').replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def install_live_device_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every real device primitive with a stand-in that refuses."""
    for module, name, what in LIVE_DEVICE_FUNCTIONS:
        real = getattr(module, name)
        stand_in = _blocked(what)
        for loaded_name, loaded in list(sys.modules.items()):
            if loaded is None or not (loaded_name == "core" or loaded_name.startswith("core.")):
                continue
            for attribute, value in list(vars(loaded).items()):
                if value is real:
                    monkeypatch.setattr(loaded, attribute, stand_in)

    original_popen = subprocess.Popen

    class GuardedPopen(original_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *pargs, **kwargs):
            if _executable_name(args) in LIVE_DEVICE_EXECUTABLES:
                raise LiveDeviceBlocked(
                    f"A test attempted to start {_executable_name(args)} (GPU or local inference). "
                    "Mark it as a live-device test or inject a fake."
                )
            super().__init__(args, *pargs, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def _refuse_model_port(address) -> None:
        if isinstance(address, tuple) and len(address) >= 2 and address[1] in LIVE_MODEL_PORTS \
                and str(address[0]).lower() in _LOOPBACK_NAMES:
            raise LiveDeviceBlocked(
                f"A test attempted to reach the real local model server at {address[0]}:{address[1]}. "
                "Mark it as a live-device test or use a fixture server on another port."
            )

    def connect(self, address):
        _refuse_model_port(address)
        return original_connect(self, address)

    def connect_ex(self, address):
        _refuse_model_port(address)
        return original_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture(autouse=True)
def block_live_devices_unless_opted_in(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a marked live-device test, run with the explicit opt-in, reaches real devices."""
    if not live_devices_allowed(request.node):
        install_live_device_boundary(monkeypatch)


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
