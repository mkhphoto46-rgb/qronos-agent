"""
A disposable channel peer that authenticates and then stops reading.

Run as a normal child by tests/fixtures/ipc_send_driver.py. It reads one JSON
line from standard input (address, key, server pid), connects through the real
``core.supervisor_ipc.connect`` (identity check and handshake), says hello, and
then never reads again, so the driver's writes fill the pipe buffer. It exits
when its standard input closes or after 60 seconds; the driver also kills it.
It touches no device, model, application or user data.
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

from core.supervisor_ipc import MessageSchema, connect  # noqa: E402

DRIVER_SENDS = MessageSchema({"note": {"text": "str"}})
PEER_SENDS = MessageSchema({"hello": {"pid": "int"}})


def main() -> int:
    bootstrap = json.loads(sys.stdin.readline())
    channel = connect(
        bootstrap["address"], bytes.fromhex(bootstrap["key"]), receive=DRIVER_SENDS, send=PEER_SENDS,
        expected_server_pid=bootstrap["serverPid"], timeout=10,
    )
    channel.send("hello", pid=os.getpid())

    # Stop reading entirely. Leave when the driver closes stdin, or at 60 s.
    done = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.read(), done.set()), daemon=True).start()
    done.wait(60)
    time.sleep(0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
