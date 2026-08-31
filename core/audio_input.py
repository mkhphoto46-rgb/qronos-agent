from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


def _get_sounddevice() -> Any:
    """Load sounddevice only when real microphone access is required."""
    import sounddevice as sd

    return sd


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 16_000
    channels: int = 1
    frame_size: int = 1_280
    sample_width: int = 2
    device: int | None = None


class AudioInput:
    """Local microphone input for Qronos."""

    def __init__(
        self,
        config: AudioConfig | None = None,
    ) -> None:
        self.config = config or AudioConfig()
        self._stream: Any | None = None

    def start(self) -> None:
        if self.is_running():
            return

        # A PortAudio stream object can survive after the underlying input
        # stream has become inactive. Treat that state as stale instead of
        # assuming a non-None object is usable. Multi-turn voice sessions may
        # otherwise fail on the next read with "Audio input is not running."
        stale_stream = self._stream

        if stale_stream is not None:
            try:
                stale_stream.stop()
            except Exception:
                pass

            try:
                stale_stream.close()
            except Exception:
                pass

            self._stream = None

        sd = _get_sounddevice()

        stream = sd.InputStream(
            samplerate=self.config.sample_rate,
            channels=self.config.channels,
            dtype="int16",
            blocksize=self.config.frame_size,
            device=self.config.device,
        )

        try:
            stream.start()
        except Exception:
            stream.close()
            raise

        self._stream = stream

    def stop(self) -> None:
        if self._stream is None:
            return

        self._stream.stop()
        self._stream.close()
        self._stream = None

    def is_running(self) -> bool:
        return (
            self._stream is not None
            and self._stream.active
        )

    def read_frame(self) -> bytes:
        if not self.is_running():
            raise RuntimeError(
                "Audio input is not running."
            )

        audio, _ = self._stream.read(
            self.config.frame_size,
        )

        return np.asarray(
            audio,
            dtype=np.int16,
        ).tobytes()


if __name__ == "__main__":
    audio = AudioInput(
        AudioConfig(device=1),
    )

    print("Starting microphone...")

    audio.start()

    try:
        frame = audio.read_frame()
        print(f"Captured {len(frame)} bytes.")
    finally:
        audio.stop()
        print("Microphone stopped.")