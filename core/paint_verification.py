from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import hashlib
import math
from typing import Protocol, TYPE_CHECKING

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from core.sketch_approval import VisualApprovedSketch
from core.sketch_preview import digest_shape_plan
from core.vision_image import PreparedImage
from core.window_binding import WindowBinding, WindowRect

if TYPE_CHECKING:
    from core.sketch_critic import SketchCritique


DEFAULT_MIN_EXPECTED_COVERAGE = 0.85
DEFAULT_MIN_OBSERVED_PRECISION = 0.80
DEFAULT_MIN_F1_SCORE = 0.82
DEFAULT_MIN_DARKENING = 18
DEFAULT_TOLERANCE_FRACTION = 0.0125
DEFAULT_MAX_TOLERANCE_PIXELS = 8
DEFAULT_EXPECTED_LINE_WIDTH = 3


class PaintPostDrawError(RuntimeError):
    """Post-draw evidence is unusable or does not satisfy the approved sketch."""


class _Critic(Protocol):
    def critique(self, request: str, preview: PreparedImage) -> "SketchCritique": ...


class _CaptureLike(Protocol):
    image: PreparedImage
    window: int | None
    binding_token: str
    layout_token: str
    source_width: int
    source_height: int
    frame_rect: WindowRect | None


class _CanvasLike(Protocol):
    rect: WindowRect
    hwnd: int
    identity_token: str
    layout_token: str


@dataclass(frozen=True)
class PaintPostDrawVerification:
    """Evidence that the real Paint side effect matches the approved sketch."""

    passed: bool
    geometric_passed: bool
    semantic_passed: bool
    expected_coverage: float
    observed_precision: float
    f1_score: float
    expected_pixels: int
    observed_pixels: int
    tolerance_radius: int
    actual_canvas_image: PreparedImage
    observed_delta_image: PreparedImage
    expected_image: PreparedImage
    semantic_critique: "SketchCritique"

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"{verdict}: geometric coverage={self.expected_coverage:.3f}, "
            f"precision={self.observed_precision:.3f}, f1={self.f1_score:.3f}; "
            f"semantic={self.semantic_critique.verdict} "
            f"({self.semantic_critique.confidence:.3f})."
        )

    def require_pass(self) -> None:
        if self.passed:
            return
        raise PaintPostDrawError(
            "Real Paint output did not match the approved sketch. " + self.summary()
        )


class PaintPostDrawVerifier:
    """
    Verify the *actual* Paint side effect after pointer execution.

    The verifier is intentionally observational only. It never clicks, redraws,
    clears the canvas, or retries a side effect. The caller supplies one capture
    from immediately before drawing and one from immediately after. Their pixel
    difference isolates newly drawn dark line art, so existing canvas content is
    not mistaken for the new sketch.

    Verification has two independent gates:
    1. deterministic geometric fidelity against the frozen approved ShapePlan;
    2. semantic recognition of the observed delta by the existing visual critic.

    Both must pass. A failure is evidence for the caller to stop; it is not
    permission to redraw on the same canvas.
    """

    def __init__(
        self,
        critic: _Critic,
        *,
        min_expected_coverage: float = DEFAULT_MIN_EXPECTED_COVERAGE,
        min_observed_precision: float = DEFAULT_MIN_OBSERVED_PRECISION,
        min_f1_score: float = DEFAULT_MIN_F1_SCORE,
        min_darkening: int = DEFAULT_MIN_DARKENING,
        tolerance_fraction: float = DEFAULT_TOLERANCE_FRACTION,
        max_tolerance_pixels: int = DEFAULT_MAX_TOLERANCE_PIXELS,
        expected_line_width: int = DEFAULT_EXPECTED_LINE_WIDTH,
    ) -> None:
        self._critic = critic
        self.min_expected_coverage = _unit_interval(
            min_expected_coverage, "min_expected_coverage"
        )
        self.min_observed_precision = _unit_interval(
            min_observed_precision, "min_observed_precision"
        )
        self.min_f1_score = _unit_interval(min_f1_score, "min_f1_score")
        if isinstance(min_darkening, bool) or not isinstance(min_darkening, int):
            raise ValueError("min_darkening must be an integer.")
        if not 1 <= min_darkening <= 128:
            raise ValueError("min_darkening must be between 1 and 128.")
        if not math.isfinite(float(tolerance_fraction)) or not 0.001 <= float(
            tolerance_fraction
        ) <= 0.1:
            raise ValueError("tolerance_fraction must be between 0.001 and 0.1.")
        if (
            isinstance(max_tolerance_pixels, bool)
            or not isinstance(max_tolerance_pixels, int)
            or not 1 <= max_tolerance_pixels <= 32
        ):
            raise ValueError("max_tolerance_pixels must be an integer from 1 through 32.")
        if (
            isinstance(expected_line_width, bool)
            or not isinstance(expected_line_width, int)
            or not 1 <= expected_line_width <= 16
        ):
            raise ValueError("expected_line_width must be an integer from 1 through 16.")

        self.min_darkening = min_darkening
        self.tolerance_fraction = float(tolerance_fraction)
        self.max_tolerance_pixels = max_tolerance_pixels
        self.expected_line_width = expected_line_width

    def verify(
        self,
        binding: WindowBinding,
        approved_sketch: VisualApprovedSketch,
        canvas: _CanvasLike,
        before_capture: _CaptureLike,
        after_capture: _CaptureLike,
        *,
        inset_pixels: int = 0,
    ) -> PaintPostDrawVerification:
        if not isinstance(approved_sketch, VisualApprovedSketch):
            raise PaintPostDrawError(
                "Post-draw verification requires the same VisualApprovedSketch "
                "capability that authorized execution."
            )
        if digest_shape_plan(approved_sketch.plan) != approved_sketch.plan_digest:
            raise PaintPostDrawError(
                "Approved sketch plan digest changed before post-draw verification."
            )
        if isinstance(inset_pixels, bool) or not isinstance(inset_pixels, int):
            raise PaintPostDrawError("inset_pixels must be an integer.")
        if inset_pixels < 0:
            raise PaintPostDrawError("inset_pixels cannot be negative.")

        _validate_canvas(binding, canvas)
        _validate_capture(binding, canvas, before_capture, label="before")
        _validate_capture(binding, canvas, after_capture, label="after")

        before, before_box = _crop_capture(before_capture, canvas.rect)
        after, after_box = _crop_capture(after_capture, canvas.rect)
        if before.size != after.size:
            raise PaintPostDrawError(
                "Before/after Paint canvas crops have different sizes; verification "
                "would compare different geometries."
            )

        observed_mask = _new_dark_strokes(
            before,
            after,
            min_darkening=self.min_darkening,
        )
        expected_mask = _render_expected_mask(
            approved_sketch,
            capture=after_capture,
            crop_box=after_box,
            canvas_screen_rect=canvas.rect,
            inset_pixels=inset_pixels,
            line_width=self.expected_line_width,
        )

        expected_pixels = int(expected_mask.sum())
        observed_pixels = int(observed_mask.sum())
        if expected_pixels <= 0:
            raise PaintPostDrawError("Approved sketch rendered no expected pixels.")

        tolerance_radius = min(
            self.max_tolerance_pixels,
            max(2, round(min(after.width, after.height) * self.tolerance_fraction)),
        )
        expected_coverage, observed_precision, f1_score = _fidelity_metrics(
            expected_mask,
            observed_mask,
            radius=tolerance_radius,
        )
        geometric_passed = (
            expected_coverage >= self.min_expected_coverage
            and observed_precision >= self.min_observed_precision
            and f1_score >= self.min_f1_score
        )

        observed_delta_image = _mask_to_prepared(observed_mask)
        expected_image = _mask_to_prepared(expected_mask)
        actual_canvas_image = _pil_to_prepared(after)

        critique = self._critic.critique(
            approved_sketch.request,
            observed_delta_image,
        )
        semantic_passed = bool(critique.passes())
        passed = geometric_passed and semantic_passed

        return PaintPostDrawVerification(
            passed=passed,
            geometric_passed=geometric_passed,
            semantic_passed=semantic_passed,
            expected_coverage=expected_coverage,
            observed_precision=observed_precision,
            f1_score=f1_score,
            expected_pixels=expected_pixels,
            observed_pixels=observed_pixels,
            tolerance_radius=tolerance_radius,
            actual_canvas_image=actual_canvas_image,
            observed_delta_image=observed_delta_image,
            expected_image=expected_image,
            semantic_critique=critique,
        )


def _unit_interval(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be within 0.0..1.0.")
    return value


def _validate_canvas(binding: WindowBinding, canvas: _CanvasLike) -> None:
    if binding.app_id != "paint":
        raise PaintPostDrawError("Post-draw verification requires a Paint binding.")
    if (
        canvas.hwnd != binding.hwnd
        or canvas.identity_token != binding.identity_token
        or canvas.layout_token != binding.layout_token
    ):
        raise PaintPostDrawError(
            "Paint canvas capability does not match the binding used for verification."
        )
    if not _rect_inside(binding.snapshot.client_rect, canvas.rect):
        raise PaintPostDrawError(
            "Paint canvas rectangle is outside the verified client area."
        )


def _validate_capture(
    binding: WindowBinding,
    canvas: _CanvasLike,
    capture: _CaptureLike,
    *,
    label: str,
) -> None:
    if capture.window != binding.hwnd:
        raise PaintPostDrawError(
            f"{label} capture belongs to a different HWND than the Paint binding."
        )
    if capture.binding_token != binding.identity_token:
        raise PaintPostDrawError(
            f"{label} capture identity token does not match the Paint binding."
        )
    if capture.layout_token != binding.layout_token:
        raise PaintPostDrawError(
            f"{label} capture layout token is stale."
        )
    if capture.frame_rect is None or not capture.frame_rect.usable:
        raise PaintPostDrawError(
            f"{label} capture is missing a verified frame rectangle."
        )
    if capture.source_width <= 0 or capture.source_height <= 0:
        raise PaintPostDrawError(
            f"{label} capture is missing full-resolution source dimensions."
        )
    if not _rect_inside(capture.frame_rect, canvas.rect):
        raise PaintPostDrawError(
            f"Paint canvas is not fully represented by the {label} capture."
        )


def _crop_capture(
    capture: _CaptureLike,
    rect: WindowRect,
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    frame = capture.frame_rect
    if frame is None:  # defended above; keeps this helper standalone.
        raise PaintPostDrawError("Capture has no frame rectangle.")

    scale_x = capture.image.width / frame.width
    scale_y = capture.image.height / frame.height

    left = math.floor((rect.left - frame.left) * scale_x)
    top = math.floor((rect.top - frame.top) * scale_y)
    right = math.ceil((rect.right - frame.left) * scale_x)
    bottom = math.ceil((rect.bottom - frame.top) * scale_y)

    left = max(0, min(capture.image.width - 1, left))
    top = max(0, min(capture.image.height - 1, top))
    right = max(left + 1, min(capture.image.width, right))
    bottom = max(top + 1, min(capture.image.height, bottom))

    try:
        with Image.open(BytesIO(capture.image.data)) as opened:
            box = (left, top, right, bottom)
            return opened.convert("RGB").crop(box), box
    except OSError as exc:
        raise PaintPostDrawError(f"Could not decode {capture.image.format} capture: {exc}") from exc


def _new_dark_strokes(
    before: Image.Image,
    after: Image.Image,
    *,
    min_darkening: int,
) -> np.ndarray:
    before_arr = np.asarray(before.convert("RGB"), dtype=np.int16)
    after_arr = np.asarray(after.convert("RGB"), dtype=np.int16)
    if before_arr.shape != after_arr.shape:
        raise PaintPostDrawError("Before/after canvas arrays do not have the same shape.")

    # The Stage 4B.3L+ reference pipeline is black/dark line art. Measuring
    # darkening rather than arbitrary RGB change ignores harmless hover/cursor
    # disappearance and pre-existing unchanged content.
    weights = np.asarray((0.299, 0.587, 0.114), dtype=np.float32)
    before_luma = before_arr.astype(np.float32) @ weights
    after_luma = after_arr.astype(np.float32) @ weights
    return (before_luma - after_luma) >= float(min_darkening)


def _render_expected_mask(
    approved_sketch: VisualApprovedSketch,
    *,
    capture: _CaptureLike,
    crop_box: tuple[int, int, int, int],
    canvas_screen_rect: WindowRect,
    inset_pixels: int,
    line_width: int,
) -> np.ndarray:
    frame = capture.frame_rect
    if frame is None:
        raise PaintPostDrawError("Capture has no frame rectangle.")
    left, top, right, bottom = crop_box
    width = right - left
    height = bottom - top
    if width <= 1 or height <= 1:
        raise PaintPostDrawError("Canvas crop is too small to verify.")

    # Project in the *same screen coordinate system used for the real mouse
    # side effect*, then apply the capture's exact resize factors. This matters
    # because vision_image rounds the resized width/height independently to
    # model patches, so a square screen canvas can become a slightly rectangular
    # capture crop. Re-projecting directly into the crop would hide that fact.
    try:
        screen_strokes = approved_sketch.plan.to_screen_strokes(
            canvas_screen_rect,
            inset_pixels=inset_pixels,
            preserve_aspect=True,
        )
    except Exception as exc:
        raise PaintPostDrawError(f"Could not render expected Paint geometry: {exc}") from exc

    scale_x = capture.image.width / frame.width
    scale_y = capture.image.height / frame.height

    local_strokes: list[tuple[tuple[int, int], ...]] = []
    for stroke in screen_strokes:
        local_points = []
        for screen_x, screen_y in stroke:
            image_x = (screen_x - frame.left) * scale_x
            image_y = (screen_y - frame.top) * scale_y
            local_x = round(image_x - left)
            local_y = round(image_y - top)
            local_x = min(width - 1, max(0, local_x))
            local_y = min(height - 1, max(0, local_y))
            local_points.append((local_x, local_y))
        local_strokes.append(tuple(local_points))

    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    for points in local_strokes:
        draw.line(points, fill=255, width=line_width, joint="curve")
    return np.asarray(image, dtype=np.uint8) > 0


def _fidelity_metrics(
    expected: np.ndarray,
    observed: np.ndarray,
    *,
    radius: int,
) -> tuple[float, float, float]:
    expected_pixels = int(expected.sum())
    observed_pixels = int(observed.sum())
    if expected_pixels <= 0:
        return 0.0, 0.0, 0.0
    if observed_pixels <= 0:
        return 0.0, 0.0, 0.0

    expected_dilated = _dilate(expected, radius)
    observed_dilated = _dilate(observed, radius)

    coverage = float(np.logical_and(expected, observed_dilated).sum()) / expected_pixels
    precision = float(np.logical_and(observed, expected_dilated).sum()) / observed_pixels
    if coverage + precision <= 0.0:
        f1 = 0.0
    else:
        f1 = 2.0 * coverage * precision / (coverage + precision)
    return coverage, precision, f1


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return mask.copy()
    size = radius * 2 + 1
    image = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    expanded = image.filter(ImageFilter.MaxFilter(size=size))
    return np.asarray(expanded, dtype=np.uint8) > 0


def _mask_to_prepared(mask: np.ndarray) -> PreparedImage:
    image = Image.fromarray(np.where(mask, 0, 255).astype(np.uint8), mode="L").convert("RGB")
    return _pil_to_prepared(image)


def _pil_to_prepared(image: Image.Image) -> PreparedImage:
    rgb = image.convert("RGB")
    buffer = BytesIO()
    rgb.save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()
    return PreparedImage(
        data=data,
        width=rgb.width,
        height=rgb.height,
        format="png",
    )


def _rect_inside(outer: WindowRect, inner: WindowRect) -> bool:
    return (
        inner.usable
        and outer.left <= inner.left
        and outer.top <= inner.top
        and inner.right <= outer.right
        and inner.bottom <= outer.bottom
    )
