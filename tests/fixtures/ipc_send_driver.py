"""
Drives the "peer stops reading" scenarios in its own process, so the test can
put a hard outer timeout around them. Run by tests/test_supervisor_ipc.py.

Prints ``PEER <pid>`` (flushed) as soon as each owned peer child exists, so the
test can find and reap it even if this driver were killed, then one final JSON
line with what happened. Every peer it starts is killed and reaped here too.
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

from core.supervisor_ipc import ChannelError, ChannelServer, ChannelTimeout, new_key  # noqa: E402
from tests.fixtures.child_python import child_python  # noqa: E402
from tests.fixtures.ipc_peer_child import DRIVER_SENDS, PEER_SENDS  # noqa: E402

PEER = Path(__file__).with_name("ipc_peer_child.py")
TEXT = "x" * 4000


def session(peers: list[subprocess.Popen]):
    key = new_key()
    server = ChannelServer("supervisor", key, receive=PEER_SENDS, send=DRIVER_SENDS)
    executable, environment = child_python()
    peer = subprocess.Popen([executable, str(PEER)], stdin=subprocess.PIPE, env=environment)
    peers.append(peer)
    print(f"PEER {peer.pid}", flush=True)
    peer.stdin.write((json.dumps({"address": server.address, "key": key.hex(), "serverPid": os.getpid()}) + "\n").encode())
    peer.stdin.flush()
    channel = server.accept(15, expected_pid=peer.pid)
    hello = channel.receive(10)
    assert hello == {"kind": "hello", "pid": peer.pid}
    return server, channel


def repeated_sends(peers):
    """Keep sending full-size messages to a peer that never reads."""
    server, channel = session(peers)
    sent, timed_out, last_elapsed = 0, False, None
    try:
        for _ in range(64):
            started = time.monotonic()
            try:
                channel.send("note", timeout=1.0, text=TEXT)
            except ChannelTimeout:
                timed_out, last_elapsed = True, time.monotonic() - started
                break
            sent += 1
        try:
            channel.send("note", timeout=1.0, text="after")
            after = "sent"
        except ChannelTimeout:
            after = "timeout"
        except ChannelError:
            after = "refused"
        return {"sent_before_block": sent, "timed_out": timed_out, "blocked_send_seconds": last_elapsed,
                "after_timeout": after}
    finally:
        channel.close()
        server.close()


def close_during_send(peers):
    """Close the channel from another thread while a send is waiting."""
    server, channel = session(peers)
    outcome: dict[str, object] = {}

    def blocked_sender():
        started = time.monotonic()
        try:
            for _ in range(64):
                channel.send("note", timeout=3.0, text=TEXT)
            outcome["result"] = "all sent"
        except ChannelTimeout:
            outcome["result"] = "timeout"
        except ChannelError:
            outcome["result"] = "error"
        outcome["sender_seconds"] = time.monotonic() - started

    sender = threading.Thread(target=blocked_sender, daemon=True)
    sender.start()
    time.sleep(0.8)  # the sender is now waiting on a full buffer
    started = time.monotonic()
    channel.close()
    outcome["close_seconds"] = time.monotonic() - started
    sender.join(10)
    outcome["sender_finished"] = not sender.is_alive()
    server.close()
    return outcome


def main() -> int:
    peers: list[subprocess.Popen] = []
    result: dict[str, object] = {"driver_pid": os.getpid()}
    try:
        result["repeated"] = repeated_sends(peers)
        result["close"] = close_during_send(peers)
    finally:
        for peer in peers:
            try:
                peer.stdin.close()
            except Exception:
                pass
            if peer.poll() is None:
                peer.kill()
            try:
                peer.wait(10)
            except subprocess.TimeoutExpired:
                pass
        result["peers"] = [{"pid": peer.pid, "exited": peer.poll() is not None} for peer in peers]
        print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
