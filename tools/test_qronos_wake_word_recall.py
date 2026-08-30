from __future__ import annotations

import sys

from core.audio_input import (
    AudioConfig,
    AudioInput,
)
from core.openwakeword_engine import (
    OpenWakeWordEngine,
)


TOTAL_ATTEMPTS = 10
CAPTURE_SECONDS = 2.5


def main() -> int:
    audio_config = AudioConfig(
        sample_rate=16_000,
        channels=1,
        frame_size=1_280,
        sample_width=2,
    )

    audio_input = AudioInput(
        audio_config
    )

    engine = OpenWakeWordEngine(
        frame_size=audio_config.frame_size,
    )

    frames_per_second = (
        audio_config.sample_rate
        / audio_config.frame_size
    )

    capture_frames = int(
        CAPTURE_SECONDS
        * frames_per_second
    )

    print()
    print("=" * 60)
    print("QRONOS WAKE WORD CONTROLLED RECALL TEST")
    print("=" * 60)
    print()

    print(
        f"Total attempts: {TOTAL_ATTEMPTS}"
    )

    print(
        f"Capture window: {CAPTURE_SECONDS:.1f} sec"
    )

    print(
        f"Threshold: {engine.threshold:.6f}"
    )

    print()
    print(
        "For each attempt:"
    )

    print(
        "1. Press Enter."
    )

    print(
        "2. Wait until you see SAY QRONOS NOW."
    )

    print(
        "3. Say Qronos exactly ONCE."
    )

    print(
        "4. Then stay silent."
    )

    print()

    try:
        print("Preparing wake-word model...")

        engine.start()
        audio_input.start()

    except Exception as exc:
        print()
        print("STARTUP ERROR:")
        print(exc)

        audio_input.stop()
        engine.stop()

        return 1

    print("Wake-word model: READY")
    print("Microphone: READY")
    print()

    results: list[
        tuple[
            bool,
            float,
        ]
    ] = []

    try:
        for attempt in range(
            1,
            TOTAL_ATTEMPTS + 1,
        ):
            print("-" * 60)
            print(
                f"ATTEMPT {attempt}/{TOTAL_ATTEMPTS}"
            )
            print("-" * 60)

            input(
                "Press Enter when ready..."
            )

            # Reset all rolling wake-word state
            # before every controlled attempt.
            engine.pause()
            engine.resume()

            max_score = 0.0
            detected = False

            print()
            print("*** SAY QRONOS NOW ***")
            print()

            for _ in range(
                capture_frames
            ):
                frame = (
                    audio_input.read_frame()
                )

                triggered = (
                    engine.process_audio(
                        frame
                    )
                )

                score = (
                    engine.last_score
                )

                if score > max_score:
                    max_score = score

                if triggered:
                    detected = True

            results.append(
                (
                    detected,
                    max_score,
                )
            )

            if detected:
                print(
                    "RESULT: DETECTED"
                )

            else:
                print(
                    "RESULT: MISSED"
                )

            print(
                "Max score: "
                f"{max_score:.6f}"
            )

            print(
                "Threshold: "
                f"{engine.threshold:.6f}"
            )

            print()

    except KeyboardInterrupt:
        print()
        print("Test cancelled.")

        return 130

    finally:
        audio_input.stop()
        engine.stop()

    detected_count = sum(
        1
        for detected, _ in results
        if detected
    )

    missed_count = (
        len(results)
        - detected_count
    )

    detection_rate = (
        (
            detected_count
            / len(results)
            * 100.0
        )
        if results
        else 0.0
    )

    print()
    print("=" * 60)
    print("FINAL RESULTS")
    print("=" * 60)
    print()

    for index, (
        detected,
        max_score,
    ) in enumerate(
        results,
        start=1,
    ):
        status = (
            "PASS"
            if detected
            else "MISS"
        )

        print(
            f"{index:02d}. "
            f"{status:<4} "
            f"max_score={max_score:.6f}"
        )

    print()

    print(
        "Detected: "
        f"{detected_count}/{len(results)}"
    )

    print(
        "Missed: "
        f"{missed_count}/{len(results)}"
    )

    print(
        "Detection rate: "
        f"{detection_rate:.1f}%"
    )

    print()

    if detection_rate >= 90.0:
        print(
            "VERDICT: MVP ACCEPTABLE"
        )

    elif detection_rate >= 80.0:
        print(
            "VERDICT: BORDERLINE"
        )

    else:
        print(
            "VERDICT: NOT ACCEPTABLE"
        )

    print()
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(
        main()
    )