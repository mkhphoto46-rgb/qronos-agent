"""
Default-off gate for tests that touch real devices.

A live-device test reads or drives something real on the machine running the
suite: the screen or a window's pixels, the foreground window and window list,
the microphone or speakers, the GPU or a local model, the UI Automation tree,
a printer or a phone. Those are acceptance checks, not software tests, and the
person running the suite has not necessarily agreed to them. They run only
when ``QRONOS_RUN_LIVE_DEVICE_TESTS`` is exactly ``1``.

Mark such a test with :func:`requires_live_device`. It applies the registered
``live_device`` pytest marker (skipped by ``tests/conftest.py`` unless opted
in) and an equivalent ``unittest`` skip, so ``python -m unittest`` is gated
too. Independently of markers, ``tests/conftest.py`` blocks the real device
primitives in every test that is not an opted-in live-device test.
"""

from __future__ import annotations

import os
import unittest
from typing import Mapping, TypeVar

import pytest

LIVE_DEVICE_ENV = "QRONOS_RUN_LIVE_DEVICE_TESTS"
LIVE_DEVICE_MARKER = "live_device"
SKIP_REASON = (
    f"Live-device acceptance test: it reads or drives a real device. "
    f"Set {LIVE_DEVICE_ENV}=1 to run it deliberately."
)

T = TypeVar("T")


class LiveDeviceBlocked(AssertionError):
    """A test reached a real device primitive without opting in."""


def live_device_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True only for the exact value ``1``; anything else keeps devices off."""
    value = (os.environ if environ is None else environ).get(LIVE_DEVICE_ENV)
    return value == "1"


def requires_live_device(obj: T) -> T:
    """Mark a test function or class as a live-device acceptance test."""
    marked = getattr(pytest.mark, LIVE_DEVICE_MARKER)(obj)
    return unittest.skipUnless(live_device_enabled(), SKIP_REASON)(marked)
