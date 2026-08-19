from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy.io import wavfile


SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "int16"


def record_clip(
    output_path: Path,
    duration: float,
    device: int | None,
) -> None:
    samples = int(SAMPLE_RATE * duration)

    print()
    print("Get ready...")
    time.sleep(0.5)

    print("3")
    time.sleep(0.5)

    print("2")
    time.sleep(0.5)

    print("1")
    time.sleep(0.5)

    print("GO!")
    print("Say: Qronos")

    audio = sd.rec(
        samples,
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
        device=device,
    )

    sd.wait()

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    audio = np.asarray(
        audio,
        dtype=np.int16,
    )

    wavfile.write(
        output_path,
        SAMPLE_RATE,
        audio,
    )

    print(f"Saved: {output_path.name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Record 16 kHz mono int16 WAV clips "
            "for Qronos wake-word training."
        ),
    )

    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output directory.",
    )

    parser.add_argument(
        "--duration",
        type=float,
        default=2.0,
        help="Recording duration in seconds.",
    )

    parser.add_argument(
        "--count",
        type=int,
        default=10,
        help="Number of clips to record.",
    )

    parser.add_argument(
        "--device",
        type=int,
        default=None,
        help="SoundDevice input device index.",
    )

    parser.add_argument(
        "--pause",
        type=float,
        default=2.0,
        help="Pause between recordings.",
    )

    args = parser.parse_args()

    if args.duration <= 0:
        raise ValueError(
            "Duration must be greater than zero."
        )

    if args.count <= 0:
        raise ValueError(
            "Count must be greater than zero."
        )

    print("Qronos Wake Word Recorder")
    print("--------------------------")
    print(f"Output:   {args.output}")
    print(f"Duration: {args.duration:.1f}s")
    print(f"Count:    {args.count}")
    print(f"Device:   {args.device}")
    print()
    print("Target phrase: Qronos")
    print()
    print(
        "Each recording starts after a "
        "3-second countdown."
    )
    print("Speak the phrase once after GO.")
    print()
    print("Press Enter to start.")
    input()

    for index in range(1, args.count + 1):
        output_path = (
            args.output
            / f"qronos_{index:05d}.wav"
        )

        print()
        print(f"Clip {index}/{args.count}")

        record_clip(
            output_path=output_path,
            duration=args.duration,
            device=args.device,
        )

        if index < args.count:
            print()
            print(
                f"Waiting {args.pause:.1f}s "
                "before the next clip..."
            )
            time.sleep(args.pause)

    print()
    print("Recording complete.")


if __name__ == "__main__":
    main()