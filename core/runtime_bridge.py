from __future__ import annotations

import json
import re
import sys
import threading
from dataclasses import dataclass
from typing import Any


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
from core.command_recorder import CommandRecorder
from core.conversation_session import ConversationSession
from core.orchestrator import Orchestrator
from core.screen_capture import (
    Capture,
    CaptureRefused,
    CaptureUnavailable,
    ScreenCapture,
    foreground_window,
)
from core.task_plan import TaskPlan
from core.task_router import TaskRouter, TaskType
from core.vision_ocr import read_screen_text
from core.vision_worker import build_vision_worker
from core.whisper_cpp_runtime import WhisperCppRuntime
from core.whisper_cpp_vad_runtime import WhisperCppVADRuntime
from security.gate import set_default_audit_sink
from core.web_worker import WebResearchWorker


PUSH_TO_TALK_ACTION = "qronos.push_to_talk"

#: Sent by the desktop once the person has been shown what will be captured
#: and has agreed. The permission for looking at the screen is UI confirmation,
#: so the answer has to come from the interface that showed them; the runtime
#: cannot give it to itself. Without ``approved`` the gate refuses and nothing
#: is captured, which is the failure that costs nothing.
LOOK_AT_SCREEN_ACTION = "qronos.look_at_screen"
AUDIO_SPECTRUM_BANDS = 32
EMIT_LOCK = threading.Lock()


def normalise_qronos_invocation(transcript: str) -> str:
    """Correct measured Whisper variants only when Qronos is addressed."""
    return re.sub(
        r"^(?P<greeting>\s*سلام\s+)?(?:کرونس|خرونس|کرونز)(?=\s|[،,؟?]|$)",
        lambda match: f"{match.group('greeting') or ''}کرونوس",
        transcript,
        count=1,
    )


@dataclass(frozen=True)
class RuntimeEvent:
    event_type: str
    status: str
    message: str


class QronosRuntime:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False

        self.audio_input: AudioInput | None = None
        self.vad_runtime: WhisperCppVADRuntime | None = None
        self.command_recorder: CommandRecorder | None = None
        self.speech_runtime: WhisperCppRuntime | None = None
        self.task_router: TaskRouter | None = None
        self.action_audit: ActionAuditLog | None = None
        self.screen_capture: ScreenCapture | None = None

        # The window the user was looking at when they last pressed the
        # hotkey. Read at that instant and remembered, because a moment later
        # Qronos may have focus itself and "read this window" would read
        # Qronos.
        self.last_foreground_window: int | None = None

        # What the user asked while the desktop is asking them whether Qronos
        # may look. Cleared as soon as it is used or refused, so a stale
        # question cannot be answered against a later screen.
        self.pending_look: str | None = None
        self.orchestrator: Orchestrator | None = None
        self.conversation_session: ConversationSession | None = None

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

        if not vad_runtime.health_check():
            raise RuntimeError(
                "Qronos VAD runtime is not ready."
            )

        if not speech_runtime.health_check():
            raise RuntimeError(
                "Qronos speech runtime is not ready."
            )

        vad_runtime.prepare()

        self.audio_input = audio_input
        self.vad_runtime = vad_runtime
        self.command_recorder = CommandRecorder(
            audio_input=audio_input,
            vad_runtime=vad_runtime,
        )
        self.speech_runtime = speech_runtime
        self.task_router = TaskRouter()

        # "Every decision is recorded" was true only in tests: the gate's
        # default sink was installed by the test suite and by nobody else, so
        # an executor that omitted the audit argument in production produced
        # no trail at all — silently, and indistinguishably from a call that
        # was never made. The first thing that can capture the screen must not
        # ship before this line exists.
        self.action_audit = ActionAuditLog()
        set_default_audit_sink(self.action_audit.record_verdict)

        # The screen is captured with Windows' own text recogniser attached.
        # It runs on the full-resolution pixels, which only exist at the moment
        # of capture, and costs no tokens and no graphics memory. Measured: on
        # a 4K desktop shrunk to what the model is sent, it takes the reading
        # error from 0.171 to 0.009. See core/windows_ocr.py.
        self.screen_capture = ScreenCapture(read_text=read_screen_text)
        self.orchestrator = Orchestrator()
        self.orchestrator.workers.register(
            WebResearchWorker(
                answer_fn=self.orchestrator.answer_web_prompt,
            )
        )

        # Vision is reachable from here on. It is given the orchestrator's own
        # runtime rather than a second one, so the two models queue behind each
        # other on one card instead of racing for it.
        #
        # Until screen capture lands, a spoken request to look at something
        # arrives with nothing attached and the worker says so. That is the
        # honest answer, and it is a different one from "vision does not
        # exist".
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

        This should be unreachable: ``prepare`` assigns all nine or raises.
        It stays because the cost of being wrong is a crash mid-utterance.
        """
        missing = [
            name
            for name in (
                "audio_input",
                "vad_runtime",
                "command_recorder",
                "speech_runtime",
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

    def push_to_talk(self) -> None:
        # First, before anything else and before any work that could take
        # focus. This is the only moment the answer is right.
        self.last_foreground_window = foreground_window()

        if self.is_busy:
            emit(
                "runtime_busy",
                "busy",
                "Qronos is already processing a voice request.",
            )
            return

        self._set_busy(True)

        try:
            self.prepare()
            self._require_prepared()

            emit(
                "voice_listening",
                "listening",
                "Listening for your command.",
            )

            self.audio_input.start()

            recording = self.command_recorder.record_to_file(
                self.speech_runtime.temp_dir
                / "qronos_push_to_talk.wav",
                on_audio_spectrum=self._emit_audio_spectrum,
            )

            self.audio_input.stop()
            self._clear_audio_spectrum()

            emit(
                "voice_transcribing",
                "processing",
                "Transcribing voice command.",
            )

            transcript = (
                self.speech_runtime.transcribe_file(
                    recording.audio_path,
                    language="fa",
                )
                .strip()
            )

            transcript = normalise_qronos_invocation(transcript)

            if not transcript:
                raise RuntimeError(
                    "Speech recognition returned an empty command."
                )

            emit(
                "voice_transcript",
                "processing",
                transcript,
            )

            if not self.conversation_session.is_active:
                self.conversation_session.start()

            self.conversation_session.add_user_message(
                transcript
            )
            self.conversation_session.begin_processing()

            route = self.task_router.route(
                transcript
            )

            emit(
                "voice_routed",
                "processing",
                route.task_type.value,
            )

            if route.task_type is TaskType.VISION:
                # Looking at the screen needs UI confirmation, and the runtime
                # cannot give itself one: a spoken "yes" is consent from
                # somebody who has not been shown what is about to be looked
                # at. So the turn stops here and the desktop asks. It comes
                # back as LOOK_AT_SCREEN_ACTION if the person agrees, and as
                # nothing at all if they do not — which is a complete and
                # correct outcome, not a dropped request.
                self.pending_look = transcript

                self.conversation_session.begin_listening()

                emit(
                    "voice_needs_screen",
                    "ready",
                    transcript,
                )

                return

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

            if not response:
                raise RuntimeError(
                    "Qronos returned an empty response."
                )

            self.conversation_session.begin_responding()
            self.conversation_session.add_assistant_message(
                response
            )
            self.conversation_session.begin_listening()

            emit(
                "voice_response",
                "ready",
                response,
            )

            emit(
                "voice_turn_complete",
                "ready",
                "Push-to-talk turn completed.",
            )

        except Exception as exc:
            emit(
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

    def close(self) -> None:
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

        if self.conversation_session is not None:
            try:
                self.conversation_session.close()
            except Exception:
                pass

        if self.orchestrator is not None:
            self.orchestrator.workers.close()
    def look_at_screen(
        self,
        approved: bool = False,
        question: str = "",
        window_only: bool = False,
    ) -> None:
        """
        The second half of a vision turn: the person has been asked, and said
        yes.

        ``approved`` comes from the desktop, never from here. A runtime that
        could approve its own captures would make the permission decorative.

        ``window_only`` uses the window that was in front when the hotkey
        fired, which is the only moment that answer was right.

        The capture never reaches disk. It is held in memory, encoded, handed
        to the model and dropped — which is a stronger guarantee than any
        retention period, and needs no janitor to keep.
        """
        if self.is_busy:
            emit(
                "runtime_busy",
                "busy",
                "Qronos is already processing a request.",
            )
            return

        self._set_busy(True)

        asked = (question or self.pending_look or "").strip()
        self.pending_look = None

        try:
            self.prepare()
            self._require_prepared()

            if not asked:
                raise RuntimeError(
                    "Qronos was asked to look at the screen without being "
                    "told what to look for."
                )

            if not approved:
                # Not an error and not a failure: a person said no, which is
                # the permission working. It ends the turn cleanly.
                emit(
                    "voice_screen_declined",
                    "ready",
                    "Qronos did not look at the screen.",
                )
                return

            emit(
                "voice_capturing_screen",
                "processing",
                asked,
            )

            capture = self._capture_for(window_only)

            emit(
                "voice_captured_screen",
                "processing",
                capture.describe(),
            )

            if capture.blank:
                # Answering this without a model is both faster and more
                # honest. Shown a flat rectangle, the model spends five
                # seconds and ten gigabytes describing a flat rectangle.
                emit(
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

            emit(
                "voice_response",
                "ready",
                response,
            )

            emit(
                "voice_turn_complete",
                "ready",
                "Screen reading turn completed.",
            )

        except (CaptureRefused, CaptureUnavailable) as exc:
            # Separated from the general failure below because these two say
            # something a person can act on — "you were not asked" and "this
            # machine cannot" — rather than describing a fault.
            emit(
                "voice_screen_unavailable",
                "error",
                str(exc),
            )

        except Exception as exc:
            emit(
                "runtime_error",
                "error",
                str(exc),
            )

        finally:
            self._set_busy(False)

    def _capture_for(self, window_only: bool) -> Capture:
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

    if action_id == PUSH_TO_TALK_ACTION:
        _in_background(runtime.push_to_talk, "qronos-push-to-talk")
        return

    if action_id == LOOK_AT_SCREEN_ACTION:
        _in_background(
            lambda: runtime.look_at_screen(
                approved=bool(payload.get("approved", False)),
                question=str(payload.get("question", "")),
                window_only=bool(payload.get("windowOnly", False)),
            ),
            "qronos-look-at-screen",
        )
        return

    emit(
        "runtime_warning",
        "warning",
        f"Unsupported runtime action: {action_id}",
    )


def _in_background(work: Any, name: str) -> None:
    """
    Run a turn off the reader thread.

    The bridge reads one command per line from stdin, and a turn that runs on
    that thread stops it reading — so a "stop" sent during a turn would not
    arrive until the turn it was meant to stop had finished.
    """
    threading.Thread(target=work, name=name, daemon=True).start()


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
