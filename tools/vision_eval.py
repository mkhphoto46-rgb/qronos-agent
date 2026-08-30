"""
The measuring instrument for the vision work.

Separated from the harnesses that use it because three of them need the same
things: send an image to a model and get an answer back, resize it first,
score the answer against something known, and record what it cost.

Two decisions worth knowing about before reading any number this produces.

**Scoring Persian needs normalising first.** ی against ي, ک against ك, Persian
digits against ASCII — a model can be entirely correct and score zero if these
are compared literally. ``core/persian_text.normalise`` already folds all of
them, and every comparison here goes through it.

**Graphics memory is sampled during generation, not read afterwards.** The
peak is transient: the vision tower's activations and the key-value cache for
the image tokens exist while the answer is being produced and are gone by the
time anything could read them. This project has already made that mistake once
— a threshold set from a load-time figure let the voice onto a card it could
only crawl on — so this samples on a thread throughout.
"""

from __future__ import annotations

import base64
import json
import math
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.persian_text import normalise

OLLAMA = "http://127.0.0.1:11434"

#: Qwen3-VL's patch size. One token per 32x32 patch — above a floor, see below.
PATCH = 32

#: The smallest an image is ever charged at, measured on this machine.
#:
#: The documented rule is one token per 32x32 patch, and above roughly a
#: megapixel that is exactly what happens. Below it, it is not: a 64x64 image
#: and a 512x512 image both cost about 1,040 tokens, because the server
#: enlarges anything smaller to a minimum before the model sees it.
#:
#: Measured, with a fixed prompt, so the ~20 tokens of text are in every row:
#:
#:      64x64       4,096 px    1041 tokens
#:      256x256    65,536 px    1041
#:      512x512   262,144 px    1041
#:      1024x576  589,824 px    1049
#:      1280x720  921,600 px    1092
#:      1920x1080 2,073,600 px  2057
#:
#: The consequence is the opposite of the obvious one: shrinking a screenshot
#: below about 1280x720 costs accuracy and saves nothing whatsoever. The lever
#: only exists above that, where it is real — 4K costs roughly four times what
#: 1080p does.
MIN_IMAGE_TOKENS = 1040


# ------------------------------------------------------------------ images


def image_tokens(width: int, height: int) -> int:
    """
    What an image of this size costs, in tokens, before any words.

    Reflects the floor above rather than the documented patch rule alone,
    because the two disagree below a megapixel and the floor is what the
    machine actually charges.
    """
    patches = math.ceil(width / PATCH) * math.ceil(height / PATCH)

    return max(MIN_IMAGE_TOKENS, patches)


def resized(path: Path, long_edge: int | None) -> tuple[bytes, int, int]:
    """
    The image, shrunk so its longest side is ``long_edge``.

    None means send it untouched. Sizes are rounded to a whole number of
    patches, because a part-used patch costs a whole token.
    """
    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGB")
        width, height = image.size

        if long_edge and max(width, height) > long_edge:
            scale = long_edge / max(width, height)
            width = max(PATCH, round(width * scale / PATCH) * PATCH)
            height = max(PATCH, round(height * scale / PATCH) * PATCH)
            image = image.resize((width, height), Image.LANCZOS)

        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)

        return buffer.getvalue(), width, height


def cropped(path: Path, box: tuple[float, float, float, float]) -> bytes:
    """A region of the image, given as fractions of its width and height."""
    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGB")
        width, height = image.size
        left, top, right, bottom = box

        region = image.crop(
            (
                int(left * width),
                int(top * height),
                int(right * width),
                int(bottom * height),
            )
        )

        buffer = BytesIO()
        region.save(buffer, format="PNG", optimize=True)

        return buffer.getvalue()


# ------------------------------------------------------------------- cost


def gpu_used_mib() -> int:
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


class CardWatcher(threading.Thread):
    """
    Samples the card while something else is working.

    The number that matters is the peak during generation. Reading it before
    and after misses it entirely.
    """

    def __init__(self, interval: float = 0.1) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self.stopping = threading.Event()
        self.peak = 0
        self.baseline = gpu_used_mib()

    def run(self) -> None:
        while not self.stopping.is_set():
            reading = gpu_used_mib()

            if reading > 0:
                self.peak = max(self.peak, reading)

            self.stopping.wait(self.interval)

    @property
    def delta(self) -> int:
        if self.peak <= 0 or self.baseline <= 0:
            return -1

        return self.peak - self.baseline


# ------------------------------------------------------------------ asking


@dataclass
class Answer:
    """One reply, and everything it cost to get."""

    text: str
    ok: bool = True
    error: str = ""

    wall_s: float = 0.0
    load_s: float = 0.0
    prompt_eval_s: float = 0.0
    eval_s: float = 0.0
    prompt_tokens: int = 0
    eval_tokens: int = 0

    image_tokens: int = 0
    image_width: int = 0
    image_height: int = 0
    peak_vram_mib: int = -1
    vram_delta_mib: int = -1


def ask(
    model: str,
    prompt: str,
    image: bytes | None = None,
    num_ctx: int = 8192,
    num_predict: int = 256,
    temperature: float = 0.0,
    seed: int = 11,
    response_format: dict | None = None,
    watch_card: bool = True,
    timeout: float = 600.0,
) -> Answer:
    """Put one question to the model, with or without a picture."""
    message: dict = {"role": "user", "content": prompt}

    if image is not None:
        message["images"] = [base64.b64encode(image).decode("ascii")]

    payload: dict = {
        "model": model,
        "messages": [message],
        "stream": False,
        "keep_alive": "5m",
        "options": {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": temperature,
            "seed": seed,
        },
    }

    if response_format is not None:
        payload["format"] = response_format

    request = urllib.request.Request(
        f"{OLLAMA}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    watcher = CardWatcher() if watch_card else None

    if watcher is not None:
        watcher.start()

    started = time.perf_counter()

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        return Answer(text="", ok=False, error=f"HTTP {error.code}: {detail}")
    except Exception as error:  # noqa: BLE001 - a harness reports, not raises
        return Answer(text="", ok=False, error=str(error))
    finally:
        if watcher is not None:
            watcher.stopping.set()
            watcher.join(timeout=2)

    wall = time.perf_counter() - started

    answer = Answer(
        text=(body.get("message") or {}).get("content", "") or "",
        wall_s=wall,
        load_s=body.get("load_duration", 0) / 1e9,
        prompt_eval_s=body.get("prompt_eval_duration", 0) / 1e9,
        eval_s=body.get("eval_duration", 0) / 1e9,
        prompt_tokens=body.get("prompt_eval_count", 0),
        eval_tokens=body.get("eval_count", 0),
    )

    if watcher is not None:
        answer.peak_vram_mib = watcher.peak
        answer.vram_delta_mib = watcher.delta

    return answer


def context_length_in_use(model: str) -> int:
    """
    What context the server actually gave the model.

    Worth checking rather than assuming: this model ships a 262,144 default
    and no num_ctx in its parameters, which is the exact shape of the defect
    that once put a 2.3 GB model into 15.7 GB of a 16 GB card.
    """
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=15) as response:
            for entry in json.load(response).get("models", []):
                if entry.get("name", "").startswith(model.split(":")[0]):
                    return int(entry.get("context_length", 0))
    except Exception:
        pass

    return 0


def unload(model: str) -> None:
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                f"{OLLAMA}/api/generate",
                data=json.dumps(
                    {"model": model, "keep_alive": 0}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=60,
        ).read()
    except Exception:
        pass


def installed(model: str) -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=15) as response:
            names = {m["name"] for m in json.load(response).get("models", [])}

        return model in names or f"{model}:latest" in names
    except Exception:
        return False


# ----------------------------------------------------------------- scoring


def edit_distance(a: str, b: str) -> int:
    """Levenshtein, iteratively, so a long line does not blow the stack."""
    if a == b:
        return 0

    if not a:
        return len(b)

    if not b:
        return len(a)

    previous = list(range(len(b) + 1))

    for i, ca in enumerate(a, start=1):
        current = [i]

        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ca != cb),
                )
            )

        previous = current

    return previous[-1]


def flatten(text: str) -> str:
    """
    The form two pieces of text are compared in.

    Persian normalisation first, then whitespace collapsed and case folded.
    Without the first step ی against ي alone would fail every comparison, and
    a correct model would score zero.
    """
    return " ".join(normalise(text or "", for_search=True).lower().split())


def character_error_rate(reference: str, hypothesis: str) -> float:
    """Edit distance over the reference length. Lower is better; 0 is exact."""
    left = flatten(reference)
    right = flatten(hypothesis)

    if not left:
        return 0.0 if not right else 1.0

    return edit_distance(left, right) / len(left)


def ordered_character_error_rate(reference: str, hypothesis: str) -> float:
    """
    Character error rate after putting both sides in the same word order.

    Needed because of a real behaviour rather than a convenience. Asked to
    transcribe Persian, the model returns every word correctly and in *visual*
    right-to-left order rather than logical order — so a literal comparison
    scores a perfect reading at nearly nine tenths wrong. Sorting the words
    removes the ordering from the comparison and leaves the recognition, which
    is the thing being measured here.

    Word order still matters to a reader, so :func:`character_error_rate`
    keeps measuring it and both are reported.
    """
    left = " ".join(sorted(flatten(reference).split()))
    right = " ".join(sorted(flatten(hypothesis).split()))

    if not left:
        return 0.0 if not right else 1.0

    return edit_distance(left, right) / len(left)


def looks_reversed(reference: str, hypothesis: str) -> bool:
    """Whether the answer is the reference's words, backwards."""
    wanted = flatten(reference).split()
    got = flatten(hypothesis).split()

    if len(wanted) < 3 or len(got) < 3:
        return False

    return got == list(reversed(wanted))


def word_recall(reference: str, hypothesis: str) -> float:
    """What fraction of the reference's words appear in the answer."""
    wanted = flatten(reference).split()

    if not wanted:
        return 1.0

    got = set(flatten(hypothesis).split())

    return sum(1 for word in wanted if word in got) / len(wanted)


def contains(reference: str, hypothesis: str) -> bool:
    """Whether an exact token survived, once both sides are normalised."""
    return flatten(reference) in flatten(hypothesis)


def first_json(text: str) -> dict | None:
    """The first JSON object in a reply, for models that add commentary."""
    depth = 0
    start = -1

    for index, character in enumerate(text or ""):
        if character == "{":
            if depth == 0:
                start = index

            depth += 1
        elif character == "}":
            depth -= 1

            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    start = -1

    return None


def iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Overlap of two boxes as a fraction of their union."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b

    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)

    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0

    intersection = (ix1 - ix0) * (iy1 - iy0)
    union = (
        (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - intersection
    )

    return intersection / union if union > 0 else 0.0


# ---------------------------------------------------------------- reporting


@dataclass
class Trial:
    """One scored attempt."""

    name: str
    score: float
    passed: bool
    detail: str = ""
    cost: Answer | None = None


@dataclass
class Group:
    """A set of trials that answer one question together."""

    title: str
    trials: list[Trial] = field(default_factory=list)

    def add(self, trial: Trial) -> None:
        self.trials.append(trial)

    @property
    def rate(self) -> float:
        if not self.trials:
            return 0.0

        return sum(1 for t in self.trials if t.passed) / len(self.trials)

    @property
    def median_score(self) -> float:
        if not self.trials:
            return 0.0

        return statistics.median(t.score for t in self.trials)


def rule(title: str) -> None:
    print(f"\n{title}")
    print("-" * 78)


def spread(values: list[float]) -> str:
    """How much a number moves on its own, which decides what is a difference."""
    if len(values) < 2:
        return "n/a"

    return f"{min(values):.3f}-{max(values):.3f} (spread {max(values) - min(values):.3f})"
