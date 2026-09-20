from __future__ import annotations

from dataclasses import replace
import json
import unittest

from core.screen_capture import Capture, DisplayGeometry
from core.vision_grounding import (
    GroundingAmbiguous,
    GroundingLowConfidence,
    GroundingNotFound,
    GroundingResponseInvalid,
    GroundingStale,
    GroundingUnavailable,
    ground_point,
    ground_structured_response,
    parse_grounding_response,
    revalidate_grounded_point,
    revalidate_grounded_target,
)
from core.vision_image import PreparedImage
from core.window_binding import (
    WindowBinder,
    WindowRect,
    WindowSnapshot,
    WindowTarget,
)


def snapshot(*, left=100, top=50, width=1600, height=900, foreground=True):
    return WindowSnapshot(
        hwnd=77,
        pid=501,
        thread_id=8,
        title="Untitled - Paint",
        process_path=r"C:\Program Files\WindowsApps\Paint\mspaint.exe",
        process_start_time_100ns=123456789,
        window_rect=WindowRect(left, top, left + width, top + height),
        client_rect=WindowRect(left + 8, top + 30, left + width - 8, top + height - 8),
        dpi=96,
        visible=True,
        minimized=False,
        foreground=foreground,
    )


def bound_capture(binding, *, model_width=1280, model_height=720):
    return Capture(
        image=PreparedImage(
            data=b"png",
            width=model_width,
            height=model_height,
            format="png",
            resized=(model_width != 1600),
        ),
        geometry=DisplayGeometry(1920, 1080, 1920, 1080),
        window=binding.hwnd,
        binding_token=binding.identity_token,
        layout_token=binding.layout_token,
        source_width=1600,
        source_height=900,
        frame_rect=WindowRect(100, 50, 1700, 950),
    )


def structured_response(
    *,
    status="found",
    image_width=1280,
    image_height=720,
    candidates=None,
):
    if candidates is None:
        candidates = [
            {
                "label": "canvas",
                "confidence": 0.93,
                "point": {"x": 640, "y": 360},
                "bbox": {"x": 400, "y": 200, "width": 480, "height": 320},
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


class TestVisionGrounding(unittest.TestCase):
    def setUp(self):
        self.current = {"value": snapshot()}
        self.binder = WindowBinder(
            enumerate_fn=lambda: (self.current["value"],),
            snapshot_fn=lambda hwnd: self.current["value"],
        )
        self.binding = self.binder.bind(
            WindowTarget(
                app_id="paint",
                display_name="Paint",
                executable="mspaint.exe",
                process_names=("mspaint.exe",),
            ),
            require_foreground=True,
        )

    def test_resized_model_coordinates_map_back_to_full_resolution_frame(self):
        capture = bound_capture(self.binding)
        point = ground_point(capture, image_x=640, image_y=360, label="canvas")
        self.assertEqual((point.screen_x, point.screen_y), (900, 500))
        self.assertEqual(point.binding_token, self.binding.identity_token)
        self.assertEqual(point.layout_token, self.binding.layout_token)

    def test_last_model_pixel_maps_inside_last_source_pixel(self):
        capture = bound_capture(self.binding)
        point = ground_point(capture, image_x=1279, image_y=719)
        self.assertEqual((point.screen_x, point.screen_y), (1698, 948))
        self.assertLess(point.screen_x, capture.frame_rect.right)
        self.assertLess(point.screen_y, capture.frame_rect.bottom)

    def test_unbound_capture_cannot_produce_coordinates(self):
        capture = replace(
            bound_capture(self.binding),
            binding_token="",
            layout_token="",
            frame_rect=None,
        )
        with self.assertRaises(GroundingUnavailable):
            ground_point(capture, image_x=10, image_y=10)

    def test_out_of_image_point_is_rejected(self):
        capture = bound_capture(self.binding)
        with self.assertRaises(ValueError):
            ground_point(capture, image_x=1280, image_y=0)

    def test_current_binding_revalidates_grounded_point(self):
        point = ground_point(bound_capture(self.binding), image_x=640, image_y=360)
        self.assertEqual(
            revalidate_grounded_point(point, self.binding, self.binder),
            (900, 500),
        )

    def test_move_or_resize_expires_grounded_coordinates(self):
        point = ground_point(bound_capture(self.binding), image_x=640, image_y=360)
        moved = snapshot(left=120, top=50)
        self.current["value"] = moved
        with self.assertRaisesRegex(GroundingStale, "coordinates expired"):
            revalidate_grounded_point(point, self.binding, self.binder)

    def test_focus_loss_expires_grounded_coordinates_when_required(self):
        point = ground_point(bound_capture(self.binding), image_x=640, image_y=360)
        self.current["value"] = replace(self.current["value"], foreground=False)
        with self.assertRaisesRegex(GroundingStale, "foreground"):
            revalidate_grounded_point(point, self.binding, self.binder)

    def test_token_mismatch_fails_before_binder_revalidation(self):
        point = replace(
            ground_point(bound_capture(self.binding), image_x=20, image_y=20),
            layout_token="wrong",
        )
        with self.assertRaisesRegex(GroundingStale, "different window layout"):
            revalidate_grounded_point(point, self.binding, self.binder)


class TestStructuredVisionGrounding(unittest.TestCase):
    def setUp(self):
        self.current = {"value": snapshot()}
        self.binder = WindowBinder(
            enumerate_fn=lambda: (self.current["value"],),
            snapshot_fn=lambda hwnd: self.current["value"],
        )
        self.binding = self.binder.bind(
            WindowTarget(
                app_id="paint",
                display_name="Paint",
                executable="mspaint.exe",
                process_names=("mspaint.exe",),
            ),
            require_foreground=True,
        )
        self.capture = bound_capture(self.binding)

    def test_valid_unique_candidate_maps_point_and_bbox_to_screen(self):
        target = ground_structured_response(self.capture, structured_response())
        self.assertEqual(target.label, "canvas")
        self.assertAlmostEqual(target.confidence, 0.93)
        self.assertEqual((target.point.screen_x, target.point.screen_y), (900, 500))
        self.assertEqual(target.screen_box, WindowRect(600, 300, 1200, 700))
        self.assertEqual(target.binding_token, self.binding.identity_token)
        self.assertEqual(target.layout_token, self.binding.layout_token)

    def test_malformed_json_is_rejected(self):
        with self.assertRaisesRegex(GroundingResponseInvalid, "valid JSON"):
            ground_structured_response(self.capture, "not-json")

    def test_extra_fields_are_rejected(self):
        payload = json.loads(structured_response())
        payload["explanation"] = "click here"
        with self.assertRaisesRegex(GroundingResponseInvalid, "unexpected"):
            ground_structured_response(self.capture, json.dumps(payload))

    def test_duplicate_json_fields_are_rejected_instead_of_last_one_winning(self):
        raw = (
            '{"status":"found","status":"not_found",'
            '"image_width":1280,"image_height":720,"candidates":[]}'
        )
        with self.assertRaisesRegex(GroundingResponseInvalid, "duplicate field"):
            parse_grounding_response(raw, expected_width=1280, expected_height=720)

    def test_model_dimension_mismatch_is_rejected(self):
        with self.assertRaisesRegex(GroundingResponseInvalid, "dimensions"):
            ground_structured_response(
                self.capture, structured_response(image_width=1279)
            )

    def test_out_of_bounds_point_is_rejected(self):
        candidate = {
            "label": "save",
            "confidence": 0.9,
            "point": {"x": 1280, "y": 10},
            "bbox": {"x": 1200, "y": 0, "width": 80, "height": 40},
        }
        with self.assertRaisesRegex(GroundingResponseInvalid, "outside the model image"):
            ground_structured_response(
                self.capture, structured_response(candidates=[candidate])
            )

    def test_point_outside_its_bbox_is_rejected(self):
        candidate = {
            "label": "save",
            "confidence": 0.9,
            "point": {"x": 800, "y": 200},
            "bbox": {"x": 100, "y": 100, "width": 100, "height": 100},
        }
        with self.assertRaisesRegex(GroundingResponseInvalid, "outside its bbox"):
            ground_structured_response(
                self.capture, structured_response(candidates=[candidate])
            )

    def test_low_confidence_unique_candidate_is_not_actionable(self):
        payload = json.loads(structured_response())
        payload["candidates"][0]["confidence"] = 0.69
        with self.assertRaises(GroundingLowConfidence):
            ground_structured_response(self.capture, json.dumps(payload))

    def test_not_found_is_explicit_and_not_a_fake_coordinate(self):
        with self.assertRaises(GroundingNotFound):
            ground_structured_response(
                self.capture,
                structured_response(status="not_found", candidates=[]),
            )

    def test_ambiguous_targets_are_not_guessed_between(self):
        first = json.loads(structured_response())["candidates"][0]
        second = {
            "label": "canvas 2",
            "confidence": 0.91,
            "point": {"x": 300, "y": 300},
            "bbox": {"x": 200, "y": 200, "width": 200, "height": 200},
        }
        with self.assertRaises(GroundingAmbiguous):
            ground_structured_response(
                self.capture,
                structured_response(status="ambiguous", candidates=[first, second]),
            )

    def test_duplicate_candidates_are_rejected_even_when_model_calls_them_ambiguous(self):
        candidate = json.loads(structured_response())["candidates"][0]
        raw = structured_response(
            status="ambiguous", candidates=[candidate, dict(candidate)]
        )
        with self.assertRaisesRegex(GroundingResponseInvalid, "duplicate"):
            parse_grounding_response(raw, expected_width=1280, expected_height=720)

    def test_boolean_coordinates_do_not_pass_as_python_integers(self):
        payload = json.loads(structured_response())
        payload["candidates"][0]["point"]["x"] = True
        with self.assertRaisesRegex(GroundingResponseInvalid, "must be an integer"):
            ground_structured_response(self.capture, json.dumps(payload))

    def test_stale_frame_attack_is_rejected_before_coordinate_reuse(self):
        target = ground_structured_response(self.capture, structured_response())
        self.current["value"] = snapshot(left=130, top=50)
        with self.assertRaisesRegex(GroundingStale, "coordinates expired"):
            revalidate_grounded_target(target, self.binding, self.binder)

    def test_tampered_screen_box_is_rejected(self):
        target = ground_structured_response(self.capture, structured_response())
        target = replace(target, screen_box=WindowRect(0, 0, 10, 10))
        with self.assertRaisesRegex(GroundingStale, "bounds"):
            revalidate_grounded_target(target, self.binding, self.binder)


if __name__ == "__main__":
    unittest.main()
