from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json

from core.shape_planner import ShapePlan
from core.vision_image import PreparedImage
from core.window_binding import WindowRect


PREVIEW_WIDTH = 960
PREVIEW_HEIGHT = 720
PREVIEW_MARGIN = 56
PREVIEW_LINE_WIDTH = 4


@dataclass(frozen=True)
class SketchPreview:
    image: PreparedImage
    plan_digest: str
    image_digest: str


def digest_shape_plan(plan: ShapePlan) -> str:
    payload = {
        "name": plan.name,
        "strokes": [
            [[point.x, point.y] for point in stroke.points]
            for stroke in plan.strokes
        ],
    }
    encoded = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SketchPreviewRenderer:
    """Deterministically rasterize a ShapePlan before any GUI side effect."""

    def __init__(
        self,
        *,
        width: int = PREVIEW_WIDTH,
        height: int = PREVIEW_HEIGHT,
        margin: int = PREVIEW_MARGIN,
        line_width: int = PREVIEW_LINE_WIDTH,
    ) -> None:
        if width < 320 or height < 240:
            raise ValueError("Preview must be at least 320x240.")
        if margin < 8 or margin * 2 >= min(width, height):
            raise ValueError("Preview margin is invalid.")
        if line_width < 1 or line_width > 16:
            raise ValueError("Preview line width must be 1..16.")
        self.width = int(width)
        self.height = int(height)
        self.margin = int(margin)
        self.line_width = int(line_width)

    def render(self, plan: ShapePlan) -> SketchPreview:
        try:
            from PIL import Image, ImageDraw
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise RuntimeError(
                "Sketch preview rendering requires Pillow, which is already a Qronos dependency."
            ) from exc

        image = Image.new("RGB", (self.width, self.height), "white")
        draw = ImageDraw.Draw(image)
        rect = WindowRect(
            left=self.margin,
            top=self.margin,
            right=self.width - self.margin,
            bottom=self.height - self.margin,
        )
        strokes = plan.to_screen_strokes(rect, preserve_aspect=True)
        for points in strokes:
            draw.line(points, fill="black", width=self.line_width, joint="curve")

        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()
        prepared = PreparedImage(
            data=data,
            width=self.width,
            height=self.height,
            format="png",
        )
        return SketchPreview(
            image=prepared,
            plan_digest=digest_shape_plan(plan),
            image_digest=hashlib.sha256(data).hexdigest(),
        )
