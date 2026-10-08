"""
A disposable stand-in for the Qronos user runtime, run as a normal child
process by tests/test_supervisor.py. It touches no device, model or user data.

Usage: python supervised_child.py MODE [RECORD_FILE]

    beat       connect, beat until asked to shut down, record it, exit 0
    crash      connect, then exit 3 at once
    hang       connect, then never beat again
    silent     never connect
    stall-auth open the pipe as the launched process, never authenticate
    wrong-key  connect with a key that is not the session key
    imposter   hand the bootstrap to a grandchild that connects instead
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.supervisor import SupervisedRuntimeClient, read_bootstrap  # noqa: E402


def main() -> int:
    mode = sys.argv[1]
    record = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if mode == "imposter-grandchild":
        bootstrap = json.loads(sys.stdin.readline())
        bootstrap["key"] = bytes.fromhex(bootstrap["key"])
        client = SupervisedRuntimeClient(bootstrap, on_shutdown=lambda reason: None, heartbeat_interval_seconds=0.05)
        try:
            client.start(timeout=5)
        except Exception:
            return 4
        time.sleep(5)
        return 0

    bootstrap = read_bootstrap(sys.stdin.buffer)

    if mode == "silent":
        time.sleep(30)
        return 0

    if mode == "stall-auth":
        # Open the pipe as the right process, then never answer the challenge.
        from multiprocessing.connection import Client

        held = Client(bootstrap["address"], family="AF_PIPE")
        time.sleep(30)
        held.close()
        return 0

    if mode == "imposter":
        grandchild = subprocess.Popen(
            [sys.executable, __file__, "imposter-grandchild"], stdin=subprocess.PIPE,
        )
        grandchild.stdin.write((json.dumps({**bootstrap, "key": bootstrap["key"].hex()}) + "\n").encode())
        grandchild.stdin.close()
        grandchild.wait(10)
        time.sleep(30)
        return 0

    if mode == "wrong-key":
        bootstrap["key"] = os.urandom(32)

    stopped = threading.Event()
    reasons: list[str] = []

    def on_shutdown(reason: str) -> None:
        reasons.append(reason)
        stopped.set()

    client = SupervisedRuntimeClient(bootstrap, on_shutdown=on_shutdown, heartbeat_interval_seconds=0.05)

    try:
        client.start(timeout=5)
    except Exception:
        return 5

    if mode == "crash":
        return 3

    if mode == "hang":
        client._stopping.set()  # stop beating, keep the process alive
        time.sleep(30)
        return 0

    stopped.wait(30)
    if record is not None:
        record.write_text(json.dumps({"pid": os.getpid(), "reasons": reasons}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
