"""
A disposable stand-in for the runtime bridge under supervision.

Started by the supervisor relay in tests/test_supervisor_relay.py exactly as
the real bridge is: the supervisor bootstrap is the first line on standard
input, the child joins the supervisor over the real authenticated channel
(``core.supervisor.SupervisedRuntimeClient``), then speaks the bridge's newline
JSON on standard input and output. It touches no device, model, application
or user data.

Mode (first argument):

    echo       answer "ping" with runtime_pong, echo "echo" payloads back,
               spend 3 s inside a "block" command, stop on "shutdown" (default)
    crash      say ready, then exit with code 3
    silent     say ready, then never beat again (a hung runtime)
    flood      say ready, then print one output line larger than the relay allows
    linger     like echo, but ignore "shutdown" on standard input
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.supervisor import SupervisedRuntimeClient, read_bootstrap  # noqa: E402


def emit(event_type: str, status: str, message: str) -> None:
    sys.stdout.write(json.dumps({"eventType": event_type, "status": status, "message": message}) + "\n")
    sys.stdout.flush()


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "echo"
    stdin = sys.stdin.buffer
    bootstrap = read_bootstrap(stdin)
    interval = 3600.0 if mode == "silent" else 0.2
    busy_since: list[float] = []
    client = SupervisedRuntimeClient(
        bootstrap, on_shutdown=lambda reason: None, heartbeat_interval_seconds=interval,
        busy_seconds=lambda: time.monotonic() - busy_since[0] if busy_since else 0,
    )
    client.start(timeout=20)
    emit("runtime_ready", "ready", json.dumps({"pid": os.getpid(), "mode": mode}))

    if mode == "crash":
        os._exit(3)

    if mode == "flood":
        from core.supervisor_relay import MAX_LINE_BYTES

        sys.stdout.write("x" * (MAX_LINE_BYTES + 10) + "\n")
        sys.stdout.flush()
        emit("after_flood", "ready", "still here")

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
