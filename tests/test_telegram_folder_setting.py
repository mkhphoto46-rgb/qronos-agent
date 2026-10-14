"""Telegram's download folder: a validated user setting, checked before the first approval (Main64 G1).

Fixture folders and an in-memory or private settings file only. The drive type is a fake (no GetDriveTypeW call),
a link is reported by a fake (no junction is created), Telegram is never opened and nothing is downloaded.
"""
from __future__ import annotations

import json
import time

import pytest

from core.action_audit import ActionAuditLog
from core.runtime_bridge import QronosRuntime, _telegram_download_folder
from core.settings_service import (
    DEFAULT_DEFINITIONS,
    TELEGRAM_DOWNLOAD_FOLDER,
    InvalidSettingValue,
    SettingsService,
)
from core.task_router import TaskRouter
from test_file_control_text_bridge import _FakeHistory

REQUEST = "آخرین فایل «Sara Ahmadi» را در تلگرام دانلود کن"


@pytest.fixture(autouse=True)
def local_drives(monkeypatch):
    monkeypatch.setattr("core.path_safety.windows_drive_type", lambda anchor: 3)  # DRIVE_FIXED, a fake
    monkeypatch.delenv("QRONOS_TELEGRAM_DOWNLOAD_FOLDER", raising=False)


# ------------------------------------------------------------------ the setting

def test_the_setting_is_empty_by_default_and_refuses_values_that_cannot_name_a_local_folder(tmp_path):
    settings = SettingsService(DEFAULT_DEFINITIONS, path=None)
    assert settings.get(TELEGRAM_DOWNLOAD_FOLDER) == ""  # never guessed
    assert settings.set(TELEGRAM_DOWNLOAD_FOLDER, str(tmp_path)) == str(tmp_path)
    for bad in ("relative\\downloads", "\\\\server\\share\\dl", "\\\\?\\C:\\dl", "C:\\a\x07b", "C:\\dl\\..\\x",
                "C:" + "\\x" * 600, 5, None):
        with pytest.raises(InvalidSettingValue):
            settings.set(TELEGRAM_DOWNLOAD_FOLDER, bad)
    assert settings.get(TELEGRAM_DOWNLOAD_FOLDER) == str(tmp_path)  # a refused value changes nothing
    assert settings.set(TELEGRAM_DOWNLOAD_FOLDER, "") == ""  # clearing is allowed


def test_a_stored_folder_survives_reload_even_while_it_is_missing_but_a_malformed_one_does_not(tmp_path):
    """Load judges the FORM only: an unplugged drive must not erase the person's setting."""
    path = tmp_path / "settings.json"
    writer = SettingsService(DEFAULT_DEFINITIONS, path=path)
    missing = str(tmp_path / "not-yet-created")
    writer.set(TELEGRAM_DOWNLOAD_FOLDER, missing)
    assert SettingsService(DEFAULT_DEFINITIONS, path=path).get(TELEGRAM_DOWNLOAD_FOLDER) == missing
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["payload"][TELEGRAM_DOWNLOAD_FOLDER] = "relative\\x"  # JsonStore keeps values under "payload"
    path.write_text(json.dumps(payload), encoding="utf-8")
    problems = []
    reloaded = SettingsService(DEFAULT_DEFINITIONS, path=path, on_problem=problems.append)
    assert reloaded.get(TELEGRAM_DOWNLOAD_FOLDER) == "" and problems


# ------------------------------------------------------------------ the check at use time

def test_the_folder_is_checked_now_by_the_library_storage_rules(tmp_path, monkeypatch):
    folder = tmp_path / "Telegram"
    folder.mkdir()
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    assert _telegram_download_folder(str(folder)) == (folder, "ready")
    assert _telegram_download_folder("") == (None, "not_configured")
    assert _telegram_download_folder(str(tmp_path / "gone")) == (None, "missing")
    assert _telegram_download_folder(str(a_file)) == (None, "not_directory")
    assert _telegram_download_folder("relative") == (None, "relative")
    monkeypatch.setattr("core.path_safety.windows_drive_type", lambda anchor: 4)  # DRIVE_REMOTE (fake)
    assert _telegram_download_folder(str(folder)) == (None, "network_drive")
    monkeypatch.setattr("core.path_safety.windows_drive_type", lambda anchor: 0)  # DRIVE_UNKNOWN (fake)
    assert _telegram_download_folder(str(folder)) == (None, "drive_unknown")
    monkeypatch.setattr("core.path_safety.windows_drive_type", lambda anchor: 3)
    import core.path_safety as path_safety

    real = path_safety.is_link_or_reparse_point
    monkeypatch.setattr(path_safety, "is_link_or_reparse_point",
                        lambda path: str(path) == str(tmp_path) or real(path))  # an ancestor is a junction (fake)
    assert _telegram_download_folder(str(folder)) == (None, "link")


def test_the_setting_wins_over_the_older_environment_variable(tmp_path, monkeypatch):
    chosen, old = tmp_path / "chosen", tmp_path / "old"
    chosen.mkdir()
    old.mkdir()
    monkeypatch.setenv("QRONOS_TELEGRAM_DOWNLOAD_FOLDER", str(old))
    assert _telegram_download_folder(str(chosen)) == (chosen, "ready")
    assert _telegram_download_folder("") == (old, "ready")


# ------------------------------------------------------------------ before the first approval

class RecordingWorkflows:
    """Stands for the coordinator: records whether a Telegram approval would have been prompted."""

    def __init__(self) -> None:
        self.begun: list[str] = []

    def try_begin(self, text, request_id):
        self.begun.append(request_id)
        return "برای ادامه، درخواست تأیید را ببینید."


def runtime_with(settings_path, monkeypatch, events):
    monkeypatch.setenv("QRONOS_SETTINGS_PATH", str(settings_path))
    runtime = QronosRuntime(notify=lambda event, status, message: events.append((event, status, message)))
    runtime.task_router = TaskRouter()
    runtime.orchestrator = object()  # no brain: a workflow request never reaches one
    runtime.conversation_history = _FakeHistory()
    runtime.action_audit = ActionAuditLog(path=None)
    workflows = RecordingWorkflows()
    runtime._app_workflows = workflows
    return runtime, workflows


def answers(events):
    return [json.loads(message) for event, _, message in events if event == "text_response"]


def test_a_missing_folder_is_said_before_any_approval_then_the_saved_setting_applies_on_reload(tmp_path, monkeypatch):
    events: list = []
    settings_path = tmp_path / "profile" / "settings.json"
    runtime, workflows = runtime_with(settings_path, monkeypatch, events)
    runtime.run_text_turn(REQUEST, "req-1")
    (answer,) = answers(events)
    assert workflows.begun == []  # no approval prompt, nothing touched Telegram
    assert not [e for e in events if e[0] == "app_workflow_approval_required"]
    assert "پوشهٔ دانلود تلگرام هنوز تعیین نشده است" in answer["text"]
    assert "تنظیمات ← عمومی ← پوشهٔ دانلود تلگرام" in answer["text"] and "کاری در تلگرام انجام نشد" in answer["text"]

    folder = tmp_path / "Telegram"
    folder.mkdir()
    # The settings service process writes the file; the desktop then asks the runtime to reload.
    SettingsService(DEFAULT_DEFINITIONS, path=settings_path).set(TELEGRAM_DOWNLOAD_FOLDER, str(folder))
    runtime.run_text_turn(REQUEST, "req-2")
    assert workflows.begun == []  # not applied until the runtime reloads its settings
    runtime.reload_settings()
    runtime.run_text_turn(REQUEST, "req-3")
    assert workflows.begun == ["req-3"]  # now the normal approval flow starts

    folder.rmdir()  # the folder disappears later: said again before any approval, no path in the answer
    runtime.run_text_turn(REQUEST, "req-4")
    last = answers(events)[-1]
    assert workflows.begun == ["req-3"] and "پیدا نشد" in last["text"]
    assert str(folder) not in last["text"] and str(tmp_path) not in json.dumps(events, ensure_ascii=False)


def test_other_workflows_and_ordinary_text_are_not_held_by_the_telegram_folder(tmp_path, monkeypatch):
    events: list = []
    runtime, workflows = runtime_with(tmp_path / "settings.json", monkeypatch, events)
    assert runtime._begin_text_app_workflow("open https://example.org/docs", "req-w", time.perf_counter()) is True
    assert workflows.begun == ["req-w"]  # browser navigation still reaches its own approval
    assert runtime._begin_text_app_workflow("search my files for the report", "req-x", time.perf_counter()) is False
