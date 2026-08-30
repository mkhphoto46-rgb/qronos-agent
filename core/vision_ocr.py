"""
The one line that turns a picture into a hint, or into nothing.

Separate from :mod:`core.windows_ocr` so that the decision — is this reading
worth passing on — has a name and a test, and so that the capture path depends
on a plain ``bytes -> str`` function rather than on an engine.
"""

from __future__ import annotations

from core.windows_ocr import read, useful_hint


def read_screen_text(png: bytes) -> str:
    """
    What Windows can read in this picture, if it is worth reading.

    Never raises: the picture is going to the model regardless, so a hint that
    fails costs nothing and a hint that throws costs the whole request.
    """
    try:
        return useful_hint(read(png))
    except Exception:
        return ""
