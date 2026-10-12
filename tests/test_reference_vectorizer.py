from __future__ import annotations

from io import BytesIO

import numpy as np
import pytest
from PIL import Image, ImageDraw

from core.reference_vectorizer import (
    ReferenceStrokeVectorizer,
    ReferenceVectorizationError,
)
from core.sketch_preview import SketchPreviewRenderer
from core.vision_image import prepare_bytes


def _png(draw_fn, *, size=(360, 280)):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw_fn(draw)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return prepare_bytes(buffer.getvalue(), long_edge=None)


def test_vectorizes_arbitrary_line_art_without_shape_templates():
    reference = _png(
        lambda d: (
            d.ellipse((145, 30, 215, 100), outline="black", width=5),
            d.line((180, 100, 180, 205), fill="black", width=5),
            d.line((180, 125, 110, 165), fill="black", width=5),
            d.line((180, 125, 245, 65), fill="black", width=5),
            d.line((180, 205, 125, 255), fill="black", width=5),
            d.line((180, 205, 235, 255), fill="black", width=5),
        )
    )
    result = ReferenceStrokeVectorizer().vectorize(reference)

    assert result.stroke_count >= 5
    assert result.total_points >= result.stroke_count * 2
    assert all(
        0.0 <= point.x <= 1.0 and 0.0 <= point.y <= 1.0
        for stroke in result.plan.strokes
        for point in stroke.points
    )
    preview = SketchPreviewRenderer().render(result.plan)
    assert preview.image.data.startswith(b"\x89PNG")


def test_tiny_noise_is_removed_before_tracing():
    def draw(d):
        d.rectangle((70, 55, 270, 220), outline="black", width=5)
        for x, y in ((10, 10), (20, 250), (345, 12)):
            d.point((x, y), fill="black")

    result = ReferenceStrokeVectorizer().vectorize(_png(draw))
    assert result.stroke_count >= 1
    assert result.foreground_pixels > 100


def test_dense_filled_image_is_rejected_instead_of_becoming_thousands_of_strokes():
    reference = _png(lambda d: d.rectangle((10, 10, 350, 270), fill="black"))
    with pytest.raises(ReferenceVectorizationError, match="too visually dense"):
        ReferenceStrokeVectorizer().vectorize(reference)


def test_blank_reference_is_rejected():
    reference = _png(lambda d: None)
    with pytest.raises(ReferenceVectorizationError, match="too little visible line work"):
        ReferenceStrokeVectorizer().vectorize(reference)


def test_dark_background_light_line_art_is_supported():
    image = Image.new("RGB", (320, 240), "black")
    draw = ImageDraw.Draw(image)
    draw.ellipse((90, 50, 230, 190), outline="white", width=6)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    reference = prepare_bytes(buffer.getvalue(), long_edge=None)

    result = ReferenceStrokeVectorizer().vectorize(reference)
    assert result.stroke_count >= 1


def test_vectorization_is_deterministic_for_same_reference():
    reference = _png(lambda d: d.line((40, 40, 320, 220), fill="black", width=5))
    vectorizer = ReferenceStrokeVectorizer()
    one = vectorizer.vectorize(reference)
    two = vectorizer.vectorize(reference)
    assert one.plan == two.plan
    assert one.stroke_count == two.stroke_count
    assert one.total_points == two.total_points


def test_complex_grid_is_rejected_instead_of_silently_truncated():
    def draw(d):
        for x in range(20, 341, 16):
            d.line((x, 20, x, 260), fill="black", width=3)
        for y in range(20, 261, 16):
            d.line((20, y, 340, y), fill="black", width=3)

    reference = _png(draw)
    with pytest.raises(ReferenceVectorizationError, match="too complex"):
        ReferenceStrokeVectorizer().vectorize(reference)


def test_topology_trace_keeps_simple_figure_coherent_instead_of_fragmenting_it():
    reference = _png(
        lambda d: (
            d.ellipse((145, 30, 215, 100), outline="black", width=5),
            d.line((180, 100, 180, 205), fill="black", width=5),
            d.line((180, 125, 110, 165), fill="black", width=5),
            d.line((180, 125, 245, 65), fill="black", width=5),
            d.line((180, 205, 125, 255), fill="black", width=5),
            d.line((180, 205, 235, 255), fill="black", width=5),
        )
    )

    result = ReferenceStrokeVectorizer().vectorize(reference)

    # A six-part stick figure should remain a small collection of coherent
    # paths. The old 8-neighbour graph created dozens of artificial branches
    # along diagonal raster staircases (typically 30+ strokes).
    assert 6 <= result.stroke_count <= 10
    assert any(
        stroke.points[0] == stroke.points[-1]
        for stroke in result.plan.strokes
        if len(stroke.points) >= 3
    )


def test_true_diagonal_line_remains_connected_after_redundant_diagonal_pruning():
    reference = _png(
        lambda d: d.line((40, 40, 320, 240), fill="black", width=5)
    )

    result = ReferenceStrokeVectorizer().vectorize(reference)

    assert result.stroke_count == 1
    assert len(result.plan.strokes[0].points) >= 2


def test_t_junction_remains_branched_without_exploding_into_micro_strokes():
    reference = _png(
        lambda d: (
            d.line((180, 45, 180, 235), fill="black", width=5),
            d.line((180, 130, 285, 130), fill="black", width=5),
        )
    )

    result = ReferenceStrokeVectorizer().vectorize(reference)

    assert 3 <= result.stroke_count <= 5
    assert result.total_points <= 20
