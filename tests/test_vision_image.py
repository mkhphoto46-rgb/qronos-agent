"""
Pictures on the way to a model: what is accepted, what it costs, what is sent.

Every image here is built inside the test. None is committed, and none is a
capture — a screenshot fixture is a photograph of whoever's desktop it came
from, and this repository is public.

The token arithmetic is the part worth testing hardest, because it is the
budget the whole vision feature is planned against and because the documented
rule and the measured behaviour disagree. See ``core/vision_image``.
"""

from __future__ import annotations

import base64
import struct
import unittest
import zlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from core import vision_image
from core.vision_image import (
    ImageUnusable,
    MINIMUM_IMAGE_TOKENS,
    PATCH_PIXELS,
    SEND_LONG_EDGE,
    dimensions,
    image_tokens,
    prepare,
    prepare_bytes,
    read,
    sniff,
)


def png_bytes(width: int, height: int, colour: tuple[int, int, int] = (32, 96, 160)) -> bytes:
    """A real PNG of a given size, encoded here rather than read from disk."""
    buffer = BytesIO()

    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")

    return buffer.getvalue()


def jpeg_bytes(width: int, height: int) -> bytes:
    """The same, as a JPEG, for the other header parser."""
    buffer = BytesIO()

    Image.new("RGB", (width, height), (200, 40, 40)).save(buffer, format="JPEG")

    return buffer.getvalue()


def handmade_png_header(width: int, height: int) -> bytes:
    """
    A PNG header with chosen dimensions, assembled byte by byte.

    Separate from :func:`png_bytes` on purpose: that one asks Pillow what a
    PNG looks like, and then asks our parser to agree with Pillow. This one
    states the dimensions independently, so a parser that read the wrong four
    bytes and happened to agree with an encoder cannot pass.
    """
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    return (
        vision_image.PNG_MAGIC
        + struct.pack(">I", len(ihdr))
        + b"IHDR"
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    )


class TestTokenCost(unittest.TestCase):
    """
    What an image costs the context.

    The floor is the surprising part and the reason the resolution budget in
    the plan was wrong before it was measured.
    """

    def test_a_large_image_costs_one_token_per_patch(self) -> None:
        # 1920x1080 is 60 by 34 patches once the part-used row is counted.
        self.assertEqual(image_tokens(1920, 1080), 60 * 34)

    def test_a_partly_used_patch_still_costs_a_whole_token(self) -> None:
        # One pixel past a patch boundary in each direction, so both round up.
        exact = image_tokens(1920, 1088)
        over = image_tokens(1921, 1089)

        self.assertEqual(exact, 60 * 34)
        self.assertEqual(over, 61 * 35)

    def test_small_images_all_cost_the_same_floor(self) -> None:
        """
        Measured, and it is why shrinking past a point saves nothing.

        A 64-pixel thumbnail and a 512-pixel one are charged identically,
        because the server enlarges both before the model sees them.
        """
        for side in (64, 128, 256, 512):
            with self.subTest(side=side):
                self.assertEqual(image_tokens(side, side), MINIMUM_IMAGE_TOKENS)

    def test_the_floor_stops_applying_around_a_megapixel(self) -> None:
        below = image_tokens(1024, 576)
        above = image_tokens(1920, 1080)

        self.assertEqual(below, MINIMUM_IMAGE_TOKENS)
        self.assertGreater(above, MINIMUM_IMAGE_TOKENS)

    def test_four_k_costs_about_four_times_what_ten_eighty_p_does(self) -> None:
        """The number the whole capture strategy turns on."""
        self.assertAlmostEqual(
            image_tokens(3840, 2160) / image_tokens(1920, 1080),
            4.0,
            places=1,
        )

    def test_a_zero_sized_image_is_a_programming_error(self) -> None:
        for width, height in ((0, 100), (100, 0), (-5, 10)):
            with self.subTest(size=(width, height)):
                with self.assertRaises(ValueError):
                    image_tokens(width, height)


class TestSniffing(unittest.TestCase):
    """The format comes from the bytes, never from the file name."""

    def test_png_is_recognised(self) -> None:
        self.assertEqual(sniff(png_bytes(40, 40)), "png")

    def test_jpeg_is_recognised(self) -> None:
        self.assertEqual(sniff(jpeg_bytes(40, 40)), "jpeg")

    def test_something_that_is_not_a_picture_is_refused(self) -> None:
        with self.assertRaises(ImageUnusable):
            sniff(b"GIF89a and the rest of a perfectly good GIF")

    def test_an_empty_file_is_refused_rather_than_indexing_past_the_end(self) -> None:
        with self.assertRaises(ImageUnusable):
            sniff(b"")

    def test_a_png_extension_on_a_text_file_does_not_help_it(self) -> None:
        with TemporaryDirectory() as folder:
            liar = Path(folder) / "screenshot.png"
            liar.write_text("This is not a picture.", encoding="utf-8")

            with self.assertRaises(ImageUnusable):
                prepare(liar)


class TestDimensions(unittest.TestCase):
    """Size from the header, without decoding the picture."""

    def test_png_dimensions_are_read_from_the_header(self) -> None:
        self.assertEqual(dimensions(handmade_png_header(1234, 567)), (1234, 567))

    def test_png_width_and_height_are_not_transposed(self) -> None:
        self.assertEqual(dimensions(png_bytes(300, 100)), (300, 100))

    def test_jpeg_dimensions_are_read_from_the_frame_marker(self) -> None:
        self.assertEqual(dimensions(jpeg_bytes(321, 123)), (321, 123))

    def test_jpeg_height_comes_before_width_in_the_frame_header(self) -> None:
        """
        A non-square JPEG, because a square one cannot catch a transposition
        and the JPEG frame header stores height first, which invites one.
        """
        self.assertEqual(dimensions(jpeg_bytes(640, 160)), (640, 160))

    def test_a_truncated_png_is_refused_rather_than_read_as_garbage(self) -> None:
        with self.assertRaises(ImageUnusable):
            dimensions(vision_image.PNG_MAGIC + b"\x00\x00")

    def test_a_png_claiming_zero_size_is_refused(self) -> None:
        with self.assertRaises(ImageUnusable):
            dimensions(handmade_png_header(0, 0))

    def test_a_jpeg_with_no_frame_marker_is_refused(self) -> None:
        with self.assertRaises(ImageUnusable):
            dimensions(vision_image.JPEG_MAGIC + b"\x00" * 200)

    def test_a_jpeg_with_a_nonsense_segment_length_does_not_loop(self) -> None:
        """
        A malformed length of zero would leave the scan on the same byte for
        ever. The parser stops instead of hanging the process.
        """
        malformed = (
            vision_image.JPEG_MAGIC
            + b"\xff\xe0\x00\x00"  # An APP0 segment claiming to be zero long.
            + b"\x00" * 64
        )

        with self.assertRaises(ImageUnusable):
            dimensions(malformed)


class TestReading(unittest.TestCase):
    """Every reason a file might not be usable, said in words."""

    def setUp(self) -> None:
        self.folder = TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.addCleanup(self.folder.cleanup)

    def test_a_missing_file_is_refused_by_name(self) -> None:
        missing = self.root / "not-here.png"

        with self.assertRaises(ImageUnusable) as caught:
            read(missing)

        self.assertIn("not-here.png", str(caught.exception))

    def test_a_folder_is_not_a_picture(self) -> None:
        with self.assertRaises(ImageUnusable):
            read(self.root)

    def test_a_zero_byte_file_is_refused(self) -> None:
        empty = self.root / "empty.png"
        empty.touch()

        with self.assertRaises(ImageUnusable):
            read(empty)

    def test_an_absurdly_large_file_is_refused_without_reading_it(self) -> None:
        """
        Checked from the file's recorded size, so refusing a huge file does
        not first require loading a huge file into memory.
        """
        huge = self.root / "huge.png"
        huge.write_bytes(b"\x00" * 128)

        original = vision_image.MAX_FILE_BYTES
        vision_image.MAX_FILE_BYTES = 64
        self.addCleanup(setattr, vision_image, "MAX_FILE_BYTES", original)

        with self.assertRaises(ImageUnusable) as caught:
            read(huge)

        self.assertIn("MB", str(caught.exception))

    def test_a_file_at_exactly_the_limit_is_accepted(self) -> None:
        edge = self.root / "edge.png"
        edge.write_bytes(b"\x00" * 64)

        original = vision_image.MAX_FILE_BYTES
        vision_image.MAX_FILE_BYTES = 64
        self.addCleanup(setattr, vision_image, "MAX_FILE_BYTES", original)

        self.assertEqual(len(read(edge)), 64)


class TestPreparing(unittest.TestCase):
    """From a file on disk to something a model can be handed."""

    def setUp(self) -> None:
        self.folder = TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.addCleanup(self.folder.cleanup)

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)

        return path

    def test_a_small_picture_is_sent_exactly_as_it_is(self) -> None:
        data = png_bytes(800, 600)
        path = self.write("small.png", data)

        prepared = prepare(path)

        self.assertEqual(prepared.data, data)
        self.assertFalse(prepared.resized)
        self.assertEqual((prepared.width, prepared.height), (800, 600))

    def test_a_large_picture_is_shrunk_to_the_operating_point(self) -> None:
        path = self.write("large.png", png_bytes(3840, 2160))

        prepared = prepare(path)

        self.assertTrue(prepared.resized)
        self.assertLessEqual(max(prepared.width, prepared.height), SEND_LONG_EDGE)

    def test_shrinking_keeps_the_shape_of_the_picture(self) -> None:
        path = self.write("wide.png", png_bytes(3840, 2160))

        prepared = prepare(path)

        self.assertAlmostEqual(
            prepared.width / prepared.height,
            3840 / 2160,
            places=1,
        )

    def test_shrinking_lands_on_whole_patches(self) -> None:
        """A part-used patch costs a whole token, so none is left part-used."""
        path = self.write("odd.png", png_bytes(3000, 1710))

        prepared = prepare(path)

        self.assertEqual(prepared.width % PATCH_PIXELS, 0)
        self.assertEqual(prepared.height % PATCH_PIXELS, 0)

    def test_shrinking_a_four_k_capture_cuts_the_cost_by_about_half(self) -> None:
        """
        The measured saving. Not the fifteen-fold one the plan assumed before
        the floor was found — but still the largest single lever there is.
        """
        native = image_tokens(3840, 2160)
        path = self.write("desktop.png", png_bytes(3840, 2160))

        self.assertLess(prepare(path).tokens, native * 0.6)

    def test_shrinking_produces_a_real_picture_of_the_declared_size(self) -> None:
        path = self.write("large.png", png_bytes(2560, 1440))

        prepared = prepare(path)

        with Image.open(BytesIO(prepared.data)) as reopened:
            self.assertEqual(reopened.size, (prepared.width, prepared.height))

    def test_a_jpeg_is_shrunk_into_a_png(self) -> None:
        """
        Because the picture is going straight to a model rather than to disk,
        and re-encoding it as JPEG would add compression artefacts to text
        that the model is about to be asked to read.
        """
        path = self.write("photo.jpg", jpeg_bytes(2400, 1600))

        prepared = prepare(path)

        self.assertEqual(prepared.format, "png")
        self.assertEqual(sniff(prepared.data), "png")

    def test_asking_for_no_resizing_sends_the_original(self) -> None:
        data = png_bytes(3840, 2160)
        path = self.write("native.png", data)

        prepared = prepare(path, long_edge=None)

        self.assertEqual(prepared.data, data)
        self.assertFalse(prepared.resized)

    def test_a_picture_already_at_the_operating_point_is_left_alone(self) -> None:
        data = png_bytes(SEND_LONG_EDGE, 720)
        path = self.write("exact.png", data)

        prepared = prepare(path)

        self.assertFalse(prepared.resized)
        self.assertEqual(prepared.data, data)

    def test_the_encoding_round_trips(self) -> None:
        path = self.write("small.png", png_bytes(64, 64))

        prepared = prepare(path)

        self.assertEqual(base64.b64decode(prepared.base64), prepared.data)

    def test_the_source_path_is_remembered_for_a_file(self) -> None:
        path = self.write("small.png", png_bytes(64, 64))

        self.assertEqual(prepare(path).source, path)

    def test_a_description_says_size_and_cost(self) -> None:
        path = self.write("small.png", png_bytes(800, 600))

        described = prepare(path).describe()

        self.assertIn("800x600", described)
        self.assertIn("token", described)

    def test_a_capture_that_was_never_on_disk_can_be_prepared(self) -> None:
        prepared = prepare_bytes(png_bytes(1000, 800))

        self.assertIsNone(prepared.source)
        self.assertEqual((prepared.width, prepared.height), (1000, 800))

    def test_a_capture_of_nothing_is_refused(self) -> None:
        with self.assertRaises(ImageUnusable):
            prepare_bytes(b"")

    def test_a_corrupt_picture_is_refused_rather_than_crashing(self) -> None:
        """
        A header that says 4000 pixels wide over a body that is nonsense: it
        passes sniffing and sizing, and only fails when something tries to
        decode it. That is the shape a truncated download takes.
        """
        corrupt = handmade_png_header(4000, 3000) + b"\x00" * 500
        path = self.write("corrupt.png", corrupt)

        with self.assertRaises(ImageUnusable):
            prepare(path)


class TestAPictureCanCarryAReading(unittest.TestCase):
    """
    Text somebody else already read off this picture, riding with it.

    It travels on the picture because that is what it is about, and because the
    reading is made at a resolution the picture no longer has: OCR runs on the
    full-size capture and the model is sent a 1280-pixel version.
    """

    def setUp(self) -> None:
        self.folder = TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.addCleanup(self.folder.cleanup)

    def test_a_picture_carries_no_reading_by_default(self) -> None:
        self.assertEqual(prepare_bytes(png_bytes(64, 64)).hint, "")

    def test_a_reading_can_be_attached(self) -> None:
        from dataclasses import replace

        prepared = replace(prepare_bytes(png_bytes(64, 64)), hint="Saved.")

        self.assertEqual(prepared.hint, "Saved.")

    def test_the_repr_says_a_hint_is_there_without_printing_it(self) -> None:
        from dataclasses import replace

        prepared = replace(
            prepare_bytes(png_bytes(64, 64)), hint="a very long reading " * 200
        )

        self.assertIn("hint", repr(prepared))
        self.assertNotIn("a very long reading", repr(prepared))
        self.assertLess(len(repr(prepared)), 200)


if __name__ == "__main__":
    unittest.main()
