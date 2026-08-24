from __future__ import annotations

import math

import numpy as np

from core.audio_input import (
    AudioConfig,
    AudioInput,
)


SAMPLE_SECONDS = 5.0


def calculate_rms(
    audio_data: bytes,
) -> float:
    if not audio_data:
        return 0.0

    samples = np.frombuffer(
        audio_data,
        dtype=np.int16,
    )

    if samples.size == 0:
        return 0.0

    values = samples.astype(
        np.float32
    )

    return float(
        np.sqrt(
            np.mean(
                np.square(values)
            )
        )
    )


def capture_levels(
    label: str,
    seconds: float,
) -> list[float]:
    config = AudioConfig(
        sample_rate=16_000,
        channels=1,
        frame_size=1_280,
        sample_width=2,
    )

    audio = AudioInput(
        config
    )

    frame_duration = (
        config.frame_size
        / config.sample_rate
    )

    frame_count = math.ceil(
        seconds / frame_duration
    )

    print()
    print("=" * 60)
    print(label)
    print("=" * 60)
    print()

    input(
        "Press ENTER when ready..."
    )

    levels: list[float] = []

    audio.start()

    try:
        for _ in range(
            frame_count
        ):
            frame = (
                audio.read_frame()
            )

            levels.append(
                calculate_rms(
                    frame
                )
            )

    finally:
        audio.stop()

    return levels


def print_summary(
    name: str,
    levels: list[float],
) -> None:
    values = np.asarray(
        levels,
        dtype=np.float32,
    )

    print()
    print("-" * 60)
    print(name)
    print("-" * 60)

    print(
        f"Frames:   {len(values)}"
    )

    print(
        f"Minimum:  {np.min(values):.2f}"
    )

    print(
        f"Average:  {np.mean(values):.2f}"
    )

    print(
        f"P50:      {np.percentile(values, 50):.2f}"
    )

    print(
        f"P75:      {np.percentile(values, 75):.2f}"
    )

    print(
        f"P90:      {np.percentile(values, 90):.2f}"
    )

    print(
        f"P95:      {np.percentile(values, 95):.2f}"
    )

    print(
        f"Maximum:  {np.max(values):.2f}"
    )


def print_threshold_analysis(
    speech_levels: list[float],
) -> None:
    values = np.asarray(
        speech_levels,
        dtype=np.float32,
    )

    thresholds = [
        100,
        150,
        200,
        250,
        300,
        400,
        500,
        600,
        800,
        1000,
    ]

    print()
    print("-" * 60)
    print("SPEECH FRAMES ABOVE THRESHOLD")
    print("-" * 60)
    print()

    for threshold in thresholds:
        percentage = (
            np.mean(
                values >= threshold
            )
            * 100.0
        )

        print(
            f"{threshold:4d} : "
            f"{percentage:6.2f}%"
        )


def main() -> None:
    print()
    print("=" * 60)
    print("QRONOS MICROPHONE CALIBRATION")
    print("=" * 60)

    print()
    print(
        "Test 1: Stay completely silent "
        "for 5 seconds."
    )

    silence_levels = (
        capture_levels(
            "SILENCE TEST",
            SAMPLE_SECONDS,
        )
    )

    print()
    print(
        "Test 2: Speak naturally for the "
        "full 5 seconds."
    )

    print(
        "Use your normal Qronos voice, "
        "not louder than usual."
    )

    speech_levels = (
        capture_levels(
            "SPEECH TEST",
            SAMPLE_SECONDS,
        )
    )

    print_summary(
        "SILENCE / BACKGROUND",
        silence_levels,
    )

    print_summary(
        "NORMAL SPEECH",
        speech_levels,
    )

    print_threshold_analysis(
        speech_levels
    )

    silence_p95 = float(
        np.percentile(
            silence_levels,
            95,
        )
    )

    speech_p50 = float(
        np.percentile(
            speech_levels,
            50,
        )
    )

    print()
    print("=" * 60)
    print("CALIBRATION REFERENCE")
    print("=" * 60)
    print()

    print(
        f"Silence P95: {silence_p95:.2f}"
    )

    print(
        f"Speech P50:  {speech_p50:.2f}"
    )

    if silence_p95 > 0:
        ratio = (
            speech_p50
            / silence_p95
        )

        print(
            f"Speech / noise ratio: "
            f"{ratio:.2f}x"
        )

    print()
    print(
        "Send this complete result "
        "for Qronos threshold tuning."
    )


if __name__ == "__main__":
    main()