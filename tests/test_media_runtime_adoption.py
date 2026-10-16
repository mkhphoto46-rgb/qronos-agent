"""Round 37: the pinned runtime set (whisper.cpp v1.9.5 official b5454, FFmpeg 9.0.2 LGPL Qronos build) as Media admits
it. Synthetic files only (stand-in bytes, never executed); no real model, server, player, codec or download."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from qronos_media import asr, tools
from qronos_media.resources import WHISPER_LARGE_V3_TURBO_Q5_0, ModelAsset, ModelSpec

ROOT = Path(__file__).resolve().parent.parent


def asset(parts: tuple[str, ...], data: bytes) -> ModelAsset:
    return ModelAsset(parts, len(data), hashlib.sha256(data).hexdigest())


def write(root: Path, parts: tuple[str, ...], data: bytes) -> None:
    path = Path(root, *parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def synthetic_spec(companions: dict[str, bytes], exe: bytes = b"cli", weights: bytes = b"weights",
                   runtime: str = "whisper.cpp synthetic") -> ModelSpec:
    bin_parts = ("whisper", "bin", "Release")
    return ModelSpec(name="synthetic", vram_mb=1, ram_mb=1,
                     executable=asset(bin_parts + ("whisper-cli.exe",), exe),
                     weights=asset(("whisper", "models", "w.bin"), weights),
                     companions=tuple(asset(bin_parts + (name,), data) for name, data in companions.items()),
                     runtime=runtime)


def provision(root: Path, spec: ModelSpec, exe: bytes = b"cli", weights: bytes = b"weights",
              companions: dict[str, bytes] | None = None) -> None:
    write(root, spec.executable.parts, exe)
    write(root, spec.weights.parts, weights)
    for item in spec.companions:
        write(root, item.parts, (companions or {}).get(item.parts[-1], b"lib:" + item.parts[-1].encode()))


LIBS = {"whisper.dll": b"lib:whisper.dll", "ggml.dll": b"lib:ggml.dll", "ggml-cpu-x64.dll": b"lib:ggml-cpu-x64.dll"}


def test_the_pinned_whisper_runtime_is_the_official_v1_9_5_set_and_the_model_is_unchanged():
    spec = WHISPER_LARGE_V3_TURBO_Q5_0
    assert spec.runtime == "whisper.cpp v1.9.5 (official b5454 whisper-bin-x64)"
    assert (spec.executable.size, spec.executable.sha256) == (
        479_744, "331dba46d6427105d2b802cdbc7eae916ea1c5abf9b0d3b5cfe460d8db8e4366")
    assert (spec.weights.size, spec.weights.sha256) == (  # the model is NOT changed
        574_041_195, "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2")
    names = sorted(item.parts[-1] for item in spec.companions)
    assert names == sorted(["whisper.dll", "ggml.dll", "ggml-base.dll", "ggml-cpu-x64.dll", "ggml-cpu-sse42.dll",
                            "ggml-cpu-sandybridge.dll", "ggml-cpu-haswell.dll", "ggml-cpu-skylakex.dll",
                            "ggml-cpu-icelake.dll", "ggml-cpu-cascadelake.dll", "ggml-cpu-cannonlake.dll",
                            "ggml-cpu-alderlake.dll"])
    assert all(item.parts[:3] == ("whisper", "bin", "Release") for item in spec.companions)


def test_every_library_the_cli_loads_is_verified_and_a_planted_or_missing_one_refuses(tmp_path):
    spec = synthetic_spec(LIBS)
    provision(tmp_path / "ok", spec)
    found = asr.locate_whisper(tmp_path / "ok", spec)
    assert found.provenance()["runtime"] == "whisper.cpp synthetic"
    assert found.provenance()["companionsSha256"] == asr.companions_digest(spec)
    provision(tmp_path / "planted", spec, companions={"ggml.dll": b"lib:ggml.dlX"})  # same size, other bytes
    with pytest.raises(asr.AsrUnavailable, match="ggml.dll"):
        asr.locate_whisper(tmp_path / "planted", spec)
    provision(tmp_path / "missing", spec)
    Path(tmp_path / "missing", *spec.companions[0].parts).unlink()
    with pytest.raises(asr.AsrUnavailable, match="not provisioned"):
        asr.locate_whisper(tmp_path / "missing", spec)


def test_a_pin_change_invalidates_the_verified_cache_and_the_asr_plan(tmp_path):
    old = synthetic_spec(LIBS, runtime="whisper.cpp old")
    provision(tmp_path, old)
    assets = asr.locate_whisper(tmp_path, old)  # cached as verified for THIS pin
    new = replace(old, executable=ModelAsset(old.executable.parts, old.executable.size, "0" * 64))
    with pytest.raises(asr.AsrUnavailable, match="SHA-256"):  # same file state, other pin: hashed again, refused
        asr.locate_whisper(tmp_path, new)
    choice = asr.AudioChoice(1, "eng", True, "default_flag", 0, 0)
    relibbed = replace(old, companions=old.companions[:-1] + (asset(old.companions[-1].parts, b"lib:other"),))
    keys = {asr.plan_key("a" * 64, choice, asr.WhisperAssets(assets.executable, assets.weights, spec))
            for spec in (old, replace(old, runtime="whisper.cpp v1.9.5"), relibbed)}
    assert len(keys) == 3  # a new runtime or library set never resumes chunks of another


def ffmpeg_pins(monkeypatch, ffmpeg: bytes, ffprobe: bytes) -> None:
    monkeypatch.setattr(tools, "SHIPPED_FFMPEG", {"ffmpeg": (len(ffmpeg), hashlib.sha256(ffmpeg).hexdigest()),
                                                  "ffprobe": (len(ffprobe), hashlib.sha256(ffprobe).hexdigest())})
    monkeypatch.setattr(tools, "_PINNED_OK", {})


def test_only_the_pinned_shipping_ffmpeg_is_admitted_from_the_runtime_folder(tmp_path, monkeypatch):
    suffix = ".exe" if tools.os.name == "nt" else ""
    ffmpeg_pins(monkeypatch, b"shipped ffmpeg", b"shipped ffprobe")
    bin_dir = tmp_path / "runtime" / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / f"ffmpeg{suffix}").write_bytes(b"shipped ffmpeg")
    (bin_dir / f"ffprobe{suffix}").write_bytes(b"shipped ffprobe")
    found = tools.MediaTools.locate(environ={}, runtime_assets=tmp_path / "runtime")
    assert found is not None and found.pinned and found.identity == tools.SHIPPED_FFMPEG_IDENTITY
    assert found.provenance() == {"identity": tools.SHIPPED_FFMPEG_IDENTITY, "pinned": True,
                                  "limits": ["av1_video_decode_unavailable"]}
    (bin_dir / f"ffprobe{suffix}").write_bytes(b"GPL ffprobe!!!!")  # same size, another build
    assert tools.MediaTools.locate(environ={}, runtime_assets=tmp_path / "runtime") is None
    flat = tmp_path / "runtime2" / "ffmpeg"  # the old unpinned layout is no longer searched
    flat.mkdir(parents=True)
    (flat / f"ffmpeg{suffix}").write_bytes(b"shipped ffmpeg")
    (flat / f"ffprobe{suffix}").write_bytes(b"shipped ffprobe")
    assert tools.MediaTools.locate(environ={}, runtime_assets=tmp_path / "runtime2") is None


def test_the_development_override_is_reported_as_unpinned(tmp_path, monkeypatch):
    suffix = ".exe" if tools.os.name == "nt" else ""
    ffmpeg_pins(monkeypatch, b"shipped ffmpeg", b"shipped ffprobe")
    (tmp_path / f"ffmpeg{suffix}").write_bytes(b"any developer build")
    (tmp_path / f"ffprobe{suffix}").write_bytes(b"any developer build")
    found = tools.MediaTools.locate(environ={tools.FFMPEG_DIR_ENV: str(tmp_path)})
    assert found is not None and not found.pinned and found.provenance()["identity"] == tools.UNPINNED_IDENTITY
    assert found.limits == ()


def test_the_registry_agrees_with_the_pins_media_admits():
    registry = json.loads((ROOT / "build_support" / "runtime_assets.json").read_text(encoding="utf-8"))
    entries = {item["path"]: item for item in registry["assets"]}
    spec = WHISPER_LARGE_V3_TURBO_Q5_0
    for item in (spec.executable,) + spec.companions:
        entry = entries["runtime/" + "/".join(item.parts)]
        assert entry["status"] == "verified" and (entry["bytes"], entry["sha256"]) == (item.size, item.sha256)
        assert entry["source"].startswith("https://github.com/ggml-org/whisper.cpp/releases/download/b5454/")
    assert entries["runtime/whisper/bin/Release/whisper-server.exe"]["status"] == "verified"
    for name, (size, sha256) in tools.SHIPPED_FFMPEG.items():
        entry = entries[f"runtime/ffmpeg/bin/{name}.exe"]
        assert (entry["bytes"], entry["sha256"]) == (size, sha256)
        assert entry["provenance"] == "reproducible_build_of_signed_source"  # never claimed as a publisher binary
        assert "NOT a publisher binary" in entry["evidence"] and "AV1" in entry["licence"]
    assert entries["runtime/ffmpeg/SOURCE/ffmpeg-9.0.2.tar.xz"]["provenance"] == "publisher"
    # still honest about what is not provisioned or not vouched for
    # The owner-supplied wake head is present but has no published identity (C109): unverified, never hashed.
    assert entries["models/wake_word/qronos.onnx"]["status"] == "unverified"
    assert "sha256" not in entries["models/wake_word/qronos.onnx"]
    assert entries["runtime/chatterbox/models/t3-fa-q4_k.gguf"]["status"] == "unverified"
