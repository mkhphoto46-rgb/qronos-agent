"""
Runtime-independent interface for turning Qronos's words into sound.

The mirror image of ``core/speech_runtime.py``, which is the interface for the
other direction. Higher-level code depends on this rather than on any one
engine, for the same reason it does there: the engine is a choice, and the
first one chosen is rarely the last.

That matters more here than it does for speech-to-text. The voice Qronos speaks
with is a product decision as much as a technical one, and the weights behind
it carry licence terms of their own. Keeping the seam narrow means answering
that question later costs a new class, not a new architecture.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Utterance:
    """One piece of synthesised speech, and what it cost to make."""

    #: Where the audio was written.
    audio_path: Path

    #: How long the audio plays, in seconds.
    audio_seconds: float

    #: How long it took to produce, in seconds. Wall clock, including any
    #: waiting for the engine.
    took_seconds: float

    #: The text that was spoken, after any normalisation the engine applied.
    text: str

    @property
    def faster_than_speech(self) -> bool:
        """
        Whether it was produced faster than it will be played.

        The number that decides whether a voice can ever feel responsive. Below
        one and Qronos can start speaking before it has finished thinking about
        how to; above one and every reply arrives late and keeps falling
        further behind the longer it is.
        """
        return self.took_seconds < self.audio_seconds

    @property
    def real_time_factor(self) -> float:
        """Seconds of work per second of speech. Lower is better."""
        if self.audio_seconds <= 0:
            return float("inf")

        return self.took_seconds / self.audio_seconds


class VoiceRuntime(ABC):
    """What Qronos needs from anything that can speak."""

    @abstractmethod
    def health_check(self) -> bool:
        """Return True when the runtime could speak if asked to."""

    @abstractmethod
    def speak_to_file(
        self,
        text: str,
        destination: str | Path | None = None,
    ) -> Utterance:
        """
        Synthesise ``text`` and write the audio to ``destination``.

        A destination of None means the runtime chooses a temporary path.
        """

    @abstractmethod
    def release(self) -> None:
        """
        Give back whatever the runtime is holding.

        Always safe to call, including when nothing is held. Speaking again
        afterwards must work; it may simply be slower the first time.
        """
