from __future__ import annotations

from abc import ABC, abstractmethod


class VADRuntime(ABC):
    """
    Runtime-independent interface used by Qronos for voice activity detection.

    Implementations receive 16-bit mono PCM audio and return speech
    probabilities produced by the underlying VAD engine.
    """

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Return the sample rate required by this VAD runtime."""

    @abstractmethod
    def health_check(self) -> bool:
        """Return True when the VAD runtime is available."""

    def prepare(self) -> None:
        """
        Prepare the VAD runtime for low-latency capture.

        Runtimes that do not require warm-up may keep this default no-op
        implementation.
        """
        return None

    @abstractmethod
    def process_pcm16(
        self,
        audio_data: bytes,
    ) -> tuple[float, ...]:
        """
        Process PCM16 audio and return speech probabilities.

        The runtime may buffer incomplete native VAD windows internally.
        Therefore an empty tuple is valid when there is not yet enough audio.
        """

    @abstractmethod
    def reset(self) -> None:
        """Reset streaming VAD state between utterances."""

    @abstractmethod
    def close(self) -> None:
        """Release native VAD resources."""