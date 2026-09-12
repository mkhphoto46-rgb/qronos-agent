from __future__ import annotations

import hashlib
import json
import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_AUDIO_ROOT = (
    PROJECT_ROOT
    / "runtime"
    / "chatterbox"
    / "temp"
)


def _get_sounddevice() -> Any:
    import sounddevice as sd

    return sd


@dataclass(frozen=True)
class OutputDeviceInfo:
    """One stable, user-facing Windows audio output endpoint."""

    id: str
    name: str
    host_api: str
    device_index: int | None
    is_default: bool
    max_output_channels: int
    default_samplerate: float

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "hostApi": self.host_api,
            "deviceIndex": self.device_index,
            "isDefault": self.is_default,
            "maxOutputChannels": self.max_output_channels,
            "defaultSampleRate": self.default_samplerate,
        }


def _normalise(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _host_api_name(hostapis: object, index: object) -> str:
    try:
        host_index = int(index)
        value = hostapis[host_index]  # type: ignore[index]
        if isinstance(value, dict):
            return str(value.get("name", "")).strip()
        return str(value["name"]).strip()
    except Exception:
        return "PortAudio"


def _default_output_index(sd: Any) -> int | None:
    try:
        raw = sd.default.device
        if isinstance(raw, (tuple, list)):
            raw = raw[1] if len(raw) > 1 else None
        if raw is None:
            return None
        value = int(raw)
        return value if value >= 0 else None
    except Exception:
        return None


def _device_fingerprint(
    *,
    host_api: str,
    name: str,
    max_output_channels: int,
    default_samplerate: float,
) -> str:
    payload = json.dumps(
        {
            "hostApi": _normalise(host_api),
            "name": _normalise(name),
            "maxOutputChannels": int(max_output_channels),
            "defaultSamplerate": round(float(default_samplerate or 0.0), 2),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def _host_api_rank(host_api: str) -> int:
    cleaned = _normalise(host_api)
    if "wasapi" in cleaned:
        return 0
    if "directsound" in cleaned:
        return 1
    if "mme" in cleaned:
        return 2
    if "wdm" in cleaned or "ks" in cleaned:
        return 3
    return 10


def _is_pseudo_output_name(name: str) -> bool:
    cleaned = _normalise(name)
    if not cleaned:
        return True
    if cleaned in {
        "primary sound driver",
        "microsoft sound mapper - output",
        "microsoft sound mapper output",
        "default output",
        "default audio output",
    }:
        return True
    return "sound mapper" in cleaned and "output" in cleaned


def _enumerate_portaudio_outputs() -> tuple[list[OutputDeviceInfo], int | None]:
    sd = _get_sounddevice()
    devices = list(sd.query_devices())
    try:
        hostapis = list(sd.query_hostapis())
    except Exception:
        hostapis = []

    default_index = _default_output_index(sd)
    result: list[OutputDeviceInfo] = []
    duplicate_counts: dict[str, int] = {}

    for index, device in enumerate(devices):
        try:
            max_output_channels = int(device["max_output_channels"])
        except Exception:
            max_output_channels = int(getattr(device, "max_output_channels", 0) or 0)

        if max_output_channels <= 0:
            continue

        try:
            name = str(device["name"]).strip()
        except Exception:
            name = str(getattr(device, "name", "")).strip()

        try:
            host_api_index = device["hostapi"]
        except Exception:
            host_api_index = getattr(device, "hostapi", -1)

        host_api = _host_api_name(hostapis, host_api_index)

        try:
            default_samplerate = float(device["default_samplerate"])
        except Exception:
            default_samplerate = float(
                getattr(device, "default_samplerate", 0.0) or 0.0
            )

        fingerprint = _device_fingerprint(
            host_api=host_api,
            name=name,
            max_output_channels=max_output_channels,
            default_samplerate=default_samplerate,
        )
        ordinal = duplicate_counts.get(fingerprint, 0) + 1
        duplicate_counts[fingerprint] = ordinal

        result.append(
            OutputDeviceInfo(
                id=f"portaudio-output:{fingerprint}:{ordinal}",
                name=name or f"Output device {index}",
                host_api=host_api,
                device_index=index,
                is_default=index == default_index,
                max_output_channels=max_output_channels,
                default_samplerate=default_samplerate,
            )
        )

    return result, default_index


def _preferred_outputs(
    all_outputs: list[OutputDeviceInfo],
    default_index: int | None,
) -> list[OutputDeviceInfo]:
    usable = [
        device
        for device in all_outputs
        if not _is_pseudo_output_name(device.name)
    ]

    wasapi = [
        device
        for device in usable
        if _host_api_rank(device.host_api) == 0
    ]
    pool = wasapi if wasapi else usable

    default_device = next(
        (
            device
            for device in usable
            if device.device_index == default_index
        ),
        None,
    )
    default_name = _normalise(default_device.name) if default_device else ""

    groups: dict[str, list[OutputDeviceInfo]] = {}
    for device in pool:
        groups.setdefault(_normalise(device.name), []).append(device)

    preferred: list[OutputDeviceInfo] = []
    for cleaned_name, devices in groups.items():
        chosen = sorted(
            devices,
            key=lambda item: (
                _host_api_rank(item.host_api),
                item.device_index if item.device_index is not None else 10**9,
            ),
        )[0]

        if default_name and cleaned_name == default_name:
            chosen = OutputDeviceInfo(
                id=chosen.id,
                name=chosen.name,
                host_api=chosen.host_api,
                device_index=chosen.device_index,
                is_default=True,
                max_output_channels=chosen.max_output_channels,
                default_samplerate=chosen.default_samplerate,
            )

        preferred.append(chosen)

    preferred.sort(
        key=lambda item: (
            not item.is_default,
            item.name.lower(),
            item.device_index if item.device_index is not None else 10**9,
        )
    )
    return preferred


def list_output_devices() -> list[OutputDeviceInfo]:
    all_outputs, default_index = _enumerate_portaudio_outputs()
    default = OutputDeviceInfo(
        id="",
        name="System Default Output",
        host_api="Windows / system default",
        device_index=None,
        is_default=True,
        max_output_channels=2,
        default_samplerate=0.0,
    )
    return [default, *_preferred_outputs(all_outputs, default_index)]


def _legacy_match(
    requested_id: str,
    all_outputs: list[OutputDeviceInfo],
) -> OutputDeviceInfo | None:
    requested = next(
        (item for item in all_outputs if item.id == requested_id),
        None,
    )
    if requested is None:
        return None

    name = _normalise(requested.name)
    preferred = _preferred_outputs(all_outputs, None)
    return next(
        (item for item in preferred if _normalise(item.name) == name),
        None,
    )


def canonical_output_device_id(device_id: str | None) -> str:
    requested = str(device_id or "").strip()
    if not requested:
        return ""

    all_outputs, default_index = _enumerate_portaudio_outputs()
    preferred = _preferred_outputs(all_outputs, default_index)

    exact = next((item for item in preferred if item.id == requested), None)
    if exact is not None:
        return exact.id

    legacy = _legacy_match(requested, all_outputs)
    if legacy is not None:
        return legacy.id

    raise LookupError(f"Saved audio output is unavailable: {requested}")


def resolve_output_device(device_id: str | None) -> OutputDeviceInfo | None:
    canonical = canonical_output_device_id(device_id)
    if not canonical:
        return None

    all_outputs, default_index = _enumerate_portaudio_outputs()
    preferred = _preferred_outputs(all_outputs, default_index)
    match = next((item for item in preferred if item.id == canonical), None)
    if match is None:
        raise LookupError(f"Audio output disappeared: {canonical}")
    return match


def _validated_wave_path(path: str | Path) -> Path:
    candidate = Path(path).resolve()
    root = ALLOWED_AUDIO_ROOT.resolve()

    if candidate.suffix.lower() != ".wav":
        raise ValueError("Qronos native audio output accepts WAV files only.")

    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "Qronos refused native audio outside its runtime temp directory."
        ) from exc

    if len(relative.parts) != 1:
        raise ValueError("Qronos refused an invalid runtime audio path.")

    if not candidate.is_file():
        raise FileNotFoundError(f"Qronos audio file does not exist: {candidate}")

    return candidate


def _resample_pcm16(
    samples: np.ndarray,
    source_rate: int,
    target_rate: int,
) -> np.ndarray:
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("Audio sample rates must be positive.")

    if source_rate == target_rate or samples.shape[0] == 0:
        return np.ascontiguousarray(samples, dtype=np.int16)

    source_frames = samples.shape[0]
    target_frames = max(
        1,
        int(round(source_frames * target_rate / source_rate)),
    )

    source_positions = np.arange(source_frames, dtype=np.float64)
    target_positions = np.linspace(
        0.0,
        max(0.0, source_frames - 1.0),
        target_frames,
        dtype=np.float64,
    )

    channels = samples.shape[1]
    output = np.empty((target_frames, channels), dtype=np.float64)
    for channel in range(channels):
        output[:, channel] = np.interp(
            target_positions,
            source_positions,
            samples[:, channel].astype(np.float64, copy=False),
        )

    return np.clip(
        np.rint(output),
        -32768,
        32767,
    ).astype(np.int16)


def play_wave_file(
    path: str | Path,
    device_id: str | None,
) -> dict[str, Any]:
    wave_path = _validated_wave_path(path)
    output = resolve_output_device(device_id)

    if output is None:
        raise ValueError(
            "Native output playback requires an explicit output device. "
            "System Default Output remains owned by the WebView player."
        )

    with wave.open(str(wave_path), "rb") as wav:
        channels = int(wav.getnchannels())
        sample_width = int(wav.getsampwidth())
        source_rate = int(wav.getframerate())
        frames = int(wav.getnframes())
        raw = wav.readframes(frames)

    if sample_width != 2:
        raise ValueError(
            f"Unsupported Qronos WAV sample width: {sample_width * 8} bit."
        )
    if channels <= 0:
        raise ValueError("Qronos WAV has no audio channels.")
    if channels > output.max_output_channels:
        raise ValueError(
            f"Selected output supports {output.max_output_channels} channels, "
            f"but the Qronos WAV has {channels}."
        )

    samples = np.frombuffer(raw, dtype="<i2")
    if samples.size % channels != 0:
        raise ValueError("Qronos WAV PCM payload is malformed.")
    samples = samples.reshape((-1, channels))

    target_rate = int(round(output.default_samplerate or source_rate))
    if target_rate <= 0:
        target_rate = source_rate

    rendered = _resample_pcm16(
        samples,
        source_rate,
        target_rate,
    )

    sd = _get_sounddevice()
    stream = sd.OutputStream(
        samplerate=target_rate,
        channels=channels,
        dtype="int16",
        device=output.device_index,
    )

    try:
        stream.start()
        chunk_frames = max(256, int(round(target_rate * 0.08)))
        for start in range(0, rendered.shape[0], chunk_frames):
            stream.write(rendered[start : start + chunk_frames])
    finally:
        try:
            stream.stop()
        finally:
            stream.close()

    duration = (
        rendered.shape[0] / float(target_rate)
        if target_rate > 0
        else 0.0
    )

    return {
        "ok": True,
        "deviceId": output.id,
        "deviceName": output.name,
        "hostApi": output.host_api,
        "sourceSampleRate": source_rate,
        "outputSampleRate": target_rate,
        "channels": channels,
        "durationSeconds": round(duration, 6),
        "resampled": source_rate != target_rate,
    }
