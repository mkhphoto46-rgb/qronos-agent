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


# ----------------------------------------------------------------------------------------------------- S2


@windows
@pytest.mark.parametrize("stop", [OutOfScope("not_allowed"), ChildCancelled("paused at the native handoff"),
                                  ResourceRefused("activity_mode", "gaming")],
                         ids=["permission", "cancellation", "resource"])
def test_s2_a_blocked_cleanup_never_replaces_an_in_flight_typed_stop(tmp_path, stop) -> None:
    cache = tmp_path / "cache"
    reader = None
    try:
        with pytest.raises(BaseException) as raised:
            with decoder_copy.verified_decoder_input(cache, DATA, DIGEST, ".ass") as copy:
                reader = _reader_without_share_delete(copy)  # a REAL same-user reader blocks the delete
                raise stop
        assert raised.value is stop, f"{type(raised.value).__name__} replaced {type(stop).__name__}"
        notes = " ".join(getattr(raised.value, "__notes__", []))
        assert "could not be removed" in notes and "sweep" in notes, notes  # reported, truthfully
        assert len(_copies(cache)) == 1  # the copy stays (the reader holds it) until a sweep
    finally:
        if reader is not None:
            _k32.CloseHandle(reader)
    assert decoder_copy.sweep_stale_inputs(cache) == {"removed": 1, "busy": 0}


@windows
def test_s2_a_normal_body_whose_cleanup_fails_still_fails(tmp_path) -> None:
    cache = tmp_path / "cache"
    reader = None
    try:
        with pytest.raises(decoder_copy.DecoderInputError):  # the behaviour, not a message wording
            with decoder_copy.verified_decoder_input(cache, DATA, DIGEST, ".ass") as copy:
                reader = _reader_without_share_delete(copy)
    finally:
        if reader is not None:
            _k32.CloseHandle(reader)
    assert decoder_copy.sweep_stale_inputs(cache) == {"removed": 1, "busy": 0}


def _at_copy_ready(monkeypatch, action) -> None:
    """Seam: run ``action(copy)`` once the private copy is ready (before admission 4 and the spawn); its returned
    callable runs after the copy's own cleanup."""
    real = stages_module.verified_decoder_input

    @contextmanager
    def wrapper(*args, **kwargs):
        after = []
        try:
            with real(*args, **kwargs) as copy:
                after.append(action(copy))
                yield copy
        finally:
            for item in after:
                if callable(item):
                    item()

    monkeypatch.setattr(stages_module, "verified_decoder_input", wrapper)


def _subtitle_rows(service) -> list:
    return [tuple(row) for row in service.store.db.execute("SELECT * FROM subtitle_tracks ORDER BY track_id")]


def _stage_raises(monkeypatch) -> list:
    """Record what the subtitles STAGE ITSELF raised, before the runner's pre-commit gate looks at anything."""
    real = stages_module.RUNNERS["subtitles"]
    seen: list = []

    def recording(ctx):
        try:
            return real(ctx)
        except BaseException as exc:
            seen.append(type(exc).__name__)
            raise

    monkeypatch.setitem(stages_module.RUNNERS, "subtitles", recording)
    return seen


@requires_ffmpeg
@windows
def test_s2_stage_a_deny_with_a_reader_blocking_cleanup_stays_a_typed_permission_stop(tmp_path, base_video, policy,
                                                                                    monkeypatch) -> None:
    set_permission_policy("files", "ALLOW")
    frozen = FakeClock()()
    service, asset, sidecar = _subtitle_fixture(tmp_path, base_video, "Film.s2.ass", _ASS,
                                                permissions=GatePermissions(monotonic=lambda: frozen),
                                                monotonic=lambda: frozen)
    readers: list = []

    def deny_with_reader(copy):
        readers.append(_reader_without_share_delete(copy))
        set_permission_policy("files", "DENY")  # admission 4 (right before ffmpeg) will refuse
        return lambda: _k32.CloseHandle(readers[0])

    _at_copy_ready(monkeypatch, deny_with_reader)
    rows_before = _subtitle_rows(service)  # the fixture's earlier complete index
    raised = _stage_raises(monkeypatch)
    spawns = _decoder_spawns(monkeypatch, service.layout.cache)
    report = service.index(max_seconds=60)
    assert readers, "the seam was not reached"
    assert raised == ["OutOfScope"], raised  # typed at the stage, not only by the pre-commit gate
    assert [s for s in spawns if s["private"]] == []
    assert report["stopped"] == "permission" and _stage(service, asset, "subtitles")[0] == "QUEUED", report
    assert _subtitle_rows(service) == rows_before  # nothing committed by this run
    assert len(_private_copies(service)) == 1  # left for the sweep (the reader held it at cleanup)
    assert decoder_copy.sweep_stale_inputs(service.layout.cache) == {"removed": 1, "busy": 0}
    service.close()


@requires_ffmpeg
@windows
def test_s2_stage_a_pause_with_a_reader_blocking_cleanup_stays_a_typed_cancellation(tmp_path, base_video,
                                                                                  monkeypatch) -> None:
    clock = FakeClock()
    service, asset, sidecar = _subtitle_fixture(tmp_path, base_video, "Film.s2p.ass", _ASS, monotonic=clock)
    other = MediaStore(service.layout.database)
    readers: list = []

    def pause_with_reader(copy):
        readers.append(_reader_without_share_delete(copy))
        other.set_meta("paused", "1")  # another process pauses indexing: the checkpoint boundary sees it
        return lambda: _k32.CloseHandle(readers[0])

    _at_copy_ready(monkeypatch, pause_with_reader)
    rows_before = _subtitle_rows(service)
    raised = _stage_raises(monkeypatch)
    spawns = _decoder_spawns(monkeypatch, service.layout.cache)
    report = service.index(max_seconds=60)
    assert readers, "the seam was not reached"
    assert raised == ["ChildCancelled"], raised
    assert [s for s in spawns if s["private"]] == []
    assert report["stopped"] == "paused" and _stage(service, asset, "subtitles") == ("QUEUED", "paused"), report
    assert _subtitle_rows(service) == rows_before  # nothing committed by this run
    other.close()
    service.close()
