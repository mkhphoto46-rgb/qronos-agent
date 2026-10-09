"""A bound window whose picture a test controls, for D3/D4 freshness fixtures.

``StillWindowCapture`` stands in for ``ScreenCapture.capture_bound``: it
returns a real ``Capture`` (a real PNG of a pixel array the test owns, with
the binding's tokens, the exact frame rectangle and the window's current
input epoch). Tests change ``pixels`` to simulate a scroll, a dialog or any
change Qronos did not cause. Nothing reads a real screen.
"""

from __future__ import annotations

from dataclasses import replace
from io import BytesIO
import time

import numpy as np
from PIL import Image

from core.grounding_freshness import region_fingerprint, state_epoch
from core.screen_capture import Capture, DisplayGeometry
from core.vision_grounding import GroundedTarget, target_fingerprint, viewport_fingerprint
from core.vision_image import PreparedImage


def textured(width: int, height: int) -> np.ndarray:
    """A deterministic picture with distinct content everywhere."""
    y, x = np.mgrid[0:height, 0:width]
    picture = np.zeros((height, width, 3), dtype=np.uint8)
    picture[..., 0] = (x * 7 + y * 3) % 256
    picture[..., 1] = (x * 3 + y * 11) % 256
    picture[..., 2] = ((x // 8 + y // 8) % 2) * 200
    return picture


def png(pixels: np.ndarray) -> bytes:
    buffer = BytesIO()
    Image.fromarray(pixels, "RGB").save(buffer, format="PNG")
    return buffer.getvalue()


class StillWindowCapture:
    def __init__(self, pixels: np.ndarray | None = None) -> None:
        self.pixels = pixels
        self.calls: list[dict] = []

    def capture_bound(self, binding, binder, **kwargs):
        self.calls.append(kwargs)
        rect = binding.snapshot.window_rect
        if self.pixels is None:
            self.pixels = textured(rect.width, rect.height)
        assert self.pixels.shape[:2] == (rect.height, rect.width)
        return Capture(
            image=PreparedImage(data=png(self.pixels), width=rect.width, height=rect.height, format="png"),
            geometry=DisplayGeometry(1920, 1080, 1920, 1080),
            window=binding.hwnd,
            binding_token=binding.identity_token,
            layout_token=binding.layout_token,
            source_width=rect.width,
            source_height=rect.height,
            frame_rect=rect,
            captured_at=time.monotonic(),
            state_epoch=state_epoch(binding.identity_token),
        )


def as_seen_in(target: GroundedTarget, capture) -> GroundedTarget:
    """What a real grounder returns: the target with the frame's provenance and pixels."""
    return replace(
        target,
        point=replace(
            target.point,
            captured_at=capture.captured_at,
            state_epoch=capture.state_epoch,
            capture_sha256=capture.pixel_sha256,
        ),
        fingerprint=target_fingerprint(capture, target.screen_box),
        context=viewport_fingerprint(capture),
    )


def canvas_from(binding, rect, capture):
    """A canvas capability grounded from ``capture`` (as ground_paint_canvas makes it)."""
    from core.paint_adapter import PaintCanvasRegion

    return PaintCanvasRegion.verified(
        binding,
        rect,
        captured_at=capture.captured_at,
        state_epoch=capture.state_epoch,
        fingerprint=region_fingerprint(capture, rect, max_side=None),
    )
