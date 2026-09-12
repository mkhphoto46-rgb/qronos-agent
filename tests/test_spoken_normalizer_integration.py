from __future__ import annotations

from core import runtime_bridge
from core.runtime_bridge import QronosRuntime


class FakeVoiceOutput:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def speak_to_file(self, text: str):
        self.calls.append(text)

        class Utterance:
            audio_path = "fake.wav"
            audio_seconds = 0.5
            took_seconds = 0.1
            real_time_factor = 0.2

        return Utterance()


class NoopThread:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        pass


def prepared_tts_runtime(monkeypatch):
    runtime = QronosRuntime()
    voice = FakeVoiceOutput()
    runtime.voice_output = voice
    runtime._notify = lambda *args: None

    monkeypatch.setattr(
        runtime_bridge.threading,
        "Thread",
        NoopThread,
    )

    return runtime, voice


def test_chunk_synthesis_preserves_display_chunk_but_normalizes_tts_input(
    monkeypatch,
) -> None:
    runtime, voice = prepared_tts_runtime(
        monkeypatch
    )

    result = runtime._synthesize_response_chunks(
        "چهار."
    )

    assert result["chunks"] == ["چهار."]
    assert result["spokenChunks"] == ["می‌شود چهار."]
    assert voice.calls == ["می‌شود چهار."]


def test_sentence_containing_four_reaches_tts_unchanged(
    monkeypatch,
) -> None:
    runtime, voice = prepared_tts_runtime(
        monkeypatch
    )

    result = runtime._synthesize_response_chunks(
        "جواب چهار است."
    )

    assert result["chunks"] == ["جواب چهار است."]
    assert result["spokenChunks"] == ["جواب چهار است."]
    assert voice.calls == ["جواب چهار است."]
