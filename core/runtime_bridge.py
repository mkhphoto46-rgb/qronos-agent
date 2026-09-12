from __future__ import annotations

import json
import os
import re
import sys
import threading
import wave
from pathlib import Path

import numpy as np
import time
from dataclasses import dataclass
from typing import Any, Callable


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="backslashreplace",
    )

if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(
        encoding="utf-8",
        errors="backslashreplace",
    )

from core.action_audit import ActionAuditLog
from core.audio_input import AudioConfig, AudioInput, resolve_input_device
from core.chatterbox_runtime import ChatterboxRuntime
from core.command_recorder import CommandRecorder, CommandRecorderConfig
from core.conversation_session import ConversationSession
from core.conversation_history import ConversationHistory
from core.hard_floor import required_vram_mb
from core.load_signal import LoadSample, SustainedLoadMonitor
from core.resource_context import build_sustained_load_monitor
from core.model_registry import MODELS
from core.openwakeword_engine import OpenWakeWordEngine
from core.orchestrator import Orchestrator
from core.spoken_normalizer import normalise_spoken_text
from core.settings_service import DEFAULT_DEFINITIONS, DEFAULT_SETTINGS_PATH, SettingsService
from core.screen_capture import (
    Capture,
    CaptureRefused,
    CaptureUnavailable,
    ScreenCapture,
    foreground_window,
)
from core.queue_scheduler import (
    DEFAULT_SCHEDULER_CONFIG,
    QueueEvent,
    QueueEventType,
    QueueScheduler,
    SchedulerConfig,
)
from core.resource_governor import ResourceGovernor, Weight
from core.resource_policy import ResourceDecision
from core.safe_queue import SafeQueue
from core.task_plan import TaskPlan
from core.arithmetic_fast_path import solve_simple_arithmetic
from core.compute_estimator import ComputeEstimator
from core.intent_gate import IntentGate
from core.routing_input import RoutingInput
from core.task_router import TaskRouter, TaskType
from core.vision_ocr import read_screen_text
from core.vision_worker import build_vision_worker
from core.whisper_cpp_runtime import WhisperCppRuntime
from core.whisper_hybrid_runtime import WhisperHybridRuntime
from core.whisper_cpp_vad_runtime import WhisperCppVADRuntime
from core.web_worker import WebResearchWorker
from security.gate import set_default_audit_sink
from core.voice_trigger import VoiceTriggerService


PUSH_TO_TALK_ACTION = "qronos.push_to_talk"
LOOK_AT_SCREEN_ACTION = "qronos.look_at_screen"
WAKE_LISTENER_START_ACTION = "qronos.wake_listener_start"
WAKE_LISTENER_STOP_ACTION = "qronos.wake_listener_stop"
SETTINGS_RELOAD_ACTION = "qronos.settings_reload"
INPUT_METER_START_ACTION = "qronos.input_meter_start"
INPUT_METER_STOP_ACTION = "qronos.input_meter_stop"
TEXT_MESSAGE_COMMAND = "text_message"
RUNTIME_SET_PAUSED_COMMAND = "runtime_set_paused"
DESKTOP_WAKE_BOOTSTRAP_ENV = "QRONOS_DESKTOP_WAKE_BOOTSTRAP"
CONVERSATION_HISTORY_REQUEST_ACTION = "qronos.conversation_history_request"
MAX_TEXT_MESSAGE_LENGTH = 16_000
MAX_TEXT_REQUEST_ID_LENGTH = 128





VOICE_PLAYBACK_COMPLETE_ACTION = "qronos.voice_playback_complete"

WAKE_PLAYBACK_GUARD_SECONDS = 0.35
FOLLOWUP_START_TIMEOUT_SECONDS = 12.0
FOLLOWUP_PLAYBACK_GUARD_SECONDS = 0.25
WAKE_STARTUP_RETRY_DELAYS_SECONDS = (0.35, 0.8, 1.6)
WAKE_COMMAND_PREP_BUFFER_LIMIT_SECONDS = 30.0
WAKE_COMMAND_PREP_RETRY_DELAYS_SECONDS = (0.0, 0.5, 1.5)

# Desktop owns actual audio playback. Python waits for an explicit playback
# completion acknowledgement instead of guessing from WAV duration.
#
# This limit is only a deadlock failsafe for a crashed/disconnected desktop.
# It does NOT close the conversation.
PLAYBACK_ACK_FAILSAFE_SECONDS = 300.0

# A wake-word candidate needs stronger speech evidence than an
# already-established follow-up turn. This prevents short wind/noise
# bursts from becoming phantom commands after a false wake.
WAKE_MIN_COMMAND_SPEECH_SECONDS = 0.40

VOICE_RETRY_RESPONSE = (
    "این درخواست اجرا نشد. دوباره بگو."
)
VOICE_STT_RETRY_RESPONSE = (
    "متوجه نشدم. دوباره بگو."
)

AUDIO_SPECTRUM_BANDS = 32

# The accepted production Wake threshold is 0.660167. The UI sensitivity slider
# is intentionally constrained to a conservative band around it rather than
# exposing 0.0..1.0 directly. Higher user sensitivity means a lower threshold.
WAKE_SENSITIVITY_DEFAULT = 56
WAKE_THRESHOLD_ACCEPTED = 0.660167
WAKE_THRESHOLD_LOW_SENSITIVITY = 0.72
WAKE_THRESHOLD_HIGH_SENSITIVITY = 0.62


def wake_sensitivity_to_threshold(
    sensitivity: int | float,
) -> float:
    value = max(
        0.0,
        min(
            100.0,
            float(sensitivity),
        ),
    )

    if value <= WAKE_SENSITIVITY_DEFAULT:
        span = float(WAKE_SENSITIVITY_DEFAULT)
        ratio = 0.0 if span == 0.0 else value / span
        threshold = (
            WAKE_THRESHOLD_LOW_SENSITIVITY
            + (
                WAKE_THRESHOLD_ACCEPTED
                - WAKE_THRESHOLD_LOW_SENSITIVITY
            )
            * ratio
        )
    else:
        span = 100.0 - float(WAKE_SENSITIVITY_DEFAULT)
        ratio = (
            value - float(WAKE_SENSITIVITY_DEFAULT)
        ) / span
        threshold = (
            WAKE_THRESHOLD_ACCEPTED
            + (
                WAKE_THRESHOLD_HIGH_SENSITIVITY
                - WAKE_THRESHOLD_ACCEPTED
            )
            * ratio
        )

    return round(
        threshold,
        6,
    )


def _runtime_settings_path() -> Path:
    override = os.environ.get(
        "QRONOS_SETTINGS_PATH",
        "",
    ).strip()

    return (
        Path(override)
        if override
        else DEFAULT_SETTINGS_PATH
    )


def _desktop_wake_bootstrap_enabled() -> bool:
    """
    Return True only for the real desktop runtime process.

    Raw bridge tests intentionally remain side-effect free. The Tauri desktop
    sets this flag so Wake becomes a startup invariant before runtime_ready.
    """
    return (
        os.environ.get(
            DESKTOP_WAKE_BOOTSTRAP_ENV,
            "",
        ).strip()
        == "1"
    )


def _preload_openwakeword_for_desktop() -> None:
    """
    Import OpenWakeWord on the main startup thread before Wake is launched.

    openwakeword.__init__ imports sklearn/scipy. Doing that work before the
    stdin command loop prevents the first Wake thread from competing with
    desktop startup work and keeps the wake gate on the highest-priority path.
    """
    from openwakeword.model import Model as _WakeModel

    _ = _WakeModel


EMIT_LOCK = threading.Lock()

CONVERSATION_END_PHRASES = {
    "تمام",
    "تموم",
    "تمام شد",
    "تموم شد",
    "خداحافظ",
    "بسه",
    "ممنون تمومه",
    "ممنون تمامه",
    "مرسی تمومه",
}

VOICE_LATENCY_LOG_PATH = (
    Path(__file__).resolve().parent.parent
    / "runtime"
    / "chatterbox"
    / "temp"
    / "voice_latency_latest.json"
)


def normalise_qronos_invocation(transcript: str) -> str:
    """Correct measured Whisper variants only when Qronos is addressed."""
    return re.sub(
        r"^(?P<greeting>\s*\u0633\u0644\u0627\u0645\s+)?(?:\u06a9\u0631\u0648\u0646\u0633|\u062e\u0631\u0648\u0646\u0633|\u06a9\u0631\u0648\u0646\u0632)(?=\s|[\u060c,\u061f?]|$)",
        lambda match: f"{match.group('greeting') or ''}\u06a9\u0631\u0648\u0646\u0648\u0633",
        transcript,
        count=1,
    )


def _normalise_conversation_control_text(
    text: str,
) -> str:
    cleaned = re.sub(
        r"[\s\u200c]+",
        " ",
        (text or "").strip().lower(),
    )

    cleaned = re.sub(
        r"[.!?؟،,؛;:]+$",
        "",
        cleaned,
    ).strip()

    return cleaned


def _is_conversation_end_phrase(
    text: str,
) -> bool:
    return (
        _normalise_conversation_control_text(
            text
        )
        in CONVERSATION_END_PHRASES
    )


VOICE_SPECTRUM_FRAME_SECONDS = 0.08
VOICE_SPECTRUM_BANDS = 32


def _write_voice_spectrum_sidecar(
    audio_path: str | Path,
) -> Path:
    """
    Precompute Qronos playback spectrum with the same analyser used for
    microphone input.

    The microphone path calls CommandRecorder._analyze_audio_frame() on
    80 ms PCM frames. Reusing that exact function here keeps playback and
    microphone visuals on the same normalization, RMS envelope, Hann window,
    logarithmic frequency bands, and per-frame band scaling.
    """
    source = Path(audio_path)

    with wave.open(
        str(source),
        "rb",
    ) as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        raw = wav_file.readframes(
            wav_file.getnframes()
        )

    if sample_width != 2:
        raise RuntimeError(
            "Qronos spectrum sidecar requires 16-bit PCM WAV audio."
        )

    samples = np.frombuffer(
        raw,
        dtype=np.int16,
    )

    if channels > 1:
        usable = (
            samples.size
            // channels
            * channels
        )

        samples = (
            samples[:usable]
            .reshape(-1, channels)
            .astype(np.float32)
            .mean(axis=1)
            .clip(-32768, 32767)
            .astype(np.int16)
        )

    frame_size = max(
        1,
        round(
            sample_rate
            * VOICE_SPECTRUM_FRAME_SECONDS
        ),
    )

    frames: list[dict[str, object]] = []

    for start in range(
        0,
        samples.size,
        frame_size,
    ):
        frame = samples[
            start:start + frame_size
        ]

        if frame.size < frame_size:
            padded = np.zeros(
                frame_size,
                dtype=np.int16,
            )
            padded[:frame.size] = frame
            frame = padded

        level, bands = (
            CommandRecorder._analyze_audio_frame(
                frame.tobytes(),
                sample_rate,
                VOICE_SPECTRUM_BANDS,
            )
        )

        frames.append(
            {
                "level": round(
                    float(level),
                    4,
                ),
                "bands": [
                    round(
                        float(value),
                        4,
                    )
                    for value in bands
                ],
            }
        )

    sidecar = source.with_suffix(
        ".spectrum.json"
    )

    sidecar.write_text(
        json.dumps(
            {
                "frameSeconds":
                    VOICE_SPECTRUM_FRAME_SECONDS,
                "frames": frames,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    return sidecar



TTS_FIRST_CHUNK_TARGET_CHARACTERS = 85
TTS_LATER_CHUNK_TARGET_CHARACTERS = 125
TTS_MIN_CHUNK_CHARACTERS = 32


def _split_semantic_tts_chunks(
    text: str,
) -> list[str]:
    """
    Split one finished Brain response into natural speech chunks.

    The Brain response is still complete before this function runs. The goal is
    only to reduce time-to-first-audio by letting Chatterbox synthesize and emit
    the first meaningful phrase before the rest of the response.

    Rules:
    - prefer sentence boundaries
    - then Persian/English clause punctuation
    - avoid tiny standalone chunks such as "سلام!"
    - keep the first chunk shorter than later chunks for lower TTFA
    """
    cleaned = re.sub(
        r"\s+",
        " ",
        (text or "").strip(),
    )

    if not cleaned:
        return []

    pieces = [
        piece.strip()
        for piece in re.split(
            r"(?<=[.!?؟؛])\s+|(?<=[،,:])\s+",
            cleaned,
        )
        if piece.strip()
    ]

    if not pieces:
        return [cleaned]

    chunks: list[str] = []
    current = ""

    for piece in pieces:
        target = (
            TTS_FIRST_CHUNK_TARGET_CHARACTERS
            if not chunks
            else TTS_LATER_CHUNK_TARGET_CHARACTERS
        )

        candidate = (
            piece
            if not current
            else f"{current} {piece}"
        )

        if (
            current
            and len(candidate) > target
            and len(current) >= TTS_MIN_CHUNK_CHARACTERS
        ):
            chunks.append(current)
            current = piece
        else:
            current = candidate

    if current:
        chunks.append(current)

    # Merge a tiny final fragment into the previous chunk so Chatterbox does
    # not synthesize awkward one- or two-word tail utterances.
    if (
        len(chunks) >= 2
        and len(chunks[-1]) < TTS_MIN_CHUNK_CHARACTERS
    ):
        chunks[-2] = (
            f"{chunks[-2]} {chunks[-1]}"
        ).strip()
        chunks.pop()

    return chunks


@dataclass(frozen=True)
class RuntimeEvent:
    event_type: str
    status: str
    message: str


def _text_event_message(
    request_id: str,
    **fields: Any,
) -> str:
    payload: dict[str, Any] = {
        "requestId": request_id,
    }
    payload.update(fields)

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


class QronosRuntime:
    def __init__(
        self,
        scheduler: QueueScheduler | None = None,
        notify: Callable[[str, str, str], None] | None = None,
        settings: SettingsService | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._busy = False
        # Global pause is deliberately process-local. It is never persisted,
        # so a Qronos runtime restart always returns to the active state.
        self._runtime_paused = False
        # Remember whether Wake was alive before the active -> paused
        # transition so a later Resume can restore what Pause suspended.
        # This is also process-local and therefore resets on restart.
        self._resume_wake_after_pause = False

        # Resolved here rather than as a default argument, because emit() is
        # defined below this class and a default is evaluated at definition
        # time — which would be a NameError at import.
        self._notify = notify if notify is not None else emit
        self._scheduler = scheduler

        self.settings = (
            settings
            if settings is not None
            else SettingsService(
                DEFAULT_DEFINITIONS,
                path=_runtime_settings_path(),
                on_problem=lambda message: self._notify(
                    "settings_warning",
                    "warning",
                    message,
                ),
            )
        )
        self._pending_wake_settings_reconcile = False
        self._pending_audio_input_reconcile = False
        self._active_input_device_id = ""
        self._input_meter_enabled = False
        self._input_meter_last_emit = 0.0
        self._input_meter_thread: threading.Thread | None = None
        self._input_meter_stop = threading.Event()

        self.audio_input: AudioInput | None = None
        self.vad_runtime: WhisperCppVADRuntime | None = None
        self.command_recorder: CommandRecorder | None = None
        self.speech_runtime: (
            WhisperHybridRuntime
            | WhisperCppRuntime
            | None
        ) = None

        # One Voice turn can have nested cleanup paths: _run_voice_turn()
        # releases STT before Brain execution, while push_to_talk() keeps a
        # final safety cleanup for failures that happen before transcription.
        # Track whether the current warmed STT generation has already yielded
        # so the second cleanup is an idempotent no-op rather than a duplicate
        # shutdown.
        self._speech_runtime_released = False

        self.voice_output: ChatterboxRuntime | None = None
        self.task_router: TaskRouter | None = None

        # Shadow routing diagnostics are intentionally non-authoritative.
        # They classify the real transcript and record what the future routing
        # stack would choose, while TaskRouter remains the only component that
        # controls the current production path.
        self.intent_gate = IntentGate()
        self.compute_estimator = ComputeEstimator()

        # Pay the classifiers' one-time Python/regex/cache setup cost before
        # the user sends the first command.  Live shadow measurements showed
        # a repeatable ~8-10 ms first-use spike while warm calls stayed near
        # or below 1 ms.  Synchronous startup warmup is preferable here:
        # it happens before runtime_ready, costs only once, and guarantees
        # the first real transcript never races an unfinished background warmup.
        self._prewarm_routing_classifiers()

        self.action_audit: ActionAuditLog | None = None
        self.screen_capture: ScreenCapture | None = None
        self.last_foreground_window: int | None = None
        self.pending_look: str | None = None
        self.orchestrator: Orchestrator | None = None
        self.conversation_session: ConversationSession | None = None
        self.conversation_history = ConversationHistory()

        self.wake_engine: OpenWakeWordEngine | None = None
        self.voice_trigger: VoiceTriggerService | None = None
        self._wake_thread: threading.Thread | None = None
        self._wake_stop = threading.Event()

        # Thread liveness is not Wake readiness. This Event is set only
        # after OpenWakeWord and the selected microphone are genuinely accepting
        # wake frames; command VAD/STT warm-up is explicitly outside this gate.
        self._wake_ready = threading.Event()
        self._wake_start_retry_count = 0
        self._wake_retry_thread: threading.Thread | None = None

        # Wake detection and command recognition have different cold-start
        # requirements. OpenWakeWord + microphone must become available first.
        # The heavier VAD/recorder/STT capture layer warms after Wake is live.
        # A dedicated event/lock makes this split deterministic and prevents
        # duplicate VAD preparation when PTT or a very early wake races it.
        self._wake_command_prepare_lock = threading.Lock()
        self._wake_command_ready = threading.Event()
        self._wake_command_prepare_thread: threading.Thread | None = None
        self._wake_command_prepare_error: str | None = None

        # Playback lifecycle is synchronized with the desktop media engine.
        # One ID identifies one complete Qronos response, even when that
        # response contains several sequential TTS chunks.
        self._voice_playback_complete = threading.Event()
        self._voice_playback_id = 0
        self._pending_playback_id: int | None = None

        # A real spoken turn must be distinguished from a wake candidate
        # that never produced valid speech. Brain/resource/TTS failures
        # after genuine speech must never be treated as false wakes.
        self._last_voice_turn_had_valid_speech_evidence = False

    def ensure_scheduler(self) -> QueueScheduler:
        """
        The queue, built the first time anybody asks for it.

        Deliberately not built in ``prepare()``, which opens the microphone —
        the queue has to work without any of that. Deliberately not built at
        startup either: four tests in
        ``tests/test_runtime_bridge_process.py`` depend on a freshly started
        bridge having no side effects, and a sampler thread launching
        ``nvidia-smi`` is a side effect. The desktop asks for the queue as
        soon as it sees ``runtime_ready``, so in practice the delay is
        imperceptible.
        """
        with self._lock:
            if self._scheduler is not None:
                return self._scheduler

            monitor = build_sustained_load_monitor()
            monitor.prime()

            self._scheduler = QueueScheduler(
                queue=SafeQueue(),
                governor=ResourceGovernor(),
                monitor=monitor,
                notify=self._on_queue_event,
                config=_scheduler_config_from_environment(),
            )
            self._scheduler.start()

            return self._scheduler

    @property
    def scheduler(self) -> QueueScheduler | None:
        """The queue if one has been built, without building one."""
        return self._scheduler

    def _on_queue_event(self, event: QueueEvent) -> None:
        """
        Put one queue event on the wire, then the whole queue after it.

        Whole state rather than a delta, and for a concrete reason: the Rust
        reader re-emits any line it cannot parse as a log entry rather than
        dropping it, so a single mangled line would desynchronise a delta
        stream permanently and silently. A few hundred bytes of full state
        repairs itself on the next event.
        """
        if event.event is not QueueEventType.CHANGED:
            event_type, status = _QUEUE_EVENT_NAMES[event.event]
            self._notify(event_type, status, _queue_event_message(event))

        scheduler = self._scheduler

        if scheduler is not None:
            self._notify(
                "queue_changed",
                "ready",
                _queue_view_message(scheduler.view()),
            )

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def runtime_paused(self) -> bool:
        with self._lock:
            return self._runtime_paused

    @property
    def wake_word_enabled(self) -> bool:
        return bool(
            self.settings.get(
                "voice.wake_word_enabled"
            )
        )

    @property
    def voice_language(self) -> str:
        value = str(
            self.settings.get(
                "voice.language"
            )
        ).strip().lower()

        return (
            value
            if value in {
                "auto",
                "fa",
                "en",
            }
            else "fa"
        )

    @property
    def wake_sensitivity(self) -> int:
        return int(
            self.settings.get(
                "voice.wake_sensitivity"
            )
        )

    @property
    def wake_threshold(self) -> float:
        return wake_sensitivity_to_threshold(
            self.wake_sensitivity
        )

    @property
    def input_device_id(self) -> str:
        value = self.settings.get(
            "voice.input_device_id"
        )
        return str(value or "").strip()

    def _resolved_input_device_index(
        self,
        device_id: str | None = None,
    ) -> int | None:
        requested = (
            self.input_device_id
            if device_id is None
            else str(device_id or "").strip()
        )

        try:
            return resolve_input_device(
                requested
            )
        except LookupError as exc:
            self._notify(
                "microphone_selection_warning",
                "warning",
                str(exc),
            )
            return None

    def _new_audio_input_from_settings(
        self,
    ) -> AudioInput:
        requested = self.input_device_id
        device_index = (
            self._resolved_input_device_index(
                requested
            )
        )
        self._active_input_device_id = (
            requested
        )
        return AudioInput(
            AudioConfig(
                device=device_index,
            )
        )

    def _replace_audio_input_from_settings(
        self,
    ) -> None:
        old_audio = self.audio_input

        if old_audio is not None:
            try:
                old_audio.stop()
            except Exception:
                pass

        audio_input = (
            self._new_audio_input_from_settings()
        )
        self.audio_input = audio_input

        if self.vad_runtime is not None:
            self.command_recorder = (
                CommandRecorder(
                    audio_input=audio_input,
                    vad_runtime=self.vad_runtime,
                    config=CommandRecorderConfig(
                        silence_seconds=0.96,
                        start_timeout_seconds=(
                            FOLLOWUP_START_TIMEOUT_SECONDS
                        ),
                    ),
                )
            )

        self._pending_audio_input_reconcile = False

    def _reconcile_audio_input_without_wake(
        self,
    ) -> None:
        # A Settings meter may temporarily own the selected microphone while
        # Wake is disabled. Stop that exclusive stream, swap the configured
        # AudioInput, then resume the meter on the newly selected device.
        meter_was_enabled = (
            self._input_meter_enabled
        )
        self._stop_standalone_input_meter()
        self._replace_audio_input_from_settings()

        if (
            meter_was_enabled
            and not self.runtime_paused
        ):
            self._start_standalone_input_meter()

    def _reconcile_audio_input_in_wake_thread(
        self,
    ) -> None:
        if not self._pending_audio_input_reconcile:
            return

        if self.audio_input is None:
            self._replace_audio_input_from_settings()
            return

        was_running = self.audio_input.is_running()

        self._replace_audio_input_from_settings()

        if (
            was_running
            and not self._wake_stop.is_set()
        ):
            self.audio_input.start()

        self._notify(
            "microphone_selection_applied",
            "ready",
            json.dumps(
                {
                    "deviceId":
                        self._active_input_device_id,
                    "usingSystemDefault":
                        self.audio_input.config.device
                        is None,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

    def _reconcile_wake_setting(self) -> None:
        if self.runtime_paused:
            with self._lock:
                self._resume_wake_after_pause = (
                    self.wake_word_enabled
                )
            return

        if self.wake_word_enabled:
            if not self.wake_listener_running:
                self.start_wake_listener()
            return

        if self.wake_listener_running:
            self.stop_wake_listener()

    def reload_settings(
        self,
        settings: SettingsService | None = None,
    ) -> None:
        previous_input_device_id = (
            self.input_device_id
        )

        next_settings = (
            settings
            if settings is not None
            else SettingsService(
                DEFAULT_DEFINITIONS,
                path=_runtime_settings_path(),
                on_problem=lambda message: self._notify(
                    "settings_warning",
                    "warning",
                    message,
                ),
            )
        )

        if next_settings.write_blocked_reason is not None:
            self._notify(
                "settings_apply_failed",
                "warning",
                next_settings.write_blocked_reason,
            )
            return

        with self._lock:
            self.settings = next_settings
            busy = self._busy

            if busy:
                self._pending_wake_settings_reconcile = True

        input_changed = (
            previous_input_device_id
            != self.input_device_id
        )

        if input_changed:
            self._pending_audio_input_reconcile = True

        engine = self.wake_engine
        if engine is not None:
            engine.threshold = self.wake_threshold

        if (
            input_changed
            and not busy
            and not self.wake_listener_running
        ):
            self._reconcile_audio_input_without_wake()

        if not busy:
            self._reconcile_wake_setting()

        self._notify(
            "settings_applied",
            "ready",
            json.dumps(
                {
                    "wakeWordEnabled":
                        self.wake_word_enabled,
                    "wakeSensitivity":
                        self.wake_sensitivity,
                    "wakeThreshold":
                        self.wake_threshold,
                    "voiceLanguage":
                        self.voice_language,
                    "inputDeviceId":
                        self.input_device_id,
                    "inputDeviceReconcileDeferred":
                        bool(
                            self._pending_audio_input_reconcile
                            and self.wake_listener_running
                        ),
                    "wakeReconcileDeferred":
                        busy,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

    def _set_busy(self, value: bool) -> None:
        reconcile = False

        with self._lock:
            self._busy = value

            if (
                not value
                and self._pending_wake_settings_reconcile
            ):
                self._pending_wake_settings_reconcile = False
                reconcile = True

        if reconcile:
            self._reconcile_wake_setting()

        if (
            not value
            and self._pending_audio_input_reconcile
            and not self.wake_listener_running
        ):
            self._reconcile_audio_input_without_wake()

    def _notify_runtime_pause_state(self) -> None:
        self._notify(
            "runtime_pause_changed",
            "paused" if self.runtime_paused else "ready",
            _compact(
                {
                    "paused": self.runtime_paused,
                    "temporary": True,
                    "resetsOnRestart": True,
                }
            ),
        )

    def _reject_if_runtime_paused(self, channel: str) -> bool:
        if not self.runtime_paused:
            return False

        self._notify(
            "runtime_paused_rejected",
            "paused",
            _compact(
                {
                    "channel": channel,
                    "temporary": True,
                }
            ),
        )
        return True

    def _require_runtime_active(self, channel: str) -> None:
        if self.runtime_paused:
            raise RuntimeError(
                f"Qronos runtime is temporarily paused; {channel} is unavailable."
            )

    def set_runtime_paused(self, paused: bool) -> None:
        if not isinstance(paused, bool):
            raise ValueError(
                "runtime_set_paused needs paused to be true or false."
            )

        # Capture the Wake state before the transition. Repeated Pause commands
        # must not erase the original resume intent after Wake has been stopped.
        wake_was_running = self.wake_listener_running if paused else False

        with self._lock:
            was_paused = self._runtime_paused

            if paused and not was_paused:
                self._resume_wake_after_pause = wake_was_running

            self._runtime_paused = paused
            busy = self._busy
            scheduler = self._scheduler

            resume_wake = (
                not paused
                and was_paused
                and self._resume_wake_after_pause
            )

            if not paused:
                self._resume_wake_after_pause = False

        # The queue follows the global runtime state, but this command never
        # creates the queue merely to pause it.
        if scheduler is not None:
            scheduler.set_paused(paused)

        if paused:
            # Stop accepting new voice input immediately and release idle voice
            # resources. An already-running non-cancellable model call is not
            # force-killed; downstream checkpoints suppress later phases.
            self._stop_standalone_input_meter()
            self.stop_wake_listener()
            self._release_speech_runtime()

            if not busy:
                self._release_brains_for_voice_phase()
                self._release_voice_output_runtime()

        elif resume_wake:
            # Resume is owned by the runtime itself rather than a second
            # frontend action. This makes Pause -> Active a single lifecycle
            # transition and prevents Wake from remaining stopped after Resume.
            self.start_wake_listener()

        elif self._input_meter_enabled:
            self._start_standalone_input_meter()

        self._notify_runtime_pause_state()

    def _prewarm_routing_classifiers(self) -> None:
        """Warm deterministic routing before the first user command.

        The Gate and Estimator remain shadow-only at this stage.  Warmup is
        deliberately best-effort so a diagnostic classifier can never block
        Qronos startup while the legacy TaskRouter is still authoritative.
        """
        samples = (
            "سلام",
            "پایتخت کانادا چیه؟",
            "چرا آسمان آبیه؟",
            "دو به علاوه دو چند میشه؟",
        )

        try:
            for sample in samples:
                routing_input = RoutingInput.from_text(
                    sample
                )
                self.intent_gate.classify(
                    routing_input
                )
                self.compute_estimator.estimate(
                    routing_input
                )
        except Exception:
            # Shadow diagnostics are non-authoritative.  Runtime startup must
            # remain available even if warmup itself ever regresses.
            pass

    def _routing_shadow(
        self,
        transcript: str,
    ) -> dict[str, Any]:
        """Classify one real transcript without changing its production route.

        This is diagnostic-only. Any failure is captured in the returned payload
        instead of escaping into the voice turn, and TaskRouter remains the sole
        authority for selected_task_type until the Resolver is separately approved.
        """
        started = time.perf_counter()

        try:
            routing_input = RoutingInput.from_text(
                transcript
            )
            intent = self.intent_gate.classify(
                routing_input
            )
            compute = self.compute_estimator.estimate(
                routing_input
            )

            return {
                "primaryIntent": intent.primary_intent.value,
                "requiredIntents": [
                    item.value
                    for item in intent.required_intents
                ],
                "intentConfidence": round(
                    float(intent.confidence),
                    6,
                ),
                "accuracyRisk": intent.accuracy_risk.value,
                "intentSignals": list(
                    intent.signals
                ),
                "computeLevel": compute.level.value,
                "computeScore": int(
                    compute.score
                ),
                "computeConfidence": round(
                    float(compute.confidence),
                    6,
                ),
                "computeFactors": list(
                    compute.factors
                ),
                "elapsedMs": round(
                    (time.perf_counter() - started)
                    * 1000.0,
                    6,
                ),
                "authoritative": False,
            }
        except Exception as exc:
            return {
                "error": str(exc),
                "elapsedMs": round(
                    (time.perf_counter() - started)
                    * 1000.0,
                    6,
                ),
                "authoritative": False,
            }

    def _emit_audio_spectrum(
        self,
        level: float,
        bands: tuple[float, ...],
    ) -> None:
        payload = {
            "level": round(
                max(0.0, min(1.0, level)),
                4,
            ),
            "bands": [
                round(
                    max(0.0, min(1.0, value)),
                    4,
                )
                for value in bands[:AUDIO_SPECTRUM_BANDS]
            ],
        }

        emit(
            "voice_audio_spectrum",
            "listening",
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

        if self._input_meter_enabled:
            self._emit_input_meter_level(
                payload["level"]
            )

    def _clear_audio_spectrum(self) -> None:
        self._emit_audio_spectrum(
            0.0,
            tuple(
                0.0
                for _ in range(AUDIO_SPECTRUM_BANDS)
            ),
        )

    def _emit_input_meter_level(
        self,
        level: float,
        *,
        active: bool = True,
        force: bool = False,
    ) -> None:
        if (
            not self._input_meter_enabled
            and not force
        ):
            return

        now = time.monotonic()

        if (
            not force
            and now - self._input_meter_last_emit
            < 0.10
        ):
            return

        self._input_meter_last_emit = now

        self._notify(
            "microphone_input_level",
            "listening" if active else "ready",
            json.dumps(
                {
                    "level": round(
                        max(
                            0.0,
                            min(
                                1.0,
                                float(level),
                            ),
                        ),
                        4,
                    ),
                    "active": bool(active),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

    @staticmethod
    def _frame_input_level(
        frame: bytes,
    ) -> float:
        samples = np.frombuffer(
            frame,
            dtype=np.int16,
        ).astype(
            np.float32,
            copy=False,
        )

        if samples.size == 0:
            return 0.0

        normalized = samples / 32768.0
        rms = float(
            np.sqrt(
                np.mean(
                    normalized *
                    normalized
                )
            )
        )

        # Speech RMS is naturally much lower than full-scale PCM. A small
        # display-only gain makes ordinary speech visible without changing the
        # audio sent to Wake/VAD/STT.
        return max(
            0.0,
            min(
                1.0,
                rms * 4.0,
            ),
        )

    def _maybe_emit_input_meter_frame(
        self,
        frame: bytes,
    ) -> None:
        if not self._input_meter_enabled:
            return

        self._emit_input_meter_level(
            self._frame_input_level(
                frame
            )
        )

    def _standalone_input_meter_loop(
        self,
    ) -> None:
        audio: AudioInput | None = None

        try:
            audio = (
                self._new_audio_input_from_settings()
            )
            audio.start()

            while (
                self._input_meter_enabled
                and not
                self._input_meter_stop.is_set()
                and not
                self.wake_listener_running
                and not
                self.runtime_paused
            ):
                frame = audio.read_frame()
                self._maybe_emit_input_meter_frame(
                    frame
                )

        except Exception as exc:
            if (
                self._input_meter_enabled
                and not self.runtime_paused
            ):
                self._notify(
                    "microphone_meter_warning",
                    "warning",
                    str(exc),
                )

        finally:
            if audio is not None:
                try:
                    audio.stop()
                except Exception:
                    pass

            self._emit_input_meter_level(
                0.0,
                active=False,
                force=True,
            )

            if (
                self._input_meter_thread
                is threading.current_thread()
            ):
                self._input_meter_thread = None

    def _start_standalone_input_meter(
        self,
    ) -> None:
        if (
            not self._input_meter_enabled
            or self.runtime_paused
            or self.wake_listener_running
        ):
            return

        thread = self._input_meter_thread
        if (
            thread is not None
            and thread.is_alive()
        ):
            return

        self._input_meter_stop.clear()

        thread = threading.Thread(
            target=self._standalone_input_meter_loop,
            name="qronos-input-meter",
            daemon=True,
        )
        self._input_meter_thread = thread
        thread.start()

    def _stop_standalone_input_meter(
        self,
    ) -> None:
        self._input_meter_stop.set()

        thread = self._input_meter_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not
            threading.current_thread()
        ):
            thread.join(timeout=0.75)

        if (
            thread is not
            threading.current_thread()
        ):
            self._input_meter_thread = None

    def start_input_meter(self) -> None:
        self._input_meter_enabled = True
        self._input_meter_last_emit = 0.0

        if not self.wake_listener_running:
            self._start_standalone_input_meter()

    def stop_input_meter(self) -> None:
        self._input_meter_enabled = False
        self._stop_standalone_input_meter()
        self._emit_input_meter_level(
            0.0,
            active=False,
            force=True,
        )

    def _record_conversation_message(
        self,
        *,
        role: str,
        text: str,
        source: str,
        request_id: str = "",
    ) -> None:
        message = self.conversation_history.append(
            role=role,
            text=text,
            source=source,
            request_id=request_id,
        )

        self._notify(
            "conversation_message_added",
            "ready",
            json.dumps(
                message.to_public_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def emit_conversation_history_snapshot(
        self,
    ) -> None:
        self._notify(
            "conversation_history_snapshot",
            "ready",
            self.conversation_history.snapshot_json(),
        )

    def _conversation_brain_context(
        self,
    ):
        return (
            self.conversation_history.brain_context()
        )

    def _ensure_text_runtime(self) -> None:
        """
        Prepare only the collaborators needed for typed Fast/Heavy turns.

        Text Chat must not warm the microphone, Whisper, VAD, or Chatterbox.
        If the full voice runtime is already prepared, reuse its router and
        orchestrator instead of constructing parallel Brain state.
        """
        if self.task_router is None:
            self.task_router = TaskRouter()

        if self.orchestrator is None:
            self.orchestrator = Orchestrator()

    def run_text_turn(
        self,
        text: str,
        request_id: str,
    ) -> None:
        """
        Execute one typed request through the production TaskRouter/Orchestrator.

        V1 intentionally supports only FAST and HEAVY. Vision, browser, and
        computer-action text turns need their own permission/approval contracts
        and are not silently executed through the chat box.
        """
        cleaned_text = (text or "").strip()
        cleaned_request_id = (request_id or "").strip()

        if self.runtime_paused:
            self._notify(
                "text_error",
                "paused",
                _text_event_message(
                    cleaned_request_id,
                    error="Qronos runtime is temporarily paused.",
                ),
            )
            return

        if self.is_busy:
            self._notify(
                "text_error",
                "busy",
                _text_event_message(
                    cleaned_request_id,
                    error=(
                        "Qronos is already processing another request. "
                        "Try again after the current turn finishes."
                    ),
                ),
            )
            return

        self._set_busy(True)
        started = time.perf_counter()
        route_name = ""

        try:
            if not cleaned_text:
                raise ValueError(
                    "Text message must not be empty."
                )

            if (
                len(cleaned_text)
                > MAX_TEXT_MESSAGE_LENGTH
            ):
                raise ValueError(
                    "Text message exceeds the "
                    f"{MAX_TEXT_MESSAGE_LENGTH} character limit."
                )

            if not cleaned_request_id:
                raise ValueError(
                    "Text request id must not be empty."
                )

            if (
                len(cleaned_request_id)
                > MAX_TEXT_REQUEST_ID_LENGTH
            ):
                raise ValueError(
                    "Text request id exceeds the "
                    f"{MAX_TEXT_REQUEST_ID_LENGTH} character limit."
                )

            self._ensure_text_runtime()

            if (
                self.task_router is None
                or self.orchestrator is None
            ):
                raise RuntimeError(
                    "Qronos text runtime did not prepare correctly."
                )

            conversation_context = (
                self._conversation_brain_context()
            )

            self._record_conversation_message(
                role="user",
                text=cleaned_text,
                source="text",
                request_id=cleaned_request_id,
            )

            self._notify(
                "text_request_started",
                "processing",
                _text_event_message(
                    cleaned_request_id,
                    text=cleaned_text,
                ),
            )

            route = self.task_router.route(
                cleaned_text
            )
            selected_task_type = route.task_type
            route_name = (
                selected_task_type.value
            )

            self._notify(
                "text_routed",
                "processing",
                _text_event_message(
                    cleaned_request_id,
                    route=route_name,
                ),
            )

            if selected_task_type not in (
                TaskType.FAST,
                TaskType.HEAVY,
            ):
                raise RuntimeError(
                    "Text Chat V1 currently supports Fast and Heavy "
                    "responses only. Vision, browser, and computer "
                    "actions remain behind their dedicated approval paths."
                )

            arithmetic_answer = (
                solve_simple_arithmetic(
                    cleaned_text
                )
                if selected_task_type
                is TaskType.FAST
                else None
            )

            if arithmetic_answer is not None:
                response = (
                    arithmetic_answer.spoken_text
                )
            else:
                plan = TaskPlan(
                    goal=cleaned_text
                )
                plan.add_step(
                    task_type=selected_task_type,
                    description=cleaned_text,
                )

                self._require_runtime_active("text execution")

                results = (
                    self.orchestrator.execute_plan(
                        plan,
                        conversation_messages=(
                            conversation_context
                        ),
                    )
                )

                self._require_runtime_active("text response")

                self._require_runtime_active("voice response")

                if not results:
                    raise RuntimeError(
                        "Qronos did not return an execution result."
                    )

                result = results[-1]

                if not result.success:
                    raise RuntimeError(
                        result.error
                        or "Qronos task failed."
                    )

                response = (
                    result.output.strip()
                )

            if not response:
                raise RuntimeError(
                    "Qronos returned an empty response."
                )

            elapsed = (
                time.perf_counter()
                - started
            )

            self._record_conversation_message(
                role="assistant",
                text=response,
                source="text",
                request_id=cleaned_request_id,
            )

            self._notify(
                "text_response",
                "ready",
                _text_event_message(
                    cleaned_request_id,
                    text=response,
                    route=route_name,
                    elapsedSeconds=round(
                        elapsed,
                        3,
                    ),
                ),
            )

        except Exception as exc:
            elapsed = (
                time.perf_counter()
                - started
            )

            self._notify(
                "text_error",
                "error",
                _text_event_message(
                    cleaned_request_id,
                    error=str(exc),
                    route=route_name,
                    elapsedSeconds=round(
                        elapsed,
                        3,
                    ),
                ),
            )

        finally:
            self._set_busy(False)

    def prepare_wake_gate(self) -> None:
        """Prepare only what is required to hear the Qronos wake word."""
        if self.audio_input is None:
            self.audio_input = (
                self._new_audio_input_from_settings()
            )

        if self.conversation_session is None:
            self.conversation_session = (
                ConversationSession()
            )

    def prepare_wake_capture(self) -> None:
        """
        Prepare command capture without making Wake readiness depend on it.

        This path is intentionally heavier than ``prepare_wake_gate`` because
        Whisper.cpp VAD can have a noticeable cold start. It is serialized so
        a background Wake warm-up, PTT, and an early spoken turn cannot create
        duplicate VAD/recorder instances.
        """
        self.prepare_wake_gate()

        if (
            self.vad_runtime is not None
            and self.command_recorder is not None
            and self.speech_runtime is not None
        ):
            self._wake_command_prepare_error = None
            self._wake_command_ready.set()
            return

        with self._wake_command_prepare_lock:
            if (
                self.vad_runtime is not None
                and self.command_recorder is not None
                and self.speech_runtime is not None
            ):
                self._wake_command_prepare_error = None
                self._wake_command_ready.set()
                return

            try:
                if self.vad_runtime is None:
                    vad_runtime = (
                        WhisperCppVADRuntime()
                    )

                    if not vad_runtime.health_check():
                        raise RuntimeError(
                            "Qronos VAD runtime is not ready."
                        )

                    vad_runtime.prepare()
                    self.vad_runtime = vad_runtime

                if self.command_recorder is None:
                    self.command_recorder = (
                        CommandRecorder(
                            audio_input=self.audio_input,
                            vad_runtime=self.vad_runtime,
                            config=CommandRecorderConfig(
                                silence_seconds=0.96,
                                start_timeout_seconds=(
                                    FOLLOWUP_START_TIMEOUT_SECONDS
                                ),
                            ),
                        )
                    )

                if self.speech_runtime is None:
                    self.speech_runtime = (
                        WhisperHybridRuntime()
                    )

            except Exception as exc:
                self._wake_command_prepare_error = str(exc)
                self._wake_command_ready.clear()
                raise

            self._wake_command_prepare_error = None
            self._wake_command_ready.set()

    def _warm_wake_command_capture_async(self) -> None:
        """Warm VAD/recorder/STT after the wake gate is already listening."""
        if self._wake_command_ready.is_set():
            return

        thread = self._wake_command_prepare_thread
        if thread is not None and thread.is_alive():
            return

        def worker() -> None:
            last_error: Exception | None = None

            for delay in WAKE_COMMAND_PREP_RETRY_DELAYS_SECONDS:
                if self._wake_stop.is_set():
                    return

                if delay > 0 and self._wake_stop.wait(delay):
                    return

                try:
                    self.prepare_wake_capture()
                    self._notify(
                        "voice_capture_ready",
                        "ready",
                        "Qronos command capture is warmed.",
                    )
                    return
                except Exception as exc:
                    last_error = exc

            if last_error is not None and not self._wake_stop.is_set():
                self._notify(
                    "runtime_warning",
                    "warning",
                    "Command capture warm-up failed while Wake remains active: "
                    + str(last_error),
                )

        worker_thread = threading.Thread(
            target=worker,
            name="qronos-wake-command-prewarm",
            daemon=True,
        )
        self._wake_command_prepare_thread = worker_thread
        worker_thread.start()

    def _buffer_wake_command_audio_until_capture_ready(
        self,
    ) -> tuple[bytes, ...]:
        """
        Keep microphone ownership and buffer post-wake speech while VAD warms.

        Normally the background warm-up has already completed by the time the
        user says Qronos. This fallback is what makes a *very early* wake safe:
        the microphone keeps being drained instead of losing the first command
        words while the heavier VAD initialization finishes.
        """
        if self._wake_command_ready.is_set():
            return ()

        if self.audio_input is None:
            raise RuntimeError(
                "Qronos microphone is unavailable while command capture warms."
            )

        frame_duration = (
            self.audio_input.config.frame_size
            / self.audio_input.config.sample_rate
        )
        max_frames = max(
            1,
            int(
                WAKE_COMMAND_PREP_BUFFER_LIMIT_SECONDS
                / frame_duration
            ),
        )

        buffered: list[bytes] = []
        deadline = (
            time.perf_counter()
            + WAKE_COMMAND_PREP_BUFFER_LIMIT_SECONDS
        )

        while not self._wake_command_ready.is_set():
            if self._wake_stop.is_set():
                raise RuntimeError(
                    "Wake listener stopped while command capture was warming."
                )

            if time.perf_counter() >= deadline:
                raise TimeoutError(
                    "Qronos command capture did not warm before the audio buffer limit."
                )

            thread = self._wake_command_prepare_thread
            if (
                self._wake_command_prepare_error
                and (thread is None or not thread.is_alive())
            ):
                raise RuntimeError(
                    "Qronos command capture warm-up failed: "
                    + self._wake_command_prepare_error
                )

            frame = self.audio_input.read_frame()
            buffered.append(bytes(frame))
            self._maybe_emit_input_meter_frame(frame)

            if len(buffered) >= max_frames:
                raise TimeoutError(
                    "Qronos command capture exceeded the buffered-audio safety limit."
                )

        return tuple(buffered)

    def prepare(self) -> None:
        """
        Prepare the full post-transcription processing/output runtime.

        This deliberately calls ``prepare_wake_capture`` first but never makes
        Wake readiness wait for TTS/Router/Orchestrator initialization.
        """
        self.prepare_wake_capture()

        full_ready = all(
            getattr(
                self,
                name,
            )
            is not None
            for name in (
                "voice_output",
                "task_router",
                "action_audit",
                "screen_capture",
                "orchestrator",
            )
        )

        if full_ready:
            return

        emit(
            "runtime_preparing",
            "preparing",
            "Preparing Qronos processing runtime.",
        )

        if self.voice_output is None:
            voice_output = (
                ChatterboxRuntime()
            )

            if not voice_output.health_check():
                raise RuntimeError(
                    "Qronos TTS runtime is not ready."
                )

            self.voice_output = voice_output

        if self.task_router is None:
            self.task_router = TaskRouter()

        if self.action_audit is None:
            self.action_audit = (
                ActionAuditLog()
            )
            set_default_audit_sink(
                self.action_audit.record_verdict
            )

        if self.screen_capture is None:
            self.screen_capture = (
                ScreenCapture(
                    read_text=read_screen_text
                )
            )

        if self.orchestrator is None:
            orchestrator = Orchestrator()
            orchestrator.workers.register(
                WebResearchWorker(
                    answer_fn=(
                        orchestrator.answer_web_prompt
                    ),
                )
            )
            orchestrator.workers.register(
                build_vision_worker(
                    orchestrator.runtime
                )
            )
            self.orchestrator = (
                orchestrator
            )

        emit(
            "runtime_prepared",
            "ready",
            "Qronos processing runtime is ready.",
        )

    def _require_wake_capture_prepared(
        self,
    ) -> None:
        missing = [
            name
            for name in (
                "audio_input",
                "vad_runtime",
                "command_recorder",
                "speech_runtime",
                "conversation_session",
            )
            if getattr(
                self,
                name,
            )
            is None
        ]

        if missing:
            raise RuntimeError(
                "Qronos voice capture runtime is not prepared: "
                + ", ".join(missing)
                + " missing."
            )

    def _require_prepared(self) -> None:
        missing = [
            name
            for name in (
                "audio_input",
                "vad_runtime",
                "command_recorder",
                "speech_runtime",
                "voice_output",
                "task_router",
                "action_audit",
                "screen_capture",
                "orchestrator",
                "conversation_session",
            )
            if getattr(self, name) is None
        ]

        if missing:
            raise RuntimeError(
                "Qronos voice runtime is not prepared: "
                + ", ".join(missing)
                + " missing."
            )

    def _warm_speech_runtime_async(
        self,
    ) -> None:
        speech = self.speech_runtime

        if speech is None:
            return

        warm = getattr(
            speech,
            "warm_async",
            None,
        )

        if callable(warm):
            # A new warm request starts a new STT residency generation. The
            # next release must be allowed to shut that generation down.
            self._speech_runtime_released = False

            try:
                warm()

            except Exception:
                # Warm-up is an optimization only.
                #
                # The hybrid runtime keeps the validated CLI fallback,
                # so warm failure must never cancel a user's voice turn.
                pass

    def _release_speech_runtime(
        self,
    ) -> bool:
        """
        Release persistent STT resources exactly once per warm generation.

        _run_voice_turn() must yield Whisper before Brain execution on the
        8 GB GPU. push_to_talk() also owns a final safety cleanup so failures
        that happen before transcription cannot leak STT residency. Those two
        cleanup paths intentionally converge here and are idempotent.
        """
        if self._speech_runtime_released:
            return True

        speech = self.speech_runtime

        if speech is None:
            self._speech_runtime_released = True
            return True

        shutdown = getattr(
            speech,
            "shutdown",
            None,
        )

        if callable(shutdown):
            try:
                shutdown()

            except Exception:
                return False

        try:
            server_running = getattr(
                speech,
                "server_running",
                None,
            )

            if (
                server_running is not None
                and bool(server_running)
            ):
                return False

        except Exception:
            pass

        self._speech_runtime_released = True
        return True

    def _release_brains_for_voice_phase(
        self,
    ) -> None:
        """
        Yield configured Qronos Brain residency before another GPU voice phase.

        Older/fake orchestrators used by tests may not expose the public seam;
        in that case this is intentionally a no-op.
        """
        orchestrator = self.orchestrator

        if orchestrator is None:
            return

        release = getattr(
            orchestrator,
            "release_brains_for_voice_phase",
            None,
        )

        if callable(release):
            release()

    def _release_voice_output_runtime(
        self,
    ) -> bool:
        """
        Release TTS model residency after WAV generation.

        Desktop playback consumes generated files, not the TTS model itself,
        so keeping Chatterbox resident during playback only steals VRAM from
        the next STT phase.
        """
        voice = self.voice_output

        if voice is None:
            return True

        release = getattr(
            voice,
            "release",
            None,
        )

        if not callable(release):
            return True

        try:
            release()
            return not bool(
                getattr(
                    voice,
                    "is_loaded",
                    False,
                )
            )

        except Exception:
            return False

    @property
    def wake_listener_running(self) -> bool:
        thread = self._wake_thread
        return thread is not None and thread.is_alive()

    @property
    def wake_listener_ready(self) -> bool:
        return (
            self.wake_listener_running
            and self._wake_ready.is_set()
        )

    def _schedule_wake_start_retry(
        self,
        error: Exception,
    ) -> bool:
        """Retry a failed initial Wake startup without requiring a Settings write."""
        if (
            self._wake_stop.is_set()
            or self.runtime_paused
            or not self.wake_word_enabled
        ):
            return False

        with self._lock:
            if (
                self._wake_start_retry_count
                >= len(WAKE_STARTUP_RETRY_DELAYS_SECONDS)
            ):
                return False

            attempt = self._wake_start_retry_count + 1
            delay = WAKE_STARTUP_RETRY_DELAYS_SECONDS[
                self._wake_start_retry_count
            ]
            self._wake_start_retry_count = attempt

        self._notify(
            "wake_word_retrying",
            "warning",
            json.dumps(
                {
                    "attempt": attempt,
                    "delaySeconds": delay,
                    "reason": str(error),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

        failed_wake_thread = threading.current_thread()

        def retry() -> None:
            if self._wake_stop.wait(delay):
                return

            if (
                self.runtime_paused
                or not self.wake_word_enabled
            ):
                return

            # A startup exception is raised on the Wake thread itself. The old
            # implementation could fire this retry while that failed thread was
            # still inside its finally/cleanup path. start_wake_listener() then
            # saw an alive _wake_thread and correctly refused a duplicate start,
            # but no later retry remained; touching Wake sensitivity happened to
            # start it again. Wait until the failed owner has completely exited
            # before replacing it. This keeps one microphone owner at a time.
            while failed_wake_thread.is_alive():
                if self._wake_stop.wait(0.05):
                    return

            with self._lock:
                if self._wake_thread is failed_wake_thread:
                    self._wake_thread = None

            if (
                self.runtime_paused
                or not self.wake_word_enabled
                or self._wake_stop.is_set()
            ):
                return

            self.start_wake_listener()

        worker = threading.Thread(
            target=retry,
            name=f"qronos-wake-retry-{attempt}",
            daemon=True,
        )
        self._wake_retry_thread = worker
        worker.start()
        return True

    def start_wake_listener(self) -> None:

        """
        Start the low-cost Qronos wake-word loop.

        Thread liveness is deliberately not reported as listening readiness.
        A duplicate start can arrive while the first thread is still preparing
        the real Wake engine and microphone. During that startup window, return
        silently and let _wake_listener_loop emit wake_word_listening only after
        trigger.start() and audio_input.start() have both completed.

        The listener uses the same AudioInput object as command recording, but
        never reads from it concurrently. After detection, this thread pauses
        wake-word inference and immediately hands the already-open microphone
        to CommandRecorder, preserving the first phoneme of the user's command.
        """
        if not self.wake_word_enabled:
            self._wake_ready.clear()
            self._notify(
                "wake_word_disabled",
                "ready",
                "Wake Word is disabled in Qronos Settings.",
            )
            return

        if self.runtime_paused:
            self._reject_if_runtime_paused(
                "wake_listener_start"
            )
            return

        # The Settings meter may own the microphone only while Wake is absent.
        # Stop that temporary owner before the persistent Wake stream opens.
        self._stop_standalone_input_meter()

        with self._lock:
            if self._wake_thread is not None and self._wake_thread.is_alive():
                if self._wake_ready.is_set():
                    self._notify(
                        "wake_word_listening",
                        "listening",
                        "Qronos wake-word listener is already active.",
                    )
                return

            self._wake_stop.clear()
            self._wake_ready.clear()

            thread = threading.Thread(
                target=self._wake_listener_loop,
                name="qronos-wake-word",
                daemon=True,
            )
            self._wake_thread = thread


        thread.start()


    def stop_wake_listener(self) -> None:
        self._wake_stop.set()
        self._wake_ready.clear()
        with self._lock:
            self._wake_start_retry_count = 0

        # Unblock a wake thread that may currently be waiting for the desktop
        # to finish speaking.
        self._voice_playback_complete.set()

        audio = self.audio_input
        if audio is not None:
            try:
                audio.stop()
            except Exception:
                pass

        trigger = self.voice_trigger
        if trigger is not None:
            try:
                trigger.stop()
            except Exception:
                pass

        thread = self._wake_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=0.75)

        self._wake_thread = None

        # Stopping wake listening also releases persistent STT resources.
        self._release_speech_runtime()

        if (
            self._input_meter_enabled
            and not self.runtime_paused
        ):
            self._start_standalone_input_meter()

    def _begin_voice_playback_wait(self) -> int:
        """
        Start tracking one complete desktop playback group.

        Every TTS chunk belonging to the same assistant response receives the
        same playback id. The follow-up microphone may open only after the
        desktop reports that this complete group has finished playing.
        """
        with self._lock:
            self._voice_playback_id += 1
            playback_id = self._voice_playback_id
            self._pending_playback_id = playback_id
            self._voice_playback_complete.clear()

        return playback_id

    def mark_voice_playback_complete(
        self,
        playback_id: int,
    ) -> bool:
        """
        Accept one desktop playback-complete acknowledgement.

        Stale acknowledgements are ignored so a late desktop event from an old
        response can never unlock listening for a newer response.
        """
        with self._lock:
            if (
                self._pending_playback_id
                is None
                or playback_id
                != self._pending_playback_id
            ):
                return False

            self._voice_playback_complete.set()
            return True

    def _wait_for_voice_playback_complete(
        self,
    ) -> bool:
        """
        Wait until the desktop confirms that the complete response finished.

        The normal path has no guessed playback duration. The five-minute
        failsafe only prevents a permanently dead desktop from deadlocking the
        wake thread. A failsafe expiry does not close the conversation.
        """
        with self._lock:
            playback_id = (
                self._pending_playback_id
            )

        if playback_id is None:
            return True

        deadline = (
            time.monotonic()
            + PLAYBACK_ACK_FAILSAFE_SECONDS
        )

        while not self._wake_stop.is_set():
            remaining = (
                deadline
                - time.monotonic()
            )

            if remaining <= 0:
                break

            if self._voice_playback_complete.wait(
                timeout=min(
                    0.25,
                    remaining,
                )
            ):
                with self._lock:
                    if (
                        self._pending_playback_id
                        == playback_id
                    ):
                        self._pending_playback_id = None

                    self._voice_playback_complete.clear()

                return True

        if self._wake_stop.is_set():
            return False

        with self._lock:
            if (
                self._pending_playback_id
                == playback_id
            ):
                self._pending_playback_id = None

            self._voice_playback_complete.clear()

        self._notify(
            "voice_playback_ack_timeout",
            "warning",
            (
                "Desktop did not confirm voice playback completion "
                "before the playback acknowledgement failsafe expired."
            ),
        )

        return False

    def _should_continue_followup_session(self) -> bool:
        """
        Return True while the current interactive conversation should continue.

        Conversation lifetime is deliberately independent of generated audio
        duration. A turn may fail before producing audio while the conversation
        itself is still valid and should return to follow-up listening.

        The session ends only when another lifecycle path explicitly closes it,
        such as the no-speech follow-up timeout or runtime shutdown.
        """
        return (
            not self._wake_stop.is_set()
            and self.conversation_session is not None
            and self.conversation_session.is_active
        )

    def _wake_listener_loop(self) -> None:

        """
        Run the wake gate and the interactive multi-turn voice session.

        A wake word is required only to open a session. After Qronos finishes
        speaking, the microphone reopens for a bounded follow-up window and the
        user may continue without saying Qronos again.

        Full command capture remains off while Qronos is speaking. True
        mid-speech barge-in is intentionally not implemented here because the
        current desktop playback path has no acoustic echo cancellation.
        """
        try:

            wake_start_perf = time.perf_counter()

            self.prepare_wake_gate()

            if self.audio_input is None:
                raise RuntimeError(
                    "Qronos microphone is not prepared for wake-word listening."
                )


            engine = OpenWakeWordEngine(
                threshold=self.wake_threshold,
            )


            trigger = VoiceTriggerService(
                wake_word="Qronos",
                engine=engine,
            )

            self.wake_engine = engine
            self.voice_trigger = trigger


            trigger.start()


            self.audio_input.start()


            self._wake_ready.set()
            with self._lock:
                self._wake_start_retry_count = 0

            self._notify(
                "wake_word_listening",
                "listening",
                json.dumps(
                    {
                        "message": "Say Qronos to start a voice session.",
                        "coldStartSeconds": round(
                            time.perf_counter() - wake_start_perf,
                            3,
                        ),
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            )

            # Do not put VAD/recorder/STT on the wake critical path. Wake is
            # already listening at this point; the heavier capture layer warms
            # behind it and an early command is buffered if necessary.
            self._warm_wake_command_capture_async()

            while not self._wake_stop.is_set():
                if self._pending_audio_input_reconcile:
                    self._reconcile_audio_input_in_wake_thread()

                try:
                    frame = self.audio_input.read_frame()
                except Exception:
                    if self._wake_stop.is_set():
                        break
                    raise

                self._maybe_emit_input_meter_frame(
                    frame
                )

                event = trigger.process_audio(
                    frame,
                    timestamp=time.time(),
                )

                if event is None:
                    continue

                wake_detected_perf = (
                    time.perf_counter()
                )

                self._wake_ready.clear()
                trigger.pause()

                # The 8 GB GPU has one Qronos high-VRAM phase slot.
                # Yield any warm Fast/Heavy Brain before Whisper starts so
                # STT does not overlap physically with Brain residency.
                self._release_brains_for_voice_phase()

                # If the user wakes Qronos before the command VAD has finished
                # its background cold start, keep draining the open microphone
                # into a bounded buffer. Those frames are consumed first by the
                # recorder, so early commands are not clipped or discarded.
                initial_command_frames = (
                    self._buffer_wake_command_audio_until_capture_ready()
                )

                # Load persistent Whisper while the user is speaking.
                # Recording time hides most or all of model startup latency.
                self._warm_speech_runtime_async()

                self._notify(
                    "wake_word_detected",
                    "listening",
                    json.dumps(
                        {
                            "wakeWord": event.wake_word,
                            "timestamp": round(
                                event.timestamp,
                                6,
                            ),
                            "score": round(
                                engine.last_score,
                                6,
                            ),
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                )

                if (
                    self.conversation_session
                    is not None
                    and not
                    self.conversation_session.is_active
                ):
                    self.conversation_session.start()
                    self._notify(
                        "conversation_session_started",
                        "listening",
                        "Interactive voice session started.",
                    )

                playback_seconds = (
                    self._run_voice_turn(
                        trigger_source="wake_word",
                        detected_at_perf=(
                            wake_detected_perf
                        ),
                        wake_score=float(
                            engine.last_score
                        ),
                        initial_frames=initial_command_frames,
                    )
                )

                # A wake-word candidate is not enough to establish an
                # interactive conversation. If no valid spoken turn was
                # completed after the wake event, close any session that
                # may have been opened and return directly to wake-word
                # listening. This prevents wind/noise false wakes from
                # falling through into unrestricted follow-up listening.
                if (
                    playback_seconds <= 0
                    and not
                    self._last_voice_turn_had_valid_speech_evidence
                    and self.conversation_session
                    is not None
                    and self.conversation_session.is_active
                ):
                    self.conversation_session.close()

                    self._notify(
                        "conversation_session_closed",
                        "ready",
                        (
                            "Wake candidate cancelled because "
                            "no valid speech turn was completed."
                        ),
                    )

                while self._should_continue_followup_session():
                    if playback_seconds > 0:
                        self._wait_for_voice_playback_complete()

                    if self._wake_stop.is_set():
                        break

                    if (
                        not
                        self.conversation_session.is_active
                    ):
                        break

                    self.conversation_session.begin_listening()

                    self._notify(
                        "conversation_followup_listening",
                        "listening",
                        json.dumps(
                            {
                                "timeoutSeconds":
                                    FOLLOWUP_START_TIMEOUT_SECONDS,
                                "wakeWordRequired":
                                    False,
                            },
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ),
                    )

                    playback_seconds = (
                        self._run_voice_turn(
                            trigger_source="followup",
                        )
                    )

                if self._wake_stop.is_set():
                    break

                # The interactive session is finished. Release
                # persistent STT VRAM before returning to idle wake listening.
                self._release_speech_runtime()

                if self._pending_audio_input_reconcile:
                    self._reconcile_audio_input_in_wake_thread()

                # Resume the existing wake-word pipeline.
                # OpenWakeWord resume() resets its rolling
                # prediction/audio buffers and performs its
                # warm-up path without rebuilding the engine.
                self.audio_input.start()
                trigger.resume()
                self._wake_ready.set()

                self._notify(
                    "wake_word_listening",
                    "listening",
                    "Say Qronos to start a voice session.",
                )

        except Exception as exc:

            if not self._wake_stop.is_set():
                retry_scheduled = (
                    self._schedule_wake_start_retry(
                        exc
                    )
                )

                if not retry_scheduled:
                    self._notify(
                        "runtime_error",
                        "error",
                        f"Wake-word listener failed: {exc}",
                    )

        finally:
            self._wake_ready.clear()

            trigger = self.voice_trigger
            if trigger is not None:
                try:
                    trigger.stop()
                except Exception:
                    pass

            if self.audio_input is not None:
                try:
                    self.audio_input.stop()
                except Exception:
                    pass

            self.voice_trigger = None
            self.wake_engine = None

    def _synthesize_response(self, response: str):
        """
        Generate the spoken form of one assistant response.

        This seam lets the production response-to-TTS path be tested without
        opening the microphone or invoking ASR/LLM work.
        """
        if self.voice_output is None:
            raise RuntimeError(
                "Qronos TTS runtime is not prepared."
            )

        self._notify(
            "voice_synthesizing",
            "processing",
            "Generating Qronos voice response.",
        )

        spoken_response = normalise_spoken_text(
            response
        )

        utterance = self.voice_output.speak_to_file(
            spoken_response
        )

        def build_spectrum_sidecar() -> None:
            try:
                _write_voice_spectrum_sidecar(
                    utterance.audio_path
                )
            except Exception:
                # Playback spectrum is visual-only. A sidecar failure must
                # never affect speech playback or the runtime event contract.
                pass

        threading.Thread(
            target=build_spectrum_sidecar,
            name="qronos-spectrum-sidecar",
            daemon=True,
        ).start()

        self._notify(
            "voice_audio_ready",
            "ready",
            json.dumps(
                {
                    "path": str(utterance.audio_path),
                    "audioSeconds": round(
                        utterance.audio_seconds,
                        3,
                    ),
                    "generationSeconds": round(
                        utterance.took_seconds,
                        3,
                    ),
                    "rtf": round(
                        utterance.real_time_factor,
                        3,
                    ),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

        return utterance


    def _synthesize_response_chunks(
        self,
        response: str,
        playback_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Synthesize one complete Brain response as sequential semantic chunks.

        There is still exactly one Chatterbox generation at a time. After each
        chunk is ready its path is emitted immediately, allowing the desktop
        player to begin playback while the next chunk is generated.
        """
        if self.voice_output is None:
            raise RuntimeError(
                "Qronos TTS runtime is not prepared."
            )

        chunks = _split_semantic_tts_chunks(
            response
        )

        if not chunks:
            raise RuntimeError(
                "Qronos response produced no TTS chunks."
            )

        self._notify(
            "voice_synthesizing",
            "processing",
            "Generating Qronos voice response.",
        )

        total_audio_seconds = 0.0
        total_model_seconds = 0.0
        first_audio_ready_perf: float | None = None
        first_chunk_model_seconds = 0.0
        first_chunk_audio_seconds = 0.0
        spoken_chunks: list[str] = []

        for index, chunk in enumerate(
            chunks
        ):
            spoken_chunk = normalise_spoken_text(
                chunk
            )
            spoken_chunks.append(
                spoken_chunk
            )

            utterance = (
                self.voice_output.speak_to_file(
                    spoken_chunk
                )
            )

            if first_audio_ready_perf is None:
                first_audio_ready_perf = (
                    time.perf_counter()
                )
                first_chunk_model_seconds = (
                    float(
                        utterance.took_seconds
                    )
                )
                first_chunk_audio_seconds = (
                    float(
                        utterance.audio_seconds
                    )
                )

            total_audio_seconds += float(
                utterance.audio_seconds
            )
            total_model_seconds += float(
                utterance.took_seconds
            )

            def build_spectrum_sidecar(
                audio_path=utterance.audio_path,
            ) -> None:
                try:
                    _write_voice_spectrum_sidecar(
                        audio_path
                    )
                except Exception:
                    # Spectrum is visual-only and must never delay speech.
                    pass

            threading.Thread(
                target=build_spectrum_sidecar,
                name=(
                    "qronos-spectrum-sidecar-"
                    f"{index + 1}"
                ),
                daemon=True,
            ).start()

            audio_payload: dict[str, Any] = {
                "path": str(
                    utterance.audio_path
                ),
                "audioSeconds": round(
                    utterance.audio_seconds,
                    3,
                ),
                "generationSeconds": round(
                    utterance.took_seconds,
                    3,
                ),
                "rtf": round(
                    utterance.real_time_factor,
                    3,
                ),
                "chunkIndex": index,
                "chunkNumber": index + 1,
                "chunkCount": len(chunks),
                "isFirstChunk": index == 0,
                "isLastChunk": (
                    index
                    == len(chunks) - 1
                ),
            }

            if playback_id is not None:
                audio_payload[
                    "playbackId"
                ] = playback_id

            self._notify(
                "voice_audio_ready",
                "ready",
                json.dumps(
                    audio_payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            )

        return {
            "chunks": chunks,
            "spokenChunks": spoken_chunks,
            "chunkCount": len(chunks),
            "totalAudioSeconds":
                total_audio_seconds,
            "totalModelSeconds":
                total_model_seconds,
            "firstAudioReadyPerf":
                first_audio_ready_perf,
            "firstChunkModelSeconds":
                first_chunk_model_seconds,
            "firstChunkAudioSeconds":
                first_chunk_audio_seconds,
        }


    def _write_latency_report(
        self,
        payload: dict[str, Any],
    ) -> None:
        """
        Persist voice diagnostics without losing earlier reports.

        ``voice_latency_latest.json`` remains the convenient latest-event
        snapshot. Every write is also archived under a unique filename so
        later follow-up timeouts, runtime errors, or successful turns can
        never erase earlier diagnostic evidence.

        Diagnostics must never break a voice turn.
        """
        try:
            VOICE_LATENCY_LOG_PATH.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            rendered = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            )

            VOICE_LATENCY_LOG_PATH.write_text(
                rendered,
                encoding="utf-8",
            )

            archive_path = (
                VOICE_LATENCY_LOG_PATH.parent
                / (
                    "voice_latency_"
                    f"{time.time_ns()}.json"
                )
            )

            archive_path.write_text(
                rendered,
                encoding="utf-8",
            )

        except Exception:
            # Diagnostics must never change the runtime event contract or
            # interfere with a voice turn.
            pass


    def _cancel_pending_voice_playback_wait(
        self,
    ) -> None:
        """
        Forget an unfinished playback group after a failed voice turn.

        This prevents a TTS/runtime failure from leaving a stale playback ID
        that could interfere with the next valid desktop acknowledgement.
        """
        with self._lock:
            self._pending_playback_id = None
            self._voice_playback_complete.clear()


    def _recover_valid_voice_turn_failure(
        self,
        report: dict[str, Any],
        *,
        user_message_added: bool,
        assistant_message_added: bool,
        rewarm_stt_after_tts: bool,
    ) -> float:
        """
        Speak a short local recovery message after genuine user speech fails
        before a usable assistant response has been produced.

        No Brain is called here. The purpose is to stop recoverable
        Brain/resource failures from becoming a silent follow-up window and
        then an apparently random session close.
        """
        if (
            not self._last_voice_turn_had_valid_speech_evidence
            or assistant_message_added
            or self.conversation_session is None
            or not self.conversation_session.is_active
            or self.voice_output is None
        ):
            return 0.0

        recovery_response = (
            VOICE_RETRY_RESPONSE
            if user_message_added
            else VOICE_STT_RETRY_RESPONSE
        )

        playback_id: int | None = None

        try:
            self.conversation_session.begin_responding()

            if user_message_added:
                self.conversation_session.add_assistant_message(
                    recovery_response
                )

            self._notify(
                "voice_response",
                "ready",
                recovery_response,
            )

            # A failed Brain turn may still have left a keep-alive model
            # resident. The local recovery voice must get the GPU phase slot.
            self._release_brains_for_voice_phase()

            playback_id = (
                self._begin_voice_playback_wait()
            )

            chunked = (
                self._synthesize_response_chunks(
                    recovery_response,
                    playback_id=playback_id,
                )
            )

            tts_released = (
                self._release_voice_output_runtime()
            )
            report[
                "recoveryTtsReleasedAfterSynthesis"
            ] = bool(
                tts_released
            )

            if (
                tts_released
                and rewarm_stt_after_tts
            ):
                self._warm_speech_runtime_async()

            playback_seconds = max(
                0.0,
                float(
                    chunked[
                        "totalAudioSeconds"
                    ]
                ),
            )

            report[
                "recoveryResponse"
            ] = recovery_response

            report[
                "recoveryGeneratedAudioSeconds"
            ] = round(
                playback_seconds,
                3,
            )

            return playback_seconds

        except Exception as recovery_exc:
            self._cancel_pending_voice_playback_wait()

            report[
                "recoveryError"
            ] = str(
                recovery_exc
            )

            return 0.0


    def _run_voice_turn(
        self,
        trigger_source: str,
        detected_at_perf: float | None = None,
        wake_score: float | None = None,
        initial_frames: tuple[bytes, ...] = (),
    ) -> float:
        """
        Capture one user command, run the existing Brain path, synthesize it,
        and return the generated audio duration.

        This version also instruments every material latency segment without
        changing the execution order. That gives us a factual baseline before
        introducing semantic chunking or streaming.
        """
        if self._reject_if_runtime_paused("voice"):
            return 0.0

        try:
            self.last_foreground_window = foreground_window()
        except Exception:
            self.last_foreground_window = None

        if self.is_busy:
            self._notify(
                "runtime_busy",
                "busy",
                "Qronos is already processing a voice request.",
            )
            return 0.0

        self._set_busy(True)
        playback_seconds = 0.0
        user_message_added = False
        assistant_message_added = False
        voice_request_id = ""
        conversation_context = ()
        self._last_voice_turn_had_valid_speech_evidence = False

        turn_started_perf = (
            detected_at_perf
            if detected_at_perf is not None
            else time.perf_counter()
        )

        report: dict[str, Any] = {
            "triggerSource": trigger_source,
            "startedAtEpoch": round(
                time.time(),
                6,
            ),
        }

        if wake_score is not None:
            report["wakeScore"] = round(
                float(wake_score),
                6,
            )

        try:
            self.prepare_wake_capture()
            self._require_wake_capture_prepared()

            self._notify(
                "voice_listening",
                "listening",
                "Listening for your command.",
            )

            self.audio_input.start()

            recording_started = time.perf_counter()

            recording_path = (
                self.speech_runtime.temp_dir
                / "qronos_voice_command.wav"
            )

            # Preserve the established recorder call contract for ordinary
            # PTT/follow-up turns. Only the very-early Wake path needs the new
            # buffered-frame argument. This keeps existing recorder adapters
            # and tests compatible while still protecting speech spoken before
            # command VAD warm-up finishes.
            if initial_frames:
                recording = self.command_recorder.record_to_file(
                    recording_path,
                    on_audio_spectrum=self._emit_audio_spectrum,
                    initial_frames=initial_frames,
                )
            else:
                recording = self.command_recorder.record_to_file(
                    recording_path,
                    on_audio_spectrum=self._emit_audio_spectrum,
                )

            recording_finished = time.perf_counter()

            self.audio_input.stop()
            self._clear_audio_spectrum()

            report.update(
                {
                    "recordingAudioSeconds": round(
                        float(
                            recording.duration_seconds
                        ),
                        3,
                    ),
                    "recordingSpeechSeconds": round(
                        float(
                            recording.speech_seconds
                        ),
                        3,
                    ),
                    "peakSpeechProbability": round(
                        float(
                            recording.peak_speech_probability
                        ),
                        6,
                    ),
                }
            )

            # A false wake must never turn a tiny burst of wind/noise into
            # an LLM request. Follow-up turns intentionally keep the
            # recorder's normal minimum so short replies such as "yes",
            # "no", or a person's name remain usable once a conversation
            # has already been established.
            if (
                trigger_source == "wake_word"
                and float(recording.speech_seconds)
                < WAKE_MIN_COMMAND_SPEECH_SECONDS
            ):
                report["wakeSpeechEvidenceRejected"] = True
                report["recordingAudioSeconds"] = round(
                    float(recording.duration_seconds),
                    3,
                )
                report["recordingSpeechSeconds"] = round(
                    float(recording.speech_seconds),
                    3,
                )
                report["peakSpeechProbability"] = round(
                    float(recording.peak_speech_probability),
                    6,
                )
                report["closedAtEpoch"] = round(
                    time.time(),
                    6,
                )

                self._write_latency_report(
                    report
                )

                if (
                    self.conversation_session
                    is not None
                    and self.conversation_session.is_active
                ):
                    self.conversation_session.close()

                self._notify(
                    "conversation_session_closed",
                    "ready",
                    (
                        "Wake candidate rejected because "
                        "speech evidence was too short."
                    ),
                )

                return 0.0

            self._last_voice_turn_had_valid_speech_evidence = True

            self._notify(
                "voice_transcribing",
                "processing",
                "Transcribing voice command.",
            )

            stt_started = time.perf_counter()

            transcript = (
                self.speech_runtime.transcribe_file(
                    recording.audio_path,
                    language=self.voice_language,
                )
                .strip()
            )

            stt_finished = time.perf_counter()

            stt_release_started = time.perf_counter()

            if not self._release_speech_runtime():
                raise RuntimeError(
                    "Qronos could not release persistent STT GPU resources "
                    "before Brain execution."
                )

            stt_release_finished = time.perf_counter()
            report["sttReleasedBeforeBrain"] = True
            report["sttReleaseSeconds"] = round(
                stt_release_finished
                - stt_release_started,
                3,
            )

            # Heavy post-transcription collaborators are intentionally delayed
            # until after the user's speech is already safely captured.
            self.prepare()
            self._require_prepared()

            self._require_runtime_active("voice brain execution")

            transcript = normalise_qronos_invocation(transcript)

            if not transcript:
                raise RuntimeError(
                    "Speech recognition returned an empty command."
                )

            report["transcript"] = transcript

            conversation_context = (
                self._conversation_brain_context()
            )
            voice_request_id = (
                f"voice-{time.time_ns()}"
            )

            self._record_conversation_message(
                role="user",
                text=transcript,
                source="voice",
                request_id=voice_request_id,
            )

            self._notify(
                "voice_transcript",
                "processing",
                transcript,
            )

            if not self.conversation_session.is_active:
                self.conversation_session.start()

            self.conversation_session.add_user_message(
                transcript
            )
            user_message_added = True
            self.conversation_session.begin_processing()

            # Observe the future Gate + Estimator on the exact real STT transcript.
            # This result is written only to diagnostics; it cannot select a worker.
            routing_shadow = self._routing_shadow(
                transcript
            )
            report["routingShadow"] = routing_shadow

            route_started = time.perf_counter()

            route = self.task_router.route(
                transcript
            )

            routed_task_type = route.task_type
            selected_task_type = routed_task_type

            report["route"] = (
                routed_task_type.value
            )
            report["selectedRoute"] = (
                selected_task_type.value
            )

            routing_shadow["legacyRoute"] = (
                routed_task_type.value
            )
            routing_shadow["selectedRoute"] = (
                selected_task_type.value
            )

            route_finished = time.perf_counter()

            self._notify(
                "voice_routed",
                "processing",
                routed_task_type.value,
            )

            if selected_task_type is TaskType.VISION:
                self.pending_look = transcript
                self.conversation_session.begin_listening()

                self._notify(
                    "voice_needs_screen",
                    "ready",
                    transcript,
                )

                report["route"] = route.task_type.value
                report["visionApprovalPending"] = True
                self._write_latency_report(report)
                return 0.0

            arithmetic_answer = (
                solve_simple_arithmetic(
                    transcript
                )
                if selected_task_type is TaskType.FAST
                else None
            )

            brain_started = time.perf_counter()

            if arithmetic_answer is not None:
                response = (
                    arithmetic_answer.spoken_text
                )
            else:
                plan = TaskPlan(
                    goal=transcript
                )
                plan.add_step(
                    task_type=selected_task_type,
                    description=transcript,
                )

                self._require_runtime_active("voice brain execution")

                results = self.orchestrator.execute_plan(
                    plan,
                    conversation_messages=(
                        conversation_context
                    ),
                )

                if not results:
                    raise RuntimeError(
                        "Qronos did not return an execution result."
                    )

                result = results[-1]

                if not result.success:
                    raise RuntimeError(
                        result.error
                        or "Qronos task failed."
                    )

                response = result.output.strip()

            brain_finished = time.perf_counter()

            if not response:
                raise RuntimeError(
                    "Qronos returned an empty response."
                )

            brain_release_started = time.perf_counter()
            self._release_brains_for_voice_phase()
            brain_release_finished = time.perf_counter()

            report["brainReleasedBeforeTts"] = True
            report["brainReleaseSeconds"] = round(
                brain_release_finished
                - brain_release_started,
                3,
            )

            self.conversation_session.begin_responding()
            self.conversation_session.add_assistant_message(
                response
            )
            assistant_message_added = True

            self._record_conversation_message(
                role="assistant",
                text=response,
                source="voice",
                request_id=voice_request_id,
            )

            self._notify(
                "voice_response",
                "ready",
                response,
            )

            self._require_runtime_active("voice output")

            tts_started = time.perf_counter()

            playback_id = (
                self._begin_voice_playback_wait()
            )

            chunked = (
                self._synthesize_response_chunks(
                    response,
                    playback_id=playback_id,
                )
            )

            all_audio_ready = (
                time.perf_counter()
            )

            tts_release_started = time.perf_counter()
            tts_released = (
                self._release_voice_output_runtime()
            )
            tts_release_finished = time.perf_counter()

            report["ttsReleasedAfterSynthesis"] = bool(
                tts_released
            )
            report["ttsReleaseSeconds"] = round(
                tts_release_finished
                - tts_release_started,
                3,
            )

            # Once TTS has yielded the GPU, warm STT while desktop playback
            # consumes the already-generated WAV files. This restores the
            # follow-up latency optimization without overlapping two large
            # Qronos GPU phases.
            if (
                tts_released
                and trigger_source != "push_to_talk"
            ):
                self._warm_speech_runtime_async()

            first_audio_ready = (
                chunked[
                    "firstAudioReadyPerf"
                ]
                or all_audio_ready
            )

            # Reliability-first playback guard.
            #
            # The desktop player owns actual audio playback. The backend only
            # knows when generated WAV chunks become available; it does not
            # know the exact moment the desktop media engine starts playing
            # them or whether there are small gaps between queued chunks.
            #
            # Do not subtract TTS generation time from the playback duration.
            # Doing so can open the follow-up microphone before desktop
            # playback has actually finished, and the desktop intentionally
            # stops active Qronos audio when a new voice-listening turn begins.
            #
            # Until an explicit desktop -> runtime playback-complete
            # acknowledgement is implemented, wait conservatively for the
            # complete generated audio duration.
            playback_seconds = max(
                0.0,
                float(
                    chunked[
                        "totalAudioSeconds"
                    ]
                ),
            )

            report.update(
                {
                    "transcript": transcript,
                    "route": routed_task_type.value,
                    "selectedRoute": selected_task_type.value,
                    "response": response,
                    "recordingAudioSeconds": round(
                        float(
                            recording.duration_seconds
                        ),
                        3,
                    ),
                    "recordingSpeechSeconds": round(
                        float(
                            recording.speech_seconds
                        ),
                        3,
                    ),
                    "generatedAudioSeconds": round(
                        float(
                            chunked[
                                "totalAudioSeconds"
                            ]
                        ),
                        3,
                    ),
                    "ttsModelSeconds": round(
                        float(
                            chunked[
                                "totalModelSeconds"
                            ]
                        ),
                        3,
                    ),
                    "ttsChunkCount": int(
                        chunked[
                            "chunkCount"
                        ]
                    ),
                    "ttsChunks": list(
                        chunked[
                            "chunks"
                        ]
                    ),
                    "ttsSpokenChunks": list(
                        chunked[
                            "spokenChunks"
                        ]
                    ),
                    "firstChunkAudioSeconds": round(
                        float(
                            chunked[
                                "firstChunkAudioSeconds"
                            ]
                        ),
                        3,
                    ),
                    "firstChunkModelSeconds": round(
                        float(
                            chunked[
                                "firstChunkModelSeconds"
                            ]
                        ),
                        3,
                    ),
                    "timingsSeconds": {
                        "wakeToRecordingStart": round(
                            recording_started
                            - turn_started_perf,
                            3,
                        ),
                        "recording": round(
                            recording_finished
                            - recording_started,
                            3,
                        ),
                        "stt": round(
                            stt_finished
                            - stt_started,
                            3,
                        ),
                        "routing": round(
                            route_finished
                            - route_started,
                            3,
                        ),
                        "brain": round(
                            brain_finished
                            - brain_started,
                            3,
                        ),
                        "ttsFirstChunk": round(
                            first_audio_ready
                            - tts_started,
                            3,
                        ),
                        "ttsAllChunks": round(
                            all_audio_ready
                            - tts_started,
                            3,
                        ),
                        "wakeToFirstAudioReady": round(
                            first_audio_ready
                            - turn_started_perf,
                            3,
                        ),
                        "wakeToAllAudioReady": round(
                            all_audio_ready
                            - turn_started_perf,
                            3,
                        ),
                    },
                }
            )

            self._write_latency_report(
                report
            )

            self._notify(
                "voice_turn_complete",
                "ready",
                (
                    "Wake-word turn completed."
                    if trigger_source == "wake_word"
                    else "Push-to-talk turn completed."
                ),
            )

        except TimeoutError as exc:
            if (
                trigger_source == "followup"
                and str(exc)
                == "No speech was detected before the start timeout."
            ):
                report["followupTimedOut"] = True
                report["closedAtEpoch"] = round(
                    time.time(),
                    6,
                )

                self._write_latency_report(
                    report
                )

                if (
                    self.conversation_session
                    is not None
                    and self.conversation_session.is_active
                ):
                    self.conversation_session.close()

                self._notify(
                    "conversation_session_closed",
                    "ready",
                    "Follow-up window expired.",
                )

                return 0.0

            report["error"] = str(exc)
            report["failedAtEpoch"] = round(
                time.time(),
                6,
            )

            self._notify(
                "runtime_error",
                "error",
                str(exc),
            )

            self._cancel_pending_voice_playback_wait()

            playback_seconds = (
                self._recover_valid_voice_turn_failure(
                    report,
                    user_message_added=(
                        user_message_added
                    ),
                    assistant_message_added=(
                        assistant_message_added
                    ),
                    rewarm_stt_after_tts=(
                        trigger_source
                        != "push_to_talk"
                    ),
                )
            )

            self._write_latency_report(
                report
            )

        except Exception as exc:
            report["error"] = str(exc)
            report["failedAtEpoch"] = round(
                time.time(),
                6,
            )

            self._notify(
                "runtime_error",
                "error",
                str(exc),
            )

            self._cancel_pending_voice_playback_wait()

            playback_seconds = (
                self._recover_valid_voice_turn_failure(
                    report,
                    user_message_added=(
                        user_message_added
                    ),
                    assistant_message_added=(
                        assistant_message_added
                    ),
                    rewarm_stt_after_tts=(
                        trigger_source
                        != "push_to_talk"
                    ),
                )
            )

            self._write_latency_report(
                report
            )

        finally:
            try:
                self._clear_audio_spectrum()
            except Exception:
                pass

            if self.audio_input is not None:
                try:
                    self.audio_input.stop()
                except Exception:
                    pass

            self._set_busy(False)

        return playback_seconds

    def push_to_talk(self) -> None:
        # Never compete with the Settings-only meter for the microphone.
        self._stop_standalone_input_meter()
        self.prepare_wake_capture()
        self._require_wake_capture_prepared()

        # Serialize GPU phases before warming Whisper for an explicit turn.
        self._release_brains_for_voice_phase()

        # Warm Whisper concurrently with command recording.
        self._warm_speech_runtime_async()

        try:
            self._run_voice_turn(
                trigger_source="push_to_talk",
            )

        finally:
            # Push-to-talk is one explicit turn, so do not retain VRAM.
            self._release_speech_runtime()

            if (
                self._input_meter_enabled
                and not self.wake_listener_running
                and not self.runtime_paused
            ):
                self._start_standalone_input_meter()


    def look_at_screen(
        self,
        approved: bool = False,
        question: str = "",
        window_only: bool = False,
    ) -> None:
        if self._reject_if_runtime_paused("screen"):
            return

        if self.is_busy:
            self._notify(
                "runtime_busy",
                "busy",
                "Qronos is already processing a request.",
            )
            return

        self._set_busy(True)
        asked = (question or self.pending_look or "").strip()
        self.pending_look = None

        try:
            if not asked:
                raise RuntimeError(
                    "Qronos was asked to look at the screen without being "
                    "told what to look for."
                )

            if not approved:
                self._notify(
                    "voice_screen_declined",
                    "ready",
                    "Qronos did not look at the screen.",
                )
                return

            self.prepare()
            self._require_prepared()

            self._notify(
                "voice_capturing_screen",
                "processing",
                asked,
            )

            capture = self._capture_for(window_only)

            self._notify(
                "voice_captured_screen",
                "processing",
                capture.describe(),
            )

            if capture.blank:
                self._notify(
                    "voice_screen_blank",
                    "ready",
                    "There is nothing on the screen to read — it may be "
                    "locked, asleep, or showing protected video.",
                )
                return

            if not self.conversation_session.is_active:
                self.conversation_session.start()

            self.conversation_session.add_user_message(asked)
            self.conversation_session.begin_processing()

            plan = TaskPlan(goal=asked)
            plan.add_step(
                task_type=TaskType.VISION,
                description=asked,
                images=(capture.image,),
            )

            results = self.orchestrator.execute_plan(plan)

            if not results:
                raise RuntimeError(
                    "Qronos did not return an execution result."
                )

            result = results[-1]

            if not result.success:
                raise RuntimeError(
                    result.error or "Qronos could not read the screen."
                )

            response = result.output.strip()

            if not response:
                raise RuntimeError(
                    "Qronos returned an empty response."
                )

            self.conversation_session.begin_responding()
            self.conversation_session.add_assistant_message(response)
            self.conversation_session.begin_listening()

            self._notify(
                "voice_response",
                "ready",
                response,
            )

            self._notify(
                "voice_turn_complete",
                "ready",
                "Screen reading turn completed.",
            )

        except (CaptureRefused, CaptureUnavailable) as exc:
            self._notify(
                "voice_screen_unavailable",
                "error",
                str(exc),
            )

        except Exception as exc:
            self._notify(
                "runtime_error",
                "error",
                str(exc),
            )

        finally:
            self._set_busy(False)

    def _capture_for(self, window_only: bool) -> Capture:
        if self.screen_capture is None:
            raise RuntimeError(
                "Qronos screen capture runtime is not prepared."
            )

        window = self.last_foreground_window if window_only else None

        return self.screen_capture.capture(
            approved=True,
            window=window,
            reason=(
                "Look at the window that was in front."
                if window
                else "Look at what is on the screen."
            ),
        )

    def close(self) -> None:
        self._input_meter_enabled = False
        self._stop_standalone_input_meter()
        self.stop_wake_listener()
        self._release_speech_runtime()

        # The scheduler first, and before anything that could raise. Its
        # thread writes to stdout, and a write after main() has returned is a
        # traceback on stderr, which the desktop reads as a crash and
        # tests/test_runtime_bridge_process.py asserts against.
        if self._scheduler is not None:
            try:
                self._scheduler.stop()
            except Exception:
                pass

        if self.audio_input is not None:
            try:
                self.audio_input.stop()
            except Exception:
                pass

        if self.vad_runtime is not None:
            try:
                self.vad_runtime.close()
            except Exception:
                pass

        if self.voice_output is not None:
            try:
                self.voice_output.release()
            except Exception:
                pass

        if self.conversation_session is not None:
            try:
                self.conversation_session.close()
            except Exception:
                pass

        if self.orchestrator is not None:
            try:
                self.orchestrator.workers.close()
            except Exception:
                pass


#: How a scheduler event reaches the desktop: an event name and a status.
#: Statuses are the ones the front end already understands. A refused override
#: is a *warning*, not an error — nothing went wrong, and ``runtime_error``
#: resets the orb to idle.
_QUEUE_EVENT_NAMES: dict[QueueEventType, tuple[str, str]] = {
    QueueEventType.QUEUED: ("queue_task_queued", "queued"),
    QueueEventType.STARTED: ("queue_task_started", "running"),
    QueueEventType.FINISHED: ("queue_task_finished", "ready"),
    QueueEventType.FAILED: ("queue_task_finished", "error"),
    QueueEventType.CANCELLED: ("queue_task_finished", "ready"),
    QueueEventType.OVERRIDE_REFUSED: ("queue_override_refused", "warning"),
    QueueEventType.HOLD_STATE: ("queue_hold_state", "busy"),
}


def _compact(payload: dict) -> str:
    """
    A structured payload, encoded into the message field.

    The event envelope is three string keys and has been since the bridge was
    written; ``voice_audio_spectrum`` already carries its spectrum this way.
    Following that rather than widening the envelope keeps the Rust
    deserialiser and every existing test untouched.
    """
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _queue_event_message(event: QueueEvent) -> str:
    payload: dict[str, Any] = {}

    # A hold-state event is about the machine rather than about any one task,
    # so it carries neither. Sending two empty strings with every one of them
    # is noise on a pipe the voice spectrum already keeps busy.
    if event.task_id:
        payload["taskId"] = event.task_id

    if event.summary:
        payload["summary"] = event.summary

    if event.reason is not None:
        payload["reason"] = event.reason.value

    if event.detail:
        payload["detail"] = event.detail

    if event.level is not None:
        payload["level"] = event.level.value

    if event.floor is not None:
        payload["breach"] = (
            None if event.floor.breach is None else event.floor.breach.value
        )
        payload["requiredVramMb"] = event.floor.required_vram_mb
        payload["freeVramMb"] = event.floor.free_vram_mb

    if event.event is QueueEventType.FINISHED:
        payload["success"] = True
    elif event.event is QueueEventType.FAILED:
        payload["success"] = False

    return _compact(payload)


def _queue_view_message(view) -> str:
    return _compact(
        {
            "revision": view.revision,
            "paused": view.paused,
            "level": view.level.value,
            "holdingSince": view.holding_since,
            "tasks": list(view.tasks),
        }
    )


def emit(
    event_type: str,
    status: str,
    message: str,
) -> None:
    event = RuntimeEvent(
        event_type=event_type,
        status=status,
        message=message,
    )

    payload = (
        json.dumps(
            {
                "eventType": event.event_type,
                "status": event.status,
                "message": event.message,
            },
            ensure_ascii=True,
        )
        + "\n"
    )

    with EMIT_LOCK:
        if sys.stdout.closed:
            # The queue runs on its own thread and can outlive main() by a few
            # milliseconds during shutdown. Writing to a closed stream raises,
            # and an exception on a background thread prints a traceback to
            # stderr, which the desktop reports to the user as a crash. There
            # is nowhere to send this line, so it is dropped.
            return

        sys.stdout.write(payload)
        sys.stdout.flush()


def parse_payload(
    line: str,
) -> dict[str, Any]:
    payload: Any = json.loads(line)

    if not isinstance(payload, dict):
        raise ValueError(
            "Runtime command must be a JSON object."
        )

    command = str(
        payload.get(
            "command",
            "",
        )
    ).strip()

    if not command:
        raise ValueError(
            "Runtime command is empty."
        )

    payload["command"] = command
    return payload


def handle_action(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    action_id = str(
        payload.get(
            "actionId",
            "",
        )
    ).strip()

    if not action_id:
        raise ValueError(
            "Runtime action id is empty."
        )

    emit(
        "runtime_action_received",
        "ready",
        action_id,
    )

    pause_safe_actions = {
        CONVERSATION_HISTORY_REQUEST_ACTION,
        VOICE_PLAYBACK_COMPLETE_ACTION,
        WAKE_LISTENER_STOP_ACTION,
        SETTINGS_RELOAD_ACTION,
        INPUT_METER_STOP_ACTION,
    }

    if (
        runtime.runtime_paused
        and action_id not in pause_safe_actions
    ):
        runtime._reject_if_runtime_paused(
            f"action:{action_id}"
        )
        return

    if action_id == CONVERSATION_HISTORY_REQUEST_ACTION:
        runtime.emit_conversation_history_snapshot()
        return

    if action_id == SETTINGS_RELOAD_ACTION:
        runtime.reload_settings()
        return

    if action_id == INPUT_METER_START_ACTION:
        runtime.start_input_meter()
        return

    if action_id == INPUT_METER_STOP_ACTION:
        runtime.stop_input_meter()
        return

    if action_id == WAKE_LISTENER_START_ACTION:

        runtime.start_wake_listener()


        return

    if action_id == WAKE_LISTENER_STOP_ACTION:
        runtime.stop_wake_listener()
        emit(
            "wake_word_stopped",
            "ready",
            "Qronos wake-word listener stopped.",
        )
        return

    if action_id == VOICE_PLAYBACK_COMPLETE_ACTION:
        raw_playback_id = payload.get(
            "playbackId",
            0,
        )

        try:
            playback_id = int(
                raw_playback_id
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Playback completion needs a valid playbackId."
            ) from exc

        if playback_id <= 0:
            raise ValueError(
                "Playback completion needs a positive playbackId."
            )

        accepted = (
            runtime.mark_voice_playback_complete(
                playback_id
            )
        )

        if accepted:
            emit(
                "voice_playback_acknowledged",
                "ready",
                str(playback_id),
            )
        else:
            emit(
                "runtime_warning",
                "warning",
                (
                    "Ignored stale voice playback completion: "
                    f"{playback_id}"
                ),
            )

        return

    if action_id == PUSH_TO_TALK_ACTION:
        worker = threading.Thread(
            target=runtime.push_to_talk,
            name="qronos-push-to-talk",
            daemon=True,
        )
        worker.start()
        return

    if action_id == LOOK_AT_SCREEN_ACTION:
        worker = threading.Thread(
            target=lambda: runtime.look_at_screen(
                approved=bool(payload.get("approved", False)),
                question=str(payload.get("question", "")),
                window_only=bool(payload.get("windowOnly", False)),
            ),
            name="qronos-look-at-screen",
            daemon=True,
        )
        worker.start()
        return

    emit(
        "runtime_warning",
        "warning",
        f"Unsupported runtime action: {action_id}",
    )


def handle_text_message(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    text = str(
        payload.get(
            "text",
            "",
        )
    ).strip()
    request_id = str(
        payload.get(
            "requestId",
            "",
        )
    ).strip()

    if not text:
        raise ValueError(
            "Text message must not be empty."
        )

    if len(text) > MAX_TEXT_MESSAGE_LENGTH:
        raise ValueError(
            "Text message exceeds the "
            f"{MAX_TEXT_MESSAGE_LENGTH} character limit."
        )

    if not request_id:
        raise ValueError(
            "Text request id must not be empty."
        )

    if (
        len(request_id)
        > MAX_TEXT_REQUEST_ID_LENGTH
    ):
        raise ValueError(
            "Text request id exceeds the "
            f"{MAX_TEXT_REQUEST_ID_LENGTH} character limit."
        )

    if runtime.runtime_paused:
        runtime._notify(
            "text_error",
            "paused",
            _text_event_message(
                request_id,
                error="Qronos runtime is temporarily paused.",
            ),
        )
        return

    worker = threading.Thread(
        target=runtime.run_text_turn,
        args=(
            text,
            request_id,
        ),
        name="qronos-text-turn",
        daemon=True,
    )
    worker.start()


#: The longest summary the desktop may submit. A queue entry is shown in a
#: narrow panel, and an unbounded string arriving over a pipe is somebody
#: else's memory to spend.
MAX_SUMMARY_LENGTH = 200

#: Larger than any card Qronos targets. A bound rather than a meaningful
#: limit: the number arrives over a pipe and is used in arithmetic.
MAX_REQUIRED_VRAM_MB = 1_048_576

#: Set to "1" to enable the commands that only exist for demonstrating the
#: queue. Off by default, and off in anything a user installs, so a line
#: arriving on stdin cannot reach them.
DEMO_VARIABLE = "QRONOS_QUEUE_DEMO"


def _demo_is_enabled() -> bool:
    return os.environ.get(DEMO_VARIABLE, "").strip() == "1"

#: Overrides the pump's interval. Not a user-facing setting and not part of the
#: protocol — the tick rate is an implementation detail. It exists so a test
#: can make the pump genuinely busy in a second rather than in a minute, which
#: is the only way to exercise the shutdown race. ``resolve_python`` in the
#: Rust bridge reads ``PYTHON`` the same way.
TICK_SECONDS_VARIABLE = "QRONOS_QUEUE_TICK_SECONDS"


def _scheduler_config_from_environment() -> SchedulerConfig:
    raw = os.environ.get(TICK_SECONDS_VARIABLE, "").strip()

    if not raw:
        return DEFAULT_SCHEDULER_CONFIG

    try:
        seconds = float(raw)
    except ValueError:
        return DEFAULT_SCHEDULER_CONFIG

    if seconds <= 0:
        return DEFAULT_SCHEDULER_CONFIG

    return SchedulerConfig(tick_seconds=seconds)

_WEIGHTS = {weight.value: weight for weight in Weight}


def _task_id_from(payload: dict[str, Any]) -> str:
    task_id = str(payload.get("taskId", "")).strip()

    if not task_id:
        raise ValueError("This command needs a taskId.")

    return task_id


def handle_queue_list(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    """Send the queue as it stands. Also what first starts the scheduler."""
    scheduler = runtime.ensure_scheduler()

    emit(
        "queue_changed",
        "ready",
        _queue_view_message(scheduler.view()),
    )


def handle_queue_submit(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    if runtime._reject_if_runtime_paused("queue_submit"):
        return

    summary = str(payload.get("summary", "")).strip()

    if not summary:
        raise ValueError("A queued task must say what it is.")

    if len(summary) > MAX_SUMMARY_LENGTH:
        raise ValueError(
            f"A task summary may be at most {MAX_SUMMARY_LENGTH} characters."
        )

    weight_name = str(payload.get("weight", "light")).strip().lower()
    weight = _WEIGHTS.get(weight_name)

    if weight is None:
        raise ValueError(
            f"Unknown task weight {weight_name!r}; "
            f"expected one of {sorted(_WEIGHTS)}."
        )

    profile = MODELS["heavy" if weight is Weight.HEAVY else "fast"]
    needs = payload.get("requiredVramMb")

    if needs is None:
        # A task that did not say assumes it will load the brain its weight
        # implies, which is the only kind of work the queue exists for.
        needed_mb = required_vram_mb(profile.estimated_vram_gb)
    elif isinstance(needs, bool) or not isinstance(needs, int):
        raise ValueError("requiredVramMb must be a whole number of megabytes.")
    elif not 0 <= needs <= MAX_REQUIRED_VRAM_MB:
        raise ValueError(
            f"requiredVramMb must be between 0 and {MAX_REQUIRED_VRAM_MB}."
        )
    else:
        needed_mb = needs

    runtime.ensure_scheduler().submit(
        work=_DemonstrationWork(summary),
        weight=weight,
        summary=summary,
        required_vram_mb=needed_mb,
    )


def handle_queue_cancel(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    runtime.ensure_scheduler().cancel(_task_id_from(payload))


def handle_queue_override(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    if runtime._reject_if_runtime_paused("queue_override"):
        return

    """
    Run it anyway — if only politeness was in the way.

    The result is not returned to the caller. It arrives as an event, because
    a refused override has to be visible to whoever is looking at the queue
    and not only to whoever pressed the button.
    """
    runtime.ensure_scheduler().override(_task_id_from(payload))


def handle_queue_set_paused(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    paused = payload.get("paused")

    if not isinstance(paused, bool):
        raise ValueError("queue_set_paused needs paused to be true or false.")

    if runtime.runtime_paused and not paused:
        runtime._reject_if_runtime_paused("queue_resume")
        return

    runtime.ensure_scheduler().set_paused(paused)


def handle_runtime_set_paused(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    paused = payload.get("paused")

    if not isinstance(paused, bool):
        raise ValueError(
            "runtime_set_paused needs paused to be true or false."
        )

    runtime.set_runtime_paused(paused)


def handle_queue_debug_load(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
    if runtime._reject_if_runtime_paused("queue_debug_load"):
        return

    """
    Tell the monitor what to think the machine is doing.

    This exists for one reason. Demonstrating that Qronos *holds* work needs
    only a busy machine, and a busy machine is easy to come by. Demonstrating
    that it *releases* work needs a machine that stops being busy, and the only
    honest ways to arrange that are to wait an unbounded amount of time or to
    close something of the user's — and closing something of the user's is the
    exact thing this whole feature exists to avoid.

    So the sensor is substituted rather than the machine. Everything downstream
    is real: the same monitor, dwell, scheduler, governor, floor, protocol and
    interface. Only the readings are supplied rather than measured, and any
    write-up of a demonstration using this has to say so.

    Off unless QRONOS_QUEUE_DEMO is set to 1.
    """
    if not _demo_is_enabled():
        raise ValueError(
            "queue_debug_load is only available when "
            f"{DEMO_VARIABLE}=1."
        )

    state = str(payload.get("state", "")).strip().lower()

    if state not in ("free", "busy"):
        raise ValueError("queue_debug_load needs state to be free or busy.")

    scheduler = runtime.ensure_scheduler()
    monitor = scheduler.monitor
    loaded = state == "busy"

    # Replace what the monitor believes rather than adding to it. Appending
    # alone does not work: readings from an earlier call are still inside the
    # window, so a later "free" and an earlier "busy" simply argue with each
    # other and the verdict sits in the hysteresis band. Observed exactly that
    # way before this line existed.
    monitor.reset()

    # Enough readings to satisfy the longer of the two dwells, so the verdict
    # actually moves. Dated backwards from now, never forwards: a reading
    # stamped in the future outlives every real one taken after it.
    span = max(
        monitor.config.busy_dwell_seconds,
        monitor.config.free_dwell_seconds,
    )
    interval = monitor.config.sample_interval_seconds
    count = int(span / interval) + 2
    now = monitor.clock()

    for index in range(count):
        monitor.offer(
            LoadSample(
                at=now - (count - 1 - index) * interval,
                decision=(
                    ResourceDecision.BLOCK
                    if loaded
                    else ResourceDecision.ALLOW
                ),
                cpu_percent=95.0 if loaded else 5.0,
                ram_percent=43.0,
                vram_free_mb=256 if loaded else 15_000,
                gpu_utilization_percent=99 if loaded else 1,
                gpu_temperature_c=45,
            )
        )

    emit(
        "queue_hold_state",
        "busy" if loaded else "ready",
        _compact(
            {
                "level": monitor.snapshot().level.value,
                "simulated": True,
            }
        ),
    )


QUEUE_COMMANDS: dict[str, Callable[[QronosRuntime, dict[str, Any]], None]] = {
    "queue_debug_load": handle_queue_debug_load,
    "queue_list": handle_queue_list,
    "queue_submit": handle_queue_submit,
    "queue_cancel": handle_queue_cancel,
    "queue_override": handle_queue_override,
    "queue_set_paused": handle_queue_set_paused,
}


class _DemonstrationWork:
    """
    A queued item with nothing behind it yet.

    There is no background worker in Qronos to attach to: research routes to
    ``TaskType.BROWSER``, whose worker is registered nowhere, and a spoken
    heavy turn runs through the voice path rather than the queue. So what the
    desktop can queue today is this — a placeholder that waits, is held, is
    shown, can be overridden and cancelled, and produces nothing.

    That is enough to exercise and demonstrate every part of the queue, and it
    is honest about being a placeholder rather than pretending to be work.
    """

    def __init__(self, summary: str) -> None:
        self.summary = summary

    def describe(self) -> str:
        return self.summary

    def run(self) -> bool:
        return True


def main() -> int:
    runtime = QronosRuntime()

    # The real desktop must not depend on a later Settings write or a frontend
    # timing race to begin listening. Tauri marks the desktop process with an
    # explicit environment flag; raw bridge tests keep their no-side-effect
    # startup behavior because they do not set it.
    if (
        _desktop_wake_bootstrap_enabled()
        and runtime.wake_word_enabled
    ):
        bootstrap_started = time.perf_counter()

        try:
            _preload_openwakeword_for_desktop()
            runtime.start_wake_listener()

            if runtime._wake_ready.wait(5.0):
                emit(
                    "wake_bootstrap_ready",
                    "ready",
                    json.dumps(
                        {
                            "seconds": round(
                                time.perf_counter()
                                - bootstrap_started,
                                3,
                            ),
                            "beforeRuntimeReady": True,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                )
            else:
                emit(
                    "wake_bootstrap_pending",
                    "warning",
                    json.dumps(
                        {
                            "seconds": round(
                                time.perf_counter()
                                - bootstrap_started,
                                3,
                            ),
                            "beforeRuntimeReady": False,
                            "listenerRunning":
                                runtime.wake_listener_running,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                )
        except Exception as exc:
            # Wake retry/recovery remains inside QronosRuntime. A bootstrap
            # failure must not prevent the rest of the desktop bridge from
            # becoming usable.
            emit(
                "wake_bootstrap_failed",
                "warning",
                str(exc),
            )

    emit(
        "runtime_ready",
        "ready",
        "Qronos runtime bridge is ready.",
    )

    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()

            if not line:
                continue

            try:
                payload = parse_payload(
                    line
                )
                command = payload[
                    "command"
                ]

                if command == "ping":
                    emit(
                        "runtime_pong",
                        "ready",
                        "Qronos runtime bridge responded.",
                    )
                    continue

                if command == RUNTIME_SET_PAUSED_COMMAND:
                    handle_runtime_set_paused(
                        runtime,
                        payload,
                    )
                    continue

                if command == TEXT_MESSAGE_COMMAND:
                    handle_text_message(
                        runtime,
                        payload,
                    )
                    continue

                if command == "action":
                    handle_action(
                        runtime,
                        payload,
                    )
                    continue

                handler = QUEUE_COMMANDS.get(command)

                if handler is not None:
                    handler(runtime, payload)
                    continue

                if command == "shutdown":
                    emit(
                        "runtime_stopping",
                        "stopping",
                        "Qronos runtime bridge is stopping.",
                    )
                    return 0

                emit(
                    "runtime_warning",
                    "warning",
                    f"Unknown runtime command: {command}",
                )

            except Exception as exc:
                emit(
                    "runtime_error",
                    "error",
                    str(exc),
                )

    finally:
        runtime.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
