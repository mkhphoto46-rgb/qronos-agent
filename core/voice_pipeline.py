from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.audio_input import AudioInput
from core.brain_runtime import (
    BrainMessage,
    BrainMessageRole,
)
from core.command_recorder import (
    CommandRecorder,
    CommandRecordingResult,
)
from core.config import CONFIG
from core.conversation_session import (
    ConversationRole,
    ConversationSession,
)
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
    Final result of one Qronos voice turn.
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
    Connect Qronos wake word, conversation session, command recording,
    speech-to-text, routing, and Brain execution.

    Conversation behavior:

        No active session
            ->
        Wait for Wake Word
            ->
        Start ConversationSession
            ->
        Record command
            ->
        Process
            ->
        Respond
            ->
        Follow-up listening

    While the ConversationSession remains active, later turns do not
    require another wake word.

    Conversation history is converted to runtime-neutral BrainMessage
    objects so Fast Brain and Heavy Brain receive the same context.
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
        conversation_session: ConversationSession,
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
        self.conversation_session = conversation_session

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
        Prepare latency-sensitive components before listening.
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

    def _ensure_microphone_running(
        self,
    ) -> None:
        if not self.audio_input.is_running():
            self.audio_input.start()

    def _prepare_for_turn(
        self,
    ) -> VoiceTriggerEvent | None:
        """
        Prepare the microphone for the next user turn.

        If the conversation is inactive, wait for the wake word and start
        a new session.

        If the conversation is already active, skip wake-word detection and
        immediately listen for the follow-up command.
        """

        self._ensure_microphone_running()

        if self.conversation_session.requires_wake_word():
            wake_event = (
                self._wait_for_wake_word()
            )

            self.voice_trigger.pause()

            self.conversation_session.start()

            self._notify_wake_detected(
                wake_event
            )

            return wake_event

        self.voice_trigger.pause()

        self.conversation_session.begin_listening()

        return None

    def _build_conversation_context(
        self,
    ) -> list[BrainMessage]:
        """
        Convert stored Qronos conversation history into Brain messages.

        The newest user message is intentionally excluded because the
        current TaskPlan step already contains that same transcript.

        Without this exclusion, the current user turn would be sent to
        the model twice.
        """

        stored_messages = list(
            self.conversation_session.messages
        )

        if not stored_messages:
            return []

        if (
            stored_messages[-1].role
            is ConversationRole.USER
        ):
            stored_messages = (
                stored_messages[:-1]
            )

        brain_messages: list[
            BrainMessage
        ] = []

        for message in stored_messages:
            if (
                message.role
                is ConversationRole.USER
            ):
                role = (
                    BrainMessageRole.USER
                )

            elif (
                message.role
                is ConversationRole.ASSISTANT
            ):
                role = (
                    BrainMessageRole.ASSISTANT
                )

            else:
                continue

            brain_messages.append(
                BrainMessage(
                    role=role,
                    content=message.content,
                )
            )

        return brain_messages

    def listen_once(
        self,
    ) -> VoicePipelineResult:
        """
        Capture and execute one conversation turn.

        The first turn of a conversation requires the Qronos wake word.
        Follow-up turns inside the active session do not.

        Previous user and assistant turns are supplied to the Brain so
        references such as "that", "he", "the previous number", and similar
        conversational dependencies can be resolved.
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
                self._prepare_for_turn()
            )

            recording = (
                self.command_recorder.record_to_file(
                    self.command_audio_path
                )
            )

            self.audio_input.stop()

            self.conversation_session.begin_processing()

            transcript = (
                self.speech_runtime.transcribe_file(
                    recording.audio_path,
                    language="auto",
                )
            ).strip()

            if not transcript:
                self.conversation_session.begin_listening()

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

            self.conversation_session.add_user_message(
                transcript
            )

            conversation_context = (
                self._build_conversation_context()
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
                    plan,
                    conversation_messages=(
                        conversation_context
                    ),
                )
            )

            final_step = (
                self._get_final_step(
                    results
                )
            )

            if final_step is None:
                self.conversation_session.begin_listening()

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
                self.conversation_session.begin_listening()

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

            self.conversation_session.begin_responding()

            response = final_step.output

            self.conversation_session.add_assistant_message(
                response
            )

            self.conversation_session.begin_listening()

            return VoicePipelineResult(
                success=True,
                wake_event=wake_event,
                recording=recording,
                transcript=transcript,
                route=route,
                response=response,
                error=None,
            )

        except TimeoutError as exc:
            if self.conversation_session.is_active:
                self.conversation_session.begin_listening()

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
            if self.conversation_session.is_active:
                self.conversation_session.begin_listening()

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
            if (
                not self._closed
                and self._prepared
            ):
                self._ensure_microphone_running()

                if self.conversation_session.requires_wake_word():
                    self.voice_trigger.resume()

    def stop(self) -> None:
        """
        Stop listening without permanently closing native runtimes.
        """

        if self._closed:
            return

        self.audio_input.stop()
        self.voice_trigger.stop()

        self._prepared = False

    def close(self) -> None:
        """
        Permanently release the pipeline and native VAD runtime.
        """

        if self._closed:
            return

        self.audio_input.stop()
        self.voice_trigger.stop()
        self.vad_runtime.close()

        self.conversation_session.close()

        self._prepared = False
        self._closed = True