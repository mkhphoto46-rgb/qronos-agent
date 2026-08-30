"""
The protocol between the desktop application and the Python voice runtime.

The bridge is a line-oriented conversation over stdin and stdout: one JSON
object per line in, one JSON event per line out. Everything here exercises that
conversation with fakes standing in for the microphone, the recorder, the
speech runtime and the orchestrator, so the whole path from a command line to
an event sequence runs with no audio hardware, no whisper binaries and no
Ollama.

Two properties get most of the attention, because they are the ones a user
would feel:

    The event sequence. The desktop drives an animation and a transcript from
    these events, so their order and their names are an interface, not a log.

    The failure paths. A voice runtime that dies quietly leaves a person
    talking to a machine that stopped listening, so every failure has to arrive
    as a ``runtime_error`` rather than a traceback on a stream nobody reads.
"""

from __future__ import annotations

import io
import json
import threading
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from core import runtime_bridge
from core.action_audit import ActionAuditLog
from core.screen_capture import (
    CaptureUnavailable,
    DisplayGeometry,
    ScreenCapture,
)
from core.command_recorder import CommandRecordingResult
from core.conversation_session import ConversationSession
from core.orchestrator import StepResult
from core.runtime_bridge import (
    AUDIO_SPECTRUM_BANDS,
    LOOK_AT_SCREEN_ACTION,
    PUSH_TO_TALK_ACTION,
    QronosRuntime,
    emit,
    handle_action,
    normalise_qronos_invocation,
    parse_payload,
)
from core.task_router import TaskRouter, TaskType


class FakeClock:
    def __init__(self, initial_time: float = 100.0) -> None:
        self.current_time = initial_time

    def __call__(self) -> float:
        return self.current_time


class FakeAudioInput:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


class FakeCommandRecorder:
    """
    Stands in for the recorder, and plays back a fixed spectrum.

    The real recorder calls ``on_audio_spectrum`` once per frame while the
    person is speaking. Two frames is enough to prove the callback is wired to
    the event stream and that the values are clamped and rounded on the way
    out.
    """

    def __init__(
        self,
        frames: tuple[tuple[float, tuple[float, ...]], ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.frames = frames
        self.error = error
        self.calls = 0

    def record_to_file(
        self,
        output_path: str | Path,
        on_audio_spectrum: Any = None,
    ) -> CommandRecordingResult:
        self.calls += 1

        if self.error is not None:
            raise self.error

        if on_audio_spectrum is not None:
            for level, bands in self.frames:
                on_audio_spectrum(level, bands)

        return CommandRecordingResult(
            audio_path=Path(output_path),
            duration_seconds=1.5,
            speech_seconds=1.0,
            stopped_by_silence=True,
            peak_speech_probability=0.9,
        )


class FakeSpeechRuntime:
    def __init__(
        self,
        transcript: str = "سلام",
        error: Exception | None = None,
        temp_dir: Path | None = None,
    ) -> None:
        self.transcript = transcript
        self.error = error
        self.temp_dir = temp_dir or Path(".")
        self.languages: list[str | None] = []

    def health_check(self) -> bool:
        return True

    def transcribe_file(
        self,
        audio_path: str | Path,
        language: str | None = None,
    ) -> str:
        self.languages.append(language)

        if self.error is not None:
            raise self.error

        return self.transcript


class FakeVoiceOutput:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.releases = 0

    def health_check(self) -> bool:
        return True

    def speak_to_file(self, text: str):
        self.calls.append(text)

        return SimpleNamespace(
            audio_path=Path("fake-qronos-voice.wav"),
            audio_seconds=1.0,
            took_seconds=0.5,
            real_time_factor=0.5,
        )

    def release(self) -> None:
        self.releases += 1


class FakeOrchestrator:
    def __init__(
        self,
        results: list[StepResult] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.results = results
        self.error = error
        self.plans: list[Any] = []

    def execute_plan(self, plan: Any) -> list[StepResult]:
        self.plans.append(plan)

        if self.error is not None:
            raise self.error

        if self.results is None:
            return [
                StepResult(
                    order=1,
                    success=True,
                    output="سلام، چطور می‌توانم کمک کنم؟",
                )
            ]

        return self.results


def screenless_capture(width: int = 8, height: int = 6, watch=None) -> ScreenCapture:
    """A capture that needs no screen, and optionally records what it was asked."""

    def grab(window):
        if watch is not None:
            watch.append(window)

        # Not a flat rectangle: a blank capture is answered without building a
        # plan at all, which is correct and is not what most of these tests
        # are about.
        return (
            bytes(
                byte
                for index in range(width * height)
                for byte in (index % 251, (index * 7) % 241, (index * 13) % 239, 0)
            ),
            width,
            height,
        )

    return ScreenCapture(
        grab=grab,
        geometry_fn=lambda: DisplayGeometry(width, height, width, height),
    )


def prepared_runtime(
    recorder: FakeCommandRecorder | None = None,
    speech: FakeSpeechRuntime | None = None,
    orchestrator: FakeOrchestrator | None = None,
    temp_dir: Path | None = None,
) -> QronosRuntime:
    """
    A runtime with every collaborator already in place.

    ``prepare`` returns immediately when ``audio_input`` is set, so filling the
    attributes in is enough to make the real code path run without touching
    hardware. Nothing is patched.
    """
    runtime = QronosRuntime()

    runtime.audio_input = FakeAudioInput()
    runtime.vad_runtime = object()
    runtime.command_recorder = recorder or FakeCommandRecorder()
    runtime.speech_runtime = speech or FakeSpeechRuntime(
        temp_dir=temp_dir
    )
    runtime.voice_output = FakeVoiceOutput()
    runtime.task_router = TaskRouter()
    runtime.action_audit = ActionAuditLog(path=None)
    runtime.screen_capture = screenless_capture()
    runtime.orchestrator = orchestrator or FakeOrchestrator()
    runtime.conversation_session = ConversationSession(clock=FakeClock())

    return runtime


def captured_events(action: Any) -> list[dict[str, Any]]:
    """Run ``action`` and return the events it wrote to stdout."""
    stream = io.StringIO()

    with redirect_stdout(stream):
        action()

    return [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if line.strip()
    ]


def event_types(events: list[dict[str, Any]]) -> list[str]:
    return [event["eventType"] for event in events]


class TestEmit(unittest.TestCase):
    def test_one_event_is_one_line_of_json(self) -> None:
        stream = io.StringIO()

        with redirect_stdout(stream):
            emit("runtime_ready", "ready", "up")
            emit("runtime_pong", "ready", "pong")

        lines = stream.getvalue().splitlines()

        self.assertEqual(len(lines), 2)
        self.assertEqual(
            json.loads(lines[0]),
            {
                "eventType": "runtime_ready",
                "status": "ready",
                "message": "up",
            },
        )

    def test_persian_survives_the_stream(self) -> None:
        # The payload is written with ensure_ascii, so Persian leaves as escape
        # sequences. It has to arrive as Persian on the other side, or every
        # transcript reaches the interface as gibberish.
        events = captured_events(
            lambda: emit("voice_transcript", "processing", "سلام دنیا")
        )

        self.assertEqual(events[0]["message"], "سلام دنیا")

    def test_a_newline_in_a_message_cannot_split_the_line(self) -> None:
        # One event is one line. A transcript containing a newline would
        # otherwise arrive as two events, the second of them unparseable.
        stream = io.StringIO()

        with redirect_stdout(stream):
            emit("voice_transcript", "processing", "first\nsecond")

        self.assertEqual(len(stream.getvalue().splitlines()), 1)


class TestParsePayload(unittest.TestCase):
    def test_a_command_is_read_and_trimmed(self) -> None:
        self.assertEqual(
            parse_payload('{"command": "  ping  "}')["command"],
            "ping",
        )

    def test_malformed_json_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_payload("{not json")

    def test_a_bare_value_is_not_a_command(self) -> None:
        for line in ('"ping"', "42", "[1, 2]", "null"):
            with self.subTest(line=line):
                with self.assertRaises(ValueError):
                    parse_payload(line)

    def test_an_empty_command_is_rejected(self) -> None:
        for line in ("{}", '{"command": ""}', '{"command": "   "}'):
            with self.subTest(line=line):
                with self.assertRaises(ValueError):
                    parse_payload(line)


class TestHandleAction(unittest.TestCase):
    def test_an_empty_action_id_is_rejected(self) -> None:
        runtime = prepared_runtime()

        with self.assertRaises(ValueError):
            handle_action(runtime, {"command": "action", "actionId": " "})

    def test_an_unsupported_action_warns_rather_than_running(self) -> None:
        # The desktop forwards every global hotkey it does not handle itself,
        # so the bridge sees actions it has no implementation for. A warning
        # keeps the runtime alive; an exception would kill the turn.
        runtime = prepared_runtime()

        events = captured_events(
            lambda: handle_action(
                runtime,
                {"command": "action", "actionId": "qronos.toggle_voice"},
            )
        )

        self.assertEqual(
            event_types(events),
            ["runtime_action_received", "runtime_warning"],
        )
        self.assertEqual(runtime.command_recorder.calls, 0)

    def test_push_to_talk_is_acknowledged_and_dispatched(self) -> None:
        runtime = prepared_runtime()
        stream = io.StringIO()

        with redirect_stdout(stream):
            handle_action(
                runtime,
                {"command": "action", "actionId": PUSH_TO_TALK_ACTION},
            )
            # The turn runs on its own thread so the reader loop stays
            # responsive. Join it rather than sleeping, or the assertions race
            # the worker.
            for thread in _push_to_talk_threads():
                thread.join(timeout=5)

        events = [
            json.loads(line)
            for line in stream.getvalue().splitlines()
            if line.strip()
        ]

        self.assertEqual(
            event_types(events)[0],
            "runtime_action_received",
        )
        self.assertIn("voice_turn_complete", event_types(events))
        self.assertEqual(runtime.command_recorder.calls, 1)


def _push_to_talk_threads() -> list[Any]:
    import threading

    return [
        thread
        for thread in threading.enumerate()
        if thread.name == "qronos-push-to-talk"
    ]


class TestPushToTalkSequence(unittest.TestCase):
    def test_a_successful_turn_emits_the_documented_sequence(self) -> None:
        runtime = prepared_runtime()

        events = captured_events(runtime.push_to_talk)
        types = event_types(events)

        # The spectrum frames are interleaved and counted separately; what
        # matters here is that the milestones arrive in this order.
        milestones = [
            name for name in types if not name.startswith("voice_audio")
        ]

        self.assertEqual(
            milestones,
            [
                "voice_listening",
                "voice_transcribing",
                "voice_transcript",
                "voice_routed",
                "voice_response",
                "voice_synthesizing",
                "voice_turn_complete",
            ],
        )

    def test_the_transcript_reaches_the_interface_unchanged(self) -> None:
        runtime = prepared_runtime(
            speech=FakeSpeechRuntime(transcript="ساعت چند است؟")
        )

        events = captured_events(runtime.push_to_talk)
        transcript = next(
            event
            for event in events
            if event["eventType"] == "voice_transcript"
        )

        self.assertEqual(transcript["message"], "ساعت چند است؟")

    def test_speech_recognition_is_asked_for_persian(self) -> None:
        # Qronos is a Persian-first assistant and does not guess the language.
        speech = FakeSpeechRuntime()
        runtime = prepared_runtime(speech=speech)

        captured_events(runtime.push_to_talk)

        self.assertEqual(speech.languages, ["fa"])

    def test_the_microphone_is_stopped_when_the_turn_ends(self) -> None:
        runtime = prepared_runtime()

        captured_events(runtime.push_to_talk)

        self.assertEqual(runtime.audio_input.starts, 1)
        self.assertGreaterEqual(runtime.audio_input.stops, 1)

    def test_the_turn_is_recorded_in_the_conversation(self) -> None:
        runtime = prepared_runtime()

        captured_events(runtime.push_to_talk)

        self.assertEqual(runtime.conversation_session.message_count, 2)


class TestAudioSpectrum(unittest.TestCase):
    def test_frames_from_the_recorder_reach_the_event_stream(self) -> None:
        recorder = FakeCommandRecorder(
            frames=(
                (0.5, tuple(0.25 for _ in range(AUDIO_SPECTRUM_BANDS))),
                (0.9, tuple(0.75 for _ in range(AUDIO_SPECTRUM_BANDS))),
            )
        )
        runtime = prepared_runtime(recorder=recorder)

        events = captured_events(runtime.push_to_talk)
        spectra = [
            json.loads(event["message"])
            for event in events
            if event["eventType"] == "voice_audio_spectrum"
        ]

        # Two frames, plus the clearing frame the runtime sends when recording
        # stops, plus the one in the finally block.
        self.assertGreaterEqual(len(spectra), 3)
        self.assertEqual(spectra[0]["level"], 0.5)
        self.assertEqual(spectra[1]["level"], 0.9)

    def test_values_outside_the_range_are_clamped(self) -> None:
        # The orb reads these directly. A level above one would push the
        # animation outside its bounds; a negative one would invert it.
        recorder = FakeCommandRecorder(
            frames=((5.0, (-1.0, 0.5, 99.0)),)
        )
        runtime = prepared_runtime(recorder=recorder)

        events = captured_events(runtime.push_to_talk)
        first = json.loads(
            next(
                event
                for event in events
                if event["eventType"] == "voice_audio_spectrum"
            )["message"]
        )

        self.assertEqual(first["level"], 1.0)
        self.assertEqual(first["bands"], [0.0, 0.5, 1.0])

    def test_more_bands_than_the_interface_expects_are_dropped(self) -> None:
        recorder = FakeCommandRecorder(
            frames=((0.5, tuple(0.5 for _ in range(200))),)
        )
        runtime = prepared_runtime(recorder=recorder)

        events = captured_events(runtime.push_to_talk)
        first = json.loads(
            next(
                event
                for event in events
                if event["eventType"] == "voice_audio_spectrum"
            )["message"]
        )

        self.assertEqual(len(first["bands"]), AUDIO_SPECTRUM_BANDS)

    def test_the_spectrum_is_cleared_when_the_turn_ends(self) -> None:
        # Left uncleared, the orb keeps pulsing at whatever level the last
        # frame carried, so the interface looks like it is still listening.
        runtime = prepared_runtime(
            recorder=FakeCommandRecorder(frames=((0.8, (0.8,)),))
        )

        events = captured_events(runtime.push_to_talk)
        spectra = [
            json.loads(event["message"])
            for event in events
            if event["eventType"] == "voice_audio_spectrum"
        ]

        self.assertEqual(spectra[-1]["level"], 0.0)


class TestPushToTalkFailures(unittest.TestCase):
    def test_a_second_request_mid_turn_is_refused_not_queued(self) -> None:
        # Two overlapping turns would fight over one microphone. The refusal
        # has to be visible, or the person presses the key again and gets
        # nothing either time.
        runtime = prepared_runtime()
        runtime._set_busy(True)

        events = captured_events(runtime.push_to_talk)

        self.assertEqual(event_types(events), ["runtime_busy"])
        self.assertEqual(runtime.command_recorder.calls, 0)

    def test_an_empty_transcript_is_an_error_not_an_empty_turn(self) -> None:
        # Silence, or speech the recogniser could not read. Sending an empty
        # string to the brain would produce a confident answer to nothing.
        runtime = prepared_runtime(
            speech=FakeSpeechRuntime(transcript="   ")
        )

        events = captured_events(runtime.push_to_talk)

        self.assertIn("runtime_error", event_types(events))
        self.assertNotIn("voice_response", event_types(events))

    def test_a_failed_step_surfaces_the_reason(self) -> None:
        runtime = prepared_runtime(
            orchestrator=FakeOrchestrator(
                results=[
                    StepResult(
                        order=1,
                        success=False,
                        output="",
                        error="Task type 'vision' is not implemented yet.",
                    )
                ]
            )
        )

        events = captured_events(runtime.push_to_talk)
        error = next(
            event
            for event in events
            if event["eventType"] == "runtime_error"
        )

        self.assertIn("vision", error["message"])

    def test_an_empty_plan_result_is_an_error(self) -> None:
        runtime = prepared_runtime(
            orchestrator=FakeOrchestrator(results=[])
        )

        self.assertIn(
            "runtime_error",
            event_types(captured_events(runtime.push_to_talk)),
        )

    def test_a_raising_collaborator_becomes_an_event(self) -> None:
        # Nothing may escape push_to_talk. It runs on its own thread, so an
        # exception that got out would be printed to a stream the desktop does
        # not read, and the interface would sit on "listening" forever.
        runtime = prepared_runtime(
            recorder=FakeCommandRecorder(
                error=OSError("microphone disappeared")
            )
        )

        events = captured_events(runtime.push_to_talk)

        self.assertIn("runtime_error", event_types(events))
        self.assertIn("microphone disappeared", events[-2]["message"])

    def test_the_runtime_is_free_again_after_a_failure(self) -> None:
        # A turn that failed must not leave the runtime marked busy, or every
        # later press is refused and the only cure is a restart.
        runtime = prepared_runtime(
            recorder=FakeCommandRecorder(error=OSError("boom"))
        )

        captured_events(runtime.push_to_talk)

        self.assertFalse(runtime.is_busy)

    def test_the_microphone_is_stopped_after_a_failure(self) -> None:
        runtime = prepared_runtime(
            recorder=FakeCommandRecorder(error=OSError("boom"))
        )

        captured_events(runtime.push_to_talk)

        self.assertGreaterEqual(runtime.audio_input.stops, 1)


class TestPreparednessIsChecked(unittest.TestCase):
    """
    The guard that replaced six ``assert`` statements.

    ``assert`` is removed entirely by ``python -O``. Under that flag the
    guards vanished and the next line dereferenced None, so a packaging step
    that enabled optimisation would have turned a clear failure into an
    AttributeError from inside the voice path.
    """

    def test_a_missing_collaborator_is_reported_by_name(self) -> None:
        runtime = prepared_runtime()
        runtime.orchestrator = None

        with self.assertRaises(RuntimeError) as caught:
            runtime._require_prepared()

        self.assertIn("orchestrator", str(caught.exception))

    def test_every_collaborator_is_checked(self) -> None:
        for name in (
            "audio_input",
            "vad_runtime",
            "command_recorder",
            "speech_runtime",
            "task_router",
            "orchestrator",
            "conversation_session",
        ):
            with self.subTest(missing=name):
                runtime = prepared_runtime()
                setattr(runtime, name, None)

                with self.assertRaises(RuntimeError):
                    runtime._require_prepared()

    def test_a_prepared_runtime_passes(self) -> None:
        prepared_runtime()._require_prepared()


class TestRoutingIsReported(unittest.TestCase):
    def test_the_chosen_task_type_is_announced(self) -> None:
        runtime = prepared_runtime(
            speech=FakeSpeechRuntime(transcript="hello there")
        )

        events = captured_events(runtime.push_to_talk)
        routed = next(
            event
            for event in events
            if event["eventType"] == "voice_routed"
        )

        self.assertEqual(routed["message"], TaskType.FAST.value)

    def test_a_persian_transcript_reaches_the_right_task_type(self) -> None:
        # Where the two halves meet. Speech recognition is pinned to Persian,
        # so this is the only kind of transcript the router sees in practice.
        # A router that matched English only sent every one of these to FAST,
        # and the bug was invisible because FAST is the only implemented
        # branch — the wrong route and the only route were the same.
        for transcript, expected in (
            ("پریمیر را باز کن", TaskType.COMPUTER),
            ("به این عکس نگاه کن", TaskType.VISION),
            ("برو به گوگل", TaskType.BROWSER),
            ("این فصل را عمیق تحلیل کن", TaskType.HEAVY),
            ("سلام کرونوس", TaskType.FAST),
        ):
            with self.subTest(transcript=transcript):
                runtime = prepared_runtime(
                    speech=FakeSpeechRuntime(transcript=transcript)
                )

                events = captured_events(runtime.push_to_talk)
                routed = next(
                    event
                    for event in events
                    if event["eventType"] == "voice_routed"
                )

                self.assertEqual(routed["message"], expected.value)

    def test_the_plan_carries_the_transcript_as_its_goal(self) -> None:
        transcript = "\u0633\u0644\u0627\u0645 \u062d\u0627\u0644\u062a \u0686\u0637\u0648\u0631\u0647"
        orchestrator = FakeOrchestrator()
        runtime = prepared_runtime(
            speech=FakeSpeechRuntime(transcript=transcript),
            orchestrator=orchestrator,
        )

        captured_events(runtime.push_to_talk)

        self.assertEqual(orchestrator.plans[0].goal, transcript)

    def test_measured_qronos_name_variants_are_corrected_at_invocation(self) -> None:
        for transcript in (
            "کرونس صدای من رو میشنوی؟",
            "خرونس، وضعیت پروژه چیه؟",
            "سلام کرونز حالت چطوره؟",
        ):
            with self.subTest(transcript=transcript):
                corrected = normalise_qronos_invocation(transcript)
                self.assertIn("کرونوس", corrected)

    def test_qronos_like_words_inside_normal_text_are_not_rewritten(self) -> None:
        transcript = "درباره اسطوره کرونس توضیح بده"

        self.assertEqual(normalise_qronos_invocation(transcript), transcript)

    def test_prepare_registers_the_production_web_worker(self) -> None:
        runtime = QronosRuntime()

        with patch("core.runtime_bridge.AudioInput"), patch(
            "core.runtime_bridge.CommandRecorder"
        ), patch(
            "core.runtime_bridge.WhisperCppVADRuntime"
        ) as vad_type, patch(
            "core.runtime_bridge.WhisperCppRuntime"
        ) as speech_type, patch(
            "core.runtime_bridge.ChatterboxRuntime"
        ) as voice_type:
            vad_type.return_value.health_check.return_value = True
            speech_type.return_value.health_check.return_value = True
            voice_type.return_value.health_check.return_value = True
            runtime.prepare()

        assert runtime.orchestrator is not None
        self.assertIn(TaskType.BROWSER, runtime.orchestrator.workers.registered())



class TestTheQueueDoesNotLeakOntoStderr(unittest.TestCase):
    """
    Two guarantees the subprocess tests cannot actually pin down.

    Closing stdin does end the bridge and does leave stderr clean, and there
    are subprocess tests asserting both. But they pass whether or not the
    protections below exist, because the pump is a daemon thread and the
    interpreter kills it on the way out before it can lose the race. Removing
    both protections and re-running them proved exactly that: still green.

    So the protections are tested directly here instead. What is being
    defended is a late write from the pump thread turning into a traceback on
    stderr, which the desktop reads as a crash and shows to the user as one.
    """

    def test_emitting_to_a_closed_stdout_does_not_raise(self) -> None:
        original = sys.stdout

        class ClosedStream:
            closed = True

            def write(self, _: str) -> int:  # pragma: no cover - must not run
                raise ValueError("I/O operation on closed file")

            def flush(self) -> None:  # pragma: no cover - must not run
                raise ValueError("I/O operation on closed file")

        sys.stdout = ClosedStream()  # type: ignore[assignment]

        try:
            runtime_bridge.emit("queue_changed", "ready", "{}")
        finally:
            sys.stdout = original

    def test_closing_the_runtime_stops_the_pump(self) -> None:
        # The ordering that matters: the scheduler is stopped first, and
        # before anything else in close() that might raise and skip it.
        with redirect_stdout(io.StringIO()):
            runtime = runtime_bridge.QronosRuntime()
            scheduler = runtime.ensure_scheduler()

            self.assertTrue(scheduler.running)

            runtime.close()

        self.assertFalse(scheduler.running)

    def test_closing_twice_is_harmless(self) -> None:
        with redirect_stdout(io.StringIO()):
            runtime = runtime_bridge.QronosRuntime()
            runtime.ensure_scheduler()

            runtime.close()
            runtime.close()

    def test_a_runtime_that_never_queued_anything_closes_cleanly(self) -> None:
        runtime_bridge.QronosRuntime().close()


class TestTheSchedulerIsBuiltOnceAndOnlyWhenAsked(unittest.TestCase):
    def test_it_does_not_exist_until_something_needs_it(self) -> None:
        # A sampler thread launching nvidia-smi every two seconds is a side
        # effect, and a bridge nobody has spoken to must have none.
        self.assertIsNone(runtime_bridge.QronosRuntime().scheduler)

    def test_asking_twice_gives_the_same_one(self) -> None:
        with redirect_stdout(io.StringIO()):
            runtime = runtime_bridge.QronosRuntime()

            try:
                self.assertIs(
                    runtime.ensure_scheduler(),
                    runtime.ensure_scheduler(),
                )
            finally:
                runtime.close()

    def test_one_supplied_from_outside_is_used_as_is(self) -> None:
        # The injection point that lets a test drive the bridge without a
        # real sampler or a real thread.
        sentinel = object()
        runtime = runtime_bridge.QronosRuntime(scheduler=sentinel)

        self.assertIs(runtime.ensure_scheduler(), sentinel)


if __name__ == "__main__":
    unittest.main()


# --- Vision integration regression coverage ---

class TestLookingAtTheScreen(unittest.TestCase):
    """
    The two-step vision turn.

    Looking at the screen needs UI confirmation, and the runtime cannot give
    itself one: a spoken yes is consent from somebody who has not been shown
    what is about to be looked at. So a spoken request to look stops, the
    desktop asks, and a second action comes back with the answer.

    Most of these are about the stopping half, because that is the half that
    protects somebody.
    """

    def test_a_spoken_request_to_look_stops_and_asks(self) -> None:
        orchestrator = FakeOrchestrator()
        runtime = prepared_runtime(
            speech=FakeSpeechRuntime(transcript="به این پنجره نگاه کن"),
            orchestrator=orchestrator,
        )

        events = captured_events(runtime.push_to_talk)
        kinds = [event["eventType"] for event in events]

        self.assertIn("voice_needs_screen", kinds)
        self.assertEqual(orchestrator.plans, [])

    def test_the_question_is_remembered_while_the_person_decides(self) -> None:
        runtime = prepared_runtime(
            speech=FakeSpeechRuntime(transcript="به این پنجره نگاه کن")
        )

        captured_events(runtime.push_to_talk)

        self.assertEqual(runtime.pending_look, "به این پنجره نگاه کن")

    def test_saying_no_captures_nothing_and_is_not_an_error(self) -> None:
        grabbed: list = []
        runtime = prepared_runtime()
        runtime.screen_capture = screenless_capture(watch=grabbed)

        events = captured_events(
            lambda: runtime.look_at_screen(
                approved=False, question="What is this?"
            )
        )
        kinds = [event["eventType"] for event in events]

        self.assertIn("voice_screen_declined", kinds)
        self.assertNotIn("runtime_error", kinds)
        self.assertEqual(grabbed, [])

    def test_forgetting_the_answer_is_the_same_as_saying_no(self) -> None:
        runtime = prepared_runtime()

        events = captured_events(
            lambda: runtime.look_at_screen(question="What is this?")
        )

        self.assertIn(
            "voice_screen_declined",
            [event["eventType"] for event in events],
        )

    def test_saying_yes_captures_and_answers(self) -> None:
        runtime = prepared_runtime()

        events = captured_events(
            lambda: runtime.look_at_screen(
                approved=True, question="What is on my screen?"
            )
        )
        kinds = [event["eventType"] for event in events]

        self.assertIn("voice_capturing_screen", kinds)
        self.assertIn("voice_captured_screen", kinds)
        self.assertIn("voice_turn_complete", kinds)

    def test_the_picture_reaches_the_plan(self) -> None:
        orchestrator = FakeOrchestrator()
        runtime = prepared_runtime(orchestrator=orchestrator)

        captured_events(
            lambda: runtime.look_at_screen(
                approved=True, question="What is on my screen?"
            )
        )

        step = orchestrator.plans[0].steps[0]

        self.assertIs(step.task_type, TaskType.VISION)
        self.assertEqual(len(step.images), 1)
        self.assertEqual((step.images[0].width, step.images[0].height), (8, 6))

    def test_a_blank_screen_is_answered_without_a_model(self) -> None:
        """
        Faster and more honest. Shown a flat rectangle, the model spends five
        seconds and ten gigabytes describing a flat rectangle.
        """
        orchestrator = FakeOrchestrator()
        runtime = prepared_runtime(orchestrator=orchestrator)
        runtime.screen_capture = ScreenCapture(
            grab=lambda window: (bytes(4 * 8 * 6), 8, 6),
            geometry_fn=lambda: DisplayGeometry(8, 6, 8, 6),
        )

        events = captured_events(
            lambda: runtime.look_at_screen(approved=True, question="What?")
        )
        kinds = [event["eventType"] for event in events]

        self.assertIn("voice_screen_blank", kinds)
        self.assertEqual(orchestrator.plans, [])

    def test_the_picture_is_never_written_to_disk(self) -> None:
        orchestrator = FakeOrchestrator()
        runtime = prepared_runtime(orchestrator=orchestrator)

        captured_events(
            lambda: runtime.look_at_screen(approved=True, question="What?")
        )

        self.assertIsNone(orchestrator.plans[0].steps[0].images[0].source)

    def test_the_remembered_question_is_used_when_none_is_given(self) -> None:
        orchestrator = FakeOrchestrator()
        runtime = prepared_runtime(
            speech=FakeSpeechRuntime(transcript="به این پنجره نگاه کن"),
            orchestrator=orchestrator,
        )

        captured_events(runtime.push_to_talk)
        captured_events(lambda: runtime.look_at_screen(approved=True))

        self.assertEqual(orchestrator.plans[0].goal, "به این پنجره نگاه کن")

    def test_a_used_question_is_forgotten(self) -> None:
        """
        So a question asked five minutes ago cannot be answered against
        whatever happens to be on the screen now.
        """
        runtime = prepared_runtime(
            speech=FakeSpeechRuntime(transcript="به این پنجره نگاه کن")
        )

        captured_events(runtime.push_to_talk)
        captured_events(lambda: runtime.look_at_screen(approved=True))

        self.assertIsNone(runtime.pending_look)

    def test_a_declined_question_is_forgotten_too(self) -> None:
        runtime = prepared_runtime(
            speech=FakeSpeechRuntime(transcript="به این پنجره نگاه کن")
        )

        captured_events(runtime.push_to_talk)
        captured_events(lambda: runtime.look_at_screen(approved=False))

        self.assertIsNone(runtime.pending_look)

    def test_looking_with_nothing_asked_is_an_error(self) -> None:
        runtime = prepared_runtime()

        events = captured_events(lambda: runtime.look_at_screen(approved=True))

        self.assertIn(
            "runtime_error",
            [event["eventType"] for event in events],
        )

    def test_a_machine_that_cannot_capture_says_so_distinctly(self) -> None:
        """
        Not a ``runtime_error``: "this machine cannot" is something a person
        can act on, and describing it as a fault is not.
        """

        def refuses(window):
            raise CaptureUnavailable(
                "Qronos can only look at the screen on Windows."
            )

        runtime = prepared_runtime()
        runtime.screen_capture = ScreenCapture(
            grab=refuses,
            geometry_fn=lambda: DisplayGeometry(8, 6, 8, 6),
        )

        events = captured_events(
            lambda: runtime.look_at_screen(approved=True, question="What?")
        )
        kinds = [event["eventType"] for event in events]

        self.assertIn("voice_screen_unavailable", kinds)
        self.assertNotIn("runtime_error", kinds)

    def test_a_failure_from_the_worker_reaches_the_person(self) -> None:
        runtime = prepared_runtime(
            orchestrator=FakeOrchestrator(
                results=[
                    StepResult(
                        order=1,
                        success=False,
                        output="",
                        error="The vision model is not installed.",
                    )
                ]
            )
        )

        events = captured_events(
            lambda: runtime.look_at_screen(approved=True, question="What?")
        )
        message = next(
            event["message"]
            for event in events
            if event["eventType"] == "runtime_error"
        )

        self.assertEqual(message, "The vision model is not installed.")

    def test_asking_for_the_window_uses_the_one_from_the_hotkey(self) -> None:
        """
        Read when the hotkey fired, because a moment later Qronos may have
        focus itself and "read this window" would read Qronos.
        """
        asked: list = []
        runtime = prepared_runtime()
        runtime.screen_capture = screenless_capture(watch=asked)
        runtime.last_foreground_window = 4242

        captured_events(
            lambda: runtime.look_at_screen(
                approved=True, question="What?", window_only=True
            )
        )

        self.assertEqual(asked, [4242])

    def test_asking_for_the_whole_screen_ignores_the_window(self) -> None:
        asked: list = []
        runtime = prepared_runtime()
        runtime.screen_capture = screenless_capture(watch=asked)
        runtime.last_foreground_window = 4242

        captured_events(
            lambda: runtime.look_at_screen(approved=True, question="What?")
        )

        self.assertEqual(asked, [None])

class TestTheLookActionIsDispatched(unittest.TestCase):
    def test_the_action_reaches_the_runtime_with_its_answer(self) -> None:
        runtime = prepared_runtime()
        seen: list = []
        done = threading.Event()

        def record(**kwargs):
            seen.append(kwargs)
            done.set()

        runtime.look_at_screen = record

        captured_events(
            lambda: handle_action(
                runtime,
                {
                    "actionId": LOOK_AT_SCREEN_ACTION,
                    "approved": True,
                    "question": "What is this?",
                    "windowOnly": True,
                },
            )
        )

        # The turn runs off the reader thread, so that a command sent during
        # one still arrives. Wait for it rather than sleeping.
        self.assertTrue(done.wait(5))
        self.assertEqual(
            seen,
            [
                {
                    "approved": True,
                    "question": "What is this?",
                    "window_only": True,
                }
            ],
        )

    def test_an_unknown_action_is_still_a_warning(self) -> None:
        runtime = prepared_runtime()

        events = captured_events(
            lambda: handle_action(runtime, {"actionId": "qronos.nonsense"})
        )

        self.assertIn(
            "runtime_warning",
            [event["eventType"] for event in events],
        )

class TestVisionIntegrationPresence(unittest.TestCase):
    def test_runtime_exposes_permission_gated_vision_action(self) -> None:
        self.assertEqual(
            LOOK_AT_SCREEN_ACTION,
            "qronos.look_at_screen",
        )

    def test_prepared_fake_runtime_has_voice_and_vision_collaborators(self) -> None:
        runtime = prepared_runtime()
        self.assertIsNotNone(runtime.voice_output)
        self.assertIsNotNone(runtime.action_audit)
        self.assertIsNotNone(runtime.screen_capture)
