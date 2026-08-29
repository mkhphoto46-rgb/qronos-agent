"""
The voice, without the voice.

None of these need the models. A stand-in HTTP server plays the part of
CrispASR, which means the whole path a real utterance takes — start the
process, wait until it can actually speak, post the text, write the audio,
release the card when the room goes quiet — is exercised on a machine with no
graphics card, including the Linux one the suite runs on.

What is deliberately not covered here is whether the audio sounds like Persian.
No test can tell you that; ``tools/test_qronos_voice_live.py`` produces real
audio on the real runtime and reports what it cost.
"""

from __future__ import annotations

import struct
import threading
import time
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.chatterbox_runtime import (
    REQUIRED_VRAM_MB,
    ChatterboxRuntime,
    VoiceUnavailable,
)
from core.resource_guard import GpuStatus, SystemStatus
from core.voice_runtime import Utterance


def a_wave(seconds: float = 1.0, rate: int = 24_000) -> bytes:
    """A real wave file, so the duration reading is a real reading."""
    frames = int(seconds * rate)
    silence = struct.pack("<h", 0) * frames

    from io import BytesIO

    buffer = BytesIO()

    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(silence)

    return buffer.getvalue()


class FakeCrispASR:
    """
    Something that answers POST /v1/audio/speech the way CrispASR does.

    A real socket rather than a patched urlopen, because the thing most likely
    to break here is the shape of the conversation with the server, and a
    patched function would agree with whatever the code already does.
    """

    def __init__(
        self,
        audio_seconds: float = 1.0,
        status: int = 200,
        speakable_after: int = 0,
        delay: float = 0.0,
    ) -> None:
        self.audio_seconds = audio_seconds
        self.status = status
        self.speakable_after = speakable_after
        self.delay = delay

        self.requests: list[str] = []
        self._lock = threading.Lock()

        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the stdlib's spelling
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")

                import json as _json

                text = _json.loads(body).get("input", "")

                with server._lock:
                    server.requests.append(text)
                    seen = len(server.requests)

                if server.delay:
                    time.sleep(server.delay)

                # Not able to speak yet — what a server that has opened its
                # port but is still reading a model off disk does.
                if seen <= server.speakable_after:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"still loading")
                    return

                if server.status != 200:
                    self.send_response(server.status)
                    self.end_headers()
                    self.wfile.write(b"the voice refused")
                    return

                audio = a_wave(server.audio_seconds)

                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)

            def log_message(self, *args: object) -> None:
                """Silence. The suite's output is not a web server log."""

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._http.server_address[1]

        # A short poll interval, because shutdown() waits for the loop to
        # notice. The stdlib default of half a second is charged to every
        # single test in this file on teardown.
        self._thread = threading.Thread(
            target=self._http.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    @property
    def spoken(self) -> list[str]:
        with self._lock:
            return list(self.requests)

    def close(self) -> None:
        self._http.shutdown()
        self._http.server_close()


class FakeProcess:
    """A process that is alive until told otherwise."""

    def __init__(self, exit_code: int | None = None) -> None:
        self._exit_code = exit_code
        self.terminated = False
        self.killed = False
        self.stderr = None

    def poll(self) -> int | None:
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True
        self._exit_code = 0

    def kill(self) -> None:
        self.killed = True
        self._exit_code = -9

    def wait(self, timeout: float | None = None) -> int:
        return self._exit_code or 0

    @property
    def returncode(self) -> int | None:
        return self._exit_code


class VoiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeCrispASR()
        self.addCleanup(self.server.close)

        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.here = Path(self.temp.name)

        # Files that exist, so health_check passes without 750 MB of weights.
        self.exe = self.here / "crispasr.exe"
        self.t3 = self.here / "t3.gguf"
        self.s3gen = self.here / "s3gen.gguf"

        for path in (self.exe, self.t3, self.s3gen):
            path.write_bytes(b"not really a model")

        self.processes: list[FakeProcess] = []

    def a_runtime(self, **overrides) -> ChatterboxRuntime:
        def launch(command, environment):
            process = FakeProcess()
            self.processes.append((process, command, environment))
            return process

        settings = dict(
            executable_path=self.exe,
            voice_model_path=self.t3,
            codec_model_path=self.s3gen,
            temp_dir=self.here / "out",
            port=self.server.port,
            launch=lambda c, e: launch(c, e),
            startup_timeout=5.0,
            # The suite must not spend real seconds proving that a retry loop
            # retries. The interval is a parameter for exactly this.
            retry_seconds=0.01,
            request_timeout=5.0,
            # Whether there is room on the card is its own subject, tested
            # below with readings supplied. Everything else here would
            # otherwise pass or fail according to what the machine running the
            # suite happened to have open.
            check_room=False,
        )
        settings.update(overrides)

        runtime = ChatterboxRuntime(**settings)
        self.addCleanup(runtime.release)

        return runtime


class TestItSpeaks(VoiceTestCase):
    def test_it_writes_audio_and_says_what_it_cost(self) -> None:
        runtime = self.a_runtime()

        utterance = runtime.speak_to_file("سلام دنیا")

        self.assertIsInstance(utterance, Utterance)
        self.assertTrue(utterance.audio_path.is_file())
        self.assertAlmostEqual(utterance.audio_seconds, 1.0, places=2)
        self.assertEqual(utterance.text, "سلام دنیا")

    def test_the_words_actually_reach_the_engine(self) -> None:
        runtime = self.a_runtime()

        runtime.speak_to_file("چیزی برای گفتن")

        # The first request is the readiness check, which is deliberate: the
        # only way to know a server can speak is to make it speak.
        self.assertIn("چیزی برای گفتن", self.server.spoken)

    def test_it_writes_where_it_is_told(self) -> None:
        runtime = self.a_runtime()
        target = self.here / "somewhere" / "else.wav"

        utterance = runtime.speak_to_file("جایی دیگر", destination=target)

        self.assertEqual(utterance.audio_path, target)
        self.assertTrue(target.is_file())

    def test_two_utterances_do_not_overwrite_each_other(self) -> None:
        runtime = self.a_runtime()

        first = runtime.speak_to_file("یک")
        second = runtime.speak_to_file("دو")

        self.assertNotEqual(first.audio_path, second.audio_path)
        self.assertTrue(first.audio_path.is_file())
        self.assertTrue(second.audio_path.is_file())


class TestNothingStartsUntilSomethingIsSaid(VoiceTestCase):
    """
    Same rule the queue follows: an idle Qronos holds nothing.

    Constructing the runtime must not put 750 MB on the graphics card. It is
    built when the interface starts, and most sessions never say a word.
    """

    def test_building_it_starts_nothing(self) -> None:
        runtime = self.a_runtime()

        self.assertFalse(runtime.is_loaded)
        self.assertEqual(self.processes, [])

    def test_health_check_starts_nothing_either(self) -> None:
        runtime = self.a_runtime()

        self.assertTrue(runtime.health_check())
        self.assertEqual(self.processes, [])

    def test_the_first_utterance_starts_it(self) -> None:
        runtime = self.a_runtime()

        runtime.speak_to_file("اولین")

        self.assertTrue(runtime.is_loaded)
        self.assertEqual(len(self.processes), 1)

    def test_the_second_does_not_start_it_again(self) -> None:
        # The entire point. Starting per utterance is what cost 1.6 seconds
        # every time in the benchmark this replaces.
        runtime = self.a_runtime()

        runtime.speak_to_file("اولین")
        runtime.speak_to_file("دومین")

        self.assertEqual(len(self.processes), 1)


class TestItWaitsUntilItCanActuallySpeak(VoiceTestCase):
    """
    A server that is listening is not the same as a server that can speak.

    CrispASR opens its port before it has finished reading the model. A
    readiness check that only asks whether something answers hands back a
    server that then blocks on the first real request, which puts the load
    cost back exactly where it was removed from.
    """

    def test_it_keeps_asking_until_the_answer_is_audio(self) -> None:
        self.server.speakable_after = 3
        runtime = self.a_runtime()

        utterance = runtime.speak_to_file("صبر کن")

        self.assertTrue(utterance.audio_path.is_file())
        self.assertGreater(len(self.server.spoken), 3)

    def test_a_server_that_never_speaks_is_reported_not_hidden(self) -> None:
        self.server.speakable_after = 10_000
        runtime = self.a_runtime(startup_timeout=0.2)

        with self.assertRaises(VoiceUnavailable) as caught:
            runtime.speak_to_file("بی‌فایده")

        self.assertIn("ready", str(caught.exception))

    def test_a_runtime_that_dies_on_startup_says_so(self) -> None:
        runtime = self.a_runtime(
            launch=lambda command, environment: FakeProcess(exit_code=1)
        )

        with self.assertRaises(VoiceUnavailable) as caught:
            runtime.speak_to_file("مرده")

        self.assertIn("stopped", str(caught.exception))

    def test_a_failed_start_does_not_leave_a_process_behind(self) -> None:
        self.server.speakable_after = 10_000
        runtime = self.a_runtime(startup_timeout=0.2)

        with self.assertRaises(VoiceUnavailable):
            runtime.speak_to_file("بی‌فایده")

        self.assertFalse(runtime.is_loaded)


class TestItGivesTheCardBack(VoiceTestCase):
    """
    Qronos stopped keeping its brains resident. The voice is smaller and asked
    for far more often, so it stays — but only while there is a conversation
    happening, not for the rest of the day.
    """

    def test_it_releases_after_a_spell_of_silence(self) -> None:
        runtime = self.a_runtime(idle_seconds=0.05)

        runtime.speak_to_file("چیزی")
        self.assertTrue(runtime.is_loaded)

        time.sleep(0.3)

        self.assertFalse(runtime.is_loaded)

    def test_speaking_again_resets_the_clock(self) -> None:
        runtime = self.a_runtime(idle_seconds=0.2)

        runtime.speak_to_file("یک")
        time.sleep(0.12)
        runtime.speak_to_file("دو")
        time.sleep(0.12)

        # A quarter of a second has passed, but never 0.2 in a row.
        self.assertTrue(runtime.is_loaded)

    def test_it_can_speak_again_after_being_released(self) -> None:
        runtime = self.a_runtime(idle_seconds=0.05)

        runtime.speak_to_file("قبل")
        time.sleep(0.3)
        self.assertFalse(runtime.is_loaded)

        utterance = runtime.speak_to_file("بعد")

        self.assertTrue(utterance.audio_path.is_file())
        self.assertTrue(runtime.is_loaded)

    def test_releasing_by_hand_works(self) -> None:
        runtime = self.a_runtime()

        runtime.speak_to_file("چیزی")
        runtime.release()

        self.assertFalse(runtime.is_loaded)

    def test_releasing_twice_is_harmless(self) -> None:
        runtime = self.a_runtime()

        runtime.speak_to_file("چیزی")
        runtime.release()
        runtime.release()

    def test_releasing_something_that_never_started_is_harmless(self) -> None:
        self.a_runtime().release()

    def test_it_releases_on_the_way_out_of_a_with_block(self) -> None:
        runtime = self.a_runtime()

        with runtime:
            runtime.speak_to_file("داخل")

        self.assertFalse(runtime.is_loaded)


class TestItRefusesNonsense(VoiceTestCase):
    def test_there_is_nothing_to_say(self) -> None:
        with self.assertRaises(ValueError):
            self.a_runtime().speak_to_file("   ")

    def test_a_whole_document_is_refused(self) -> None:
        # Qronos speaks answers. Text this long is an upstream bug, and
        # discovering it as a two-minute silence is the worst way to find out.
        with self.assertRaises(ValueError) as caught:
            self.a_runtime().speak_to_file("ب" * 5_000)

        self.assertIn("documents", str(caught.exception))

    def test_missing_pieces_are_named(self) -> None:
        runtime = self.a_runtime(voice_model_path=self.here / "absent.gguf")

        self.assertFalse(runtime.health_check())

        with self.assertRaises(VoiceUnavailable) as caught:
            runtime.speak_to_file("چیزی")

        self.assertIn("absent.gguf", str(caught.exception))

    def test_zero_steps_is_refused_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            self.a_runtime(steps=0)

    def test_a_refusal_from_the_engine_is_reported(self) -> None:
        self.server.status = 500
        runtime = self.a_runtime(startup_timeout=0.2)

        with self.assertRaises(VoiceUnavailable):
            runtime.speak_to_file("چیزی")

    def test_nothing_listening_is_reported_rather_than_hanging(self) -> None:
        # A port that was open a moment ago and is not now, so the connection
        # is refused immediately rather than left to time out.
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]

        runtime = self.a_runtime(port=closed_port, startup_timeout=0.2)

        with self.assertRaises(VoiceUnavailable):
            runtime.speak_to_file("چیزی")


class TestTheMeasuredSettingsAreTheOnesUsed(VoiceTestCase):
    """
    A sweep of thirty-eight runs decided these. If the command stops carrying
    them the sweep stops meaning anything, and nobody would notice by ear.
    """

    def command_for(self, runtime: ChatterboxRuntime) -> list[str]:
        runtime.speak_to_file("چیزی")

        return self.processes[0][1]

    def environment_for(self, runtime: ChatterboxRuntime) -> dict:
        runtime.speak_to_file("چیزی")

        return self.processes[0][2]

    def test_it_runs_as_a_server_so_the_model_stays_put(self) -> None:
        self.assertIn("--server", self.command_for(self.a_runtime()))

    def test_ten_diffusion_steps(self) -> None:
        command = self.command_for(self.a_runtime())

        self.assertIn("--tts-steps", command)
        self.assertEqual(command[command.index("--tts-steps") + 1], "10")

    def test_the_whole_pipeline_goes_on_the_graphics_card(self) -> None:
        # Measured at 0.40 real-time against the default placement's 0.74,
        # for less graphics memory rather than more.
        environment = self.environment_for(self.a_runtime())

        self.assertEqual(
            environment.get("CRISPASR_CHATTERBOX_FORCE_GPU"),
            "1",
        )

    def test_it_listens_only_to_this_machine(self) -> None:
        command = self.command_for(self.a_runtime())

        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")


class TestItWillNotSqueezeOntoAFullCard(VoiceTestCase):
    """
    The failure that does not announce itself.

    With 931 MiB free on the development card, the voice did not raise
    anything — it spilled and crawled. The same line that costs 0.33 seconds
    of work per second of speech when there is room cost 3.01 when there was
    not, which is three times slower than a person can listen, while fighting
    whatever application had the card first.

    So refusing is the kind answer. A voice that says it cannot speak can be
    reported to the user; a voice that takes thirty-five seconds to say a
    sentence merely looks broken.
    """

    def a_card(self, free_mb: int, total_mb: int = 16_303):
        return GpuStatus(
            name="RTX 5080",
            temperature_c=45,
            gpu_utilization_percent=10,
            vram_used_mb=total_mb - free_mb,
            vram_total_mb=total_mb,
        )

    def a_machine(self):
        return SystemStatus(
            cpu_usage_percent=12.0,
            ram_usage_percent=43.0,
            ram_used_gb=27.0,
            ram_total_gb=63.0,
        )

    def with_card(self, free_mb: int):
        return patch(
            "core.chatterbox_runtime.read_gpu_status",
            return_value=self.a_card(free_mb),
        )

    def speak_with(self, free_mb: int, **overrides):
        runtime = self.a_runtime(check_room=True, **overrides)

        with self.with_card(free_mb):
            return runtime.speak_to_file("چیزی")

    def test_a_card_with_room_speaks(self) -> None:
        utterance = self.speak_with(free_mb=12_000)

        self.assertTrue(utterance.audio_path.is_file())

    def test_a_card_without_room_refuses(self) -> None:
        with self.assertRaises(VoiceUnavailable):
            self.speak_with(free_mb=1_098)

    def test_a_card_that_fits_the_model_but_not_the_work_refuses(self) -> None:
        """
        The mistake this constant was corrected for.

        1,739 MiB is more than the model needs to load, and more than the
        measured peak — and the voice still crawled there, at real-time
        factors of 2.9 to 7.1. Fitting is not the same as working.
        """
        with self.assertRaises(VoiceUnavailable):
            self.speak_with(free_mb=1_739)

    def test_the_refusal_carries_the_numbers(self) -> None:
        with self.assertRaises(VoiceUnavailable) as caught:
            self.speak_with(free_mb=900)

        message = str(caught.exception).replace(",", "")

        self.assertIn(str(REQUIRED_VRAM_MB), message)
        self.assertIn("900", message)

    def test_a_refusal_starts_nothing(self) -> None:
        # Otherwise the card is taken anyway and the refusal is a lie.
        runtime = self.a_runtime(check_room=True)

        with self.with_card(900):
            with self.assertRaises(VoiceUnavailable):
                runtime.speak_to_file("چیزی")

        self.assertFalse(runtime.is_loaded)
        self.assertEqual(self.processes, [])

    def test_a_card_that_cannot_be_read_does_not_block_the_voice(self) -> None:
        # No reading is not the same as no room. On a machine with no NVIDIA
        # card at all, read_gpu_status returns None for both reasons, and
        # refusing would silence Qronos permanently on every such machine.
        runtime = self.a_runtime(check_room=True)

        with patch(
            "core.chatterbox_runtime.read_gpu_status", return_value=None
        ):
            utterance = runtime.speak_to_file("بدون کارت")

        self.assertTrue(utterance.audio_path.is_file())

    def test_a_reading_that_throws_does_not_block_it_either(self) -> None:
        runtime = self.a_runtime(check_room=True)

        with patch(
            "core.chatterbox_runtime.read_gpu_status",
            side_effect=OSError("nvidia-smi went away"),
        ):
            utterance = runtime.speak_to_file("بدون خواندن")

        self.assertTrue(utterance.audio_path.is_file())

    def test_a_card_that_is_too_hot_refuses(self) -> None:
        runtime = self.a_runtime(check_room=True)
        hot = GpuStatus(
            name="RTX 5080",
            temperature_c=90,
            gpu_utilization_percent=99,
            vram_used_mb=1_000,
            vram_total_mb=16_303,
        )

        with patch(
            "core.chatterbox_runtime.read_gpu_status", return_value=hot
        ):
            with self.assertRaises(VoiceUnavailable) as caught:
                runtime.speak_to_file("داغ")

        self.assertIn("cool", str(caught.exception))

    def test_a_warm_card_with_room_still_speaks(self) -> None:
        runtime = self.a_runtime(check_room=True)
        warm = GpuStatus(
            name="RTX 5080",
            temperature_c=84,
            gpu_utilization_percent=99,
            vram_used_mb=1_000,
            vram_total_mb=16_303,
        )

        with patch(
            "core.chatterbox_runtime.read_gpu_status", return_value=warm
        ):
            self.assertTrue(
                runtime.speak_to_file("گرم").audio_path.is_file()
            )

    def test_a_voice_already_speaking_is_not_interrupted(self) -> None:
        # The check is on the way in only. Something else growing while Qronos
        # is mid-sentence must not cut it off.
        runtime = self.a_runtime(check_room=True)

        with self.with_card(12_000):
            runtime.speak_to_file("اول")

        with self.with_card(200):
            second = runtime.speak_to_file("دوم")

        self.assertTrue(second.audio_path.is_file())
        self.assertEqual(len(self.processes), 1)


class TestWhatAnUtteranceReports(unittest.TestCase):
    def test_faster_than_speech_is_the_number_that_matters(self) -> None:
        quick = Utterance(Path("a.wav"), audio_seconds=4.0, took_seconds=1.5, text="")
        slow = Utterance(Path("b.wav"), audio_seconds=1.0, took_seconds=3.0, text="")

        self.assertTrue(quick.faster_than_speech)
        self.assertFalse(slow.faster_than_speech)

    def test_the_real_time_factor_is_work_over_speech(self) -> None:
        utterance = Utterance(Path("a.wav"), 4.0, 1.0, "")

        self.assertAlmostEqual(utterance.real_time_factor, 0.25)

    def test_silence_does_not_divide_by_zero(self) -> None:
        utterance = Utterance(Path("a.wav"), 0.0, 1.0, "")

        self.assertEqual(utterance.real_time_factor, float("inf"))
        self.assertFalse(utterance.faster_than_speech)


if __name__ == "__main__":
    unittest.main()
