from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 16_000
    channels: int = 1
    frame_size: int = 1_280
    sample_width: int = 2


class AudioInput:
    """Abstract local audio input layer for Qronos."""

    def __init__(
        self,
        config: AudioConfig | None = None,
    ) -> None:
        self.config = config or AudioConfig()
        self._running = False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def read_frame(self) -> bytes:
        if not self._running:
            raise RuntimeError(
                "Audio input is not running."
            )

        frame_bytes = (
            self.config.frame_size
            * self.config.channels
            * self.config.sample_width
        )

        return bytes(frame_bytes)


if __name__ == "__main__":
    audio = AudioInput()

    print("Qronos Audio Input")
    print(f"Sample rate: {audio.config.sample_rate}")
    print(f"Channels: {audio.config.channels}")
    print(f"Frame size: {audio.config.frame_size}")