"""Misheard spoken app commands are recovered by how they sound, and ordinary speech is left alone.

The misheard transcripts are real speech-recognition output from a headset microphone.
"""
from __future__ import annotations

import pytest

from core.computer_executor import ComputerOperation
from core.sound_alike_commands import SoundAlikeDecision, match_spoken_command


@pytest.mark.parametrize(
    "transcript, app_id",
    [
        ("نوت پدرو بسکو", "notepad"),
        ("نوت پدرو باسко", "notepad"),  # the recogniser slipped into Cyrillic
        ("نوت پیرو بسکا", "notepad"),
        ("نوت پیرو باز کن", "notepad"),
        ("نوت‌پد رو باز کن", "notepad"),
        ("ما شنه هزا براباس کن.", "calculator"),
    ],
)
def test_a_misheard_open_command_is_acted_on(transcript, app_id):
    match = match_spoken_command(transcript)
    assert match is not None and match.decision is SoundAlikeDecision.ACT
    assert (match.operation, match.app_id) == (ComputerOperation.OPEN_APP, app_id)
    assert match.action().app_id == app_id


@pytest.mark.parametrize("transcript", ["اتفاد رو باز کن", "لطمان نوتپاد را باز کن."])
def test_a_fairly_close_command_asks_first(transcript):
    match = match_spoken_command(transcript)
    assert match is not None and match.decision is SoundAlikeDecision.CONFIRM


@pytest.mark.parametrize(
    "transcript",
    [
        "سلام حالت شطوره",
        "هل اچه تاره؟",
        "آنه خوب همچه خواهره شکاره میکنیم",
        "پای تخت ایران کجا است؟",
        "امروز هوا چطوره؟",
        "یه جوک بگو",
        "باز هم ممنون",
        "نوشتن یک پیام برای مادرم",
        "پدرت کجاست؟",
        "هده",
        "Not Pedro Bosco.",
        "",
    ],
)
def test_ordinary_speech_is_never_matched(transcript):
    assert match_spoken_command(transcript) is None


def test_close_commands_are_matched_too():
    match = match_spoken_command("نوت پد رو ببند")
    assert match is not None and (match.operation, match.app_id) == (ComputerOperation.CLOSE_APP, "notepad")


def test_only_voice_transcripts_are_promoted_to_computer_control():
    from core.runtime_bridge import QronosRuntime
    from core.task_router import TaskType

    runtime = QronosRuntime.__new__(QronosRuntime)
    spoken, _ = QronosRuntime._select_production_task_type(
        runtime, "نوت پدرو بسکو", TaskType.FAST, {}, spoken=True
    )
    assert spoken is TaskType.COMPUTER
    typed, _ = QronosRuntime._select_production_task_type(runtime, "نوت پدرو بسکو", TaskType.FAST, {})
    assert typed is not TaskType.COMPUTER


class _FakeProcess:
    pid = 5151

    def poll(self):
        return None


@pytest.fixture
def voice_runtime(tmp_path, monkeypatch):
    from core.action_audit import ActionAuditLog
    from core.computer_executor import ComputerExecutor
    from core.runtime_bridge import QronosRuntime
    from core.undo_journal import UndoJournal
    from security.gate import set_default_audit_sink

    monkeypatch.setenv("QRONOS_PERMISSION_POLICY_PATH", str(tmp_path / "permissions.json"))
    monkeypatch.setenv("QRONOS_PERMISSION_AUDIT_PATH", str(tmp_path / "permission_audit.jsonl"))
    runtime = QronosRuntime(notify=lambda *event: None)
    runtime.action_audit = ActionAuditLog(path=None)
    runtime.undo_journal = UndoJournal(path=None)
    launched = []
    runtime.computer_executor = ComputerExecutor(
        audit=runtime.action_audit,
        journal=runtime.undo_journal,
        launcher=lambda command: launched.append(command) or _FakeProcess(),
        verification_delay_seconds=0,
        dynamic_apps=False,
    )
    yield runtime, launched
    set_default_audit_sink(None)
    runtime.close()


def test_a_misheard_voice_command_opens_the_app_it_sounds_like(voice_runtime):
    runtime, launched = voice_runtime
    report = {}
    response = runtime._execute_voice_computer_action("نوت پدرو بسکو", report)
    assert launched == [("notepad.exe",)]
    assert report["voiceComputerExecuted"] and report["voiceSoundAlike"]["decision"] == "act"
    assert "باز شد" in response


def test_a_fairly_close_voice_command_asks_and_opens_nothing(voice_runtime):
    runtime, launched = voice_runtime
    report = {}
    response = runtime._execute_voice_computer_action("اتفاد رو باز کن", report)
    assert launched == []
    assert report["voiceComputerReason"] == "sound_alike_needs_confirmation"
    assert "منظورتان" in response and "نوت پد رو باز کن" in response


def test_an_unknown_app_still_opens_nothing(voice_runtime):
    runtime, launched = voice_runtime
    report = {}
    runtime._execute_voice_computer_action("Premiere رو باز کن", report)
    assert launched == []
