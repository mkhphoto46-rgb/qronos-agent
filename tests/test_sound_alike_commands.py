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


def _ask(runtime):
    report = {}
    response = runtime._execute_voice_computer_action("اتفاد رو باز کن", report)
    assert "آره" in response and report["voiceComputerReason"] == "sound_alike_needs_confirmation"


@pytest.mark.parametrize("answer", ["آره", "بله.", "باشه"])
def test_a_spoken_yes_runs_the_command_it_was_asked_about(voice_runtime, answer):
    runtime, launched = voice_runtime
    _ask(runtime)
    report = {}
    response = runtime._execute_voice_computer_action(answer, report)
    assert launched == [("notepad.exe",)] and report["voiceSoundAlikeConfirmed"]
    assert "باز شد" in response
    # Single use: a second yes does nothing.
    runtime._execute_voice_computer_action("آره", {})
    assert launched == [("notepad.exe",)]


def test_a_spoken_no_opens_nothing(voice_runtime):
    runtime, launched = voice_runtime
    _ask(runtime)
    report = {}
    assert runtime._execute_voice_computer_action("نه", report) == "باشه، کاری انجام نشد."
    assert launched == [] and report["voiceSoundAlikeDeclined"]


def test_anything_else_drops_the_question(voice_runtime):
    from core.task_router import TaskType

    runtime, launched = voice_runtime
    _ask(runtime)
    selected, _ = runtime._select_production_task_type("یه جوک بگو", TaskType.FAST, {}, spoken=True)
    assert selected is TaskType.FAST and runtime._live_pending_sound_alike() is None
    runtime._execute_voice_computer_action("آره", {})
    assert launched == []


def test_a_yes_is_routed_to_computer_control_only_while_a_question_is_open(voice_runtime):
    from core.task_router import TaskType

    runtime, _ = voice_runtime
    selected, _ = runtime._select_production_task_type("آره", TaskType.FAST, {}, spoken=True)
    assert selected is TaskType.FAST
    _ask(runtime)
    selected, _ = runtime._select_production_task_type("آره", TaskType.FAST, {}, spoken=True)
    assert selected is TaskType.COMPUTER


def test_the_question_expires(voice_runtime, monkeypatch):
    import core.runtime_bridge as bridge

    runtime, launched = voice_runtime
    _ask(runtime)
    later = bridge.time.monotonic() + 31
    monkeypatch.setattr(bridge.time, "monotonic", lambda: later)
    runtime._execute_voice_computer_action("آره", {})
    assert launched == []


def test_stop_clears_the_question(voice_runtime):
    runtime, launched = voice_runtime
    _ask(runtime)
    runtime._discard_pending_approvals("stopped")
    runtime._execute_voice_computer_action("آره", {})
    assert launched == []


@pytest.mark.parametrize("word", ["آره", "اره", "ارِه", "بله!", "Yes."])
def test_yes_words(word):
    from core.sound_alike_commands import is_yes

    assert is_yes(word)


@pytest.mark.parametrize("word", ["بلیط", "موسیقی", "آب", "نوت پد رو باز کن", "نه", ""])
def test_not_yes_words(word):
    from core.sound_alike_commands import is_yes

    assert not is_yes(word)


@pytest.mark.parametrize("heard", ["Okay.", "Yeah.", "All right.", "Are.", "Bale.", "بل"])
def test_a_yes_as_an_english_transcription_hears_it(heard):
    # A headset recording of «آره» read in English came out as "Okay." and, under a yes/no grammar, "Yeah.".
    from core.sound_alike_commands import is_yes

    assert is_yes(heard)


@pytest.mark.parametrize("heard", ["No.", "Nah.", "Nope"])
def test_a_no_as_an_english_transcription_hears_it(heard):
    from core.sound_alike_commands import is_no

    assert is_no(heard)


@pytest.mark.parametrize("heard", ["Grazie.", "?.", "با", "موسیقی", "Pedro Bosco"])
def test_noise_transcriptions_are_neither_yes_nor_no(heard):
    from core.sound_alike_commands import is_no, is_yes

    assert not is_yes(heard) and not is_no(heard)


def test_a_bare_music_transcript_is_not_speech():
    from core.sound_alike_commands import is_whisper_non_speech

    assert is_whisper_non_speech("موسیقی") and is_whisper_non_speech("Music.")
    assert not is_whisper_non_speech("موسیقی پخش کن") and not is_whisper_non_speech("")


class _GrammarReader:
    """A speech runtime whose grammar-limited re-read returns a fixed text."""

    def __init__(self, reading):
        self.reading = reading
        self.calls = []

    def transcribe_file(self, audio_path, language="auto", grammar=None):
        self.calls.append((language, grammar is not None))
        return self.reading


@pytest.mark.parametrize(
    "reading, opens",
    [
        ("نوت پد رو باز کن.", True),       # the audio confirms the same command: act
        ("ماشی", False),                    # a fragment: not a command, still ask
        ("ماشین حساب رو باز کن", False),    # a different app: still ask
    ],
)
def test_a_fairly_close_command_is_acted_on_only_when_the_audio_confirms_it(voice_runtime, tmp_path, reading, opens):
    runtime, launched = voice_runtime
    audio = tmp_path / "command.wav"
    audio.write_bytes(b"RIFF")
    runtime._last_voice_audio_path = audio
    runtime.speech_runtime = _GrammarReader(reading)
    report = {}
    response = runtime._execute_voice_computer_action("اتفاد رو باز کن", report)
    assert runtime.speech_runtime.calls == [("fa", True)]
    assert report["voiceCommandGrammar"] == reading
    if opens:
        assert launched == [("notepad.exe",)] and "باز شد" in response
    else:
        assert launched == [] and "منظورتان" in response


def test_the_command_grammar_accepts_only_whole_command_sentences():
    from core.sound_alike_commands import command_from_grammar_reading

    assert command_from_grammar_reading(" نوت پد رو باز کن.")[1] == "notepad"
    assert command_from_grammar_reading("نوت پد رو") is None
    assert command_from_grammar_reading("اکس") is None
