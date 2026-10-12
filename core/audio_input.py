from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import numpy as np


def _get_sounddevice() -> Any:
    """Load sounddevice only when real microphone access is required."""
    import sounddevice as sd

    return sd


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 16_000
    channels: int = 1
    frame_size: int = 1_280
    sample_width: int = 2
    device: int | None = None


@dataclass(frozen=True)
class InputDeviceInfo:
    """
    One input device as exposed to Settings.

    ``id`` is deliberately not the PortAudio device index. Device indices can
    move after a reboot, USB reconnect, driver update, or Bluetooth reconnect.
    Qronos persists a fingerprint of stable descriptive fields and resolves it
    back to the current PortAudio index each time the runtime starts.
    """

    id: str
    name: str
    host_api: str
    device_index: int | None
    is_default: bool
    max_input_channels: int

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "hostApi": self.host_api,
            "deviceIndex": self.device_index,
            "isDefault": self.is_default,
            "maxInputChannels": self.max_input_channels,
        }


def _normalise_device_field(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _default_input_index(sd: Any) -> int | None:
    try:
        raw = sd.default.device
        if isinstance(raw, (tuple, list)):
            raw = raw[0] if raw else None

        if raw is None:
            return None

        index = int(raw)
        return index if index >= 0 else None
    except Exception:
        return None


def _host_api_name(hostapis: object, index: object) -> str:
    try:
        host_index = int(index)
        value = hostapis[host_index]  # type: ignore[index]
        if isinstance(value, dict):
            return str(value.get("name", "")).strip()
        return str(value["name"]).strip()
    except Exception:
        return "PortAudio"


def _device_fingerprint(
    *,
    host_api: str,
    name: str,
    max_input_channels: int,
    default_samplerate: float,
) -> str:
    payload = json.dumps(
        {
            "hostApi": _normalise_device_field(host_api),
            "name": _normalise_device_field(name),
            "maxInputChannels": int(max_input_channels),
            "defaultSamplerate": round(float(default_samplerate or 0.0), 2),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")

    return hashlib.sha256(payload).hexdigest()[:20]


def _enumerate_portaudio_inputs() -> tuple[list[InputDeviceInfo], int | None]:
    """Enumerate every PortAudio input endpoint, including host-API aliases."""
    sd = _get_sounddevice()

    devices = list(sd.query_devices())
    try:
        hostapis = list(sd.query_hostapis())
    except Exception:
        hostapis = []

    default_index = _default_input_index(sd)
    result: list[InputDeviceInfo] = []
    duplicate_counts: dict[str, int] = {}

    for index, device in enumerate(devices):
        try:
            max_input_channels = int(device["max_input_channels"])
        except Exception:
            try:
                max_input_channels = int(device.max_input_channels)
            except Exception:
                max_input_channels = 0

        # Output-only endpoints never belong in the microphone selector.
        if max_input_channels <= 0:
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
            max_input_channels=max_input_channels,
            default_samplerate=default_samplerate,
        )

        ordinal = duplicate_counts.get(fingerprint, 0) + 1
        duplicate_counts[fingerprint] = ordinal

        result.append(
            InputDeviceInfo(
                id=f"portaudio:{fingerprint}:{ordinal}",
                name=name or f"Input device {index}",
                host_api=host_api,
                device_index=index,
                is_default=index == default_index,
                max_input_channels=max_input_channels,
            )
        )

    return result, default_index


def _host_api_rank(host_api: str) -> int:
    """Prefer one modern Windows capture backend for each physical endpoint."""
    cleaned = _normalise_device_field(host_api)

    if "wasapi" in cleaned:
        return 0
    if "directsound" in cleaned:
        return 1
    if "mme" in cleaned:
        return 2
    if "wdm" in cleaned or "ks" in cleaned:
        return 3
    return 10


def _is_pseudo_capture_name(name: str) -> bool:
    """Return True for PortAudio/Windows routing aliases, not real endpoints."""
    cleaned = _normalise_device_field(name)

    if not cleaned:
        return True

    exact = {
        "primary sound capture driver",
        "microsoft sound mapper - input",
        "microsoft sound mapper input",
        "default input",
        "default audio input",
    }

    if cleaned in exact:
        return True

    return (
        "sound mapper" in cleaned
        and "input" in cleaned
    )


def _preferred_physical_inputs(
    all_inputs: list[InputDeviceInfo],
    default_index: int | None,
) -> list[InputDeviceInfo]:
    """Return one real user-facing Windows capture endpoint per device name.

    PortAudio exposes routing aliases such as ``Primary Sound Capture Driver``
    and ``Microsoft Sound Mapper - Input`` in addition to the actual endpoint.
    It can also expose the same endpoint through MME, DirectSound and WASAPI.

    On Windows, prefer the WASAPI endpoint set globally when it exists. That is
    the modern endpoint-oriented API and avoids mixing in legacy routing aliases.
    If WASAPI is unavailable, fall back to the remaining real capture endpoints.
    Within the chosen backend set, keep only one entry per visible device name.
    """
    usable = [
        device
        for device in all_inputs
        if not _is_pseudo_capture_name(device.name)
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
    default_name = (
        _normalise_device_field(default_device.name)
        if default_device is not None
        else ""
    )

    groups: dict[str, list[InputDeviceInfo]] = {}
    for device in pool:
        key = _normalise_device_field(device.name)
        groups.setdefault(key, []).append(device)

    selected: list[InputDeviceInfo] = []

    for group in groups.values():
        # Deterministic one-row-per-name policy. Prefer the actual current
        # default endpoint if this group contains it, otherwise the lowest
        # PortAudio index on the preferred backend.
        winner = sorted(
            group,
            key=lambda item: (
                0 if item.device_index == default_index else 1,
                item.device_index if item.device_index is not None else 1_000_000,
            ),
        )[0]

        selected.append(
            InputDeviceInfo(
                id=winner.id,
                name=winner.name,
                host_api=winner.host_api,
                device_index=winner.device_index,
                is_default=(
                    winner.device_index == default_index
                    or (
                        bool(default_name)
                        and _normalise_device_field(winner.name) == default_name
                    )
                ),
                max_input_channels=winner.max_input_channels,
            )
        )

    selected.sort(
        key=lambda item: (
            _normalise_device_field(item.name),
            item.device_index if item.device_index is not None else -1,
        )
    )
    return selected


def list_input_devices() -> list[InputDeviceInfo]:
    """
    Return one user-facing entry per Windows microphone.

    sounddevice/PortAudio can expose the same physical input through several
    Windows host APIs. Those aliases are useful to low-level audio software but
    are noise in Qronos Settings, so the selector keeps the best capture backend
    for each device name and filters every output-only endpoint.
    """
    all_inputs, default_index = _enumerate_portaudio_inputs()
    preferred = _preferred_physical_inputs(all_inputs, default_index)

    return [
        InputDeviceInfo(
            id="",
            name="Default Microphone",
            host_api="Windows / PortAudio default",
            device_index=None,
            is_default=True,
            max_input_channels=1,
        ),
        *preferred,
    ]


def _canonical_input_device(
    device_id: str | None,
) -> InputDeviceInfo | None:
    cleaned = str(device_id or "").strip()

    if not cleaned:
        return None

    all_inputs, default_index = _enumerate_portaudio_inputs()
    preferred = _preferred_physical_inputs(all_inputs, default_index)

    for device in preferred:
        if device.id == cleaned:
            return device

    # Stage 2B originally persisted host-API-specific ids. If a user selected
    # an MME/DirectSound alias before de-duplication, map that old id to the
    # preferred endpoint with the same Windows-facing device name rather than
    # forcing the user to select the microphone again.
    legacy = next(
        (
            device
            for device in all_inputs
            if device.id == cleaned
        ),
        None,
    )

    if legacy is not None:
        if _is_pseudo_capture_name(legacy.name):
            return None

        legacy_name = _normalise_device_field(legacy.name)
        matches = [
            device
            for device in preferred
            if _normalise_device_field(device.name) == legacy_name
        ]

        if matches:
            return matches[0]

        return legacy

    raise LookupError(
        "The selected microphone is no longer available. "
        "Qronos will use the system default until another input is selected."
    )


def canonical_input_device_id(device_id: str | None) -> str:
    """Return the currently displayed id for a persisted microphone choice."""
    device = _canonical_input_device(device_id)
    return "" if device is None else device.id


def resolve_input_device(device_id: str | None) -> int | None:
    """
    Resolve a persisted Settings device id to today's preferred PortAudio index.

    Empty means follow the current system default. Legacy Stage 2B ids from a
    duplicate host API are transparently remapped to the preferred endpoint.
    """
    device = _canonical_input_device(device_id)
    return None if device is None else device.device_index


class AudioInput:
    """Local microphone input for Qronos."""

    def __init__(
        self,
        config: AudioConfig | None = None,
    ) -> None:
        self.config = config or AudioConfig()
        self._stream: Any | None = None

    def start(self) -> None:
        if self.is_running():
            return

        # A PortAudio stream object can survive after the underlying input
        # stream has become inactive. Treat that state as stale instead of
        # assuming a non-None object is usable. Multi-turn voice sessions may
        # otherwise fail on the next read with "Audio input is not running."
        stale_stream = self._stream

        if stale_stream is not None:
            try:
                stale_stream.stop()
            except Exception:
                pass

            try:
                stale_stream.close()
            except Exception:
                pass

            self._stream = None

        sd = _get_sounddevice()

        stream_kwargs: dict[str, Any] = {
            "samplerate": self.config.sample_rate,
            "channels": self.config.channels,
            "dtype": "int16",
            "blocksize": self.config.frame_size,
            "device": self.config.device,
        }

        # Explicit Windows WASAPI endpoints in the field often expose only
        # their native 44.1/48 kHz rate. Qronos itself intentionally remains
        # 16 kHz for Wake/VAD/STT, so ask the Windows Audio Engine to perform
        # shared-mode conversion instead of rejecting the selected endpoint.
        # The system-default path is left untouched because PortAudio already
        # resolves that route compatibly on this machine.
        if self.config.device is not None:
            try:
                device = sd.query_devices(self.config.device)
                host_api_index = device["hostapi"]
                host_api = sd.query_hostapis(host_api_index)
                host_name = str(host_api["name"]).strip().lower()

                if "wasapi" in host_name:
                    stream_kwargs["extra_settings"] = sd.WasapiSettings(
                        auto_convert=True
                    )
            except Exception:
                # Enumeration metadata is advisory here. Preserve the legacy
                # open attempt if the backend cannot describe the endpoint.
                pass

        stream = sd.InputStream(**stream_kwargs)

        try:
            stream.start()
        except Exception:
            stream.close()
            raise

        self._stream = stream

    def stop(self) -> None:
        if self._stream is None:
            return

        self._stream.stop()
        self._stream.close()
        self._stream = None

    def is_running(self) -> bool:
        return (
            self._stream is not None
            and self._stream.active
        )

    def read_frame(self) -> bytes:
        if not self.is_running():
            raise RuntimeError(
                "Audio input is not running."
            )

        audio, _ = self._stream.read(
            self.config.frame_size,
        )

        return np.asarray(
            audio,
            dtype=np.int16,
        ).tobytes()


if __name__ == "__main__":
    audio = AudioInput(
        AudioConfig(device=1),
    )

    print("Starting microphone...")

    audio.start()

    try:
        frame = audio.read_frame()
        print(f"Captured {len(frame)} bytes.")
    finally:
        audio.stop()
        print("Microphone stopped.")
