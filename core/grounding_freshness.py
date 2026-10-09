"""Freshness rails for screen-grounded coordinates (requirement D3).

A coordinate that Vision found in a captured frame is evidence about that
frame only. ``core.window_binding`` already expires it when the window's
identity, geometry, DPI or focus changes. Three more things leave all of
those intact and would otherwise go unnoticed:

* time passes;
* Qronos itself changed the application (a click, a key, a wheel step);
* something else changed the content: the user scrolled, a dialog opened
  inside the window, the document reflowed.

The first two are cheap bookkeeping: every bound capture records when it was
taken and the window's *input state epoch* at that moment, and every native
input Qronos sends advances the epoch for that exact window identity
(process-wide, whichever binder or action sent it). The third needs a fresh
look: immediately before a coordinate is used, the window is captured again
and the pixels around the target are compared with the pixels the model saw.
If they differ, the coordinate is refused and must be grounded again.

Nothing here calls Windows or a model; captures come from the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import math
import threading
import time
from typing import Any, Callable


#: How long a Vision coordinate stays usable after its frame was captured.
GROUNDING_MAX_AGE_SECONDS = 20.0

#: How long a grounded drawing canvas stays usable before its first stroke.
#: Every later stroke is authorised by its own fresh frame (PaintCanvasWatch).
CANVAS_MAX_AGE_SECONDS = 45.0

#: A pixel counts as changed when its grey level moved more than this.
PIXEL_CHANGE_THRESHOLD = 40

#: A region counts as unchanged while at most this share of pixels changed
#: (and at most ``MIN_CHANGED_PIXELS`` in small regions). Window captures
#: are deterministic, so real content changes are far above this.
MAX_CHANGED_FRACTION = 0.01
MIN_CHANGED_PIXELS = 4

#: Fingerprints are stored at most this size per side.
FINGERPRINT_MAX_SIDE = 64

#: A drawing canvas is compared at full resolution and far more strictly:
#: even a small tooltip or dialog over it must stop the drawing.
CANVAS_MAX_CHANGED_FRACTION = 0.002

#: The whole window (the target's viewport context) is compared at full
#: resolution too. A repeated icon can sit in the same spot after a scroll
#: while the row, document or view it acts on changed; the context check
#: catches that. Its limit: changes smaller than this share of the window
#: (about 0.05%, e.g. one icon-sized repaint far from the target, a ticking
#: clock) are not treated as a change of context, and two different screens
#: that are pixel-identical cannot be told apart by pixels at all.
CONTEXT_MAX_CHANGED_FRACTION = 0.0005
MIN_CONTEXT_CHANGED_PIXELS = 16

Clock = Callable[[], float]


class FreshnessExpired(RuntimeError):
    """Grounded evidence is too old, or the window changed since it."""


# ------------------------------------------------------------ input epochs

_LOCK = threading.Lock()
_EPOCHS: dict[str, int] = {}


def monotonic() -> float:
    return time.monotonic()


def state_epoch(identity_token: str) -> int:
    """The current input state epoch for one exact window identity.

    Identity tokens name one window of one process instance, so the table is
    process-wide: input sent through any binder or action counts.
    """
    with _LOCK:
        return _EPOCHS.get(str(identity_token), 0)


def advance_state_epoch(identity_token: str) -> int:
    """Record that Qronos may have changed this window; returns the new epoch."""
    token = str(identity_token)
    with _LOCK:
        _EPOCHS[token] = _EPOCHS.get(token, 0) + 1
        return _EPOCHS[token]


def require_fresh(
    *,
    captured_at: float,
    captured_epoch: int,
    identity_token: str,
    max_age_seconds: float,
    clock: Clock = monotonic,
    what: str = "Grounded coordinates",
) -> None:
    """Refuse evidence that is too old or predates a Qronos input to the window.

    A missing or non-finite timestamp is refused rather than treated as new.
    """
    limit = float(max_age_seconds)
    if not math.isfinite(limit) or limit <= 0.0:
        raise ValueError("max_age_seconds must be a positive finite number.")
    stamp = float(captured_at)
    if not math.isfinite(stamp) or stamp <= 0.0:
        raise FreshnessExpired(f"{what} carry no capture time; ground them again.")
    age = float(clock()) - stamp
    if age < 0.0:
        raise FreshnessExpired(f"{what} come from a frame newer than now; refusing.")
    if age > limit:
        raise FreshnessExpired(
            f"{what} are {age:.1f} s old (limit {limit:.0f} s); ground them again."
        )
    if int(captured_epoch) != state_epoch(identity_token):
        raise FreshnessExpired(
            f"{what} were found before Qronos changed the window; ground them again."
        )


# ------------------------------------------------------------ pixel evidence


@dataclass(frozen=True)
class RegionFingerprint:
    """Grey pixels of one screen rectangle as one frame showed it."""

    screen_rect: Any  # WindowRect; kept untyped to avoid an import cycle
    width: int
    height: int
    grey: bytes
    image_size: tuple[int, int]
    #: Block-averaging bound used when it was recorded; None keeps full resolution.
    max_side: int | None = FINGERPRINT_MAX_SIDE


def _grey_region(capture: Any, screen_rect: Any):
    """The grey pixels of ``screen_rect`` in ``capture``'s model image, as a numpy array."""
    import numpy as np
    from PIL import Image

    frame = capture.frame_rect
    if frame is None or not frame.usable:
        raise FreshnessExpired("The frame does not record the screen rectangle it shows.")
    scale_x = capture.image.width / frame.width
    scale_y = capture.image.height / frame.height
    left = max(0, math.floor((screen_rect.left - frame.left) * scale_x))
    top = max(0, math.floor((screen_rect.top - frame.top) * scale_y))
    right = min(capture.image.width, math.ceil((screen_rect.right - frame.left) * scale_x))
    bottom = min(capture.image.height, math.ceil((screen_rect.bottom - frame.top) * scale_y))
    if right <= left or bottom <= top:
        raise FreshnessExpired("The checked region lies outside the captured frame.")
    try:
        with Image.open(BytesIO(capture.image.data)) as opened:
            region = opened.convert("L").crop((left, top, right, bottom))
    except OSError as exc:
        raise FreshnessExpired(f"The frame could not be decoded: {exc}") from exc
    return np.asarray(region, dtype=np.int16)


def _shrink(array, max_side: int | None = FINGERPRINT_MAX_SIDE):
    """Block-average down to at most ``max_side`` per side (deterministic)."""
    import numpy as np

    if max_side is None:
        return array
    height, width = array.shape
    step = max(1, math.ceil(max(height, width) / max_side))
    if step == 1:
        return array
    h = (height // step) * step
    w = (width // step) * step
    if h == 0 or w == 0:
        return array
    return array[:h, :w].reshape(h // step, step, w // step, step).mean(axis=(1, 3)).astype(np.int16)


def region_fingerprint(
    capture: Any,
    screen_rect: Any,
    *,
    max_side: int | None = FINGERPRINT_MAX_SIDE,
) -> RegionFingerprint:
    grey = _shrink(_grey_region(capture, screen_rect), max_side)
    return RegionFingerprint(
        screen_rect=screen_rect,
        width=int(grey.shape[1]),
        height=int(grey.shape[0]),
        grey=grey.astype("uint8").tobytes(),
        image_size=(int(capture.image.width), int(capture.image.height)),
        max_side=max_side,
    )


def changed_pixels(fingerprint: RegionFingerprint, capture: Any) -> tuple[int, int]:
    """(changed, total) pixels between the fingerprint and the same region in ``capture``."""
    import numpy as np

    if (int(capture.image.width), int(capture.image.height)) != fingerprint.image_size:
        raise FreshnessExpired("The fresh frame has a different size; ground again.")
    now = _shrink(_grey_region(capture, fingerprint.screen_rect), fingerprint.max_side)
    before = np.frombuffer(fingerprint.grey, dtype=np.uint8).reshape(
        fingerprint.height, fingerprint.width
    ).astype(np.int16)
    if now.shape != before.shape:
        raise FreshnessExpired("The checked region changed shape; ground again.")
    changed = int((np.abs(now - before) > PIXEL_CHANGE_THRESHOLD).sum())
    return changed, int(before.size)


def require_region_unchanged(
    fingerprint: RegionFingerprint,
    capture: Any,
    *,
    what: str,
    max_fraction: float = MAX_CHANGED_FRACTION,
) -> None:
    """Refuse when the fresh frame shows different content where the target was."""
    changed, total = changed_pixels(fingerprint, capture)
    minimum = MIN_CONTEXT_CHANGED_PIXELS if max_fraction == CONTEXT_MAX_CHANGED_FRACTION else MIN_CHANGED_PIXELS
    if changed > max(minimum, float(max_fraction) * total):
        raise FreshnessExpired(
            f"{what} changed on screen since it was found ({changed} of {total} "
            "sampled pixels differ); ground it again."
        )
