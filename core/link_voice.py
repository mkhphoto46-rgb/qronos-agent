"""
Push-to-talk over the device link: a spoken question in, an answer back.

The phone records while the user holds the button, and nothing else: no
always-open microphone, no speech recognition on the phone, no model on the
phone. It sends the recording to the PC in bounded chunks; the PC transcribes
it with its own speech-to-text, answers it through the same path a typed
question takes, and can speak the answer back as audio.

    voice_begin   {format: "pcm_s16le", sample_rate: 16000, channels: 1,
                   language: "fa" | "en" | "auto"}
                  -> {voice_id, max_seconds, max_chunk_bytes, sample_rate}
    voice_chunk   {voice_id, seq: 0, 1, 2 ..., data: base64 PCM}
                  -> {received_bytes}
    voice_end     {voice_id, reply: "text" | "audio"}
                  -> {transcript, answer, refused_action, performed_actions,
                      reply_audio_chunks}
                  preceded, for "audio", by ``voice_reply`` events carrying
                  the spoken answer as WAV in order: {seq, data, last}

Raw 16 kHz mono PCM rather than a compressed codec: it needs no codec on
either side and nothing to decode on the PC before transcription. The cost is
bandwidth, 32 kB/s, which is why a recording is capped at 30 seconds. A codec
is a later optimisation, not a protocol change: ``format`` is already a field.

Bounded everywhere: one upload per device at a time, a cap on seconds, on
chunk size and on total bytes; an upload left idle is dropped; chunks must
arrive in order. Chunks carry command ids like every other remote request, so
a chunk resent after a dropped connection is acknowledged, not appended twice.

The recording is the user's voice, so it is treated as such. It lives in
memory while it is uploaded, in a private temporary file only while it is
transcribed, and is deleted as soon as the transcript exists. Neither the audio
nor the transcript is logged: the audit log records the operations by name,
as for everything else.

The voice path answers through ``core.link_handlers.answer_question``, so it
inherits the same rule as typed questions: over the relay only a
``ToolFreeAsk`` can answer, and it can do nothing on the PC.
"""

from __future__ import annotations

import base64
import binascii
import secrets
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from core.link_capability import LinkOp
from core.link_protocol import Request
from core.link_server import Handler, LinkOperationError, LinkSession


VOICE_FORMAT = "pcm_s16le"
VOICE_SAMPLE_RATE = 16_000
VOICE_CHANNELS = 1
BYTES_PER_SAMPLE = 2

MAX_VOICE_SECONDS = 30
MAX_VOICE_BYTES = MAX_VOICE_SECONDS * VOICE_SAMPLE_RATE * BYTES_PER_SAMPLE

# Raw bytes per chunk. Base64 makes it about 64 KiB on the wire, far below the
# frame limit, and small enough that a lost chunk is cheap to resend.
MAX_CHUNK_BYTES = 48 * 1024

# An upload with nothing arriving for this long is abandoned.
UPLOAD_IDLE_SECONDS = 60.0

# The spoken answer sent back. A long answer at 24 kHz is a few hundred
# kilobytes; this bounds what one reply can push down a mobile link.
MAX_REPLY_AUDIO_BYTES = 4 * 1024 * 1024
REPLY_CHUNK_BYTES = 48 * 1024

LANGUAGES = frozenset({"fa", "en", "auto"})

VOICE_ID_BYTES = 16


class Transcribe(Protocol):
    """The PC's speech-to-text: a WAV file in, text out."""

    def __call__(self, wav_path: Path, language: str) -> str:  # pragma: no cover
        ...


class Speak(Protocol):
    """The PC's text-to-speech: text in, the path of a WAV file out."""

    def __call__(self, text: str) -> Path:  # pragma: no cover - protocol
        ...


@dataclass
class _Upload:
    voice_id: str
    language: str
    started_at: float
    touched_at: float
    next_seq: int = 0
    audio: bytearray = field(default_factory=bytearray)


class VoiceUploads:
    """
    The recordings in flight, one per device at most.

    Shared by the three voice handlers of one server, so an upload begun on one
    connection can be finished on the next after the mobile link drops.
    """

    def __init__(
        self,
        max_bytes: int = MAX_VOICE_BYTES,
        idle_seconds: float = UPLOAD_IDLE_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.max_bytes = max_bytes
        self.idle_seconds = idle_seconds
        self.clock = clock if clock is not None else time.monotonic
        self._uploads: dict[str, _Upload] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._uploads)

    def begin(self, device_id: str, language: str) -> str:
        """Start a recording, replacing any the device left unfinished."""

        now = self.clock()
        voice_id = secrets.token_hex(VOICE_ID_BYTES)

        with self._lock:
            self._expire(now)
            self._uploads[device_id] = _Upload(
                voice_id=voice_id,
                language=language,
                started_at=now,
                touched_at=now,
            )

        return voice_id

    def append(self, device_id: str, voice_id: str, seq: int, data: bytes) -> int:
        now = self.clock()

        with self._lock:
            self._expire(now)
            upload = self._uploads.get(device_id)

            if upload is None or upload.voice_id != voice_id:
                raise LinkOperationError(
                    "voice_unknown", "That recording is not open any more."
                )

            if seq != upload.next_seq:
                raise LinkOperationError(
                    "voice_out_of_order",
                    f"Expected part {upload.next_seq} of the recording.",
                )

            if len(upload.audio) + len(data) > self.max_bytes:
                del self._uploads[device_id]

                raise LinkOperationError(
                    "voice_too_long",
                    f"A recording can be at most {MAX_VOICE_SECONDS} seconds.",
                )

            upload.audio.extend(data)
            upload.next_seq += 1
            upload.touched_at = now

            return len(upload.audio)

    def finish(self, device_id: str, voice_id: str) -> tuple[bytes, str]:
        """Close the recording and hand over its audio and language."""

        with self._lock:
            self._expire(self.clock())
            upload = self._uploads.get(device_id)

            if upload is None or upload.voice_id != voice_id:
                raise LinkOperationError(
                    "voice_unknown", "That recording is not open any more."
                )

            del self._uploads[device_id]

            return bytes(upload.audio), upload.language

    def discard(self, device_id: str) -> None:
        with self._lock:
            self._uploads.pop(device_id, None)

    def _expire(self, now: float) -> None:
        for device_id in [
            key
            for key, upload in self._uploads.items()
            if now - upload.touched_at > self.idle_seconds
        ]:
            del self._uploads[device_id]


def write_wav(path: Path, pcm: bytes) -> None:
    """16 kHz mono 16-bit PCM into a WAV file, which is what the STT reads."""

    with wave.open(str(path), "wb") as out:
        out.setnchannels(VOICE_CHANNELS)
        out.setsampwidth(BYTES_PER_SAMPLE)
        out.setframerate(VOICE_SAMPLE_RATE)
        out.writeframes(pcm)


def _param(request: Request, name: str, kind: type) -> Any:
    value = request.params.get(name)

    if kind is int and isinstance(value, bool):
        value = None

    if not isinstance(value, kind):
        raise LinkOperationError("bad_request", f"'{name}' is missing or malformed.")

    return value


def voice_handlers(
    transcribe: Transcribe | None,
    answer: Callable[[LinkSession, str], dict[str, Any]],
    speak: Speak | None = None,
    uploads: VoiceUploads | None = None,
) -> dict[LinkOp, Handler]:
    """
    The three push-to-talk operations, sharing one set of uploads.

    ``answer`` is how a transcript becomes an answer for a session — in
    production ``core.link_handlers.answer_question`` with the server's own
    ``ask`` and ``remote_ask``, so voice and text obey the same scope rule.
    Without ``transcribe`` the operations refuse, honestly, rather than
    accepting audio nobody can hear.
    """

    store = uploads if uploads is not None else VoiceUploads()

    def require_stt() -> Transcribe:
        if transcribe is None:
            raise LinkOperationError(
                "not_configured",
                "Speaking to Qronos from the phone is not set up on this computer.",
            )

        return transcribe

    def begin(session: LinkSession, request: Request) -> dict[str, Any]:
        require_stt()

        fmt = request.params.get("format", VOICE_FORMAT)
        rate = request.params.get("sample_rate", VOICE_SAMPLE_RATE)
        channels = request.params.get("channels", VOICE_CHANNELS)
        language = request.params.get("language", "auto")

        if (fmt, rate, channels) != (VOICE_FORMAT, VOICE_SAMPLE_RATE, VOICE_CHANNELS):
            raise LinkOperationError(
                "voice_format",
                "Recordings must be 16 kHz mono 16-bit PCM.",
            )

        if language not in LANGUAGES:
            raise LinkOperationError("bad_request", "Unknown language.")

        voice_id = store.begin(session.device.device_id, language)

        return {
            "voice_id": voice_id,
            "max_seconds": MAX_VOICE_SECONDS,
            "max_chunk_bytes": MAX_CHUNK_BYTES,
            "sample_rate": VOICE_SAMPLE_RATE,
        }

    def chunk(session: LinkSession, request: Request) -> dict[str, Any]:
        voice_id = _param(request, "voice_id", str)
        seq = _param(request, "seq", int)
        encoded = _param(request, "data", str)

        # Checked before decoding, so an oversized chunk costs nothing.
        if len(encoded) > (MAX_CHUNK_BYTES * 4) // 3 + 4:
            raise LinkOperationError("voice_chunk_too_large", "That part is too large.")

        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise LinkOperationError("bad_request", "The audio is not base64.") from exc

        if not data or len(data) > MAX_CHUNK_BYTES or len(data) % BYTES_PER_SAMPLE:
            raise LinkOperationError("voice_chunk_too_large", "That part is the wrong size.")

        received = store.append(session.device.device_id, voice_id, seq, data)

        return {"received_bytes": received}

    def end(session: LinkSession, request: Request) -> dict[str, Any]:
        stt = require_stt()
        voice_id = _param(request, "voice_id", str)
        reply = request.params.get("reply", "text")

        if reply not in {"text", "audio"}:
            raise LinkOperationError("bad_request", "'reply' must be text or audio.")

        pcm, language = store.finish(session.device.device_id, voice_id)

        if not pcm:
            raise LinkOperationError("voice_empty", "The recording was empty.")

        session.emit("thinking", phase="transcribing", progress=None)
        transcript = _transcribe_privately(stt, pcm, language)

        if not transcript:
            return {
                "transcript": "",
                "answer": "",
                "heard_nothing": True,
                "refused_action": False,
                "performed_actions": [],
                "reply_audio_chunks": 0,
            }

        result = dict(answer(session, transcript))
        result["transcript"] = transcript
        result.setdefault("performed_actions", [])
        result["reply_audio_chunks"] = 0

        text = str(result.get("answer", "")).strip()

        if reply == "audio" and speak is not None and text:
            try:
                result["reply_audio_chunks"] = _speak_back(session, speak, text)
            except Exception:
                # A voice that fails must not cost the user the answer: the
                # text is already here, and the phone shows it.
                result["reply_audio_error"] = True

        return result

    return {
        LinkOp.VOICE_BEGIN: begin,
        LinkOp.VOICE_CHUNK: chunk,
        LinkOp.VOICE_END: end,
    }


def _transcribe_privately(stt: Transcribe, pcm: bytes, language: str) -> str:
    """Write the recording where only this user can read it, transcribe, delete."""

    with tempfile.TemporaryDirectory(prefix="qronos-link-voice-") as folder:
        path = Path(folder) / "question.wav"
        write_wav(path, pcm)

        try:
            return str(stt(path, language)).strip()
        finally:
            path.unlink(missing_ok=True)


def _speak_back(session: LinkSession, speak: Speak, text: str) -> int:
    """Synthesise the answer and stream it to the phone as events."""

    path = Path(speak(text))

    try:
        audio = path.read_bytes()
    finally:
        path.unlink(missing_ok=True)

    if len(audio) > MAX_REPLY_AUDIO_BYTES:
        # Too long to send down a mobile link. The text answer still goes.
        return 0

    pieces = [
        audio[offset: offset + REPLY_CHUNK_BYTES]
        for offset in range(0, len(audio), REPLY_CHUNK_BYTES)
    ]

    for seq, piece in enumerate(pieces):
        session.emit(
            "voice_reply",
            seq=seq,
            format="wav",
            data=base64.b64encode(piece).decode("ascii"),
            last=seq == len(pieces) - 1,
        )

    return len(pieces)
