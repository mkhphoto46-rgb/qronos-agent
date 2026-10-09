"""Shared helpers for the media index tests. Synthetic fixtures only.

Videos are generated with ffmpeg's ``lavfi`` colour sources (two solid
colours, so the only shot change is at a known, measured time), subtitles
are written by the tests, and everything lives under pytest's ``tmp_path``.
Tests that decode need ``QRONOS_MEDIA_FFMPEG_DIR`` pointing at a provisioned
ffmpeg build and are skipped, with that reason, when it is absent.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil

import pytest

from core.activity_guard import ActivityMode, ResourcePressure
from qronos_media.permissions import Approval, MediaPermissionRefused, Pending
from qronos_media.resources import StandaloneCpuAuthority
from qronos_media.service import MediaService
from qronos_media.tools import FFMPEG_DIR_ENV, MediaTools, run_bounded

TOOLS = MediaTools.locate(environ={FFMPEG_DIR_ENV: os.environ.get(FFMPEG_DIR_ENV, "")}) \
    if os.environ.get(FFMPEG_DIR_ENV) else None
requires_ffmpeg = pytest.mark.skipif(
    TOOLS is None, reason=f"{FFMPEG_DIR_ENV} is not set; synthetic container tests need a provisioned ffmpeg",
)

CUT_MS = 2_000  # where the generated video changes colour
DURATION_MS = 4_000


def quiet_activity() -> tuple[ActivityMode, ResourcePressure]:
    return ActivityMode.NORMAL, ResourcePressure.NORMAL


class FakePermissions:
    """Test stand-in for the Qronos gate: allows until told to refuse.

    ``ask`` makes root and export requests wait for a single-use approval,
    like the real adapter under an ALWAYS_ASK policy.
    """

    def __init__(self) -> None:
        self.refuse_roots: dict[str, str] = {}
        self.refuse_exports = False
        self.ask = False
        self.pending: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def _maybe_wait(self, payload: dict, preview: dict):
        if not self.ask:
            return Approval("test-action", "allow", 1.0)
        approval_id = f"pending-{len(self.pending) + 1:04d}-xxxxxxxxxxxxxxxx"
        self.pending[approval_id] = payload
        return Pending(approval_id, "require_ui_confirmation", preview)

    def _take(self, approval_id: str, operation: str) -> dict:
        payload = self.pending.pop(approval_id, None)
        if payload is None or payload["operation"] != operation:
            raise MediaPermissionRefused("stale_approval")
        return payload

    def request_root(self, path: Path, kind: str, max_depth: int):
        self.calls.append(("request_root", str(path)))
        if str(path) in self.refuse_roots:
            raise MediaPermissionRefused(self.refuse_roots[str(path)])
        payload = {"operation": "media.add_root", "path": str(path), "kind": kind, "maxDepth": max_depth}
        return self._maybe_wait(payload, dict(payload))

    def approve_root(self, approval_id: str, decision, secret=None):
        return self._take(approval_id, "media.add_root"), Approval(approval_id, "grant:" + decision.value, 1.0)

    def recheck_root(self, path: Path, approval: Approval, kind: str, max_depth: int) -> None:
        self.calls.append(("recheck_root", str(path)))
        if str(path) in self.refuse_roots:
            raise MediaPermissionRefused(self.refuse_roots[str(path)])

    def request_export(self, destination: Path, content: bytes, sha256: str, details: dict):
        self.calls.append(("request_export", str(destination)))
        if self.refuse_exports:
            raise MediaPermissionRefused("denied")
        payload = {"operation": "media.export_report", "destination": str(destination), "content": content,
                   "sha256": sha256, **details}
        return self._maybe_wait(payload, {"destination": str(destination), "sha256": sha256})

    def approve_export(self, approval_id: str, decision, secret=None):
        return self._take(approval_id, "media.export_report"), Approval(approval_id, "grant:" + decision.value, 1.0)


def make_service(data_dir: Path, *, tools: MediaTools | None = TOOLS, permissions=None, authority=None,
                 cache_quota_bytes: int = 64 * 1024**2, **options) -> MediaService:
    return MediaService(
        data_dir,
        tools=tools,
        authority=authority or StandaloneCpuAuthority(data_dir, activity=quiet_activity, reserve_bytes=0),
        permissions=permissions or FakePermissions(),
        cache_quota_bytes=cache_quota_bytes,
        **options,
    )


def write_srt(path: Path, cues: list[tuple[str, str, str]], encoding: str = "utf-8") -> Path:
    blocks = [f"{index}\n{start} --> {end}\n{text}\n" for index, (start, end, text) in enumerate(cues, 1)]
    path.write_bytes("\n".join(blocks).encode(encoding))
    return path


def make_video(target: Path, *, embedded_srt: Path | None = None, sub_language: str = "fas",
               colours: tuple[str, str] = ("red", "blue"), rates: tuple[int, int] = (10, 10),
               start_offset: float = 0.0) -> Path:
    """Two solid colours, CUT_MS each, a sine tone, and optionally one text subtitle stream.

    ``rates`` different from each other gives a variable-frame-rate file;
    ``start_offset`` shifts every timestamp, so the container starts there.
    """
    assert TOOLS is not None
    seconds = CUT_MS / 1000
    argv = [
        str(TOOLS.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c={colours[0]}:s=160x120:d={seconds}:r={rates[0]}",
        "-f", "lavfi", "-i", f"color=c={colours[1]}:s=160x120:d={seconds}:r={rates[1]}",
        "-f", "lavfi", "-i", f"sine=f=440:d={2 * seconds}",
    ]
    if embedded_srt is not None:
        argv += ["-i", str(embedded_srt)]
    argv += ["-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a"]
    if embedded_srt is not None:
        argv += ["-map", "3:s", "-c:s", "srt", "-metadata:s:s:0", f"language={sub_language}"]
    argv += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]
    if rates[0] != rates[1]:
        argv += ["-fps_mode", "vfr"]
    if start_offset:
        argv += ["-output_ts_offset", f"{start_offset:.3f}"]
    argv.append(str(target))
    result = run_bounded(argv, timeout_seconds=60, stdout_limit=1 << 20, stderr_limit=1 << 20,
                         env=TOOLS.child_env())
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return target


EMBEDDED_CUES = [
    ("00:00:00,500", "00:00:01,800", "Hello there friend"),
    ("00:00:02,200", "00:00:03,500", "I never asked for this"),
]


@pytest.fixture(scope="session")
def base_video(tmp_path_factory) -> Path:
    if TOOLS is None:
        pytest.skip(requires_ffmpeg.kwargs["reason"])
    directory = tmp_path_factory.mktemp("media-base")
    subtitle = write_srt(directory / "embedded.srt", EMBEDDED_CUES)
    return make_video(directory / "base.mkv", embedded_srt=subtitle)


def copy_video(base: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(base, target)
    return target
