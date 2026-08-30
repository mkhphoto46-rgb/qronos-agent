"""
Runtime-independent interface for Qronos text-to-speech.

Higher-level Qronos code depends on this interface instead of depending
directly on Chatterbox, CrispASR, or any future TTS engine.

This keeps voice generation replaceable without changing the rest of the
assistant architecture.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Utterance:
    """
    One synthesized voice response and its measured generation cost.
    """

    audio_path: Path
    audio_seconds: float
    took_seconds: float
    text: str

    @property
    def faster_than_speech(self) -> bool:
        """
        True when generation completed faster than the resulting audio plays.
        """
        return (
            self.audio_seconds > 0
            and self.took_seconds < self.audio_seconds
        )

    @property
    def real_time_factor(self) -> float:
        """
        Seconds of generation work per second of produced audio.

        Lower is better.
        RTF < 1 means faster than real-time.
        """
        if self.audio_seconds <= 0:
            return float("inf")

        return self.took_seconds / self.audio_seconds


class VoiceRuntime(ABC):
    """
    Common interface implemented by every Qronos TTS runtime.
    """

    @abstractmethod
    def health_check(self) -> bool:
        """
        Return True when the runtime has all required local components and
        could attempt to speak if requested.

        This method must not load a heavy model.
        """

    @abstractmethod
    def speak_to_file(
        self,
        text: str,
        destination: str | Path | None = None,
    ) -> Utterance:
        """
        Synthesize text and write the resulting audio to disk.

        If destination is None, the runtime chooses a temporary output path.
        """

    @abstractmethod
    def release(self) -> None:
        """
        Release resources owned by this voice runtime.

        Calling release when nothing is loaded must be harmless.

        The runtime must be able to speak again after release, subject to
        normal resource admission rules.
        """
