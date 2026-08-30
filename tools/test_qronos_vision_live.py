"""
What Qwen3-VL can actually do on this machine, measured before anything is built.

The voice work went the same way: measure, conclude, then implement against the
conclusions rather than against expectations. Two of the expectations here were
already wrong before this file existed, which is the argument for it.

Every image is drawn by ``tools/vision_corpus.py`` from HTML, so the right
answer is the input rather than something somebody labelled. Nothing is
captured from a real screen: this repository is public, and a screenshot of a
desktop is a photograph of whatever was open on it.

    .venv\\Scripts\\python.exe tools\\vision_corpus.py
    .venv\\Scripts\\python.exe tools\\test_qronos_vision_live.py
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools import vision_corpus
from tools.vision_eval import (
    Answer,
    Group,
    Trial,
    ask,
    character_error_rate,
    contains,
    context_length_in_use,
    first_json,
    gpu_used_mib,
    image_tokens,
    installed,
    iou,
    ordered_character_error_rate,
    resized,
    rule,
    spread,
    unload,
    word_recall,
)

MODEL = "qwen3-vl:4b-instruct"

#: Sent untouched unless the image is larger. The corpus is drawn at sizes
#: below the token floor, so resizing it down would change nothing.
DEFAULT_CTX = 4096

READ_PROMPT = (
    "Transcribe every character of text in this image exactly, in normal "
    "reading order. Output only the transcribed text and nothing else."
)

RESULTS: list[tuple[str, bool]] = []


def check(label: str, passed: bool, detail: str = "") -> bool:
    RESULTS.append((label, bool(passed)))
    print(f"  [{'ok' if passed else 'FAILED'}] {label}")

    if detail and not passed:
        print(f"         {detail}")

    return bool(passed)


def corpus() -> list[dict]:
    try:
        return vision_corpus.load()
    except SystemExit:
        print("  drawing the corpus first ...")
        vision_corpus.render(vision_corpus.build_items())

        return vision_corpus.load()


def of(records: list[dict], family: str) -> list[dict]:
    return [r for r in records if r["family"] == family]


# --------------------------------------------------------------- 1. reading


def measure_reading(records: list[dict]) -> None:
    rule("2. Reading text off an interface")
    print("     the load-bearing question: Windows OCR cannot do Persian at all,")
    print("     so if the model cannot either, 'read my screen' is English-only.\n")

    print(f"  {'language':<9} {'size':>5} {'ordered CER':>12} {'recall':>7} "
          f"{'literal CER':>12}")
    print("  " + "-" * 52)

    by_language: dict[str, list[tuple[int, float, float, float]]] = {}

    for record in sorted(of(records, "read"),
                         key=lambda r: (r["language"], r["font_px"])):
        data, width, height = resized(Path(record["path"]), None)
        answer = ask(MODEL, READ_PROMPT, image=data, num_ctx=DEFAULT_CTX,
                     num_predict=200, watch_card=False)

        if not answer.ok:
            print(f"  {record['name']}: {answer.error[:60]}")
            continue

        expected = record["text"]
        ordered = ordered_character_error_rate(expected, answer.text)
        literal = character_error_rate(expected, answer.text)
        recall = word_recall(expected, answer.text)

        by_language.setdefault(record["language"], []).append(
            (record["font_px"], ordered, recall, literal)
        )

        print(f"  {record['language']:<9} {record['font_px']:>4}px "
              f"{ordered:>12.3f} {recall:>7.3f} {literal:>12.3f}")

    print()

    for language, rows in sorted(by_language.items()):
        ordered = statistics.median(r[1] for r in rows)
        recall = statistics.median(r[2] for r in rows)
        literal = statistics.median(r[3] for r in rows)

        label = "Persian" if language == "fa" else "English"
        print(f"  {label:<8} median  ordered CER {ordered:.3f}   "
              f"recall {recall:.3f}   literal CER {literal:.3f}")

        # Two different bars, because the two scripts are in different
        # situations. English has an alternative — Windows OCR reads it for
        # free — so the model only earns its place there by being close to
        # exact. Persian has no alternative at all: Windows has no Arabic-
        # script recogniser and cannot be given one, so the question is
        # whether this is good enough to use, not whether it is perfect.
        #
        # Recall is the bar for Persian rather than character error, because
        # the errors that remain are word order and short-string edge effects
        # rather than misread letters, and a reader who gets every word can
        # act on the answer.
        if language == "en":
            check("English is read essentially exactly",
                  ordered < 0.05, f"median ordered CER {ordered:.3f}")
        else:
            check("Persian words are recovered (recall at or above 0.80)",
                  recall >= 0.80, f"median recall {recall:.3f}")
            print(f"           character error {ordered:.3f}; the remaining "
                  "error is ordering and short lines, not misread letters")

        # The gap between the two is the ordering artefact, isolated.
        if literal - ordered > 0.25:
            print(f"           note: literal error is {literal:.3f} against "
                  f"{ordered:.3f} ordered — the words are right, the order is "
                  "visual rather than logical")

    # Does it read small text as well as large?
    for language, rows in sorted(by_language.items()):
        small = [r[1] for r in rows if r[0] <= 12]
        large = [r[1] for r in rows if r[0] >= 24]

        if small and large:
            label = "Persian" if language == "fa" else "English"
            print(f"  {label:<8} 12px {statistics.median(small):.3f} vs "
                  f"24px+ {statistics.median(large):.3f}")


def measure_targets(records: list[dict]) -> None:
    rule("3. Reading one exact thing — an error code")
    print("     scored by exact match, which is what 'read the error' means\n")

    hits = 0
    rows = of(records, "target")

    for record in rows:
        data, _, _ = resized(Path(record["path"]), None)
        answer = ask(
            MODEL,
            "What is the error code shown in this image? "
            "Reply with only the code.",
            image=data, num_ctx=DEFAULT_CTX, num_predict=32, watch_card=False,
        )

        got = contains(record["target"], answer.text)
        hits += got

        print(f"  [{'ok' if got else '  '}] {record['name']:<24} "
              f"want {record['target']:<12} got {answer.text.strip()[:28]!r}")

    check("every error code was read exactly", hits == len(rows),
          f"{hits} of {len(rows)}")


# ------------------------------------------------------------ 2. resolution


def measure_resolution(records: list[dict]) -> None:
    rule("4. How much can be thrown away before reading breaks")
    print("     the surprise: below about 1280x720 the server enlarges the image")
    print("     anyway, so shrinking further costs accuracy and saves nothing\n")

    # The largest reading item, upscaled first so there is something to lose.
    source = max(
        (r for r in of(records, "read") if r["language"] == "fa"),
        key=lambda r: r["font_px"],
    )

    from PIL import Image

    big = ROOT / "temp" / "vision_corpus" / "_upscaled.png"

    with Image.open(source["path"]) as image:
        image.convert("RGB").resize(
            (image.width * 3, image.height * 3), Image.LANCZOS
        ).save(big)

    print(f"  {'long edge':>10} {'sent':>12} {'tokens':>7} {'ordered CER':>12} "
          f"{'wall':>6}")
    print("  " + "-" * 52)

    for long_edge in (None, 1920, 1280, 1024, 768, 512):
        data, width, height = resized(big, long_edge)
        answer = ask(MODEL, READ_PROMPT, image=data, num_ctx=16384,
                     num_predict=200, watch_card=False)

        if not answer.ok:
            print(f"  {long_edge}: {answer.error[:50]}")
            continue

        error = ordered_character_error_rate(source["text"], answer.text)
        label = "native" if long_edge is None else str(long_edge)

        print(f"  {label:>10} {f'{width}x{height}':>12} "
              f"{answer.prompt_tokens:>7} {error:>12.3f} {answer.wall_s:>5.1f}s")

    big.unlink(missing_ok=True)


# ------------------------------------------------------------ 3. understanding


def measure_identify(records: list[dict]) -> None:
    rule("5. Knowing what it is looking at")

    hits = 0
    rows = of(records, "identify")

    for record in rows:
        data, _, _ = resized(Path(record["path"]), None)
        answer = ask(
            MODEL,
            "Is this image a screenshot of a computer application interface — "
            "windows, buttons, menus? Answer only yes or no.",
            image=data, num_ctx=DEFAULT_CTX, num_predict=16, watch_card=False,
        )

        want = record["answers"]["interface"]
        got = answer.text.strip().lower().startswith(want)
        hits += got

        print(f"  [{'ok' if got else '  '}] {record['name']:<24} "
              f"interface? want {want:<4} got {answer.text.strip()[:20]!r}")

    check("it can tell an application interface from anything else",
          hits == len(rows), f"{hits} of {len(rows)}")


def measure_understanding(records: list[dict]) -> None:
    rule("6. Understanding what is in the interface")

    questions = {
        "buttons": "How many buttons are in this dialog? Reply with only the number.",
        "primary": "What is the label on the highlighted or primary button? Reply with only the label.",
        "checked": "Is the checkbox ticked? Answer only yes or no.",
        "percent": "What percentage does the progress bar show? Reply with only the number.",
    }

    hits = total = 0

    for record in of(records, "count") + of(records, "state"):
        data, _, _ = resized(Path(record["path"]), None)

        for key, want in record["answers"].items():
            if key not in questions:
                continue

            answer = ask(MODEL, questions[key], image=data,
                         num_ctx=DEFAULT_CTX, num_predict=24, watch_card=False)

            got = contains(want, answer.text)
            hits += got
            total += 1

            print(f"  [{'ok' if got else '  '}] {record['name']:<24} {key:<8} "
                  f"want {want:<12} got {answer.text.strip()[:22]!r}")

    check("it answers closed questions about an interface",
          total and hits / total >= 0.75, f"{hits} of {total}")


def measure_locating(records: list[dict]) -> None:
    rule("7. Pointing at things — bounding boxes")
    print("     upstream reports these can be 'significantly off', so this")
    print("     measures rather than assumes\n")

    rows = of(records, "locate")

    if not rows:
        print("  nothing to locate in the corpus")
        return

    for record in rows:
        data, width, height = resized(Path(record["path"]), None)
        answer = ask(
            MODEL,
            'Locate the primary button. Reply with only JSON of the form '
            '{"bbox_2d": [x1, y1, x2, y2]} using coordinates normalised to '
            "0-1000.",
            image=data, num_ctx=DEFAULT_CTX, num_predict=64, watch_card=False,
        )

        parsed = first_json(answer.text)
        box = (parsed or {}).get("bbox_2d")

        if not box or len(box) != 4:
            check("it returned a usable box", False,
                  f"got {answer.text.strip()[:60]!r}")
            continue

        check("it returned a usable box", True)

        truth = record.get("boxes", {}).get("primary")

        if not truth:
            print("  no measured box in the corpus to compare against")
            continue

        fraction = tuple(v / 1000 for v in box)
        overlap = iou(fraction, tuple(truth))

        print(f"  model    {[round(v, 3) for v in fraction]}")
        print(f"  measured {[round(v, 3) for v in truth]}")
        print(f"  overlap  {overlap:.2f}")

        # Half is a demanding bar for a box drawn from a description. The
        # first version of this test compared against a hand-estimated box,
        # scored zero, and was itself the thing that was wrong.
        check("the box lands on the button", overlap > 0.5,
              f"overlap {overlap:.2f}")


# ------------------------------------------------------------------ 4. cost


def measure_cost(records: list[dict]) -> None:
    rule("8. What it costs")

    record = of(records, "read")[0]
    data, width, height = resized(Path(record["path"]), None)

    unload(MODEL)
    import time as _time
    _time.sleep(3)

    idle = gpu_used_mib()

    cold = ask(MODEL, READ_PROMPT, image=data, num_ctx=DEFAULT_CTX,
               num_predict=120)
    warm = [
        ask(MODEL, READ_PROMPT, image=data, num_ctx=DEFAULT_CTX,
            num_predict=120)
        for _ in range(3)
    ]

    print(f"  card idle              {idle} MiB")
    print(f"  peak during generation {cold.peak_vram_mib} MiB "
          f"(+{cold.vram_delta_mib} over idle)")
    print(f"  cold  {cold.wall_s:6.2f}s  of which {cold.load_s:.2f}s loading")
    print(f"  warm  {statistics.median(a.wall_s for a in warm):6.2f}s  median "
          f"of {len(warm)}")
    print(f"  prompt {cold.prompt_tokens} tokens for a "
          f"{width}x{height} image (estimate {image_tokens(width, height)})")

    in_use = context_length_in_use(MODEL)
    print(f"  context asked for {DEFAULT_CTX}, server reports {in_use}")

    check("the declared context is honoured", in_use == DEFAULT_CTX,
          f"asked {DEFAULT_CTX}, got {in_use} — the default is 262144")
    check("it fits on this card with room to spare",
          0 < cold.vram_delta_mib < 8000, f"{cold.vram_delta_mib} MiB")


def measure_noise(records: list[dict]) -> None:
    rule("9. How much a score moves on its own")
    print("     anything smaller than this spread is not a difference\n")

    record = [r for r in of(records, "read")
              if r["language"] == "fa" and r["font_px"] == 16][0]
    data, _, _ = resized(Path(record["path"]), None)

    scores = []

    for seed in (11, 22, 33, 44, 55):
        answer = ask(MODEL, READ_PROMPT, image=data, num_ctx=DEFAULT_CTX,
                     num_predict=200, seed=seed, watch_card=False)
        scores.append(ordered_character_error_rate(record["text"], answer.text))

    print(f"  five seeds, same image: {[round(s, 3) for s in scores]}")
    print(f"  {spread(scores)}")

    check("the measurement is stable enough to compare configurations",
          max(scores) - min(scores) < 0.2,
          f"spread {max(scores) - min(scores):.3f}")


# ------------------------------------------------------------------- main


def main() -> int:
    print("=" * 78)
    print("Qwen3-VL on this machine — what it can do, before anything is built")
    print("=" * 78)

    rule("1. Prerequisites")

    if not check(f"{MODEL} is installed", installed(MODEL),
                 f"run: ollama pull {MODEL}"):
        return 1

    records = corpus()
    check(f"the corpus is drawn ({len(records)} images)", bool(records))

    free = 16303 - max(0, gpu_used_mib())
    print(f"  card: {free} MiB free")

    if free < 6000:
        print("\n  The card is too busy to measure honestly. Re-run when free.")
        return 1

    measure_reading(records)
    measure_targets(records)
    measure_resolution(records)
    measure_identify(records)
    measure_understanding(records)
    measure_locating(records)
    measure_cost(records)
    measure_noise(records)

    rule("Verdict")
    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"  {passed} of {len(RESULTS)} checks passed")

    for label, ok in RESULTS:
        if not ok:
            print(f"    FAILED: {label}")

    unload(MODEL)

    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
