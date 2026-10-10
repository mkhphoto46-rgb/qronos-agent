"""Round 19: cleanup of the private decoder copy never hides a stop, never leaks, never deletes a stranger.

Main13 (work/opus-production-installer-review-media-round13.txt) reproduced:
- S1: an identity failure right after the copy is created leaked its handle (the file stayed held);
- S2: a cleanup failure (a same-user reader holding the copy without delete sharing) replaced an in-flight typed
  stop (permission, cancellation, resource) with an OSError, which the stage turned into a failed track;
- S3: the crash sweep deleted by name after its check, so a file swapped in between was deleted.

Native boundary: synthetic files under pytest tmp; CreateFileW readers on those files only; real ffmpeg (the
WinGet test fixture) through the product's run_bounded for the stage cases. Seams are named where used.
"""
from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.permission_control import set_permission_policy
from qronos_media import decoder_copy
from qronos_media import stages as stages_module
from qronos_media.permissions import GatePermissions
from qronos_media.resources import ResourceRefused
from qronos_media.scope import OutOfScope
from qronos_media.store import MediaStore
from qronos_media.tools import ChildCancelled
from tests.media.media_support import base_video, requires_ffmpeg  # noqa: F401
from tests.test_media_scope import FakeClock, policy  # noqa: F401
from tests.test_media_subtitle_handoff import _ASS, _decoder_spawns, _private_copies, _stage, _subtitle_fixture

windows = pytest.mark.skipif(os.name != "nt", reason="Windows share modes")
DATA = b"[Script Info]\nScriptType: v4.00+\n" + b"x" * 4000
DIGEST = hashlib.sha256(DATA).hexdigest()

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _INVALID = wintypes.HANDLE(-1).value


def _reader_without_share_delete(path: Path):
    """A same-user reader (a scanner, an indexer): GENERIC_READ, sharing read|write but NOT delete."""
    handle = _k32.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0, None)
    if handle is None or handle == _INVALID:
        raise OSError(ctypes.get_last_error(), "reader open failed")
    return handle


def _held_by_someone(path: Path) -> bool:
    """True when another handle refuses a delete-capable open (share-all): the file is still held."""
    handle = _k32.CreateFileW(str(path), 0x00010000 | 0x0080, 0x7, None, 3, 0x00200000, None)
    if handle is None or handle == _INVALID:
        return True
    _k32.CloseHandle(handle)  # opened and closed only: nothing is deleted here
    return False


def _copies(cache: Path) -> list[Path]:
    folder = cache / decoder_copy.INPUT_DIR
    return sorted(folder.iterdir()) if folder.exists() else []


# ----------------------------------------------------------------------------------------------------- S1


@windows
def test_s1_an_identity_failure_after_create_closes_the_handle_and_leaves_the_empty_copy_to_the_sweep(
        tmp_path) -> None:
    cache = tmp_path / "cache"
    real_identity = decoder_copy._identity_of
    deletes_by_name: list = []
    with pytest.MonkeyPatch.context() as seam:  # private: the conftest's isolation patches stay in place
        seam.setattr(decoder_copy, "_identity_of", lambda handle: (_ for _ in ()).throw(
            decoder_copy.DecoderInputError("seam: identity failed on a fresh handle")))
        seam.setattr(os, "unlink", lambda *a, **k: deletes_by_name.append(a) or (_ for _ in ()).throw(
            AssertionError("nothing may be deleted by name on this path")))
        with pytest.raises(decoder_copy.DecoderInputError) as raised:
            with decoder_copy.verified_decoder_input(cache, DATA, DIGEST, ".ass"):
                pass
    left = _copies(cache)
    assert len(left) == 1 and left[0].stat().st_size == 0, left  # created, nothing written into it
    assert not _held_by_someone(left[0]), "the constructor's handle leaked: the copy is still held"
    assert deletes_by_name == []
    assert "sweep" in str(raised.value), str(raised.value)  # honest: where the empty copy goes
    assert real_identity is decoder_copy._identity_of
    assert decoder_copy.sweep_stale_inputs(cache) == {"removed": 1, "busy": 0}
    assert _copies(cache) == []
