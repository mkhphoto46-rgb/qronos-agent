"""
Taking a picture of the screen: the permission, the pixels, and the plumbing.

Almost everything here runs with no screen. ``ScreenCapture`` takes the twenty
lines of GDI as an injected callable precisely so that the decisions around
them — is this allowed, was anything captured, how large is it, where did it
go — can be exercised on a build machine and on Ubuntu, where those twenty
lines cannot run at all.

The GDI itself is covered by a live check at the end that skips off Windows,
and by ``tools/test_qronos_vision_live.py``, which captures this machine's real
screen and reads it back. That split is deliberate: a test that needs a
particular desktop in front of it is not a test the suite can own.
"""

from __future__ import annotations

import sys
import time
import unittest
from io import BytesIO

from PIL import Image

from core import screen_capture
from core.actions import ActionOutcome
from core.screen_capture import (
    Capture,
    CaptureRefused,
    CaptureUnavailable,
    DisplayGeometry,
    ScreenCapture,
    available,
    foreground_window,
)
from core.vision_image import SEND_LONG_EDGE
from security import gate
from security.permissions import ActionCategory


def blue_pixels(width: int, height: int) -> bytes:
    """
    A bitmap in the byte order GDI hands back: blue, green, red, unused.

    The alpha byte is left at zero, which is what GDI actually does, and is
    the reason the encoder drops the channel rather than trusting it.
    """
    one = bytes((200, 100, 50, 0))

    return one * (width * height)


def busy_pixels(width: int, height: int) -> bytes:
    """
    A bitmap with more colours in it than a flat rectangle has.

    Used wherever a test needs a capture that is a picture of *something*,
    because the blank check would otherwise call every fixture blank.
    """
    return bytes(
        byte
        for index in range(width * height)
        for byte in (index % 251, (index * 7) % 241, (index * 13) % 239, 0)
    )


def fake_grab(width: int, height: int, pixels=blue_pixels):
    def grab(window):
        return pixels(width, height), width, height

    return grab


def fake_geometry(width: int = 1920, height: int = 1080, scale: float = 1.0):
    def geometry():
        return DisplayGeometry(
            physical_width=int(width * scale),
            physical_height=int(height * scale),
            logical_width=width,
            logical_height=height,
        )

    return geometry


def screenless(
    width: int = 1920,
    height: int = 1080,
    pixels=blue_pixels,
    **kwargs,
) -> ScreenCapture:
    return ScreenCapture(
        grab=fake_grab(width, height, pixels),
        geometry_fn=fake_geometry(width, height),
        **kwargs,
    )


class TestBlankCaptures(unittest.TestCase):
    """
    A flat rectangle is reported as one rather than sent as a picture.

    It is what a locked screen, a sleeping display and protected video all look
    like — and also what a broken capture looks like. Shown one, a model
    describes a black rectangle at length instead of saying anything useful, so
    the capture says it first.
    """

    def test_a_flat_rectangle_is_recognised(self) -> None:
        self.assertTrue(screenless(64, 48).capture(approved=True).blank)

    def test_a_picture_of_something_is_not(self) -> None:
        result = screenless(64, 48, pixels=busy_pixels).capture(approved=True)

        self.assertFalse(result.blank)

    def test_it_is_said_out_loud(self) -> None:
        result = screenless(64, 48).capture(approved=True)

        self.assertIn("blank", result.describe())

    def test_a_picture_of_something_does_not_say_it(self) -> None:
        result = screenless(64, 48, pixels=busy_pixels).capture(approved=True)

        self.assertNotIn("blank", result.describe())

    def test_a_blank_capture_is_still_a_capture(self) -> None:
        """
        Reported, not refused. A screen that really is blank is a true answer
        to "what is on my screen", and refusing it would be a lie.
        """
        result = screenless(64, 48).capture(approved=True)

        self.assertTrue(result.image.data)


class TestPermissionComesFirst(unittest.TestCase):
    """
    Nothing is copied before the gate has answered.

    The interesting assertion in most of these is not the exception — it is
    that the grab function was never called.
    """

    def setUp(self) -> None:
        self.grabbed: list = []

        def grab(window):
            self.grabbed.append(window)

            return blue_pixels(64, 48), 64, 48

        self.capture = ScreenCapture(
            grab=grab,
            geometry_fn=fake_geometry(64, 48),
        )

    def test_an_unapproved_capture_takes_no_pixels(self) -> None:
        with self.assertRaises(CaptureRefused):
            self.capture.capture()

        self.assertEqual(self.grabbed, [])

    def test_forgetting_to_ask_is_the_default(self) -> None:
        """
        ``approved`` has no default of True and never will. A caller that
        forgets it gets a refusal, which is the failure that costs nothing.
        """
        with self.assertRaises(CaptureRefused) as caught:
            self.capture.capture()

        self.assertIn("ui confirmation", str(caught.exception))

    def test_an_approved_capture_goes_ahead(self) -> None:
        result = self.capture.capture(approved=True)

        self.assertEqual(self.grabbed, [None])
        self.assertIsInstance(result, Capture)

    def test_the_decision_is_recorded_whichever_way_it_went(self) -> None:
        seen: list = []
        capture = ScreenCapture(
            grab=fake_grab(64, 48),
            geometry_fn=fake_geometry(64, 48),
            audit=seen.append,
        )

        capture.capture(approved=True)

        try:
            capture.capture()
        except CaptureRefused:
            pass

        self.assertEqual(len(seen), 2)
        self.assertEqual(
            [record.outcome for record in seen],
            [ActionOutcome.AWAITING_APPROVAL, ActionOutcome.AWAITING_APPROVAL],
        )

    def test_it_asks_about_the_right_category(self) -> None:
        seen: list = []
        capture = ScreenCapture(
            grab=fake_grab(64, 48),
            geometry_fn=fake_geometry(64, 48),
            audit=seen.append,
        )

        capture.capture(approved=True)

        self.assertIs(seen[0].request.category, ActionCategory.READ_SCREEN)

    def test_the_reason_shown_to_the_person_reaches_the_trail(self) -> None:
        seen: list = []
        capture = ScreenCapture(
            grab=fake_grab(64, 48),
            geometry_fn=fake_geometry(64, 48),
            audit=seen.append,
        )

        capture.capture(approved=True, reason="Read the error on screen.")

        self.assertEqual(seen[0].request.summary, "Read the error on screen.")

    def test_it_records_through_the_default_sink_when_given_none(self) -> None:
        seen: list = []
        previous = gate.set_default_audit_sink(seen.append)
        self.addCleanup(gate.set_default_audit_sink, previous)

        screenless(64, 48).capture(approved=True)

        self.assertEqual(len(seen), 1)


class TestWhatComesBack(unittest.TestCase):
    def test_a_capture_is_a_real_picture(self) -> None:
        result = screenless(800, 600).capture(approved=True)

        with Image.open(BytesIO(result.image.data)) as reopened:
            self.assertEqual(reopened.size, (800, 600))

    def test_the_colours_are_not_swapped(self) -> None:
        """
        GDI hands back blue, green, red. Reading it as red, green, blue
        produces a picture that looks fine and is wrong — and a model asked
        what colour something is would answer confidently and incorrectly.
        """
        result = screenless(16, 16).capture(approved=True)

        with Image.open(BytesIO(result.image.data)) as reopened:
            self.assertEqual(reopened.convert("RGB").getpixel((0, 0)), (50, 100, 200))

    def test_the_unused_alpha_byte_does_not_make_it_transparent(self) -> None:
        """
        GDI leaves alpha at zero. Kept, that is a fully transparent picture,
        which encodes and sends perfectly well and arrives as nothing at all.
        """
        result = screenless(16, 16).capture(approved=True)

        with Image.open(BytesIO(result.image.data)) as reopened:
            self.assertEqual(reopened.mode, "RGB")

    def test_a_large_screen_is_shrunk_to_the_size_the_model_wants(self) -> None:
        result = screenless(3840, 2160).capture(approved=True)

        self.assertLessEqual(
            max(result.image.width, result.image.height),
            SEND_LONG_EDGE,
        )

    def test_a_four_k_screen_costs_about_a_thousand_tokens(self) -> None:
        """
        Which is the whole reason vision fits in a 4,096-token context at all.
        """
        result = screenless(3840, 2160).capture(approved=True)

        self.assertLess(result.image.tokens, 1_200)

    def test_the_capture_never_becomes_a_file(self) -> None:
        """
        No temporary file to clean up, no janitor to trust, nothing left
        behind if the process is killed halfway. That is the retention policy:
        there is no retention.
        """
        result = screenless(800, 600).capture(approved=True)

        self.assertIsNone(result.image.source)

    def test_a_capture_describes_itself_without_printing_itself(self) -> None:
        result = screenless(800, 600).capture(approved=True)

        self.assertIn("800x600", repr(result.image))
        self.assertLess(len(repr(result.image)), 200)

    def test_capturing_nothing_is_reported_rather_than_returned(self) -> None:
        """
        A minimised or closed window hands back an empty bitmap rather than
        failing, so this is a normal case and not a crash.
        """
        empty = ScreenCapture(
            grab=lambda window: (b"", 0, 0),
            geometry_fn=fake_geometry(),
        )

        with self.assertRaises(CaptureUnavailable):
            empty.capture(approved=True)


class TestScaledDisplays(unittest.TestCase):
    """
    The free saving, and the reason both numbers are recorded.

    A capture smaller than the panel is otherwise indistinguishable from a bug.
    """

    def test_an_unscaled_display_reports_scaling_of_one(self) -> None:
        self.assertEqual(DisplayGeometry(1920, 1080, 1920, 1080).scaling, 1.0)

    def test_a_scaled_display_reports_what_it_kept_and_what_it_has(self) -> None:
        shape = DisplayGeometry(3840, 2160, 3072, 1728)

        self.assertAlmostEqual(shape.scaling, 1.25)
        self.assertIn("3072x1728", shape.describe())
        self.assertIn("3840x2160", shape.describe())
        self.assertIn("125%", shape.describe())

    def test_an_unscaled_display_says_one_size_and_not_two(self) -> None:
        self.assertEqual(
            DisplayGeometry(1920, 1080, 1920, 1080).describe(),
            "1920x1080",
        )

    def test_a_display_of_no_size_does_not_divide_by_zero(self) -> None:
        self.assertEqual(DisplayGeometry(0, 0, 0, 0).scaling, 1.0)

    def test_the_geometry_travels_with_the_picture(self) -> None:
        result = ScreenCapture(
            grab=fake_grab(3072, 1728),
            geometry_fn=fake_geometry(3072, 1728, scale=1.25),
        ).capture(approved=True)

        self.assertAlmostEqual(result.geometry.scaling, 1.25)
        self.assertIn("125%", result.describe())


class TestWindows(unittest.TestCase):
    def test_a_window_handle_reaches_the_grab(self) -> None:
        asked: list = []

        capture = ScreenCapture(
            grab=lambda window: (
                asked.append(window) or (blue_pixels(64, 48), 64, 48)
            ),
            geometry_fn=fake_geometry(64, 48),
        )

        capture.capture(approved=True, window=12345)

        self.assertEqual(asked, [12345])

    def test_the_handle_is_recorded_in_the_audit_trail(self) -> None:
        seen: list = []

        screenless(64, 48, audit=seen.append).capture(
            approved=True, window=12345
        )

        self.assertEqual(seen[0].request.parameters["window"], 12345)

    def test_a_whole_screen_capture_records_no_window(self) -> None:
        seen: list = []

        screenless(64, 48, audit=seen.append).capture(approved=True)

        self.assertEqual(seen[0].request.parameters["window"], 0)

    def test_the_two_are_described_differently(self) -> None:
        whole = screenless(64, 48).capture(approved=True)
        one = screenless(64, 48).capture(approved=True, window=1)

        self.assertIn("the screen", whole.describe())
        self.assertIn("one window", one.describe())


class TestOffWindows(unittest.TestCase):
    """
    On Ubuntu the module imports, says no, and refuses.

    Which is what lets the rest of the suite run on both CI platforms.
    """

    def test_availability_is_false_when_there_is_no_windll(self) -> None:
        original = screen_capture.sys.platform
        screen_capture.sys.platform = "linux"
        self.addCleanup(setattr, screen_capture.sys, "platform", original)

        self.assertFalse(available())

    def test_there_is_no_foreground_window_without_windows(self) -> None:
        original = screen_capture.sys.platform
        screen_capture.sys.platform = "linux"
        self.addCleanup(setattr, screen_capture.sys, "platform", original)

        self.assertIsNone(foreground_window())

    def test_capturing_says_so_rather_than_raising_an_attribute_error(
        self,
    ) -> None:
        original = screen_capture.sys.platform
        screen_capture.sys.platform = "linux"
        self.addCleanup(setattr, screen_capture.sys, "platform", original)

        with self.assertRaises(CaptureUnavailable) as caught:
            ScreenCapture().capture(approved=True)

        self.assertIn("Windows", str(caught.exception))

    def test_the_health_check_is_honest_about_it(self) -> None:
        original = screen_capture.sys.platform
        screen_capture.sys.platform = "linux"
        self.addCleanup(setattr, screen_capture.sys, "platform", original)

        self.assertFalse(ScreenCapture().health_check())

    def test_an_injected_grab_works_anywhere(self) -> None:
        self.assertTrue(screenless().health_check())


class TestTheTextHint(unittest.TestCase):
    """
    The reading happens here because here is the only place the full-size
    pixels exist.

    By the time a picture reaches a model it has been shrunk to a 1280-pixel
    long edge, and reading it at full size is the entire reason the hint is
    worth having.
    """

    def test_no_reader_means_no_hint(self) -> None:
        self.assertEqual(
            screenless(64, 48, pixels=busy_pixels).capture(approved=True).image.hint,
            "",
        )

    def test_a_reader_is_given_the_full_size_picture(self) -> None:
        seen: list = []

        def reader(png: bytes) -> str:
            with Image.open(BytesIO(png)) as reopened:
                seen.append(reopened.size)

            return "some text"

        ScreenCapture(
            grab=fake_grab(2560, 1440, busy_pixels),
            geometry_fn=fake_geometry(2560, 1440),
            read_text=reader,
        ).capture(approved=True)

        self.assertEqual(seen, [(2560, 1440)])

    def test_what_it_reads_rides_with_the_picture(self) -> None:
        result = ScreenCapture(
            grab=fake_grab(64, 48, busy_pixels),
            geometry_fn=fake_geometry(64, 48),
            read_text=lambda png: "Error code: 0x8024402C",
        ).capture(approved=True)

        self.assertEqual(result.image.hint, "Error code: 0x8024402C")

    def test_a_reader_that_fails_does_not_fail_the_capture(self) -> None:
        """
        A hint that throws is worse than a hint that is missing. The picture is
        going to the model either way.
        """

        def explodes(png: bytes) -> str:
            raise RuntimeError("PowerShell is not on this machine.")

        result = ScreenCapture(
            grab=fake_grab(64, 48, busy_pixels),
            geometry_fn=fake_geometry(64, 48),
            read_text=explodes,
        ).capture(approved=True)

        self.assertEqual(result.image.hint, "")
        self.assertTrue(result.image.data)

    def test_a_blank_screen_is_not_read(self) -> None:
        """Nothing to read, and a quarter of a second not to spend."""
        called: list = []

        ScreenCapture(
            grab=fake_grab(64, 48),
            geometry_fn=fake_geometry(64, 48),
            read_text=lambda png: called.append(1) or "",
        ).capture(approved=True)

        self.assertEqual(called, [])


@unittest.skipUnless(sys.platform == "win32", "GDI is a Windows thing.")
class TestTheRealScreen(unittest.TestCase):
    """
    The twenty lines the rest of this file stands in for.

    Deliberately shallow: it checks that the pixels arrive and are the right
    shape, not what is in them, because what is in them is whatever the person
    running the suite had open.
    """

    def _stable_foreground_window(self) -> int:
        """
        Return a real foreground window whose clipped capture region is usable.

        Windows can briefly expose a transient shell/helper window with a
        zero-area clipped rectangle while test processes start or finish.
        That is an environment transition, not a screen-capture defect.
        """
        shape = screen_capture.geometry()
        last_seen = None

        for _ in range(20):
            handle = foreground_window()

            if handle is not None:
                left, top, width, height = screen_capture._window_region(
                    handle,
                    shape.logical_width,
                    shape.logical_height,
                )

                last_seen = (handle, left, top, width, height)

                if width > 0 and height > 0:
                    return handle

            time.sleep(0.05)

        if last_seen is None:
            self.skipTest(
                "Windows did not expose a foreground window during the live "
                "screen-capture check."
            )

        handle, left, top, width, height = last_seen

        self.skipTest(
            "Windows did not expose a foreground window with a positive "
            "capture region during the live check "
            f"(handle={handle}, left={left}, top={top}, "
            f"width={width}, height={height})."
        )

    def test_the_display_reports_a_plausible_size(self) -> None:
        shape = screen_capture.geometry()

        self.assertGreater(shape.logical_width, 0)
        self.assertGreaterEqual(shape.physical_width, shape.logical_width)

    def test_a_real_capture_matches_the_logical_resolution(self) -> None:
        raw, width, height = screen_capture._grab_with_gdi(None)
        shape = screen_capture.geometry()

        self.assertEqual((width, height), (shape.logical_width, shape.logical_height))
        self.assertEqual(len(raw), width * height * 4)

    def test_a_real_capture_is_not_a_blank_rectangle(self) -> None:
        """
        A leaked device context, a failed BitBlt or an unselected bitmap all
        produce a picture of a uniform colour rather than an error.
        """
        result = ScreenCapture().capture(approved=True)

        with Image.open(BytesIO(result.image.data)) as reopened:
            colours = reopened.convert("RGB").getcolors(maxcolors=8)

        self.assertIsNone(
            colours,
            "The screen came back with fewer than eight colours in it, which "
            "means the capture is blank rather than a picture.",
        )

    def test_there_is_a_window_in_front_of_something(self) -> None:
        self.assertIsNotNone(self._stable_foreground_window())

    def test_a_real_window_capture_is_not_a_black_rectangle(self) -> None:
        """
        The defect this file exists to have caught.

        Capturing a window through its own device context returns solid black
        for anything drawn on the GPU — the browser, the terminal, the editor,
        most of what somebody would ask Qronos to read. It looks like a
        working capture: right size, right handle, no error. It was only found
        by sending one to a model, which described a black rectangle at
        length. Measured here: one distinct colour from the window's context,
        over a hundred thousand from the screen's at the same coordinates.
        """
        window = self._stable_foreground_window()

        result = ScreenCapture().capture(
            approved=True, window=window
        )

        self.assertFalse(
            result.blank,
            "The foreground window came back as a flat rectangle, which means "
            "it is being captured from its own device context again rather "
            "than from the composited screen.",
        )

    def test_a_window_capture_is_smaller_than_the_whole_screen(self) -> None:
        """Otherwise the handle is being ignored and the screen returned."""
        window = self._stable_foreground_window()
        shape = screen_capture.geometry()
        left, top, width, height = screen_capture._window_region(
            window, shape.logical_width, shape.logical_height
        )

        self.assertGreater(width, 0)
        self.assertLessEqual(left + width, shape.logical_width)
        self.assertLessEqual(top + height, shape.logical_height)

    def test_capturing_repeatedly_does_not_leak_handles(self) -> None:
        """
        Each GDI handle is a process-wide resource Windows does not reclaim
        until the process exits, so a capture that leaks one leaks it on every
        press of the hotkey. Ten thousand is the per-process default limit;
        twenty captures leaking two each would show here as a rising count.
        """
        import ctypes

        GR_GDIOBJECTS = 0
        process = ctypes.windll.kernel32.GetCurrentProcess()

        def handles() -> int:
            return ctypes.windll.user32.GetGuiResources(process, GR_GDIOBJECTS)

        # Straight at the GDI, skipping the PNG encode: the handles are all
        # in these twenty lines, and encoding twenty 4K screenshots to prove
        # it would make this the slowest test in the suite.
        screen_capture._grab_with_gdi(None)

        before = handles()

        for _ in range(20):
            screen_capture._grab_with_gdi(None)

        self.assertLessEqual(handles() - before, 5)


if __name__ == "__main__":
    unittest.main()
