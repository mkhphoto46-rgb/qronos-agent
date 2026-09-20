from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from io import BytesIO
import math
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image

from core.shape_planner import (
    MAX_POINTS_PER_STROKE,
    MAX_STROKES,
    MAX_TOTAL_POINTS,
    ShapePlan,
    ShapePlanError,
)
from core.vision_image import PreparedImage, as_prepared


MAX_WORKING_EDGE = 768
MIN_WORKING_EDGE = 96
MIN_CONTRAST = 28
MIN_FOREGROUND_PIXELS = 24
MAX_FOREGROUND_FRACTION = 0.30
MIN_COMPONENT_PIXELS = 10
MIN_STROKE_PIXELS = 2.0
DEFAULT_SIMPLIFY_EPSILON = 1.6
MAX_SKELETON_ITERATIONS = 192
NORMALIZED_MARGIN = 0.06


class ReferenceVectorizationError(ValueError):
    """A reference image cannot be safely reduced to bounded pointer strokes."""


@dataclass(frozen=True)
class ReferenceVectorization:
    plan: ShapePlan
    source_width: int
    source_height: int
    working_width: int
    working_height: int
    foreground_pixels: int
    skeleton_pixels: int
    stroke_count: int
    total_points: int


class ReferenceStrokeVectorizer:
    """
    Convert sparse line art into a bounded, application-independent ShapePlan.

    This is deliberately not a general photo tracer.  It accepts simple line art
    on a mostly uniform background, removes tiny isolated noise, thins the marks
    to one-pixel paths, traces those paths, simplifies them, and finally maps the
    result into normalized 0..1 coordinates.  The output contains no HWND, Paint,
    mouse, or screen coordinates.
    """

    def __init__(
        self,
        *,
        max_working_edge: int = MAX_WORKING_EDGE,
        min_contrast: int = MIN_CONTRAST,
        min_component_pixels: int = MIN_COMPONENT_PIXELS,
        min_stroke_pixels: float = MIN_STROKE_PIXELS,
        simplify_epsilon: float = DEFAULT_SIMPLIFY_EPSILON,
        normalized_margin: float = NORMALIZED_MARGIN,
    ) -> None:
        if not (MIN_WORKING_EDGE <= max_working_edge <= 1536):
            raise ValueError("max_working_edge must be between 96 and 1536 pixels.")
        if not (8 <= min_contrast <= 96):
            raise ValueError("min_contrast must be between 8 and 96.")
        if not (2 <= min_component_pixels <= 512):
            raise ValueError("min_component_pixels must be between 2 and 512.")
        if not (1.0 <= min_stroke_pixels <= 128.0):
            raise ValueError("min_stroke_pixels must be between 1 and 128 pixels.")
        if not (0.25 <= simplify_epsilon <= 12.0):
            raise ValueError("simplify_epsilon must be between 0.25 and 12 pixels.")
        if not (0.0 <= normalized_margin < 0.25):
            raise ValueError("normalized_margin must be in [0.0, 0.25).")
        self.max_working_edge = int(max_working_edge)
        self.min_contrast = int(min_contrast)
        self.min_component_pixels = int(min_component_pixels)
        self.min_stroke_pixels = float(min_stroke_pixels)
        self.simplify_epsilon = float(simplify_epsilon)
        self.normalized_margin = float(normalized_margin)

    def vectorize(
        self,
        image: str | Path | PreparedImage,
        *,
        name: str = "reference-trace",
    ) -> ReferenceVectorization:
        prepared = as_prepared(image, long_edge=None)
        gray, source_width, source_height = _decode_grayscale(prepared)
        working = _resize_for_work(gray, self.max_working_edge)

        mask = _foreground_mask(working, min_contrast=self.min_contrast)
        foreground_pixels = int(mask.sum())
        total_pixels = int(mask.size)
        if foreground_pixels < MIN_FOREGROUND_PIXELS:
            raise ReferenceVectorizationError(
                "Reference contains too little visible line work to trace."
            )
        fraction = foreground_pixels / total_pixels
        if fraction > MAX_FOREGROUND_FRACTION:
            raise ReferenceVectorizationError(
                "Reference is too visually dense for the line-art tracer; "
                "provide sparse black-line art on a plain background."
            )

        cleaned = _remove_small_components(mask, self.min_component_pixels)
        cleaned_pixels = int(cleaned.sum())
        if cleaned_pixels < MIN_FOREGROUND_PIXELS:
            raise ReferenceVectorizationError(
                "Only tiny isolated marks remained after line-art cleanup."
            )

        skeleton = _zhang_suen(cleaned)
        skeleton_pixels = int(skeleton.sum())
        if skeleton_pixels < MIN_FOREGROUND_PIXELS:
            raise ReferenceVectorizationError("Reference skeleton is too small to trace.")

        raw_paths = _trace_skeleton(skeleton)
        paths = _prepare_paths(
            raw_paths,
            min_stroke_pixels=self.min_stroke_pixels,
            simplify_epsilon=self.simplify_epsilon,
        )
        if not paths:
            raise ReferenceVectorizationError("No usable continuous strokes were found.")

        # Try progressively stronger simplification before refusing an overly
        # detailed reference.  Nothing is silently truncated: if bounded limits
        # still cannot be met, the image is rejected.
        eps = self.simplify_epsilon
        for _ in range(4):
            simplified = [
                _simplify_path(path, eps, closed=_is_closed(path))
                for path in paths
            ]
            simplified = [path for path in simplified if len(path) >= 2]
            if _within_shape_limits(simplified):
                paths = simplified
                break
            eps *= 1.75
        else:
            raise ReferenceVectorizationError(
                "Reference is too complex for bounded pointer execution."
            )

        normalized = _normalize_paths(
            paths,
            working_width=working.shape[1],
            working_height=working.shape[0],
            margin=self.normalized_margin,
        )
        try:
            plan = ShapePlan.from_strokes(normalized, name=name)
        except ShapePlanError as exc:
            raise ReferenceVectorizationError(str(exc)) from exc

        total_points = sum(len(stroke.points) for stroke in plan.strokes)
        return ReferenceVectorization(
            plan=plan,
            source_width=source_width,
            source_height=source_height,
            working_width=int(working.shape[1]),
            working_height=int(working.shape[0]),
            foreground_pixels=cleaned_pixels,
            skeleton_pixels=skeleton_pixels,
            stroke_count=len(plan.strokes),
            total_points=total_points,
        )


def _decode_grayscale(image: PreparedImage) -> tuple[np.ndarray, int, int]:
    try:
        with Image.open(BytesIO(image.data)) as opened:
            rgba = opened.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            composited = Image.alpha_composite(background, rgba).convert("L")
            array = np.asarray(composited, dtype=np.uint8)
    except OSError as exc:
        raise ReferenceVectorizationError(f"Reference image could not be decoded: {exc}") from exc
    if array.ndim != 2 or array.shape[0] < 2 or array.shape[1] < 2:
        raise ReferenceVectorizationError("Reference image dimensions are unusable.")
    return array, image.width, image.height


def _resize_for_work(gray: np.ndarray, max_edge: int) -> np.ndarray:
    height, width = gray.shape
    if max(width, height) <= max_edge:
        return gray.copy()
    scale = max_edge / max(width, height)
    new_width = max(1, round(width * scale))
    new_height = max(1, round(height * scale))
    image = Image.fromarray(gray, mode="L")
    resized = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
    return np.asarray(resized, dtype=np.uint8)


def _foreground_mask(gray: np.ndarray, *, min_contrast: int) -> np.ndarray:
    # Estimate background from the outer border.  This supports both black-on-
    # white and white-on-black line art without trusting a filename or prompt.
    border = np.concatenate(
        (
            gray[0, :],
            gray[-1, :],
            gray[:, 0],
            gray[:, -1],
        )
    )
    background = float(np.median(border))
    distance = np.abs(gray.astype(np.int16) - int(round(background)))
    return distance >= min_contrast


def _remove_small_components(mask: np.ndarray, minimum: int) -> np.ndarray:
    height, width = mask.shape
    seen = np.zeros(mask.shape, dtype=bool)
    output = np.zeros(mask.shape, dtype=bool)
    for y, x in zip(*np.nonzero(mask)):
        if seen[y, x]:
            continue
        queue = deque([(int(y), int(x))])
        seen[y, x] = True
        component: list[tuple[int, int]] = []
        while queue:
            cy, cx = queue.popleft()
            component.append((cy, cx))
            for ny in range(max(0, cy - 1), min(height, cy + 2)):
                for nx in range(max(0, cx - 1), min(width, cx + 2)):
                    if ny == cy and nx == cx:
                        continue
                    if mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        queue.append((ny, nx))
        if len(component) >= minimum:
            ys, xs = zip(*component)
            output[np.asarray(ys), np.asarray(xs)] = True
    return output


def _neighbors(binary: np.ndarray) -> tuple[np.ndarray, ...]:
    p = np.pad(binary.astype(np.uint8), 1, mode="constant")
    return (
        p[:-2, 1:-1],   # p2 north
        p[:-2, 2:],     # p3 north-east
        p[1:-1, 2:],    # p4 east
        p[2:, 2:],      # p5 south-east
        p[2:, 1:-1],    # p6 south
        p[2:, :-2],     # p7 south-west
        p[1:-1, :-2],   # p8 west
        p[:-2, :-2],    # p9 north-west
    )


def _zhang_suen(mask: np.ndarray) -> np.ndarray:
    image = mask.astype(np.uint8).copy()
    for _ in range(MAX_SKELETON_ITERATIONS):
        changed = False
        for phase in (0, 1):
            p2, p3, p4, p5, p6, p7, p8, p9 = _neighbors(image)
            neighbours = (p2, p3, p4, p5, p6, p7, p8, p9)
            count = sum(neighbours)
            transitions = sum(
                ((neighbours[i] == 0) & (neighbours[(i + 1) % 8] == 1))
                for i in range(8)
            )
            condition = (
                (image == 1)
                & (count >= 2)
                & (count <= 6)
                & (transitions == 1)
            )
            if phase == 0:
                condition &= (p2 * p4 * p6 == 0) & (p4 * p6 * p8 == 0)
            else:
                condition &= (p2 * p4 * p8 == 0) & (p2 * p6 * p8 == 0)
            if np.any(condition):
                image[condition] = 0
                changed = True
        if not changed:
            return image.astype(bool)
    raise ReferenceVectorizationError(
        "Line-art skeletonization did not converge within the bounded iteration limit."
    )


def _trace_skeleton(skeleton: np.ndarray) -> list[list[tuple[float, float]]]:
    pixels = {(int(y), int(x)) for y, x in zip(*np.nonzero(skeleton))}
    if not pixels:
        return []

    offsets = (
        (-1, -1), (-1, 0), (-1, 1),
        (0, -1),            (0, 1),
        (1, -1),  (1, 0),   (1, 1),
    )

    def neighbours(pixel: tuple[int, int]) -> list[tuple[int, int]]:
        y, x = pixel
        result: list[tuple[int, int]] = []
        for dy, dx in offsets:
            candidate = (y + dy, x + dx)
            if candidate not in pixels:
                continue

            # A thinned raster often contains tiny 2x2 / staircase patterns.
            # Treating every diagonal in those blocks as a graph edge creates
            # artificial degree-3/4 junctions and shatters one visible stroke
            # into many two-pixel paths. Keep a diagonal edge only when it is
            # the sole local connection between the two pixels. If either
            # orthogonal bridge exists, the orthogonal path already preserves
            # connectivity and the diagonal is redundant. True diagonal lines
            # (whose orthogonal bridge pixels are absent) remain connected.
            if dy != 0 and dx != 0:
                horizontal_bridge = (y, x + dx)
                vertical_bridge = (y + dy, x)
                if horizontal_bridge in pixels or vertical_bridge in pixels:
                    continue

            result.append(candidate)
        return result

    neighbour_map = {pixel: neighbours(pixel) for pixel in pixels}
    special = {pixel for pixel, near in neighbour_map.items() if len(near) != 2}
    visited_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    paths: list[list[tuple[float, float]]] = []

    def edge(a: tuple[int, int], b: tuple[int, int]):
        return (a, b) if a <= b else (b, a)

    def walk(start: tuple[int, int], nxt: tuple[int, int]) -> list[tuple[int, int]]:
        path = [start, nxt]
        visited_edges.add(edge(start, nxt))
        previous = start
        current = nxt
        while current not in special:
            options = [n for n in neighbour_map[current] if n != previous]
            if not options:
                break
            candidate = options[0]
            if edge(current, candidate) in visited_edges:
                break
            path.append(candidate)
            visited_edges.add(edge(current, candidate))
            previous, current = current, candidate
        return path

    for start in sorted(special):
        for nxt in sorted(neighbour_map[start]):
            if edge(start, nxt) not in visited_edges:
                path = walk(start, nxt)
                if len(path) >= 2:
                    paths.append([(float(x), float(y)) for y, x in path])

    # Remaining unvisited edges belong to closed loops with degree==2 everywhere.
    for start in sorted(pixels):
        for first in sorted(neighbour_map[start]):
            if edge(start, first) in visited_edges:
                continue
            loop = [start, first]
            visited_edges.add(edge(start, first))
            previous, current = start, first
            while True:
                options = [n for n in neighbour_map[current] if n != previous]
                if not options:
                    break
                candidate = options[0]
                e = edge(current, candidate)
                if candidate == start:
                    loop.append(start)
                    visited_edges.add(e)
                    break
                if e in visited_edges:
                    break
                loop.append(candidate)
                visited_edges.add(e)
                previous, current = current, candidate
            if len(loop) >= 3:
                paths.append([(float(x), float(y)) for y, x in loop])

    return paths


def _prepare_paths(
    paths: Iterable[list[tuple[float, float]]],
    *,
    min_stroke_pixels: float,
    simplify_epsilon: float,
) -> list[list[tuple[float, float]]]:
    prepared: list[list[tuple[float, float]]] = []
    for path in paths:
        if len(path) < 2 or _path_length(path) < min_stroke_pixels:
            continue
        simplified = _simplify_path(path, simplify_epsilon, closed=_is_closed(path))
        if len(simplified) >= 2 and _path_length(simplified) >= min_stroke_pixels:
            prepared.append(simplified)
    prepared.sort(key=_path_length, reverse=True)
    return prepared


def _within_shape_limits(paths: Iterable[list[tuple[float, float]]]) -> bool:
    paths = list(paths)
    return (
        len(paths) <= MAX_STROKES
        and all(2 <= len(path) <= MAX_POINTS_PER_STROKE for path in paths)
        and sum(len(path) for path in paths) <= MAX_TOTAL_POINTS
    )


def _normalize_paths(
    paths: list[list[tuple[float, float]]],
    *,
    working_width: int,
    working_height: int,
    margin: float,
) -> tuple[tuple[tuple[float, float], ...], ...]:
    all_points = [point for path in paths for point in path]
    min_x = min(x for x, _ in all_points)
    max_x = max(x for x, _ in all_points)
    min_y = min(y for _, y in all_points)
    max_y = max(y for _, y in all_points)
    width = max(max_x - min_x, 1.0)
    height = max(max_y - min_y, 1.0)

    # Isotropic scaling avoids turning a circle into a shape with a different
    # aspect before the application adapter even sees it.
    usable = 1.0 - 2.0 * margin
    scale = usable / max(width, height)
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0

    normalized = []
    for path in paths:
        converted = []
        for x, y in path:
            nx = 0.5 + (x - center_x) * scale
            ny = 0.5 + (y - center_y) * scale
            # Floating-point guard only; geometry outside this interval would
            # indicate a bug in the normalization itself.
            nx = min(1.0, max(0.0, nx))
            ny = min(1.0, max(0.0, ny))
            converted.append((nx, ny))
        normalized.append(tuple(converted))
    return tuple(normalized)


def _path_length(points: list[tuple[float, float]]) -> float:
    return sum(
        math.hypot(x2 - x1, y2 - y1)
        for (x1, y1), (x2, y2) in zip(points, points[1:])
    )


def _is_closed(points: list[tuple[float, float]]) -> bool:
    if len(points) < 3:
        return False
    return math.hypot(points[0][0] - points[-1][0], points[0][1] - points[-1][1]) <= 1.5


def _simplify_path(
    points: list[tuple[float, float]],
    epsilon: float,
    *,
    closed: bool,
) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return list(points)
    work = list(points)
    if closed and work[0] == work[-1]:
        work = work[:-1]
    if len(work) <= 2:
        result = work
    else:
        result = _rdp(work, epsilon)
    if closed and result:
        if result[0] != result[-1]:
            result.append(result[0])
    return result


def _rdp(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return points
    start = points[0]
    end = points[-1]
    max_distance = -1.0
    index = -1
    for i, point in enumerate(points[1:-1], start=1):
        distance = _distance_to_segment(point, start, end)
        if distance > max_distance:
            max_distance = distance
            index = i
    if max_distance > epsilon and index > 0:
        left = _rdp(points[: index + 1], epsilon)
        right = _rdp(points[index:], epsilon)
        return left[:-1] + right
    return [start, end]


def _distance_to_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = min(1.0, max(0.0, t))
    qx = x1 + t * dx
    qy = y1 + t * dy
    return math.hypot(px - qx, py - qy)
