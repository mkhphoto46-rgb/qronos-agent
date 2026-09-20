from __future__ import annotations

from io import BytesIO

from PIL import Image

from core.shape_planner import ShapePlanner
from core.sketch_preview import SketchPreviewRenderer, digest_shape_plan


def test_preview_is_deterministic_png_and_not_blank():
    plan = ShapePlanner.rectangle(margin=0.2)
    renderer = SketchPreviewRenderer()

    one = renderer.render(plan)
    two = renderer.render(plan)

    assert one.image.data.startswith(b"\x89PNG")
    assert one.plan_digest == two.plan_digest == digest_shape_plan(plan)
    assert one.image_digest == two.image_digest

    image = Image.open(BytesIO(one.image.data)).convert("L")
    extrema = image.getextrema()
    assert extrema[0] < 255
    assert extrema[1] == 255


def test_different_plans_have_different_plan_digests():
    assert digest_shape_plan(ShapePlanner.line()) != digest_shape_plan(
        ShapePlanner.rectangle()
    )


def test_preview_preserves_unit_square_aspect_on_rectangular_image():
    plan = ShapePlanner.rectangle(margin=0.2)
    preview = SketchPreviewRenderer(width=960, height=720, margin=56).render(plan)

    image = Image.open(BytesIO(preview.image.data)).convert("L")
    pixels = image.load()
    xs = []
    ys = []
    for y in range(image.height):
        for x in range(image.width):
            if pixels[x, y] < 128:
                xs.append(x)
                ys.append(y)

    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    assert abs(width - height) <= 2
