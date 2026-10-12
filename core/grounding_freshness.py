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

#: A pixel counts as changed when ANY colour channel moved more than this.
#: Bound window captures are lossless PNGs of deterministic GDI copies, made
#: smaller (when at all) by the same deterministic resampling every time, so
#: an unchanged window gives identical pixels. 12 of 255 per channel is far
#: below a light list-selection tint (204,232,255 on white: 51 on red) and
#: sees colour-only changes of equal brightness (red <-> green), which a
#: grey-level comparison cannot.
CHANNEL_CHANGE_THRESHOLD = 12

#: The target box itself (+4 px): at most this many model pixels may differ.
MIN_CHANGED_PIXELS = 4

#: The rest of the window (the target's viewport context), in NATIVE screen
#: pixels: captures may be scaled down to the model size first, so the model
#: pixel count is converted back with the capture's own scale. About one
#: 8x8 blob anywhere in the window (a blinking caret, a busy spinner) is
#: tolerated; a changed status label, a moved selection row or a rewritten
#: text line is not.
CONTEXT_MAX_CHANGED_NATIVE_PIXELS = 64

#: A band around the target (its row and its column across the whole window,
#: padded by twice the target's size, at least 32 native px) where the rows,
#: labels or state the control acts on usually are: as strict as the target.
NEIGHBOURHOOD_MIN_PAD_NATIVE = 32

#: Inside a drawing canvas, outside Qronos's own strokes, and in the band
#: around it: at most this many native pixels may differ.
CANVAS_MAX_CHANGED_NATIVE_PIXELS = 16

Clock = Callable[[], float]


class FreshnessExpired(RuntimeError):
    """Grounded evidence is too old, or the window changed since it."""


class EvidenceTooOld(FreshnessExpired):
    """The frame is older than its limit (or carries no usable capture time)."""


class InputSinceCapture(FreshnessExpired):
    """Qronos sent input to the window after the frame was taken."""


class ContentChanged(FreshnessExpired):
    """A fresh frame shows different pixels than the frame the evidence came from."""


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
        raise EvidenceTooOld(f"{what} carry no capture time; ground them again.")
    age = float(clock()) - stamp
    if age < 0.0:
        raise EvidenceTooOld(f"{what} come from a frame newer than now; refusing.")
    if age > limit:
        raise EvidenceTooOld(
            f"{what} are {age:.1f} s old (limit {limit:.0f} s); ground them again."
        )
    if int(captured_epoch) != state_epoch(identity_token):
        raise InputSinceCapture(
            f"{what} were found before Qronos changed the window; ground them again."
        )


# ------------------------------------------------------------ pixel evidence
#
# What these checks can and cannot see (stated limits, not a guarantee):
# - They compare model-resolution pixels per colour channel. A change smaller
#   than one model pixel after scaling (a 4K window is shown to the model at
#   one third of its size) can disappear in resampling.
# - Away from the target and its row/column band, up to
#   CONTEXT_MAX_CHANGED_NATIVE_PIXELS native pixels may change unnoticed.
# - Two different states that render pixel-identically cannot be told apart
#   (no UI Automation identity is bound here; the UIA executor keeps its own
#   element re-read rails).
# - Animated content is not exempted: an animating window is refused, which
#   fails closed and may need the person to retry.


@dataclass(frozen=True)
class RegionFingerprint:
    """Model-resolution RGB pixels of one screen rectangle as one frame showed it."""

    screen_rect: Any  # WindowRect; kept untyped to avoid an import cycle
    width: int
    height: int
    rgb: bytes
    image_size: tuple[int, int]
    #: Model pixels per native screen pixel (x, y) in the frame it came from.
    scale: tuple[float, float]


def _scale(capture: Any) -> tuple[float, float]:
    frame = capture.frame_rect
    if frame is None or not frame.usable:
        raise FreshnessExpired("The frame does not record the screen rectangle it shows.")
    return capture.image.width / frame.width, capture.image.height / frame.height


def _crop_box(capture: Any, screen_rect: Any) -> tuple[int, int, int, int]:
    frame = capture.frame_rect
    scale_x, scale_y = _scale(capture)
    left = max(0, math.floor((screen_rect.left - frame.left) * scale_x))
    top = max(0, math.floor((screen_rect.top - frame.top) * scale_y))
    right = min(capture.image.width, math.ceil((screen_rect.right - frame.left) * scale_x))
    bottom = min(capture.image.height, math.ceil((screen_rect.bottom - frame.top) * scale_y))
    if right <= left or bottom <= top:
        raise FreshnessExpired("The checked region lies outside the captured frame.")
    return left, top, right, bottom


def _rgb_region(capture: Any, screen_rect: Any):
    """The RGB pixels of ``screen_rect`` in ``capture``'s model image, as an int16 array (H, W, 3)."""
    import numpy as np
    from PIL import Image

    box = _crop_box(capture, screen_rect)
    try:
        with Image.open(BytesIO(capture.image.data)) as opened:
            region = opened.convert("RGB").crop(box)
    except OSError as exc:
        raise FreshnessExpired(f"The frame could not be decoded: {exc}") from exc
    return np.asarray(region, dtype=np.int16)


def changed_mask(before, after):
    """True where any colour channel moved by more than CHANNEL_CHANGE_THRESHOLD."""
    import numpy as np

    return np.abs(after - before).max(axis=2) > CHANNEL_CHANGE_THRESHOLD


def region_fingerprint(capture: Any, screen_rect: Any) -> RegionFingerprint:
    rgb = _rgb_region(capture, screen_rect)
    return RegionFingerprint(
        screen_rect=screen_rect,
        width=int(rgb.shape[1]),
        height=int(rgb.shape[0]),
        rgb=rgb.astype("uint8").tobytes(),
        image_size=(int(capture.image.width), int(capture.image.height)),
        scale=_scale(capture),
    )


def fingerprint_pixels(fingerprint: RegionFingerprint):
    import numpy as np

    return np.frombuffer(fingerprint.rgb, dtype=np.uint8).reshape(
        fingerprint.height, fingerprint.width, 3
    ).astype(np.int16)


def region_change(fingerprint: RegionFingerprint, capture: Any):
    """The changed-pixel mask between the fingerprint and the same region in ``capture``."""
    if (int(capture.image.width), int(capture.image.height)) != fingerprint.image_size:
        raise FreshnessExpired("The fresh frame has a different size; ground again.")
    now = _rgb_region(capture, fingerprint.screen_rect)
    before = fingerprint_pixels(fingerprint)
    if now.shape != before.shape:
        raise FreshnessExpired("The checked region changed shape; ground again.")
    return changed_mask(before, now)


def changed_pixels(fingerprint: RegionFingerprint, capture: Any) -> tuple[int, int]:
    """(changed, total) model pixels between the fingerprint and the same region in ``capture``."""
    mask = region_change(fingerprint, capture)
    return int(mask.sum()), int(mask.size)


def native_pixel_limit(fingerprint: RegionFingerprint, native_pixels: float) -> float:
    """``native_pixels`` expressed in this fingerprint's model pixels (never below MIN_CHANGED_PIXELS)."""
    scale_x, scale_y = fingerprint.scale
    return max(float(MIN_CHANGED_PIXELS), float(native_pixels) * scale_x * scale_y)


def rect_mask(fingerprint: RegionFingerprint, screen_rect: Any):
    """True for the fingerprint's pixels that lie inside ``screen_rect`` (clipped)."""
    import numpy as np

    mask = np.zeros((fingerprint.height, fingerprint.width), dtype=bool)
    scale_x, scale_y = fingerprint.scale
    origin = fingerprint.screen_rect
    left = max(0, math.floor((screen_rect.left - origin.left) * scale_x))
    top = max(0, math.floor((screen_rect.top - origin.top) * scale_y))
    right = min(fingerprint.width, math.ceil((screen_rect.right - origin.left) * scale_x))
    bottom = min(fingerprint.height, math.ceil((screen_rect.bottom - origin.top) * scale_y))
    if right > left and bottom > top:
        mask[top:bottom, left:right] = True
    return mask


def require_region_unchanged(
    fingerprint: RegionFingerprint,
    capture: Any,
    *,
    what: str,
    max_native_pixels: float = 0.0,
    exclude: Any = None,
) -> None:
    """Refuse when the fresh frame shows different content in the fingerprinted region.

    ``max_native_pixels`` tolerates that many changed native pixels (converted
    to model pixels; never fewer than MIN_CHANGED_PIXELS). ``exclude`` is one
    screen rectangle left out of the comparison.
    """
    mask = region_change(fingerprint, capture)
    if exclude is not None:
        mask &= ~rect_mask(fingerprint, exclude)
    changed = int(mask.sum())
    if changed > native_pixel_limit(fingerprint, max_native_pixels):
        raise ContentChanged(
            f"{what} changed on screen since it was found ({changed} of {mask.size} "
            "model pixels differ); ground it again."
        )


def require_neighbourhood_unchanged(
    context: RegionFingerprint,
    capture: Any,
    target_box: Any,
    *,
    what: str,
) -> None:
    """The target's row and column bands across the whole window must be unchanged.

    A list row, a status label or a sibling control that says what the target
    acts on usually sits in line with it. Changes there are refused as
    strictly as changes to the target itself.
    """
    mask = region_change(context, capture)
    pad_x = max(NEIGHBOURHOOD_MIN_PAD_NATIVE, 2 * target_box.width)
    pad_y = max(NEIGHBOURHOOD_MIN_PAD_NATIVE, 2 * target_box.height)
    frame = context.screen_rect
    row = type(target_box)(frame.left, target_box.top - pad_y, frame.right, target_box.bottom + pad_y)
    column = type(target_box)(target_box.left - pad_x, frame.top, target_box.right + pad_x, frame.bottom)
    band = rect_mask(context, row) | rect_mask(context, column)
    changed = int((mask & band).sum())
    if changed > MIN_CHANGED_PIXELS:
        raise ContentChanged(
            f"{what}: the row or column around it changed on screen ({changed} model pixels); ground it again."
        )
