"""
A disposable stand-in for the runtime bridge under supervision.

Started by the supervisor relay in tests/test_supervisor_relay.py exactly as
the real bridge is: the supervisor bootstrap is the first line on standard
input, the child joins the supervisor over the real authenticated channel
(``core.supervisor.SupervisedRuntimeClient``), then speaks the bridge's newline
JSON on standard input and output. It touches no device, model, application
or user data; the only file it may write is the marker file the test names.

Mode (first argument):

    echo       answer "ping" with runtime_pong, echo "echo" payloads back,
               spend 3 s inside a "block" command, stop on "shutdown" (default)
    crash      say ready, then exit with code 3
    silent     say ready, then never beat again (a hung runtime)
    flood      say ready, then print one output line larger than the relay allows
    linger     like echo, but ignore "shutdown" on standard input
    chatty     like echo, while another thread prints 4 KiB lines without end
    deaf       say ready, keep beating, never read standard input again
    act        like echo; "act" appends one line to $QRONOS_TEST_RELAY_MARKER
               and then the process dies at once, before any reply

Generation-aware modes (generation N is claimed with an exclusive marker file
gen-N in $QRONOS_TEST_RELAY_DIR; generation 2 and later behave like echo and
write gen-N-ready once they have said ready):

    flood-then-die  gen 1: a thread writes ~20 MiB of "old_output" lines (more
                    than the relay's output budget); the process dies after 4 s
    deaf-then-die   gen 1: never reads stdin; dies when $QRONOS_TEST_RELAY_DIR/die exists
    cut-then-die    gen 1: writes one line that is not valid UTF-8, then half a
                    line with no newline, and dies
    noconnect       reads its bootstrap and never joins the supervisor (120 s)
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.supervisor import SupervisedRuntimeClient, read_bootstrap  # noqa: E402

_OUT = threading.Lock()


def emit(event_type: str, status: str, message: str) -> None:
    with _OUT:
        sys.stdout.write(json.dumps({"eventType": event_type, "status": status, "message": message}) + "\n")
        sys.stdout.flush()


def chatter() -> None:
    line = "c" * 4096
    while True:
        emit("chatter", "ready", line)


def claim_generation() -> int:
    folder = os.environ.get("QRONOS_TEST_RELAY_DIR", "")
    if not folder:
        return 1
    for number in range(1, 50):
        marker = Path(folder) / f"gen-{number}"
        try:
            os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            continue
        marker.write_text(str(os.getpid()))
        return number
    raise SystemExit(9)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "echo"
    stdin = sys.stdin.buffer
    bootstrap = read_bootstrap(stdin)
    generation = claim_generation()
    if mode == "noconnect":
        time.sleep(120)
        return 0
    if generation > 1 and mode in ("flood-then-die", "deaf-then-die", "cut-then-die"):
        mode = "echo"
    interval = 3600.0 if mode == "silent" else 0.2
    busy_since: list[float] = []
    client = SupervisedRuntimeClient(
        bootstrap, on_shutdown=lambda reason: None, heartbeat_interval_seconds=interval,
        busy_seconds=lambda: time.monotonic() - busy_since[0] if busy_since else 0,
    )
    client.start(timeout=20)
    emit("runtime_ready", "ready", json.dumps({"pid": os.getpid(), "mode": mode, "generation": generation}))
    if generation > 1 and os.environ.get("QRONOS_TEST_RELAY_DIR"):
        (Path(os.environ["QRONOS_TEST_RELAY_DIR"]) / f"gen-{generation}-ready").write_text("1")

    if mode == "flood-then-die":
        def flood() -> None:
            for index in range(320):
                emit("old_output", "ready", f"OLD-{index}-" + "o" * 65000)

        threading.Thread(target=flood, daemon=True).start()
        time.sleep(4)
        os._exit(3)

    if mode == "deaf-then-die":
        marker = Path(os.environ["QRONOS_TEST_RELAY_DIR"]) / "die"
        deadline = time.monotonic() + 120
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os._exit(3)

    if mode == "cut-then-die":
        with _OUT:
            sys.stdout.buffer.write(b'{"eventType":"bad","message":"\xff\xfe"}\n')
            sys.stdout.buffer.write(b'{"eventType":"cut_off","status":"ready","message":"tail-')
            sys.stdout.flush()
        time.sleep(0.3)
        os._exit(3)

    if mode == "crash":
        os._exit(3)

    if mode == "flood":
        from core.supervisor_relay import MAX_LINE_BYTES

        sys.stdout.write("x" * (MAX_LINE_BYTES + 10) + "\n")
        sys.stdout.flush()
        emit("after_flood", "ready", "still here")

    if mode == "chatty":
        threading.Thread(target=chatter, daemon=True).start()

    if mode == "deaf":
        while True:
            time.sleep(1)

    try:
        for raw in stdin:
            payload = json.loads(raw)
            command = payload.get("command")
            if command == "ping":
                emit("runtime_pong", "ready", str(os.getpid()))
            elif command == "block":
                busy_since.append(time.monotonic())
                time.sleep(3)
                busy_since.clear()
                emit("unblocked", "ready", "")
            elif command == "act" and mode == "act":
                with open(os.environ["QRONOS_TEST_RELAY_MARKER"], "a", encoding="utf-8") as marker:
                    marker.write(f"{os.getpid()}\n")
                os._exit(3)  # acted, then gone before any reply
            elif command == "echo":
                emit("echo", "ready", payload.get("text", ""))
            elif command == "shutdown" and mode != "linger":
                emit("runtime_stopping", "stopping", str(os.getpid()))
                return 0
        if mode == "linger":
            while True:
                time.sleep(1)
        return 0
    finally:
        client.stop("fixture exiting")


if __name__ == "__main__":
    raise SystemExit(main())
