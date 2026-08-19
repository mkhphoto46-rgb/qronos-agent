from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class VoiceTriggerState(Enum):
    DISABLED = "disabled"
    LISTENING = "listening"
    TRIGGERED = "triggered"
    PAUSED = "paused"
    ERROR = "error"


@dataclass(frozen=True)
class VoiceTriggerEvent:
    event_type: str
    wake_word: str
    timestamp: float


class WakeWordEngine(Protocol):
    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def pause(self) -> None:
        ...

    def resume(self) -> None:
        ...

    def is_running(self) -> bool:
        ...

    def process_audio(self, audio_data: bytes) -> bool:
        ...


class VoiceTriggerService:
    def __init__(
        self,
        wake_word: str = "Qronos",
        engine: WakeWordEngine | None = None,
    ) -> None:
        self.wake_word = wake_word
        self.engine = engine
        self.state = VoiceTriggerState.DISABLED

    def start(self) -> None:
        if self.engine is None:
            raise RuntimeError(
                "Wake-word engine is not configured."
            )

        self.engine.start()
        self.state = VoiceTriggerState.LISTENING

    def stop(self) -> None:
        if self.engine is not None:
            self.engine.stop()

        self.state = VoiceTriggerState.DISABLED

    def pause(self) -> None:
        if self.engine is None:
            return

        self.engine.pause()
        self.state = VoiceTriggerState.PAUSED

    def resume(self) -> None:
        if self.engine is None:
            return

        self.engine.resume()
        self.state = VoiceTriggerState.LISTENING

    def is_running(self) -> bool:
        if self.engine is None:
            return False

        return self.engine.is_running()

    def process_audio(
        self,
        audio_data: bytes,
        timestamp: float,
    ) -> VoiceTriggerEvent | None:
        if self.engine is None:
            return None

        if self.state is not VoiceTriggerState.LISTENING:
            return None

        detected = self.engine.process_audio(audio_data)

        if not detected:
            return None

        self.state = VoiceTriggerState.TRIGGERED

        return VoiceTriggerEvent(
            event_type="wake_word_detected",
            wake_word=self.wake_word,
            timestamp=timestamp,
        )


if __name__ == "__main__":
    print("Qronos Voice Trigger module")