from __future__ import annotations

from pathlib import Path

import pytest

import core.computer_executor as computer_executor
import core.windows_accessibility_mutation as accessibility_mutation


@pytest.fixture(autouse=True)
def isolate_qronos_storage_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy storage defaults must not read or write the real user profile."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
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

    # Anything that resolves storage locations — the artifact registry and
    # user memory do so even when handed explicit paths — would otherwise
    # read the user's own locator file and create every folder it names.
    import core.storage_locations as storage_locations

    monkeypatch.setattr(
        storage_locations,
        "_local_app_data",
        lambda: tmp_path / "LocalAppData",
    )
    monkeypatch.setattr(
        storage_locations,
        "_documents",
        lambda: tmp_path / "Documents",
    )


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
