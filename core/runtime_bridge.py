from __future__ import annotations

import json
import os
import re
import sys
import threading
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

from core.audio_input import AudioInput
from core.command_recorder import CommandRecorder
from core.conversation_session import ConversationSession
from core.hard_floor import required_vram_mb
from core.load_signal import LoadSample, SustainedLoadMonitor
from core.model_registry import MODELS
from core.orchestrator import Orchestrator
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
from core.task_router import TaskRouter
from core.whisper_cpp_runtime import WhisperCppRuntime
from core.whisper_cpp_vad_runtime import WhisperCppVADRuntime
from core.web_worker import WebResearchWorker


PUSH_TO_TALK_ACTION = "qronos.push_to_talk"
AUDIO_SPECTRUM_BANDS = 32
EMIT_LOCK = threading.Lock()



def normalise_qronos_invocation(transcript: str) -> str:
    """Correct measured Whisper variants only when Qronos is addressed."""
    return re.sub(
        r"^(?P<greeting>\s*\u0633\u0644\u0627\u0645\s+)?(?:\u06a9\u0631\u0648\u0646\u0633|\u062e\u0631\u0648\u0646\u0633|\u06a9\u0631\u0648\u0646\u0632)(?=\s|[\u060c,\u061f?]|$)",
        lambda match: f"{match.group('greeting') or ''}\u06a9\u0631\u0648\u0646\u0648\u0633",
        transcript,
        count=1,
    )


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
        self.task_router: TaskRouter | None = None
        self.orchestrator: Orchestrator | None = None
        self.conversation_session: ConversationSession | None = None

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

            monitor = SustainedLoadMonitor()
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
        self.orchestrator = Orchestrator()
        self.orchestrator.workers.register(
            WebResearchWorker(
                answer_fn=self.orchestrator.answer_web_prompt,
            )
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
                "task_router",
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

    if action_id != PUSH_TO_TALK_ACTION:
        emit(
            "runtime_warning",
            "warning",
            f"Unsupported runtime action: {action_id}",
        )
        return

    worker = threading.Thread(
        target=runtime.push_to_talk,
        name="qronos-push-to-talk",
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
