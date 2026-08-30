from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class SpeechRuntime(ABC):
    """
    Runtime-independent interface used by Qronos for local speech-to-text.

    Higher-level Qronos code should depend on this interface instead of a
    specific STT engine. The MVP uses whisper.cpp behind this interface.
    """

    @abstractmethod
    def health_check(self) -> bool:
        """Return True when the speech runtime is ready."""

    @abstractmethod
    def transcribe_file(
        self,
        audio_path: str | Path,
        language: str = "auto",
    ) -> str:
        """Transcribe one complete audio file and return final text."""