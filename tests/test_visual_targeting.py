from __future__ import annotations

import json
import unittest

from core.screen_capture import Capture, DisplayGeometry
from core.visual_targeting import (
    VisualTargetAmbiguous,
    VisualTargetInvalid,
    VisualTargetLocator,
    VisualTargetLowConfidence,
    VisualTargetNotFound,
)
from core.vision_image import PreparedImage
from core.window_binding import (
    WindowBinder,
    WindowRect,
    WindowSnapshot,
    WindowTarget,
)


def snapshot():
    return WindowSnapshot(
        hwnd=77,
        pid=501,
        thread_id=8,
        title="Example Application",
        process_path=r"C:\Example\example.exe",
        process_start_time_100ns=123456789,
        window_rect=WindowRect(100, 50, 1700, 950),
        client_rect=WindowRect(108, 80, 1692, 942),
        dpi=96,
        visible=True,
        minimized=False,
        foreground=True,
    )


def binding_and_capture():
    current = snapshot()

    binder = WindowBinder(
        enumerate_fn=lambda: (current,),
        snapshot_fn=lambda hwnd: current,
    )

    binding = binder.bind(
        WindowTarget(
            app_id="example",
            display_name="Example Application",
            executable="example.exe",
            process_names=("example.exe",),
        ),
        require_foreground=True,
    )

    capture = Capture(
        image=PreparedImage(
            data=b"png",
            width=1280,
            height=720,
            format="png",
        ),
        geometry=DisplayGeometry(
            physical_width=1920,
            physical_height=1080,
            logical_width=1920,
            logical_height=1080,
        ),
        window=binding.hwnd,
        binding_token=binding.identity_token,
        layout_token=binding.layout_token,
        source_width=1600,
        source_height=900,
        frame_rect=WindowRect(100, 50, 1700, 950),
    )

    return binder, binding, capture


def response(
    *,
    status="found",
    image_width=1280,
    image_height=720,
    confidence=0.97,
    x=640,
    y=360,
    candidates=None,
):
    if candidates is None:
        candidates = [
            {
                "label": "requested control",
                "confidence": confidence,
                "point": {
                    "x": x,
                    "y": y,
                },
            }
        ]

    return json.dumps(
        {
            "status": status,
            "image_width": image_width,
            "image_height": image_height,
            "candidates": candidates,
        }
    )


class FakeRuntime:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []
        self.stopped = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.raw

    def stop_model(self, name):
        self.stopped.append(name)


class TestGenericVisualTargeting(unittest.TestCase):
    def test_generic_target_maps_to_verified_screen_point(self):
        _, binding, capture = binding_and_capture()

        runtime = FakeRuntime(response())

        target = VisualTargetLocator(runtime).locate(
            "the search icon",
            capture,
        )

        self.assertEqual(target.hwnd, binding.hwnd)
        self.assertEqual(
            (target.screen_x, target.screen_y),
            (900, 500),
        )
        self.assertAlmostEqual(target.confidence, 0.97)

    def test_schema_is_locked_to_exact_image_dimensions(self):
        _, _, capture = binding_and_capture()

        runtime = FakeRuntime(response())

        VisualTargetLocator(runtime).locate(
            "the send button",
            capture,
        )

        schema = runtime.calls[0]["response_format"]

        self.assertEqual(
            schema["properties"]["image_width"]["enum"],
            [1280],
        )
        self.assertEqual(
            schema["properties"]["image_height"]["enum"],
            [720],
        )

        point = (
            schema["properties"]["candidates"]
            ["items"]["properties"]["point"]["properties"]
        )

        self.assertEqual(point["x"]["maximum"], 1279)
        self.assertEqual(point["y"]["maximum"], 719)

    def test_dimension_mismatch_fails_closed(self):
        _, _, capture = binding_and_capture()

        runtime = FakeRuntime(
            response(image_width=1152)
        )

        with self.assertRaises(VisualTargetInvalid):
            VisualTargetLocator(runtime).locate(
                "the settings icon",
                capture,
            )

    def test_out_of_bounds_point_fails_closed(self):
        _, _, capture = binding_and_capture()

        runtime = FakeRuntime(
            response(x=1280)
        )

        with self.assertRaises(VisualTargetInvalid):
            VisualTargetLocator(runtime).locate(
                "the icon",
                capture,
            )

    def test_low_confidence_target_is_not_actionable(self):
        _, _, capture = binding_and_capture()

        runtime = FakeRuntime(
            response(confidence=0.50)
        )

        with self.assertRaises(VisualTargetLowConfidence):
            VisualTargetLocator(runtime).locate(
                "the icon",
                capture,
            )

    def test_not_found_is_not_a_fake_coordinate(self):
        _, _, capture = binding_and_capture()

        runtime = FakeRuntime(
            response(
                status="not_found",
                candidates=[],
            )
        )

        with self.assertRaises(VisualTargetNotFound):
            VisualTargetLocator(runtime).locate(
                "something not visible",
                capture,
            )

    def test_ambiguous_target_is_not_guessed(self):
        _, _, capture = binding_and_capture()

        candidates = [
            {
                "label": "first",
                "confidence": 0.97,
                "point": {"x": 200, "y": 200},
            },
            {
                "label": "second",
                "confidence": 0.96,
                "point": {"x": 900, "y": 200},
            },
        ]

        runtime = FakeRuntime(
            response(
                status="ambiguous",
                candidates=candidates,
            )
        )

        with self.assertRaises(VisualTargetAmbiguous):
            VisualTargetLocator(runtime).locate(
                "one of the similar buttons",
                capture,
            )

    def test_unbound_capture_is_rejected_before_model_call(self):
        capture = Capture(
            image=PreparedImage(
                data=b"png",
                width=1280,
                height=720,
                format="png",
            ),
            geometry=DisplayGeometry(
                physical_width=1920,
                physical_height=1080,
                logical_width=1920,
                logical_height=1080,
            ),
            source_width=1920,
            source_height=1080,
        )

        runtime = FakeRuntime(response())

        with self.assertRaises(Exception):
            VisualTargetLocator(runtime).locate(
                "the icon",
                capture,
            )

        self.assertEqual(runtime.calls, [])


if __name__ == "__main__":
    unittest.main()
