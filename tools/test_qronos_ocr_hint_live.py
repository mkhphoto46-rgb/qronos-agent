"""
Does Windows OCR earn its place beside the vision model?

The plan called for three arms on the same pictures — the model alone, OCR
alone, and both — because "add OCR, it is free" is an assumption, and this is
the measurement that turns it into a fact or discards it. OCR is free in tokens
and graphics memory and is not free in complexity: another engine, another
failure mode, another thing in the prompt for the model to be confused by.

**The answer depends on how big the text is, and the first measurement asked
the wrong question.** On a single dialog filling the frame the model is exact
and OCR is not, so OCR looks like a liability. But Qronos is never shown a
dialog filling the frame. It is shown a whole 4K screen shrunk to a 1280-pixel
long edge, where the same text is a third of the size — and there the model
reads the layout perfectly and invents the numbers. So both conditions are
measured here, and the second is the one that decides.

Five sections:

    **Single dialogs**, where the model wins outright and the hint must at
    least not make it worse.

    **A generated desktop** at the density Qronos actually sees, which is where
    the hint is worth having and is the reason any of this exists.

    **Persian**, which Windows has no recogniser for at all. The question is
    not whether it reads it — it cannot — but what it produces when shown some,
    because that output would otherwise be handed to the model as evidence.

    **Error codes**, the thing most worth getting character-perfect.

    **Cost**, in wall-clock, since the tokens are zero.

Run with the corpus rendered:

    python tools/vision_corpus.py
    python tools/test_qronos_ocr_hint_live.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import windows_ocr  # noqa: E402
from core.persian_text import contains_persian  # noqa: E402
from core.vision_image import prepare  # noqa: E402
from core.windows_ocr import useful_hint, word_likeness  # noqa: E402
from tools import vision_corpus  # noqa: E402
from tools.vision_eval import (  # noqa: E402
    ask,
    contains,
    ordered_character_error_rate,
    rule,
    unload,
    word_recall,
)

MODEL = "qwen3-vl:4b-instruct"
DEFAULT_CTX = 4096
DESKTOP_CTX = 8192

RESULTS: list[tuple[str, bool, str]] = []


def check(title: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((title, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {title}")

    if detail:
        print(f"        {detail}")


TRANSCRIBE = (
    "Transcribe every piece of text visible in this image, exactly as it "
    "appears. Output only the text."
)

HINTED = (
    "Another program read this image with optical character recognition and "
    "produced the text below. It read the image at a higher resolution than "
    "you can see, so it is worth checking against - especially for codes and "
    "numbers. Its reading order may be scrambled and it may contain mistakes, "
    "so trust your own eyes about what is where."
)


def transcribe(data: bytes, num_ctx: int = DEFAULT_CTX) -> str:
    return ask(
        MODEL, TRANSCRIBE, image=data, num_ctx=num_ctx, num_predict=700
    ).text


def transcribe_with_hint(
    data: bytes,
    hint: str,
    num_ctx: int = DEFAULT_CTX,
) -> str:
    return ask(
        MODEL,
        (
            f"{HINTED}\n\n"
            f"--- what the other program read ---\n{hint}\n--- end ---\n\n"
            f"{TRANSCRIBE}"
        ),
        image=data,
        num_ctx=num_ctx,
        num_predict=700,
    ).text


# ---------------------------------------------------------- the busy desktop

DESKTOP_WIDTH, DESKTOP_HEIGHT = 3072, 1728

PANELS = (
    (
        "Build output",
        (
            "Compiling qronos-core v0.4.1",
            "warning: unused import: std::io::Cursor",
            "error[E0433]: failed to resolve: use of undeclared crate",
            "Build finished with 1 error, 3 warnings in 14.72s",
        ),
    ),
    (
        "System notice",
        (
            "The update could not be installed.",
            "Error code: 0x8024402C",
            "Retry the installation after restarting.",
        ),
    ),
    (
        "Transfer",
        (
            "Copying 1,482 of 3,907 files",
            "Estimated time remaining: 6 minutes",
            "Reference: ERR-9013-QT",
        ),
    ),
    (
        "Notes",
        (
            "Remember to pin the version before release.",
            "The threshold measured 1,497 MiB, not 542 MiB.",
            "Second pass finished in 0.26 seconds.",
        ),
    ),
)

DESKTOP_CODES = ("0x8024402C", "ERR-9013-QT")

DESKTOP_TRUTH = " ".join(
    title + " " + " ".join(lines) for title, lines in PANELS
)


def desktop_html() -> str:
    """
    Four panels of known text at the size real application text is.

    Generated rather than captured, for the reason every image in this project
    is generated: a screenshot of a real desktop is a photograph of whatever
    was open on it, and this repository is public.
    """
    panels = "".join(
        f'<section class="panel"><header>{title}</header>'
        f'<div class="body">{"".join(f"<p>{line}</p>" for line in lines)}</div>'
        "</section>"
        for title, lines in PANELS
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  html, body {{ margin: 0; width: {DESKTOP_WIDTH}px; height: {DESKTOP_HEIGHT}px;
                background: #1e1f22; font-family: "Segoe UI", sans-serif; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr;
           grid-template-rows: 1fr 1fr; gap: 24px; padding: 24px;
           height: calc(100% - 48px); box-sizing: border-box; }}
  .panel {{ background: #2b2d31; border: 1px solid #3a3d43; border-radius: 6px;
            display: flex; flex-direction: column; overflow: hidden; }}
  header {{ background: #35373c; color: #e6e6e6; font-size: 13px;
            padding: 6px 10px; border-bottom: 1px solid #3a3d43; }}
  .body {{ padding: 10px; color: #dcdcdc; font-size: 13px; line-height: 1.6; }}
  p {{ margin: 0 0 4px 0; }}
</style></head><body><div class="grid">{panels}</div></body></html>
"""


def render_desktop(folder: Path) -> Path:
    source = folder / "desktop.html"
    source.write_text(desktop_html(), encoding="utf-8")

    target = folder / "desktop.png"

    subprocess.run(
        [
            str(vision_corpus.browser()),
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            f"--window-size={DESKTOP_WIDTH},{DESKTOP_HEIGHT}",
            f"--screenshot={target}",
            f"file:///{source.as_posix()}",
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )

    return target


def single_dialogs(corpus: dict) -> None:
    rule("One dialog filling the frame: the model wins outright")

    names = sorted(
        name
        for name in corpus
        if name.startswith("read_en_") and name.endswith("16px")
    )

    for name in names:
        item = corpus[name]
        data = Path(item["path"]).read_bytes()
        truth = item["text"]

        reading = windows_ocr.read(data)
        model_only = transcribe(data)
        both = transcribe_with_hint(data, reading.text)

        model_error = ordered_character_error_rate(truth, model_only)
        ocr_error = ordered_character_error_rate(truth, reading.text)
        both_error = ordered_character_error_rate(truth, both)

        check(
            f"{name}: the model is exact and OCR is not",
            model_error < 0.02 and ocr_error > model_error,
            f"model {model_error:.3f} | ocr {ocr_error:.3f} | "
            f"both {both_error:.3f}",
        )

        check(
            f"{name}: the hint does not make it worse",
            both_error <= model_error + 0.02,
            f"{model_error:.3f} -> {both_error:.3f}",
        )


def whole_desktop() -> None:
    rule("A whole desktop at the size Qronos sees: the hint earns its place")

    folder = Path(tempfile.mkdtemp())

    try:
        desktop = render_desktop(folder)
        full = desktop.read_bytes()
        shrunk = prepare(desktop)

        reading = windows_ocr.read(full)
        hint = useful_hint(reading)

        check(
            "the reading is worth passing on",
            bool(hint),
            f"{reading.describe()}, word-likeness "
            f"{word_likeness(reading.text):.2f}",
        )

        model_only = transcribe(shrunk.data, DESKTOP_CTX)
        both = transcribe_with_hint(shrunk.data, hint, DESKTOP_CTX)

        model_error = ordered_character_error_rate(DESKTOP_TRUTH, model_only)
        ocr_error = ordered_character_error_rate(DESKTOP_TRUTH, reading.text)
        both_error = ordered_character_error_rate(DESKTOP_TRUTH, both)

        check(
            "at this density the model alone is NOT exact",
            model_error > 0.05,
            f"model {model_error:.3f} at {shrunk.width}x{shrunk.height} - "
            "which is why the single-dialog result does not settle it",
        )

        check(
            "the hint cuts the error substantially",
            both_error < model_error / 2,
            f"model {model_error:.3f} | ocr {ocr_error:.3f} | "
            f"both {both_error:.3f}",
        )

        model_codes = [code for code in DESKTOP_CODES if contains(code, model_only)]
        both_codes = [code for code in DESKTOP_CODES if contains(code, both)]

        check(
            "the hint recovers error codes the model invented",
            len(both_codes) > len(model_codes),
            f"model found {model_codes}, with the hint {both_codes}",
        )

    finally:
        for leftover in folder.glob("*"):
            leftover.unlink()

        folder.rmdir()


def persian(corpus: dict) -> None:
    rule("Persian: what an engine with no recogniser for it produces")

    for name in ("read_fa_short_16px", "read_fa_long_16px"):
        item = corpus[name]
        data = Path(item["path"]).read_bytes()
        truth = item["text"]

        reading = windows_ocr.read(data)
        error = ordered_character_error_rate(truth, reading.text)

        check(
            f"{name}: OCR cannot read it",
            error > 0.5,
            f"ocr error {error:.3f}, read {len(reading.words)} words",
        )

        check(
            f"{name}: it does not fail, it invents Latin letters",
            not contains_persian(reading.text),
            f"produced {reading.text[:60]!r}",
        )

        check(
            f"{name}: and that is thrown away rather than passed on",
            useful_hint(reading) == "",
            f"word-likeness {word_likeness(reading.text):.2f}",
        )

        model_only = transcribe(data)

        check(
            f"{name}: the model reads what the engine cannot",
            word_recall(truth, model_only) >= 0.6,
            f"model recall {word_recall(truth, model_only):.3f}",
        )


def error_codes(corpus: dict) -> None:
    rule("Error codes in single dialogs")

    names = sorted(
        name for name in corpus if name.startswith("target_code_")
    )

    for name in names:
        item = corpus[name]
        data = Path(item["path"]).read_bytes()
        code = item["target"]

        check(
            f"{name}: the model has the code exactly",
            contains(code, transcribe(data)),
            f"looking for {code}",
        )


def cost(corpus: dict) -> None:
    rule("Cost")

    data = Path(corpus["read_en_long_16px"]["path"]).read_bytes()
    timings = []

    for _ in range(5):
        started = time.perf_counter()
        windows_ocr.read(data)
        timings.append(time.perf_counter() - started)

    average = sum(timings) / len(timings)

    check(
        "OCR is cheap enough to run on every capture",
        average < 2.0,
        f"{average:.2f}s average over 5 runs, "
        f"{min(timings):.2f}-{max(timings):.2f}s, no tokens, no VRAM",
    )


def main() -> int:
    if not windows_ocr.available():
        print("Windows OCR needs Windows. Nothing measured.")
        return 1

    corpus = {item["name"]: item for item in vision_corpus.load()}

    rule("The engine is reachable")

    first = windows_ocr.read(
        Path(corpus["read_en_short_16px"]["path"]).read_bytes()
    )

    check(
        "Windows has a recogniser and it works",
        first.ok,
        first.describe() if first.ok else first.reason,
    )

    if not first.ok:
        return 1

    single_dialogs(corpus)
    whole_desktop()
    persian(corpus)
    error_codes(corpus)
    cost(corpus)

    unload(MODEL)

    rule("Summary")

    passed = sum(1 for _, ok, _ in RESULTS if ok)

    for title, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILED  {title}: {detail}")

    print(f"\n  {passed} of {len(RESULTS)} checks passed")

    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
