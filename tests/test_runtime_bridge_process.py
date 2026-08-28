"""
The bridge as a process, not as a set of functions.

``tests/test_runtime_bridge.py`` calls into the module. This starts it the way
``desktop/src-tauri/src/runtime/mod.rs`` does — a child process with piped
stdin and stdout — and exercises the contract between them. Several things can
only break at that level: line buffering, text encoding across a Windows pipe,
whether a bad line kills the reader loop, and whether the process actually
exits when it is told to.

Deliberately excluded: push-to-talk. On a machine where the speech runtime is
installed, asking for a turn would open the microphone and record. A test suite
must not do that, so the voice path is covered in-process with fakes and this
file stays on the paths that touch nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

# Generous: the child has to start an interpreter and import the whole of core,
# which on a cold filesystem is not instant. Short enough that a hang fails the
# suite rather than stalling it.
STARTUP_TIMEOUT = 60.0
REPLY_TIMEOUT = 30.0


class BridgeProcess:
    """A running bridge, with the plumbing to talk to it."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "core.runtime_bridge"],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def send(self, payload: dict) -> None:
        self.send_raw(json.dumps(payload))

    def send_raw(self, line: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def read_event(self, timeout: float = REPLY_TIMEOUT) -> dict | None:
        assert self.process.stdout is not None
        deadline = time.time() + timeout

        while time.time() < deadline:
            line = self.process.stdout.readline()

            if not line:
                return None

            if line.strip():
                return json.loads(line)

        return None

    def read_until(
        self,
        event_type: str,
        timeout: float = REPLY_TIMEOUT,
    ) -> dict | None:
        """
        Read past events that are not the one being waited for.

        One command does not always produce exactly one event, so a caller
        cannot pair them up positionally.
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            event = self.read_event(timeout=max(0.1, deadline - time.time()))

            if event is None:
                return None

            if event["eventType"] == event_type:
                return event

        return None

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()

        for stream in (
            self.process.stdin,
            self.process.stdout,
            self.process.stderr,
        ):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


class BridgeProcessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = BridgeProcess()
        ready = self.bridge.read_event(timeout=STARTUP_TIMEOUT)

        self.assertIsNotNone(ready, "the bridge never announced itself")
        self.assertEqual(ready["eventType"], "runtime_ready")

    def tearDown(self) -> None:
        self.bridge.close()


class TestTheBridgeSpeaks(BridgeProcessTestCase):
    def test_it_announces_itself_on_startup(self) -> None:
        # Asserted in setUp. The desktop waits for this before sending
        # anything, so a bridge that starts silently would look like a hang.
        self.assertIsNone(self.bridge.process.poll())

    def test_ping_is_answered(self) -> None:
        self.bridge.send({"command": "ping"})

        self.assertIsNotNone(self.bridge.read_until("runtime_pong"))

    def test_one_event_is_one_line(self) -> None:
        # The reader on the Rust side splits on newlines. An event spanning two
        # lines would be two unparseable halves.
        self.bridge.send({"command": "ping"})
        event = self.bridge.read_until("runtime_pong")

        self.assertEqual(len(json.dumps(event).splitlines()), 1)


class TestBadInputDoesNotKillIt(BridgeProcessTestCase):
    """
    Every one of these arrives from another process over a pipe.

    A reader loop that dies on a malformed line takes the whole voice runtime
    with it, and the desktop would see the pipe close rather than an error it
    could show anybody.
    """

    def test_malformed_json_becomes_an_error_event(self) -> None:
        self.bridge.send_raw("{ this is not json")

        self.assertIsNotNone(self.bridge.read_until("runtime_error"))
        self.assertIsNone(self.bridge.process.poll())

    def test_an_empty_command_is_rejected(self) -> None:
        self.bridge.send({"command": "   "})

        self.assertIsNotNone(self.bridge.read_until("runtime_error"))

    def test_an_unknown_command_warns(self) -> None:
        self.bridge.send({"command": "fly"})

        self.assertIsNotNone(self.bridge.read_until("runtime_warning"))

    def test_a_blank_line_is_skipped(self) -> None:
        self.bridge.send_raw("")
        self.bridge.send({"command": "ping"})

        self.assertIsNotNone(self.bridge.read_until("runtime_pong"))

    def test_a_bare_json_value_is_rejected(self) -> None:
        self.bridge.send_raw('"ping"')

        self.assertIsNotNone(self.bridge.read_until("runtime_error"))

    def test_it_still_works_after_all_of_them(self) -> None:
        for line in ("{ bad", '"bare"', "", "{}", '{"command": ""}'):
            self.bridge.send_raw(line)

        self.bridge.send({"command": "ping"})

        self.assertIsNotNone(self.bridge.read_until("runtime_pong"))
        self.assertIsNone(self.bridge.process.poll())


class TestActions(BridgeProcessTestCase):
    def test_an_unsupported_action_warns_rather_than_failing(self) -> None:
        # The desktop forwards every global hotkey it does not handle itself,
        # so the bridge sees actions it has no implementation for.
        self.bridge.send(
            {"command": "action", "actionId": "qronos.toggle_voice"}
        )

        self.assertIsNotNone(
            self.bridge.read_until("runtime_action_received")
        )
        self.assertIsNotNone(self.bridge.read_until("runtime_warning"))

    def test_an_action_with_no_id_is_an_error(self) -> None:
        self.bridge.send({"command": "action", "actionId": "  "})

        self.assertIsNotNone(self.bridge.read_until("runtime_error"))


class TestShutdown(BridgeProcessTestCase):
    def test_it_acknowledges_and_exits(self) -> None:
        self.bridge.send({"command": "shutdown"})

        self.assertIsNotNone(self.bridge.read_until("runtime_stopping"))
        self.assertEqual(self.bridge.process.wait(timeout=30), 0)

    def test_it_writes_nothing_to_stderr(self) -> None:
        # stderr is not part of the contract, and the desktop does not read it.
        # Anything appearing there is output that went missing.
        self.bridge.send({"command": "ping"})
        self.bridge.read_until("runtime_pong")
        self.bridge.send({"command": "shutdown"})
        self.bridge.process.wait(timeout=30)

        assert self.bridge.process.stderr is not None

        self.assertEqual(self.bridge.process.stderr.read().strip(), "")

    def test_a_closed_stdin_ends_it(self) -> None:
        # What happens when the desktop exits without saying goodbye. The
        # bridge must not linger as an orphan holding the microphone.
        assert self.bridge.process.stdin is not None
        self.bridge.process.stdin.close()

        self.assertEqual(self.bridge.process.wait(timeout=30), 0)


if __name__ == "__main__":
    unittest.main()
