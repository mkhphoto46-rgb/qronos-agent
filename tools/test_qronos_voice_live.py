"""
Qronos's voice on the real runtime, with the real weights.

``tests/test_chatterbox_runtime.py`` proves the plumbing against a stand-in
server and needs no models. This produces actual Persian speech and reports
what it cost, which is the only way to know whether the settings that a
thirty-eight-run sweep chose still hold on this machine today.

The comparison it draws is the one that justifies the design. The sweep
launched the executable once per sentence, so every utterance paid to load the
model again — a constant 1.6 seconds across all fifteen of its sentences. This
keeps the model resident and speaks over a local socket. For a short
acknowledgement, which is most of what an assistant says, that is the
difference between waiting and not noticing.

Deliberately in ``tools/`` rather than ``tests/``: the suite runs on a Linux
machine with no graphics card and none of these weights, and ``tools/`` is
already excluded from release builds wholesale.

    .venv\\Scripts\\python.exe tools\\test_qronos_voice_live.py
"""

from __future__ import annotations

import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.chatterbox_runtime import ChatterboxRuntime, VoiceUnavailable

OUT = ROOT / "temp" / "voice_live"

#: What the benchmark measured for the same shapes of sentence, launching the
#: executable each time. Its own numbers, for comparison.
BENCHMARK_WALL = {
    "acknowledgement": 2.15,
    "a fact with numbers": 2.62,
    "borrowed technical words": 5.67,
    "a longer introduction": 14.37,
}

LINES = [
    ("acknowledgement", "بله، انجام شد."),
    (
        "a fact with numbers",
        "ساعت الان سه و ربع بعد از ظهر است و دمای بیرون هجده درجه است.",
    ),
    (
        "borrowed technical words",
        "مدل روی کارت گرافیک لود شد و سرور محلی روی پورت هشت هزار و دویست و "
        "سی و یک در حال اجراست.",
    ),
    (
        "a longer introduction",
        "من کرونوس هستم، دستیار محلی شما. همه چیز روی همین دستگاه اجرا "
        "می‌شود و هیچ صدایی به بیرون فرستاده نمی‌شود. هر وقت خواستید "
        "می‌توانید مرا متوقف کنید.",
    ),
]

REPEATS = 3


def gpu_mib() -> int:
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout

        return int(out.strip().splitlines()[0])
    except Exception:
        return -1


def rule(title: str) -> None:
    print(f"\n{title}")
    print("-" * 74)


RESULTS: list[tuple[str, bool]] = []


def check(label: str, passed: bool, detail: str = "") -> bool:
    RESULTS.append((label, bool(passed)))
    print(f"  [{'ok' if passed else 'FAILED'}] {label}")

    if detail and not passed:
        print(f"         {detail}")

    return bool(passed)


def main() -> int:
    print("=" * 74)
    print("Qronos's voice, on the real runtime")
    print("=" * 74)

    OUT.mkdir(parents=True, exist_ok=True)

    runtime = ChatterboxRuntime(idle_seconds=20.0)

    rule("1. Is the voice installed at all")
    if not check("the runtime and both models are on disk", runtime.health_check()):
        print("\n  Nothing else can run. Expected under runtime/chatterbox/:")
        print(f"    {runtime.executable_path}")
        print(f"    {runtime.voice_model_path}")
        print(f"    {runtime.codec_model_path}")
        return 1

    idle_card = gpu_mib()
    free = 16_303 - idle_card if idle_card >= 0 else -1
    print(f"  card before anything: {idle_card} MiB used, {free} MiB free")
    check("nothing is loaded before it is asked for", not runtime.is_loaded)

    # Whether there is room decides what this run can honestly measure. With
    # 931 MiB free the voice did not fail, it crawled: 3.01 seconds of work
    # per second of speech against 0.33 with room. Timing it in that state
    # would produce numbers that say nothing about the settings and
    # everything about what else was open.
    from core.chatterbox_runtime import VRAM_HEADROOM_MB

    needed = runtime.required_vram_mb + VRAM_HEADROOM_MB
    has_room = free < 0 or free >= needed

    if not has_room:
        rule("The card is busy, so speed cannot be measured today")
        print(f"  {free} MiB free; the voice needs {runtime.required_vram_mb} "
              f"at its peak plus {VRAM_HEADROOM_MB} of headroom.")
        print("  What can be checked is that Qronos says so rather than")
        print("  crawling, which is the whole reason the check exists.")

        try:
            runtime.speak_to_file(LINES[0][1], destination=OUT / "refused.wav")
            check("it refused rather than crawling", False,
                  "it went ahead on a card with no room for it")
        except VoiceUnavailable as refusal:
            check("it refused rather than crawling", True)
            check("and said how much it needed against how much there was",
                  str(runtime.required_vram_mb) in str(refusal).replace(",", "")
                  and str(free) in str(refusal).replace(",", ""),
                  str(refusal))
            print(f"\n  {refusal}")

        check("nothing was left running", not runtime.is_loaded)

        rule("Verdict")
        passed = sum(1 for _, ok in RESULTS if ok)
        print(f"  {passed} of {len(RESULTS)} checks passed")
        print("  Re-run when the card is free to measure the speed.")

        return 0 if passed == len(RESULTS) else 1

    rule("2. The first utterance, which pays for the model")
    started = time.perf_counter()

    try:
        first = runtime.speak_to_file(
            LINES[0][1], destination=OUT / "01_first.wav"
        )
    except VoiceUnavailable as error:
        check("it spoke", False, str(error))
        return 1

    cold = time.perf_counter() - started
    loaded_card = gpu_mib()

    print(f"  start to sound      {cold:6.2f}s")
    print(f"  of which the words  {first.took_seconds:6.2f}s")
    print(f"  audio produced      {first.audio_seconds:6.2f}s")
    print(f"  card now            {loaded_card} MiB used "
          f"(+{loaded_card - idle_card})")

    check("it produced audio", first.audio_path.is_file())
    check("the model is now resident", runtime.is_loaded)

    rule(f"3. Speaking, with the model already there ({REPEATS} times each)")
    print(f"  {'line':<26} {'said in':>8} {'audio':>7} {'rtf':>6} "
          f"{'benchmark':>10} {'saved':>7}")

    every_rtf = []

    for index, (label, text) in enumerate(LINES):
        takes = []

        for repeat in range(REPEATS):
            utterance = runtime.speak_to_file(
                text,
                destination=OUT / f"{index + 2:02d}_{index}_{repeat}.wav",
            )
            takes.append(utterance)

        took = statistics.median(u.took_seconds for u in takes)
        audio = statistics.median(u.audio_seconds for u in takes)
        rtf = took / audio if audio else float("inf")
        every_rtf.append(rtf)

        was = BENCHMARK_WALL.get(label)
        saved = f"{was - took:6.2f}s" if was else "     -"

        print(
            f"  {label:<26} {took:7.2f}s {audio:6.2f}s {rtf:6.2f} "
            f"{was if was else 0:9.2f}s {saved:>7}"
        )

    check(
        "every line was produced faster than it plays",
        all(rtf < 1.0 for rtf in every_rtf),
        f"real-time factors: {[round(r, 2) for r in every_rtf]}",
    )

    short_now = statistics.median(
        runtime.speak_to_file(
            LINES[0][1], destination=OUT / f"warm_{n}.wav"
        ).took_seconds
        for n in range(REPEATS)
    )

    check(
        "a short reply is quicker than the benchmark's whole-process launch",
        short_now < BENCHMARK_WALL["acknowledgement"],
        f"{short_now:.2f}s against {BENCHMARK_WALL['acknowledgement']:.2f}s",
    )

    rule("4. Giving the card back")
    peak_card = gpu_mib()
    runtime.release()
    time.sleep(3.0)
    after = gpu_mib()

    print(f"  while resident      {peak_card} MiB used")
    print(f"  after release       {after} MiB used")

    check("releasing frees the card", after < peak_card - 100,
          f"{peak_card} -> {after}")
    check("it reports itself unloaded", not runtime.is_loaded)

    rule("5. And it can speak again afterwards")
    again = runtime.speak_to_file("دوباره سلام.", destination=OUT / "99_again.wav")

    check("speaking after release works", again.audio_path.is_file())
    runtime.release()

    rule("Verdict")
    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"  {passed} of {len(RESULTS)} checks passed")
    print(f"  audio written to {OUT}")
    print("  listen to it — no test can tell you whether it sounds like Persian.")

    for label, ok in RESULTS:
        if not ok:
            print(f"    FAILED: {label}")

    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
