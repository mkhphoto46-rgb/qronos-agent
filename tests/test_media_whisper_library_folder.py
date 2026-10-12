"""Main33 R1: the verified whisper folder holds no unpinned library, and the whisper child runs in that folder.

whisper.cpp v1.9.5 (GGML_BACKEND_DL) calls ggml_backend_load_all(), which LoadLibrary's every ggml-<backend>*.dll in
the executable's folder AND its working directory. Synthetic files and fake pins only; the one child here is the
base-interpreter fake whisper of tests/test_media_host_asr.py under the real run_bounded. Nothing real is executed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from qronos_media import asr, tools
from qronos_media.resources import ModelAsset, ModelSpec
from tests.test_media_host_asr import runners  # noqa: F401 (the owner's fake whisper fixture)

BIN = ("whisper", "bin", "Release")
LIBS = {"ggml.dll": b"MZ ggml", "ggml-base.dll": b"MZ base", "ggml-cpu-x64.dll": b"MZ cpu x64", "whisper.dll": b"MZ w"}


def pin(parts: tuple[str, ...], data: bytes) -> ModelAsset:
    return ModelAsset(parts, len(data), hashlib.sha256(data).hexdigest())


def put(root: Path, parts: tuple[str, ...], data: bytes) -> Path:
    path = Path(root, *parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def fake_whisper(root: Path, libs: dict[str, bytes] = LIBS) -> ModelSpec:
    spec = ModelSpec(name="fake/whisper", vram_mb=1, ram_mb=1,
                     executable=pin(BIN + ("whisper-cli.exe",), b"MZ cli"),
                     weights=pin(("whisper", "models", "w.bin"), b"weights"),
                     companions=tuple(pin(BIN + (name,), data) for name, data in sorted(libs.items())),
                     runtime="fake runtime")
    put(root, BIN + ("whisper-cli.exe",), b"MZ cli")
    put(root, ("whisper", "models", "w.bin"), b"weights")
    for name, data in libs.items():
        put(root, BIN + (name,), data)
    return spec


def test_control_the_complete_pinned_set_with_its_other_files_is_admitted(tmp_path):
    spec = fake_whisper(tmp_path)
    put(tmp_path, BIN + ("whisper-server.exe",), b"MZ server")  # staged beside it; not a library
    put(tmp_path, BIN + ("LICENSE",), b"MIT")
    found = asr.locate_whisper(tmp_path, spec)
    assert found.executable == Path(tmp_path, *BIN, "whisper-cli.exe") and asr.whisper_cwd(found) == found.executable.parent


def test_a_missing_or_tampered_pinned_library_still_refuses(tmp_path):
    spec = fake_whisper(tmp_path)
    put(tmp_path, BIN + ("ggml-cpu-x64.dll",), b"MZ cpu x65")  # same size, other bytes
    with pytest.raises(asr.AsrUnavailable):
        asr.locate_whisper(tmp_path, spec)
    os.remove(Path(tmp_path, *BIN, "ggml-cpu-x64.dll"))
    with pytest.raises(asr.AsrUnavailable):
        asr.locate_whisper(tmp_path, spec)


@pytest.mark.parametrize("planted", ["ggml-cpu-zen9.dll", "ggml-vulkan.dll", "ggml-cuda-x.dll", "GGML-CPU-X.DLL",
                                     "version.dll"])
def test_an_unpinned_library_beside_the_pinned_set_is_refused(tmp_path, planted):
    spec = fake_whisper(tmp_path)
    put(tmp_path, BIN + (planted,), b"MZ not pinned")
    with pytest.raises(asr.AsrUnavailable, match="not pinned"):
        asr.locate_whisper(tmp_path, spec)


@pytest.mark.parametrize("cwd", ["relative", "string"])
def test_run_bounded_takes_only_an_existing_absolute_folder_as_cwd(tmp_path, cwd):
    value = Path("relative") if cwd == "relative" else str(tmp_path)
    with pytest.raises(ValueError, match="cwd"):
        tools.run_bounded([sys.executable, "-c", "pass"], timeout_seconds=30, stdout_limit=1024, stderr_limit=1024,
                          cwd=value, limits=None)


CWD_RECORDER = r'''
import os, runpy, sys
from pathlib import Path
Path(sys.argv[1]).open("a", encoding="utf-8").write(os.getcwd() + "\n")
runner = sys.argv[2]
sys.argv = [runner, *sys.argv[3:]]
runpy.run_path(runner, run_name="__main__")
'''


@pytest.mark.skipif(os.name != "nt", reason="Job containment and the bridge host are Windows paths")
def test_the_whisper_child_runs_in_its_verified_folder_not_the_callers_directory(tmp_path, runners, monkeypatch):
    from tests.test_media_host_asr import make_authority, prepared_service

    recorder, log = tmp_path / "cwd_recorder.py", tmp_path / "whisper-cwd.log"
    recorder.write_text(CWD_RECORDER, encoding="utf-8")
    fake_launch = asr.launcher  # the owner's fixture launcher: [python, -B, -I, runner, *argv]

    def recording_launch(argv: list[str]) -> list[str]:
        command = fake_launch(argv)
        if argv[0] != str(runners["exe"]):
            return command
        python, flags, runner, rest = command[0], command[1:3], command[3], command[4:]
        return [python, *flags, str(recorder), str(log), runner, *rest]

    monkeypatch.setattr(asr, "launcher", recording_launch)
    caller = tmp_path / "caller-dir"
    caller.mkdir()
    monkeypatch.chdir(caller)  # a directory that is NOT the verified folder
    authority, _ = make_authority(tmp_path)
    service, asset_id, _ = prepared_service(tmp_path, authority, audio_ms=15_000, origin_ms=0, audio_start_ms=0)
    report = service.index(max_seconds=120, max_stages=5)
    assert f"{asset_id}/asr" in report["completed"], json.dumps(report)[:1500]
    seen = {Path(line) for line in log.read_text(encoding="utf-8").splitlines() if line}
    assert seen == {runners["exe"].parent}  # every whisper child ran in the executable's own verified folder

