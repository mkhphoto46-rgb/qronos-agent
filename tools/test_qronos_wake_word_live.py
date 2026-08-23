from __future__ import annotations

import argparse
import time

from core.audio_input import AudioConfig, AudioInput
from core.openwakeword_engine import (
    DEFAULT_MODEL_PATH,
    DEFAULT_THRESHOLD,
    OpenWakeWordEngine,
)
from core.voice_trigger import (
    VoiceTriggerService,
    VoiceTriggerState,
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Test the local Qronos wake-word model with a microphone."
        ),
    )
    parser.add_argument(
        "--device",
        type=int,
        default=None,
        help="Optional sounddevice input device number.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="Detection threshold between 0 and 1.",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=1.5,
        help="Seconds to wait before listening again after detection.",
    )
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()

    engine = OpenWakeWordEngine(
        model_path=DEFAULT_MODEL_PATH,
        threshold=arguments.threshold,
    )
    service = VoiceTriggerService(
        wake_word="Qronos",
        engine=engine,
    )
    audio = AudioInput(
        AudioConfig(device=arguments.device),
    )

    print(f"Model: {engine.model_path}")
    print(f"Threshold: {engine.threshold:.6f}")
    print("Say 'Qronos'. Press Ctrl+C to stop.")

    service.start()

    try:
        audio.start()
    except Exception:
        service.stop()
        raise

    next_listen_time = 0.0
    next_status_time = 0.0

    try:
        while True:
            current_monotonic = time.monotonic()

            if (
                current_monotonic >= next_listen_time
                and service.state is VoiceTriggerState.PAUSED
            ):
                service.resume()

            frame = audio.read_frame()
            event = service.process_audio(
                frame,
                timestamp=time.time(),
            )

            if event is not None:
                print(
                    "\nDetected Qronos "
                    f"(score={engine.last_score:.6f})"
                )
                service.pause()
                next_listen_time = (
                    current_monotonic
                    + arguments.cooldown
                )

            if current_monotonic >= next_status_time:
                print(
                    f"\rListening... score={engine.last_score:.6f}",
                    end="",
                    flush=True,
                )
                next_status_time = current_monotonic + 0.25
    except KeyboardInterrupt:
        print("\nLive microphone test stopped.")
    finally:
        audio.stop()
        service.stop()


if __name__ == "__main__":
    main()
