"""
Push-to-talk over the link: bounded upload, transcription, answer, reply audio.

The speech-to-text and text-to-speech here are stand-ins that check what they
are given — a real WAV with the uploaded samples — and record where it was, so
the tests can also check the recording was deleted. No microphone, no model.
"""

from __future__ import annotations

import base64
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from core.link_handlers import REMOTE_ACTION_REFUSAL, ToolFreeAsk, default_handlers
from core.link_server import LinkOperationError, LinkSettings
from core.link_voice import (
    MAX_CHUNK_BYTES,
    MAX_VOICE_BYTES,
    VOICE_SAMPLE_RATE,
    VoiceUploads,
    write_wav,
)
from tests.fixtures.link_remote_rig import RemoteRig


DEVICE = "0123456789abcdef"


def tone(seconds: float, hz: float = 440.0) -> bytes:
    count = int(seconds * VOICE_SAMPLE_RATE)

    return b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * n / VOICE_SAMPLE_RATE)))
        for n in range(count)
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TestUploads(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.uploads = VoiceUploads(clock=self.clock)

    def test_chunks_are_joined_in_order(self) -> None:
        voice_id = self.uploads.begin(DEVICE, "fa")
        self.uploads.append(DEVICE, voice_id, 0, b"ab")
        self.uploads.append(DEVICE, voice_id, 1, b"cd")

        self.assertEqual(self.uploads.finish(DEVICE, voice_id), (b"abcd", "fa"))
        self.assertEqual(len(self.uploads), 0)

    def test_out_of_order_is_refused(self) -> None:
        voice_id = self.uploads.begin(DEVICE, "fa")

        with self.assertRaises(LinkOperationError) as caught:
            self.uploads.append(DEVICE, voice_id, 1, b"ab")

        self.assertEqual(caught.exception.code, "voice_out_of_order")

    def test_a_recording_is_capped_and_dropped_when_it_overruns(self) -> None:
        voice_id = self.uploads.begin(DEVICE, "fa")
        piece = b"\x00\x00" * (MAX_CHUNK_BYTES // 2)
        seq = 0

        with self.assertRaises(LinkOperationError) as caught:
            while True:
                self.uploads.append(DEVICE, voice_id, seq, piece)
                seq += 1

        self.assertEqual(caught.exception.code, "voice_too_long")
        self.assertLessEqual(seq * len(piece), MAX_VOICE_BYTES)
        self.assertEqual(len(self.uploads), 0)

    def test_one_recording_per_device(self) -> None:
        first = self.uploads.begin(DEVICE, "fa")
        second = self.uploads.begin(DEVICE, "en")

        with self.assertRaises(LinkOperationError):
            self.uploads.append(DEVICE, first, 0, b"ab")

        self.uploads.append(DEVICE, second, 0, b"ab")
        self.assertEqual(len(self.uploads), 1)

    def test_an_idle_recording_is_dropped(self) -> None:
        voice_id = self.uploads.begin(DEVICE, "fa")
        self.clock.now += self.uploads.idle_seconds + 1

        with self.assertRaises(LinkOperationError) as caught:
            self.uploads.append(DEVICE, voice_id, 0, b"ab")

        self.assertEqual(caught.exception.code, "voice_unknown")

    def test_wav_is_16k_mono_16_bit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "x.wav"
            write_wav(path, b"\x01\x00" * 160)

            with wave.open(str(path), "rb") as audio:
                self.assertEqual(
                    (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()),
                    (16_000, 1, 2, 160),
                )


class Stt:
    def __init__(self, transcript: str = "سلام قرونوس") -> None:
        self.transcript = transcript
        self.paths: list[Path] = []
        self.frames: list[bytes] = []
        self.languages: list[str] = []

    def __call__(self, wav_path: Path, language: str) -> str:
        self.paths.append(wav_path)
        self.languages.append(language)

        with wave.open(str(wav_path), "rb") as audio:
            assert audio.getframerate() == 16_000 and audio.getnchannels() == 1
            self.frames.append(audio.readframes(audio.getnframes()))

        return self.transcript


class Tts:
    def __init__(self, folder: Path, size: int = 150_000, fail: bool = False) -> None:
        self.folder = folder
        self.size = size
        self.fail = fail
        self.paths: list[Path] = []
        self.texts: list[str] = []

    def __call__(self, text: str) -> Path:
        if self.fail:
            raise RuntimeError("voice unavailable")

        self.texts.append(text)
        path = self.folder / f"reply-{len(self.paths)}.wav"
        path.write_bytes(bytes(range(256)) * (self.size // 256))
        self.paths.append(path)

        return path


class Chat:
    def __call__(self, messages):
        return "پاسخ به: " + messages[-1]["content"]


class VoiceFlowTestCase(unittest.TestCase):
    def build(self, stt: Stt | None = None, tts: Tts | None = None, transcribe: bool = True) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.stt = stt if stt is not None else Stt()
        self.tts = tts if tts is not None else Tts(Path(self.folder.name))
        self.rig = RemoteRig(
            handlers=default_handlers(
                remote_ask=ToolFreeAsk(Chat()),
                transcribe=self.stt if transcribe else None,
                speak=self.tts,
            ),
            settings=LinkSettings(remote_burst=200),
        )
        self.addCleanup(self.rig.close)
        self.record = self.rig.remote_device()

    def upload(self, phone, audio: bytes, language: str = "fa") -> str:
        begun = phone.call("voice_begin", format="pcm_s16le", sample_rate=16000, channels=1, language=language)
        self.assertTrue(begun.ok, begun.code)
        voice_id = begun.result["voice_id"]
        step = begun.result["max_chunk_bytes"]

        for seq, offset in enumerate(range(0, len(audio), step)):
            piece = audio[offset: offset + step]
            sent = phone.call(
                "voice_chunk",
                voice_id=voice_id,
                seq=seq,
                data=base64.b64encode(piece).decode("ascii"),
            )
            self.assertTrue(sent.ok, sent.code)

        return voice_id


class TestPushToTalkThroughTheRelay(VoiceFlowTestCase):
    def test_a_spoken_question_is_transcribed_answered_and_spoken_back(self) -> None:
        self.build()
        audio = tone(2.0)

        with self.rig.phone(self.record, timeout=10) as phone:
            voice_id = self.upload(phone, audio)
            response = phone.call("voice_end", voice_id=voice_id, reply="audio")
            events = phone.events

        self.assertTrue(response.ok, response.code)
        self.assertEqual(response.result["transcript"], "سلام قرونوس")
        self.assertEqual(response.result["answer"], "پاسخ به: سلام قرونوس")
        self.assertEqual(response.result["performed_actions"], [])

        # The PC heard exactly what the phone recorded, in the language asked.
        self.assertEqual(self.stt.frames, [audio])
        self.assertEqual(self.stt.languages, ["fa"])

        # The spoken answer arrived in order and whole.
        replies = [e for e in events if e.topic == "voice_reply"]
        self.assertEqual(len(replies), response.result["reply_audio_chunks"])
        self.assertEqual([e.data["seq"] for e in replies], list(range(len(replies))))
        self.assertTrue(replies[-1].data["last"])
        rebuilt = b"".join(base64.b64decode(e.data["data"]) for e in replies)
        self.assertEqual(rebuilt, bytes(range(256)) * (150_000 // 256))

        # Neither the recording nor the synthesised answer is left on disk.
        self.assertFalse(any(path.exists() for path in self.stt.paths))
        self.assertFalse(any(path.exists() for path in self.tts.paths))

    def test_a_spoken_action_request_is_refused_like_a_typed_one(self) -> None:
        self.build(stt=Stt("فایل گزارش را پاک کن"))

        with self.rig.phone(self.record) as phone:
            voice_id = self.upload(phone, tone(0.5))
            response = phone.call("voice_end", voice_id=voice_id, reply="text")

        self.assertEqual(response.result["answer"], REMOTE_ACTION_REFUSAL)
        self.assertTrue(response.result["refused_action"])

    def test_a_resent_chunk_is_acknowledged_not_appended_twice(self) -> None:
        self.build()
        piece = tone(0.5)
        encoded = base64.b64encode(piece).decode("ascii")

        with self.rig.phone(self.record) as phone:
            begun = phone.call("voice_begin", language="fa")
            voice_id = begun.result["voice_id"]
            cid = "c" * 32

            first = phone.call("voice_chunk", _command_id=cid, voice_id=voice_id, seq=0, data=encoded)
            again = phone.call("voice_chunk", _command_id=cid, voice_id=voice_id, seq=0, data=encoded)
            response = phone.call("voice_end", voice_id=voice_id)

        self.assertEqual(first.result, again.result)
        self.assertEqual(self.stt.frames, [piece])
        self.assertTrue(response.ok)

    def test_an_upload_survives_a_dropped_connection(self) -> None:
        self.build()
        audio = tone(1.0)
        half = len(audio) // 2

        with self.rig.phone(self.record) as phone:
            voice_id = phone.call("voice_begin", language="en").result["voice_id"]
            phone.call("voice_chunk", voice_id=voice_id, seq=0, data=base64.b64encode(audio[:half]).decode())

        self.assertTrue(self.rig.wait_parked(self.record))

        with self.rig.phone(self.record) as phone:
            phone.call("voice_chunk", voice_id=voice_id, seq=1, data=base64.b64encode(audio[half:]).decode())
            response = phone.call("voice_end", voice_id=voice_id)

        self.assertTrue(response.ok)
        self.assertEqual(self.stt.frames, [audio])

    def test_a_voice_that_fails_still_returns_the_text_answer(self) -> None:
        self.build(tts=Tts(Path(tempfile.gettempdir()), fail=True))

        with self.rig.phone(self.record) as phone:
            voice_id = self.upload(phone, tone(0.3))
            response = phone.call("voice_end", voice_id=voice_id, reply="audio")

        self.assertTrue(response.ok)
        self.assertEqual(response.result["answer"], "پاسخ به: سلام قرونوس")
        self.assertTrue(response.result["reply_audio_error"])

    def test_silence_is_reported_not_answered(self) -> None:
        self.build(stt=Stt(""))

        with self.rig.phone(self.record) as phone:
            voice_id = self.upload(phone, b"\x00\x00" * 1600)
            response = phone.call("voice_end", voice_id=voice_id)

        self.assertTrue(response.result["heard_nothing"])
        self.assertEqual(response.result["answer"], "")


class TestVoiceRefusals(VoiceFlowTestCase):
    def test_only_16k_mono_pcm_is_accepted(self) -> None:
        self.build()

        with self.rig.phone(self.record) as phone:
            response = phone.call("voice_begin", format="pcm_s16le", sample_rate=8000, channels=1)

        self.assertEqual(response.code, "voice_format")

    def test_an_oversized_chunk_is_refused_before_decoding(self) -> None:
        self.build()

        with self.rig.phone(self.record) as phone:
            voice_id = phone.call("voice_begin").result["voice_id"]
            response = phone.call(
                "voice_chunk", voice_id=voice_id, seq=0, data="A" * (MAX_CHUNK_BYTES * 2)
            )

        self.assertEqual(response.code, "voice_chunk_too_large")

    def test_a_chunk_that_is_not_base64_is_refused(self) -> None:
        self.build()

        with self.rig.phone(self.record) as phone:
            voice_id = phone.call("voice_begin").result["voice_id"]
            response = phone.call("voice_chunk", voice_id=voice_id, seq=0, data="not base64!")

        self.assertEqual(response.code, "bad_request")

    def test_without_speech_to_text_voice_is_refused(self) -> None:
        self.build(transcribe=False)

        with self.rig.phone(self.record) as phone:
            response = phone.call("voice_begin")

        self.assertEqual(response.code, "not_configured")


if __name__ == "__main__":
    unittest.main()
