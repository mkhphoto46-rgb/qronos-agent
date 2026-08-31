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
from core.audio_input import AudioInput
from core.chatterbox_runtime import ChatterboxRuntime
from core.command_recorder import CommandRecorder, CommandRecorderConfig
from core.conversation_session import ConversationSession
from core.hard_floor import required_vram_mb
from core.load_signal import LoadSample, SustainedLoadMonitor
from core.resource_context import build_sustained_load_monitor
from core.model_registry import MODELS
from core.openwakeword_engine import OpenWakeWordEngine
from core.orchestrator import Orchestrator
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
from core.task_router import TaskRouter, TaskType
from core.vision_ocr import read_screen_text
from core.vision_worker import build_vision_worker
from core.whisper_cpp_runtime import WhisperCppRuntime
from core.whisper_cpp_vad_runtime import WhisperCppVADRuntime
from core.web_worker import WebResearchWorker
from security.gate import set_default_audit_sink
from core.voice_trigger import VoiceTriggerService


PUSH_TO_TALK_ACTION = "qronos.push_to_talk"
LOOK_AT_SCREEN_ACTION = "qronos.look_at_screen"
WAKE_LISTENER_START_ACTION = "qronos.wake_listener_start"
WAKE_LISTENER_STOP_ACTION = "qronos.wake_listener_stop"
WAKE_PLAYBACK_GUARD_SECONDS = 0.35
FOLLOWUP_START_TIMEOUT_SECONDS = 12.0
FOLLOWUP_PLAYBACK_GUARD_SECONDS = 0.25

# A wake-word candidate needs stronger speech evidence than an
# already-established follow-up turn. This prevents short wind/noise
# bursts from becoming phantom commands after a false wake.
WAKE_MIN_COMMAND_SPEECH_SECONDS = 0.40

AUDIO_SPECTRUM_BANDS = 32
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


class QronosRuntime:
    def __init__(
        self,
        scheduler: QueueScheduler | None = None,
        notify: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._busy = False

        # Resolved here rather than as a default argument, because emit() is
        # defined below this class and a default is evaluated at definition
        # time — which would be a NameError at import.
        self._notify = notify if notify is not None else emit
        self._scheduler = scheduler

        self.audio_input: AudioInput | None = None
        self.vad_runtime: WhisperCppVADRuntime | None = None
        self.command_recorder: CommandRecorder | None = None
        self.speech_runtime: WhisperCppRuntime | None = None
        self.voice_output: ChatterboxRuntime | None = None
        self.task_router: TaskRouter | None = None
        self.action_audit: ActionAuditLog | None = None
        self.screen_capture: ScreenCapture | None = None
        self.last_foreground_window: int | None = None
        self.pending_look: str | None = None
        self.orchestrator: Orchestrator | None = None
        self.conversation_session: ConversationSession | None = None

        self.wake_engine: OpenWakeWordEngine | None = None
        self.voice_trigger: VoiceTriggerService | None = None
        self._wake_thread: threading.Thread | None = None
        self._wake_stop = threading.Event()

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

    def _set_busy(self, value: bool) -> None:
        with self._lock:
            self._busy = value

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

    def _clear_audio_spectrum(self) -> None:
        self._emit_audio_spectrum(
            0.0,
            tuple(
                0.0
                for _ in range(AUDIO_SPECTRUM_BANDS)
            ),
        )

    def prepare(self) -> None:
        if self.audio_input is not None:
            return

        emit(
            "runtime_preparing",
            "preparing",
            "Preparing Qronos voice runtime.",
        )

        audio_input = AudioInput()
        vad_runtime = WhisperCppVADRuntime()
        speech_runtime = WhisperCppRuntime()
        voice_output = ChatterboxRuntime()

        if not vad_runtime.health_check():
            raise RuntimeError(
                "Qronos VAD runtime is not ready."
            )

        if not speech_runtime.health_check():
            raise RuntimeError(
                "Qronos speech runtime is not ready."
            )

        if not voice_output.health_check():
            raise RuntimeError(
                "Qronos TTS runtime is not ready."
            )

        vad_runtime.prepare()

        self.audio_input = audio_input
        self.vad_runtime = vad_runtime
        self.command_recorder = CommandRecorder(
            audio_input=audio_input,
            vad_runtime=vad_runtime,
            config=CommandRecorderConfig(
                silence_seconds=0.96,
                start_timeout_seconds=(
                    FOLLOWUP_START_TIMEOUT_SECONDS
                ),
            ),
        )
        self.speech_runtime = speech_runtime
        self.voice_output = voice_output
        self.task_router = TaskRouter()

        self.action_audit = ActionAuditLog()
        set_default_audit_sink(self.action_audit.record_verdict)

        self.screen_capture = ScreenCapture(read_text=read_screen_text)

        self.orchestrator = Orchestrator()
        self.orchestrator.workers.register(
            WebResearchWorker(
                answer_fn=self.orchestrator.answer_web_prompt,
            )
        )
        self.orchestrator.workers.register(
            build_vision_worker(self.orchestrator.runtime)
        )
        self.conversation_session = ConversationSession()

        emit(
            "runtime_prepared",
            "ready",
            "Qronos voice runtime is ready.",
        )

    def _require_prepared(self) -> None:
        """
        Fail loudly when a collaborator is missing.

        These were ``assert`` statements. Python removes ``assert`` entirely
        under ``-O``, so in an optimised run the guard disappeared and the very
        next line dereferenced ``None`` — a raw AttributeError from inside the
        voice path rather than a message the user could act on. An explicit
        check survives the flag, and the caller already turns a RuntimeError
        into a ``runtime_error`` event the desktop displays.

        This should be unreachable: ``prepare`` assigns all seven or raises.
        It stays because the cost of being wrong is a crash mid-utterance.
        """
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

    @property
    def wake_listener_running(self) -> bool:
        thread = self._wake_thread
        return thread is not None and thread.is_alive()

    def start_wake_listener(self) -> None:
        """
        Start the low-cost Qronos wake-word loop.

        The listener uses the same AudioInput object as command recording, but
        never reads from it concurrently. After detection, this thread pauses
        wake-word inference and immediately hands the already-open microphone
        to CommandRecorder, preserving the first phoneme of the user's command.
        """
        with self._lock:
            if self._wake_thread is not None and self._wake_thread.is_alive():
                self._notify(
                    "wake_word_listening",
                    "listening",
                    "Qronos wake-word listener is already active.",
                )
                return

            self._wake_stop.clear()
            thread = threading.Thread(
                target=self._wake_listener_loop,
                name="qronos-wake-word",
                daemon=True,
            )
            self._wake_thread = thread

        thread.start()

    def stop_wake_listener(self) -> None:
        self._wake_stop.set()

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
            self.prepare()
            self._require_prepared()

            if self.audio_input is None:
                raise RuntimeError(
                    "Qronos microphone is not prepared for wake-word listening."
                )

            engine = OpenWakeWordEngine()
            trigger = VoiceTriggerService(
                wake_word="Qronos",
                engine=engine,
            )

            self.wake_engine = engine
            self.voice_trigger = trigger

            trigger.start()
            self.audio_input.start()

            self._notify(
                "wake_word_listening",
                "listening",
                "Say Qronos to start a voice session.",
            )

            while not self._wake_stop.is_set():
                try:
                    frame = self.audio_input.read_frame()
                except Exception:
                    if self._wake_stop.is_set():
                        break
                    raise

                event = trigger.process_audio(
                    frame,
                    timestamp=time.time(),
                )

                if event is None:
                    continue

                wake_detected_perf = (
                    time.perf_counter()
                )

                trigger.pause()

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
                    )
                )

                # A wake-word candidate is not enough to establish an
                # interactive conversation. If no valid spoken turn was
                # completed after the wake event, close any session that
                # may have been opened and return directly to wake-word
                # listening. This prevents wind/noise false wakes from
                # falling through into unrestricted follow-up listening.
                if playback_seconds <= 0:
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
                            "Wake candidate cancelled because "
                            "no valid speech turn was completed."
                        ),
                    )

                while (
                    playback_seconds > 0
                    and not self._wake_stop.is_set()
                    and self.conversation_session
                    is not None
                    and self.conversation_session.is_active
                ):
                    if playback_seconds > 0:
                        self._wake_stop.wait(
                            playback_seconds
                            + FOLLOWUP_PLAYBACK_GUARD_SECONDS
                        )

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

                # Resume the existing wake-word pipeline.
                # OpenWakeWord resume() resets its rolling
                # prediction/audio buffers and performs its
                # warm-up path without rebuilding the engine.
                self.audio_input.start()
                trigger.resume()

                self._notify(
                    "wake_word_listening",
                    "listening",
                    "Say Qronos to start a voice session.",
                )

        except Exception as exc:
            if not self._wake_stop.is_set():
                self._notify(
                    "runtime_error",
                    "error",
                    f"Wake-word listener failed: {exc}",
                )

        finally:
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

        utterance = self.voice_output.speak_to_file(
            response
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

        for index, chunk in enumerate(
            chunks
        ):
            utterance = (
                self.voice_output.speak_to_file(
                    chunk
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

            self._notify(
                "voice_audio_ready",
                "ready",
                json.dumps(
                    {
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
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            )

        return {
            "chunks": chunks,
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
        Persist one voice-turn latency report under the project E: runtime.

        Diagnostics must never break a voice turn. If writing fails, emit only
        a warning and continue.
        """
        try:
            VOICE_LATENCY_LOG_PATH.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            VOICE_LATENCY_LOG_PATH.write_text(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        except Exception:
            # Diagnostics must never change the runtime event contract or
            # interfere with a voice turn.
            pass


    def _run_voice_turn(
        self,
        trigger_source: str,
        detected_at_perf: float | None = None,
        wake_score: float | None = None,
    ) -> float:
        """
        Capture one user command, run the existing Brain path, synthesize it,
        and return the generated audio duration.

        This version also instruments every material latency segment without
        changing the execution order. That gives us a factual baseline before
        introducing semantic chunking or streaming.
        """
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
            self.prepare()
            self._require_prepared()

            self._notify(
                "voice_listening",
                "listening",
                "Listening for your command.",
            )

            self.audio_input.start()

            recording_started = time.perf_counter()

            recording = self.command_recorder.record_to_file(
                self.speech_runtime.temp_dir
                / "qronos_voice_command.wav",
                on_audio_spectrum=self._emit_audio_spectrum,
            )

            recording_finished = time.perf_counter()

            self.audio_input.stop()
            self._clear_audio_spectrum()

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

            self._notify(
                "voice_transcribing",
                "processing",
                "Transcribing voice command.",
            )

            stt_started = time.perf_counter()

            transcript = (
                self.speech_runtime.transcribe_file(
                    recording.audio_path,
                    language="fa",
                )
                .strip()
            )

            stt_finished = time.perf_counter()

            transcript = normalise_qronos_invocation(transcript)

            if not transcript:
                raise RuntimeError(
                    "Speech recognition returned an empty command."
                )

            self._notify(
                "voice_transcript",
                "processing",
                transcript,
            )

            if (
                trigger_source == "followup"
                and _is_conversation_end_phrase(
                    transcript
                )
            ):
                if (
                    self.conversation_session
                    is not None
                    and self.conversation_session.is_active
                ):
                    self.conversation_session.close()

                self._notify(
                    "conversation_session_closed",
                    "ready",
                    "Conversation ended by user.",
                )

                self._notify(
                    "voice_turn_complete",
                    "ready",
                    "Follow-up session ended.",
                )

                return 0.0

            if not self.conversation_session.is_active:
                self.conversation_session.start()

            self.conversation_session.add_user_message(
                transcript
            )
            self.conversation_session.begin_processing()

            route_started = time.perf_counter()

            route = self.task_router.route(
                transcript
            )

            route_finished = time.perf_counter()

            self._notify(
                "voice_routed",
                "processing",
                route.task_type.value,
            )

            if route.task_type is TaskType.VISION:
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
                if route.task_type is TaskType.FAST
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
                    task_type=route.task_type,
                    description=transcript,
                )

                results = self.orchestrator.execute_plan(
                    plan
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

            self.conversation_session.begin_responding()
            self.conversation_session.add_assistant_message(
                response
            )

            self._notify(
                "voice_response",
                "ready",
                response,
            )

            tts_started = time.perf_counter()

            chunked = (
                self._synthesize_response_chunks(
                    response
                )
            )

            all_audio_ready = (
                time.perf_counter()
            )

            first_audio_ready = (
                chunked[
                    "firstAudioReadyPerf"
                ]
                or all_audio_ready
            )

            playback_elapsed_during_generation = max(
                0.0,
                all_audio_ready
                - first_audio_ready,
            )

            playback_seconds = max(
                0.0,
                float(
                    chunked[
                        "totalAudioSeconds"
                    ]
                )
                - playback_elapsed_during_generation,
            )

            report.update(
                {
                    "transcript": transcript,
                    "route": route.task_type.value,
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
            if trigger_source == "followup":
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

            self._write_latency_report(
                report
            )

            self._notify(
                "runtime_error",
                "error",
                str(exc),
            )

        except Exception as exc:
            report["error"] = str(exc)
            report["failedAtEpoch"] = round(
                time.time(),
                6,
            )

            self._write_latency_report(
                report
            )

            self._notify(
                "runtime_error",
                "error",
                str(exc),
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
        self._run_voice_turn(
            trigger_source="push_to_talk",
        )


    def look_at_screen(
        self,
        approved: bool = False,
        question: str = "",
        window_only: bool = False,
    ) -> None:
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
        self.stop_wake_listener()

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

    runtime.ensure_scheduler().set_paused(paused)


def handle_queue_debug_load(
    runtime: QronosRuntime,
    payload: dict[str, Any],
) -> None:
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
