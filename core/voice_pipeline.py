from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.audio_input import AudioInput
from core.command_recorder import (
    CommandRecorder,
    CommandRecordingResult,
)
from core.config import CONFIG
from core.orchestrator import (
    Orchestrator,
    StepResult,
)
from core.speech_runtime import SpeechRuntime
from core.task_plan import TaskPlan
from core.task_router import (
    RouteDecision,
    TaskRouter,
)
from core.vad_runtime import VADRuntime
from core.voice_trigger import (
    VoiceTriggerEvent,
    VoiceTriggerService,
)


DEFAULT_COMMAND_AUDIO_PATH = (
    CONFIG.paths.temp
    / "qronos_voice_command.wav"
)


WakeDetectedCallback = Callable[
    [VoiceTriggerEvent],
    None,
]


@dataclass(frozen=True)
class VoicePipelineResult:
    """
    Final result of one complete Qronos voice interaction.
    """

    success: bool
    wake_event: VoiceTriggerEvent | None
    recording: CommandRecordingResult | None
    transcript: str
    route: RouteDecision | None
    response: str
    error: str | None = None


class VoicePipeline:
    """
    Connect Qronos wake word, command recording, STT, routing, and Brain.

    Flow:

        Wake Word
            ->
        Command Recorder + VAD
            ->
        Speech-to-Text
            ->
        Task Router
            ->
        Orchestrator
            ->
        Fast / Heavy Brain

    A lightweight wake-detected callback may be supplied by the UI or
    diagnostics layer. It runs immediately after the wake word is accepted
    and before command recording begins.

    This allows Qronos to visibly or audibly acknowledge the transition:

        LISTENING FOR WAKE WORD
            ->
        WAKE DETECTED
            ->
        LISTENING FOR COMMAND
    """

    def __init__(
        self,
        audio_input: AudioInput,
        voice_trigger: VoiceTriggerService,
        command_recorder: CommandRecorder,
        vad_runtime: VADRuntime,
        speech_runtime: SpeechRuntime,
        task_router: TaskRouter,
        orchestrator: Orchestrator,
        command_audio_path: str | Path = DEFAULT_COMMAND_AUDIO_PATH,
        on_wake_detected: WakeDetectedCallback | None = None,
    ) -> None:
        self.audio_input = audio_input
        self.voice_trigger = voice_trigger
        self.command_recorder = command_recorder
        self.vad_runtime = vad_runtime
        self.speech_runtime = speech_runtime
        self.task_router = task_router
        self.orchestrator = orchestrator

        self.command_audio_path = Path(
            command_audio_path
        )

        self.on_wake_detected = (
            on_wake_detected
        )

        self._prepared = False
        self._closed = False

    @property
    def is_prepared(self) -> bool:
        return self._prepared

    @property
    def is_closed(self) -> bool:
        return self._closed

    def prepare(self) -> None:
        """
        Prepare every latency-sensitive component before listening.

        Wake-word and VAD models are loaded before the user is asked to
        speak. The microphone is then started and Qronos enters listening
        mode.
        """

        if self._closed:
            raise RuntimeError(
                "Voice pipeline is closed."
            )

        if self._prepared:
            return

        if not self.vad_runtime.health_check():
            raise RuntimeError(
                "VAD runtime is not ready."
            )

        if not self.speech_runtime.health_check():
            raise RuntimeError(
                "Speech runtime is not ready."
            )

        try:
            self.vad_runtime.prepare()
            self.voice_trigger.start()
            self.audio_input.start()

        except Exception:
            self.audio_input.stop()
            self.voice_trigger.stop()
            raise

        self._prepared = True

    def _wait_for_wake_word(
        self,
    ) -> VoiceTriggerEvent:
        while True:
            frame = (
                self.audio_input.read_frame()
            )

            event = (
                self.voice_trigger.process_audio(
                    frame,
                    timestamp=time.time(),
                )
            )

            if event is not None:
                return event

    def _notify_wake_detected(
        self,
        event: VoiceTriggerEvent,
    ) -> None:
        """
        Notify the presentation layer that command capture is about to start.

        The callback must remain lightweight. It is intended for state
        changes such as UI animation, text feedback, or a short acknowledgement
        sound, not expensive processing.
        """

        if self.on_wake_detected is None:
            return

        self.on_wake_detected(
            event
        )

    def _build_plan(
        self,
        transcript: str,
        route: RouteDecision,
    ) -> TaskPlan:
        plan = TaskPlan(
            goal=transcript
        )

        plan.add_step(
            task_type=route.task_type,
            description=transcript,
        )

        return plan

    @staticmethod
    def _get_final_step(
        results: list[StepResult],
    ) -> StepResult | None:
        if not results:
            return None

        return results[-1]

    def _rearm_microphone(
        self,
    ) -> None:
        if (
            self._closed
            or not self._prepared
        ):
            return

        if not self.audio_input.is_running():
            self.audio_input.start()

        self.voice_trigger.resume()

    def listen_once(
        self,
    ) -> VoicePipelineResult:
        """
        Wait for Qronos, capture one command, and return one Brain response.
        """

        if self._closed:
            raise RuntimeError(
                "Voice pipeline is closed."
            )

        if not self._prepared:
            raise RuntimeError(
                "Voice pipeline must be prepared "
                "before listening."
            )

        wake_event: VoiceTriggerEvent | None = None
        recording: CommandRecordingResult | None = None
        transcript = ""
        route: RouteDecision | None = None

        try:
            wake_event = (
                self._wait_for_wake_word()
            )

            # Freeze wake-word processing immediately so the accepted wake
            # word cannot create another trigger while command capture starts.
            self.voice_trigger.pause()

            # Notify UI / diagnostics immediately. This is the boundary
            # between "say Qronos" and "say your command".
            self._notify_wake_detected(
                wake_event
            )

            recording = (
                self.command_recorder.record_to_file(
                    self.command_audio_path
                )
            )

            # Qronos is now thinking. Stop microphone capture so unnecessary
            # microphone input is not consumed while Whisper and the Brain
            # are working.
            self.audio_input.stop()

            transcript = (
                self.speech_runtime.transcribe_file(
                    recording.audio_path,
                    language="auto",
                )
            ).strip()

            if not transcript:
                return VoicePipelineResult(
                    success=False,
                    wake_event=wake_event,
                    recording=recording,
                    transcript="",
                    route=None,
                    response="",
                    error=(
                        "Speech recognition returned "
                        "an empty command."
                    ),
                )

            route = (
                self.task_router.route(
                    transcript
                )
            )

            plan = self._build_plan(
                transcript=transcript,
                route=route,
            )

            results = (
                self.orchestrator.execute_plan(
                    plan
                )
            )

            final_step = (
                self._get_final_step(
                    results
                )
            )

            if final_step is None:
                return VoicePipelineResult(
                    success=False,
                    wake_event=wake_event,
                    recording=recording,
                    transcript=transcript,
                    route=route,
                    response="",
                    error=(
                        "Qronos did not produce "
                        "an execution result."
                    ),
                )

            if not final_step.success:
                return VoicePipelineResult(
                    success=False,
                    wake_event=wake_event,
                    recording=recording,
                    transcript=transcript,
                    route=route,
                    response="",
                    error=(
                        final_step.error
                        or "Qronos task failed."
                    ),
                )

            return VoicePipelineResult(
                success=True,
                wake_event=wake_event,
                recording=recording,
                transcript=transcript,
                route=route,
                response=final_step.output,
                error=None,
            )

        except TimeoutError as exc:
            return VoicePipelineResult(
                success=False,
                wake_event=wake_event,
                recording=recording,
                transcript=transcript,
                route=route,
                response="",
                error=str(exc),
            )

        except Exception as exc:
            return VoicePipelineResult(
                success=False,
                wake_event=wake_event,
                recording=recording,
                transcript=transcript,
                route=route,
                response="",
                error=str(exc),
            )

        finally:
            self._rearm_microphone()

    def stop(self) -> None:
        """
        Stop listening without permanently destroying native runtimes.
        """

        if self._closed:
            return

        self.audio_input.stop()
        self.voice_trigger.stop()

        self._prepared = False

    def close(self) -> None:
        """
        Permanently release the voice pipeline and native VAD resources.
        """

        if self._closed:
            return

        self.audio_input.stop()
        self.voice_trigger.stop()
        self.vad_runtime.close()

        self._prepared = False
        self._closed = True