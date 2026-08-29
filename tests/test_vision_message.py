"""
A message that carries a picture, and one that carries only words.

The second half matters more than the first. Every request Qronos already
sends must go out **byte-identical** after this change, because a vision
feature that quietly alters ordinary chat has broken something nobody will
connect back to it. So there is a test that builds a text conversation, builds
the payload, and asserts the exact dictionaries — not "contains", not "has the
right keys", but equality against a literal.

The last test in the file sends a real request over a real socket to a
stand-in server, because a patched ``requests.post`` agrees with whatever the
code already does. That is the pattern the voice tests used, and the reason
they caught a threshold that was wrong.
"""

from __future__ import annotations

import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from core.brain_runtime import BrainMessage, BrainMessageRole
from core.ollama_controller import OllamaController


def png_bytes(width: int, height: int) -> bytes:
    buffer = BytesIO()

    Image.new("RGB", (width, height), (18, 52, 86)).save(buffer, format="PNG")

    return buffer.getvalue()


class TestMessageShape(unittest.TestCase):
    """What a message is allowed to be."""

    def test_a_message_with_words_and_no_pictures_is_the_ordinary_case(self) -> None:
        message = BrainMessage(
            role=BrainMessageRole.USER,
            content="What is on my screen?",
        )

        self.assertEqual(message.images, ())

    def test_a_message_may_carry_pictures(self) -> None:
        message = BrainMessage(
            role=BrainMessageRole.USER,
            content="What is on my screen?",
            images=("shot.png",),
        )

        self.assertEqual(message.images, ("shot.png",))

    def test_a_picture_with_no_words_is_legal(self) -> None:
        """
        Because "look at this" is a complete request. The old rule was that
        content must not be empty; the rule now is that a message must carry
        something.
        """
        message = BrainMessage(
            role=BrainMessageRole.USER,
            content="",
            images=("shot.png",),
        )

        self.assertEqual(message.images, ("shot.png",))

    def test_a_message_carrying_nothing_at_all_is_still_refused(self) -> None:
        with self.assertRaises(ValueError):
            BrainMessage(role=BrainMessageRole.USER, content="   ")

    def test_one_path_given_as_a_bare_string_is_refused(self) -> None:
        """
        ``images="shot.png"`` is a plausible mistake and a silent one: the
        string is a sequence, so it would be read as one path per character
        and the failure would surface far away as eleven missing files.
        """
        with self.assertRaises(TypeError):
            BrainMessage(
                role=BrainMessageRole.USER,
                content="Look",
                images="shot.png",
            )

    def test_the_message_holds_paths_rather_than_encoded_bytes(self) -> None:
        """
        A frozen dataclass ends up in log lines and tracebacks, and a megabyte
        of base64 in a repr makes every one of those unreadable.
        """
        message = BrainMessage(
            role=BrainMessageRole.USER,
            content="Look",
            images=("shot.png",),
        )

        self.assertIn("shot.png", repr(message))
        self.assertLess(len(repr(message)), 400)


class TestPayload(unittest.TestCase):
    """What actually goes to the model runtime."""

    def setUp(self) -> None:
        self.folder = TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.addCleanup(self.folder.cleanup)

        self.picture = self.root / "shot.png"
        self.picture.write_bytes(png_bytes(200, 150))

    def build(self, messages) -> list[dict]:
        return OllamaController._build_messages(prompt="", messages=messages)

    def test_a_text_conversation_is_built_exactly_as_before(self) -> None:
        """
        The load-bearing test of this change. Equality against a literal, so
        an extra key of any kind fails it.
        """
        built = self.build(
            [
                BrainMessage(BrainMessageRole.SYSTEM, "You are Qronos."),
                BrainMessage(BrainMessageRole.USER, "Hello."),
                BrainMessage(BrainMessageRole.ASSISTANT, "Hello back."),
            ]
        )

        self.assertEqual(
            built,
            [
                {"role": "system", "content": "You are Qronos."},
                {"role": "user", "content": "Hello."},
                {"role": "assistant", "content": "Hello back."},
            ],
        )

    def test_a_message_without_pictures_has_no_images_key_at_all(self) -> None:
        """
        Not an empty list. An empty ``images`` key is a different request from
        no key, and only one of them is what Qronos sends today.
        """
        built = self.build([BrainMessage(BrainMessageRole.USER, "Hello.")])

        self.assertNotIn("images", built[0])

    def test_a_message_with_a_picture_carries_it_encoded(self) -> None:
        built = self.build(
            [
                BrainMessage(
                    BrainMessageRole.USER,
                    "What is this?",
                    images=(str(self.picture),),
                )
            ]
        )

        self.assertEqual(built[0]["content"], "What is this?")
        self.assertEqual(len(built[0]["images"]), 1)

        decoded = base64.b64decode(built[0]["images"][0])

        with Image.open(BytesIO(decoded)) as reopened:
            self.assertEqual(reopened.size, (200, 150))

    def test_several_pictures_keep_their_order(self) -> None:
        second = self.root / "second.png"
        second.write_bytes(png_bytes(300, 100))

        built = self.build(
            [
                BrainMessage(
                    BrainMessageRole.USER,
                    "Compare these.",
                    images=(str(self.picture), str(second)),
                )
            ]
        )

        sizes = []

        for encoded in built[0]["images"]:
            with Image.open(BytesIO(base64.b64decode(encoded))) as reopened:
                sizes.append(reopened.size)

        self.assertEqual(sizes, [(200, 150), (300, 100)])

    def test_pictures_ride_on_their_own_message_only(self) -> None:
        built = self.build(
            [
                BrainMessage(BrainMessageRole.SYSTEM, "You are Qronos."),
                BrainMessage(
                    BrainMessageRole.USER,
                    "Read this.",
                    images=(str(self.picture),),
                ),
            ]
        )

        self.assertNotIn("images", built[0])
        self.assertIn("images", built[1])

    def test_a_picture_that_cannot_be_read_fails_before_the_request(self) -> None:
        """
        Rather than sending a request that the server rejects with something
        unhelpful, or worse, silently answering about nothing.
        """
        from core.vision_image import ImageUnusable

        with self.assertRaises(ImageUnusable):
            self.build(
                [
                    BrainMessage(
                        BrainMessageRole.USER,
                        "Read this.",
                        images=(str(self.root / "gone.png"),),
                    )
                ]
            )

    def test_an_oversized_picture_is_shrunk_on_the_way_out(self) -> None:
        """
        The caller passes a path and gets the measured operating point without
        having to know about it.
        """
        from core.vision_image import SEND_LONG_EDGE

        large = self.root / "large.png"
        large.write_bytes(png_bytes(3840, 2160))

        built = self.build(
            [
                BrainMessage(
                    BrainMessageRole.USER,
                    "Read this.",
                    images=(str(large),),
                )
            ]
        )

        decoded = base64.b64decode(built[0]["images"][0])

        with Image.open(BytesIO(decoded)) as reopened:
            self.assertLessEqual(max(reopened.size), SEND_LONG_EDGE)


class RecordingHandler(BaseHTTPRequestHandler):
    """A stand-in Ollama that keeps the request it was given."""

    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - the base class names it
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        RecordingHandler.received.append(json.loads(body))

        reply = json.dumps(
            {"message": {"content": "A blue rectangle."}, "done": True}
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args) -> None:
        """Quiet: the test's output is the test's own."""


class TestOverARealSocket(unittest.TestCase):
    """
    The same thing again, but actually sent.

    A patched ``requests.post`` agrees with whatever the code already does.
    This one serialises the payload, puts it through a socket, and reads it
    back off the wire — so an image that cannot survive JSON encoding, or a
    header that is wrong, fails here rather than in front of a user.
    """

    def setUp(self) -> None:
        RecordingHandler.received = []

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)

        # The default poll interval is half a second and every teardown is
        # charged for it. Measured on the voice tests: 22 seconds became 4.
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()

        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        host, port = self.server.server_address[:2]
        self.controller = OllamaController(base_url=f"http://{host}:{port}")

        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

        self.picture = Path(self.folder.name) / "shot.png"
        self.picture.write_bytes(png_bytes(640, 480))

    def test_a_picture_survives_the_whole_round_trip(self) -> None:
        answer = self.controller.chat(
            model_name="qwen3-vl:4b-instruct",
            messages=[
                BrainMessage(
                    BrainMessageRole.USER,
                    "What is in this picture?",
                    images=(str(self.picture),),
                )
            ],
            think=False,
        )

        self.assertEqual(answer, "A blue rectangle.")

        sent = RecordingHandler.received[0]
        encoded = sent["messages"][0]["images"][0]

        with Image.open(BytesIO(base64.b64decode(encoded))) as reopened:
            self.assertEqual(reopened.size, (640, 480))

    def test_an_ordinary_text_request_still_carries_no_images_key(self) -> None:
        self.controller.chat(
            model_name="qwen3:4b-instruct",
            messages=[BrainMessage(BrainMessageRole.USER, "Hello.")],
            think=False,
        )

        sent = RecordingHandler.received[0]

        self.assertEqual(
            sent["messages"],
            [{"role": "user", "content": "Hello."}],
        )


if __name__ == "__main__":
    unittest.main()
