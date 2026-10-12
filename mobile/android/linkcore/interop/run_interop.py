"""
Run the phone's Kotlin link client against the real PC link server.

Developer tooling, not product: it starts, on loopback only,

    the Qronos link server (core/link_server.py, Python + OpenSSL),
    the real relay (relay/qronos_relay) with a throwaway CA,
    the PC's relay dialler,

enrols one device with a fresh random key in an in-memory registry, and then
runs ``app.qronos.link.InteropMainKt`` twice — straight to the server, and
through the relay with the outer TLS verified against the throwaway CA. The
"assistant", speech-to-text and text-to-speech are stand-ins that check what
they receive; no model, microphone or phone is involved.

    python mobile/android/linkcore/interop/run_interop.py \
        --java <path to java> --classpath <linkcore main+test, kotlin-stdlib,
        bouncycastle, org.json> [--evidence out.json]

Run from the repository root. Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

import socket  # noqa: E402

from core.link_audit import AuditEvent, AuditLog  # noqa: E402
from core.link_capability import LinkOp, LinkScope  # noqa: E402
from core.link_devices import DeviceRegistry  # noqa: E402
from core.link_handlers import ToolFreeAsk, default_handlers  # noqa: E402
from core.link_server import LinkServer, LinkSettings  # noqa: E402
from core.link_tunnel import RelayDialler, TunnelSettings, connect_relay  # noqa: E402
from tests.fixtures.link_real_relay import RunningRelay  # noqa: E402


def chat(messages):
    return "پاسخ آزمایشی به: " + messages[-1]["content"]


class Stt:
    def __init__(self) -> None:
        self.seen: list[dict[str, int]] = []

    def __call__(self, wav_path: Path, language: str) -> str:
        with wave.open(str(wav_path), "rb") as audio:
            self.seen.append(
                {
                    "rate": audio.getframerate(),
                    "channels": audio.getnchannels(),
                    "frames": audio.getnframes(),
                }
            )

        return "سلام قرونوس"


def make_tts(folder: Path):
    def speak(text: str) -> Path:
        path = folder / f"reply-{secrets.token_hex(4)}.wav"

        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(24_000)
            out.writeframes(b"\x00\x01" * 24_000)

        return path

    return speak


DROP_ONCE = "یک پرسش ساده برای آزمودن تکرار"


class DropOnceAsk:
    """
    Wraps the ask handler. For the drop-once question it runs the command, then
    cuts the connection before the answer can be sent — the realistic failure
    a retry has to survive — and counts how many times it really ran.
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.executions = 0

    def __call__(self, session, request):
        result = self.inner(session, request)

        if request.params.get("question") == DROP_ONCE:
            self.executions += 1
            session.connection.shutdown(socket.SHUT_RDWR)

        return result


def run_client(java: str, classpath: str, args: list[str]) -> list[dict]:
    completed = subprocess.run(
        [java, "-Dstdout.encoding=UTF-8", "-cp", classpath, "app.qronos.link.InteropMainKt", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    results = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]

    if completed.returncode != 0 or not results or not all(r["ok"] for r in results):
        sys.stderr.write(completed.stderr[-4000:])

    return results


def _refused_at_connect(results: list[dict]) -> bool:
    """The client failed before any check passed: no session was opened."""

    return len(results) == 1 and results[0]["check"] == "fatal" and not results[0]["ok"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--java", required=True)
    parser.add_argument("--classpath", required=True)
    parser.add_argument("--evidence")
    options = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="qronos-interop-") as scratch:
        folder = Path(scratch)
        relay = RunningRelay(folder / "relay")
        registry = DeviceRegistry(path=None)
        audit = AuditLog(path=None)
        stt = Stt()

        remote_ask = ToolFreeAsk(chat)
        server = LinkServer(
            registry=registry,
            audit=audit,
            handlers=default_handlers(
                ask=remote_ask,
                remote_ask=remote_ask,
                transcribe=stt,
                speak=make_tts(folder),
            ),
            settings=LinkSettings(
                host="127.0.0.1",
                port=0,
                allowed_scopes=frozenset({LinkScope.LOCAL_NETWORK, LinkScope.REMOTE_TUNNEL}),
                remote_burst=100,
            ),
        )
        drop_once = DropOnceAsk(server.handlers[LinkOp.ASK])
        server.handle(LinkOp.ASK, drop_once)
        server.start()

        record = registry.create("interop phone")
        registry.activate(record.device_id)
        registry.set_remote_enabled(record.device_id, True)

        dialler = RelayDialler(
            server,
            relay.endpoint,
            settings=TunnelSettings(refresh=0.1, backoff_initial=0.1, backoff_max=0.5),
            connector=connect_relay,
        )
        dialler.start()

        try:
            deadline = time.monotonic() + 10

            while record.device_id not in dialler.parked_devices() or relay.stats()["parked"] < 1:
                if time.monotonic() > deadline:
                    raise RuntimeError("the PC never parked at the relay")

                time.sleep(0.05)

            key = record.secret.hex()
            direct = run_client(
                options.java,
                options.classpath,
                ["direct", "127.0.0.1", str(server.address[1]), record.device_id, key],
            )
            relayed = run_client(
                options.java,
                options.classpath,
                [
                    "relay",
                    "127.0.0.1",
                    str(relay.port),
                    relay.certs.server_name,
                    str(relay.certs.ca_file),
                    record.device_id,
                    key,
                ],
            )

            # The other side of the proof: the wrong key cannot complete the
            # handshake, and a revoked device gets nowhere through the relay.
            wrong_key = run_client(
                options.java,
                options.classpath,
                ["direct", "127.0.0.1", str(server.address[1]), record.device_id, secrets.token_hex(32)],
            )
            server.revoke(record.device_id)
            dialler.refresh()
            time.sleep(0.5)
            revoked = run_client(
                options.java,
                options.classpath,
                [
                    "relay",
                    "127.0.0.1",
                    str(relay.port),
                    relay.certs.server_name,
                    str(relay.certs.ca_file),
                    record.device_id,
                    key,
                ],
            )
        finally:
            dialler.stop()
            server.stop()
            relay.close()

        scopes = sorted(
            {r.scope.value for r in audit.records() if r.event is AuditEvent.HANDSHAKE_OK and r.scope}
        )
        evidence = {
            "client": "app.qronos.link (Kotlin) + BouncyCastle bctls",
            "server": "core.link_server (Python ssl / OpenSSL)",
            "direct": direct,
            "relay": relayed,
            "wrong_key": wrong_key,
            "revoked_via_relay": revoked,
            "server_handshake_scopes": scopes,
            "stt_saw": stt.seen,
            "drop_once_executions": drop_once.executions,
        }

    ok = (
        direct
        and relayed
        and all(r["ok"] for r in direct + relayed)
        and scopes == ["local_network", "remote_tunnel"]
        and drop_once.executions == 2  # once per path, never twice
        and _refused_at_connect(wrong_key)
        and _refused_at_connect(revoked)
        and all(s["rate"] == 16_000 and s["channels"] == 1 for s in stt.seen)
    )
    evidence["passed"] = bool(ok)
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2)

    if options.evidence:
        Path(options.evidence).write_text(rendered, encoding="utf-8")

    sys.stdout.reconfigure(encoding="utf-8")
    print(rendered)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
