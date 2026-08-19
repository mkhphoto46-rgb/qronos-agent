from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import sounddevice as sd


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
        self._stream: sd.InputStream | None = None

    def start(self) -> None:
        if self._stream is not None:
            return

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