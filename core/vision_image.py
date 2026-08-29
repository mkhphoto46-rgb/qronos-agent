"""
Turning a picture on disk into something a model can be given.

Small and pure on purpose: it reads files and returns bytes, starts nothing,
and imports nothing that could change the machine. That keeps it outside the
scope of the executor rules in ``tests/test_gate_discipline.py`` and lets every
test here run with images built inside the test rather than fixtures on disk.

Two things it knows that are not obvious, both measured on this machine rather
than taken from documentation.

**There is a floor on what an image costs.** The documented rule for this model
is one token per 32x32 patch. Above roughly a megapixel that is exactly right;
below it, it is not. A 64x64 image and a 512x512 image both cost about 1,040
tokens, because the server enlarges anything smaller before the model sees it.

**So shrinking a screenshot past a point is pure loss.** Measured on the same
text at the same moment: perfect reading at a 1280-pixel long edge, 0.353
character error at 1024, 0.324 at 512 — for an identical token cost. The saving
is real only above 1280, where it is worth taking: a 4K capture costs about
four times what the same thing does at 1080p, and reads no better.

Hence :data:`SEND_LONG_EDGE`. It is not a compromise between speed and quality;
below it there is nothing left to trade.
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

#: The model's patch size. One token per 32x32 patch, above the floor below.
PATCH_PIXELS = 32

#: The fewest tokens an image is ever charged, measured. See the module
#: docstring: everything below about a megapixel costs this.
MINIMUM_IMAGE_TOKENS = 1040

#: What a picture is shrunk to before it is sent.
#:
#: Measured, not chosen: at this size reading is exact and the cost is already
#: at the floor, so going smaller loses accuracy and saves nothing. Going
#: larger is worth it only when there is genuinely more detail to see — a 4K
#: capture at native cost 1,740 tokens and 3.5 seconds against 1,081 tokens and
#: 0.5 seconds here, and read the same text equally well.
SEND_LONG_EDGE = 1280

#: Refused above this. A picture is an input from outside; a hundred megabytes
#: of it is a mistake somewhere, not a request.
MAX_FILE_BYTES = 40 * 1024 * 1024

#: What Ollama will accept, and what a screenshot or a photograph will be.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


class ImageUnusable(ValueError):
    """The file cannot be sent to a model, and why."""


@dataclass(frozen=True)
class PreparedImage:
    """A picture ready to be handed to a model, and what it will cost."""

    data: bytes
    width: int
    height: int
    format: str
    source: Path | None = None

    #: True when the picture was shrunk on the way through.
    resized: bool = False

    @property
    def tokens(self) -> int:
        """What this will cost the model's context, before any words."""
        return image_tokens(self.width, self.height)

    @property
    def base64(self) -> str:
        """The encoding Ollama expects on a message."""
        return base64.b64encode(self.data).decode("ascii")

    def describe(self) -> str:
        return (
            f"{self.width}x{self.height} {self.format}, "
            f"about {self.tokens} tokens"
        )


def image_tokens(width: int, height: int) -> int:
    """
    What an image of this size costs, in tokens.

    Honours the measured floor rather than the documented patch rule alone,
    because the two disagree below a megapixel and the floor is what is
    actually charged.
    """
    if width <= 0 or height <= 0:
        raise ValueError("An image cannot have a zero or negative side.")

    patches = math.ceil(width / PATCH_PIXELS) * math.ceil(height / PATCH_PIXELS)

    return max(MINIMUM_IMAGE_TOKENS, patches)


def planned_size(
    width: int,
    height: int,
    long_edge: int | None = SEND_LONG_EDGE,
) -> tuple[int, int]:
    """
    The size a picture will be sent at, without touching the pixels.

    Separate from the resizing itself so that a caller can find out what an
    image is going to cost before deciding whether to send it — which is a
    question worth answering before spending a decode on the answer.

    Rounded to whole patches, because a part-used patch costs a whole token.
    """
    if long_edge is None or max(width, height) <= long_edge:
        return width, height

    scale = long_edge / max(width, height)

    return (
        max(PATCH_PIXELS, round(width * scale / PATCH_PIXELS) * PATCH_PIXELS),
        max(PATCH_PIXELS, round(height * scale / PATCH_PIXELS) * PATCH_PIXELS),
    )


def cost(
    path: str | Path,
    long_edge: int | None = SEND_LONG_EDGE,
) -> int:
    """
    What this picture will cost the context, read from its header alone.

    No decode: a request that will not fit should be refused before anything
    expensive happens, and the header carries everything the arithmetic needs.
    """
    data = read(path)
    sniff(data)

    width, height = planned_size(*dimensions(data), long_edge=long_edge)

    return image_tokens(width, height)


def sniff(data: bytes) -> str:
    """
    The format, from the first few bytes.

    By content rather than by file extension, because the extension is a claim
    made by whoever named the file and the bytes are not.
    """
    if data.startswith(PNG_MAGIC):
        return "png"

    if data.startswith(JPEG_MAGIC):
        return "jpeg"

    raise ImageUnusable(
        "That file is not a PNG or a JPEG. Qronos can look at pictures, "
        "not at whatever this is."
    )


def dimensions(data: bytes) -> tuple[int, int]:
    """
    How large the picture is, read from its header.

    Without decoding it. The size decides whether the thing is worth decoding
    at all, so finding out should not cost as much as the decode would.
    """
    if data.startswith(PNG_MAGIC):
        # IHDR is the first chunk and its width and height are the first two
        # four-byte fields of the chunk's data.
        if len(data) < 24:
            raise ImageUnusable("That PNG is truncated before its header ends.")

        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")

        if width and height:
            return width, height

        raise ImageUnusable("That PNG declares a zero-sized image.")

    if data.startswith(JPEG_MAGIC):
        index = 2

        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue

            marker = data[index + 1]

            # The start-of-frame markers carry the dimensions. C4, C8 and CC
            # look like frame markers and are not.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(data[index + 5 : index + 7], "big")
                width = int.from_bytes(data[index + 7 : index + 9], "big")

                if width and height:
                    return width, height

                raise ImageUnusable("That JPEG declares a zero-sized image.")

            length = int.from_bytes(data[index + 2 : index + 4], "big")

            if length < 2:
                break

            index += 2 + length

        raise ImageUnusable("That JPEG has no readable size in it.")

    raise ImageUnusable("Qronos could not tell what kind of picture that is.")


def read(path: str | Path) -> bytes:
    """The file's bytes, with the reasons it might not be usable named."""
    source = Path(path)

    if not source.exists():
        raise ImageUnusable(f"There is no file at {source}.")

    if source.is_dir():
        raise ImageUnusable(f"{source} is a folder, not a picture.")

    size = source.stat().st_size

    if size == 0:
        raise ImageUnusable(f"{source} is empty.")

    if size > MAX_FILE_BYTES:
        raise ImageUnusable(
            f"{source} is {size // (1024 * 1024)} MB. Qronos looks at "
            f"pictures up to {MAX_FILE_BYTES // (1024 * 1024)} MB; anything "
            "larger is a mistake rather than a request."
        )

    try:
        return source.read_bytes()
    except OSError as error:
        raise ImageUnusable(f"{source} could not be read: {error}") from error


def prepare(
    path: str | Path,
    long_edge: int | None = SEND_LONG_EDGE,
) -> PreparedImage:
    """
    A picture from disk, shrunk if it is worth shrinking, ready to send.

    ``long_edge`` of None sends it untouched, which is for a caller that
    already knows the size is right.
    """
    source = Path(path)
    data = read(source)
    kind = sniff(data)
    width, height = dimensions(data)

    if long_edge is None or max(width, height) <= long_edge:
        return PreparedImage(
            data=data,
            width=width,
            height=height,
            format=kind,
            source=source,
        )

    return _shrink(data, width, height, long_edge, source)


def prepare_bytes(
    data: bytes,
    long_edge: int | None = SEND_LONG_EDGE,
) -> PreparedImage:
    """The same, for a picture that was never on disk — a screen capture."""
    if not data:
        raise ImageUnusable("There is no picture here — the data is empty.")

    kind = sniff(data)
    width, height = dimensions(data)

    if long_edge is None or max(width, height) <= long_edge:
        return PreparedImage(data=data, width=width, height=height, format=kind)

    return _shrink(data, width, height, long_edge, None)


def _shrink(
    data: bytes,
    width: int,
    height: int,
    long_edge: int,
    source: Path | None,
) -> PreparedImage:
    """
    Resize so the longest side is ``long_edge``.

    Pillow is imported here rather than at module scope so that everything
    above — validation, sizes, token arithmetic — works on a machine that has
    no decoder at all, and only resizing needs one.
    """
    try:
        from PIL import Image
    except ImportError as error:  # pragma: no cover - a packaging fault
        raise ImageUnusable(
            "Qronos cannot resize this picture because Pillow is not "
            "installed. It is in requirements.txt; the interpreter running "
            "this is probably not the project's virtual environment."
        ) from error

    new_width, new_height = planned_size(width, height, long_edge)

    try:
        with Image.open(BytesIO(data)) as image:
            shrunk = image.convert("RGB").resize(
                (new_width, new_height), Image.LANCZOS
            )

            buffer = BytesIO()
            shrunk.save(buffer, format="PNG", optimize=True)
    except OSError as error:
        raise ImageUnusable(
            f"That picture could not be opened: {error}"
        ) from error

    return PreparedImage(
        data=buffer.getvalue(),
        width=new_width,
        height=new_height,
        format="png",
        source=source,
        resized=True,
    )
