"""
R3 for the elevated action broker: its writer admission runs only AFTER the parent runtime is
authenticated, and BEFORE any broker store (approval secret check, single-use journal, audit).

Native boundary (audited): a real private ChannelServer pipe in this process plays the runtime
(it releases the session key as BrokerSession does); the broker's serve_once runs on a thread
with fake identity checks; the secret check, journal and audit are recording fakes in tmp. No
child process, UAC, SCM, known folder, real profile, production mutex, store or network.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from core.action_broker import FROM_RUNTIME, TO_RUNTIME, ConsumedApprovals, serve_once
from core.startup_admission import StartupRefused
from core.supervisor_ipc import ChannelError, ChannelServer, new_key

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="broker sessions are Windows named pipes")


class Recorder:
    def __init__(self, tmp: Path) -> None:
        self.events: list[str] = []
        self.consumed_dir = tmp / "consumed"
        self.audit_file = tmp / "audit.jsonl"

    def secret(self, value: str) -> bool:
        self.events.append("secret-store")
        return False

    def audit(self, record: dict) -> None:
        self.events.append("audit")
        self.audit_file.write_text("x", encoding="utf-8")

    def journal(self) -> ConsumedApprovals:
        return ConsumedApprovals(self.consumed_dir)


def runtime_side(server: ChannelServer, seen: dict, release_key: bool = True) -> threading.Thread:
    """The runtime's half: accept the broker, then wait for its hello (and send a request if it comes)."""
    def run() -> None:
        try:
            channel = server.accept(10, expected_pid=os.getpid(), release_key=release_key)
        except ChannelError as error:
            seen["accept"] = type(error).__name__
            return
        try:
            hello = channel.receive(5)
            seen["hello"] = hello["kind"]
            channel.send("request", action={"kind": "printer.spooler_restart", "params": {}}, approval={},
                         secret="wrong")
            seen["reply"] = channel.receive(5)["kind"]
        except ChannelError as error:
            seen["after"] = type(error).__name__
        finally:
            channel.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def serve(server: ChannelServer, recorder: Recorder, admit, *, identity: bool = True):
    return serve_once(server.address, os.getpid(), consumed=recorder.journal(), audit=recorder.audit,
                      owner_check=lambda pid: identity, identity_check=lambda pid: identity,
                      verify_secret=recorder.secret, admit=admit)


def test_admission_follows_authentication_and_precedes_every_broker_store(tmp_path):
    recorder = Recorder(tmp_path)
    server = ChannelServer("action-broker", new_key(), receive=TO_RUNTIME, send=FROM_RUNTIME)
    seen: dict = {}
    try:
        thread = runtime_side(server, seen)

        def admit():
            # The parent is authenticated (its key was received) and nothing is stored yet.
            recorder.events.append("admit")
            assert recorder.events == ["admit"] and not recorder.consumed_dir.exists()

        serve(server, recorder, admit)
        thread.join(10)
    finally:
        server.close()
    assert recorder.events[0] == "admit" and seen.get("hello") == "hello"


def test_a_refused_admission_opens_no_broker_store_and_sends_no_hello(tmp_path):
    recorder = Recorder(tmp_path)
    server = ChannelServer("action-broker", new_key(), receive=TO_RUNTIME, send=FROM_RUNTIME)
    seen: dict = {}

    def refuse():
        recorder.events.append("admit")
        raise StartupRefused("update_in_progress")

    try:
        thread = runtime_side(server, seen)
        with pytest.raises(StartupRefused):
            serve(server, recorder, refuse)
        thread.join(10)
    finally:
        server.close()
    assert recorder.events == ["admit"]  # no secret store, no audit
    assert not recorder.consumed_dir.exists() and not recorder.audit_file.exists()
    assert "hello" not in seen and seen.get("after") == "ChannelError"  # the runtime sees "unavailable"


def test_an_unauthenticated_parent_never_reaches_admission(tmp_path):
    recorder = Recorder(tmp_path)
    server = ChannelServer("action-broker", new_key(), receive=TO_RUNTIME, send=FROM_RUNTIME)
    admitted = []
    try:
        with pytest.raises(ChannelError):
            serve(server, recorder, lambda: admitted.append(True), identity=False)  # not the installed runtime
    finally:
        server.close()
    assert admitted == [] and recorder.events == []


def test_a_same_user_parent_whose_image_is_not_the_installed_runtime_never_reaches_admission(tmp_path):
    """Media17 G1: the owner check passes (same user) but the image proof fails. The image proof alone must stop
    it: nothing is admitted, stored or audited, and the runtime side never gets a hello."""
    recorder = Recorder(tmp_path)
    server = ChannelServer("action-broker", new_key(), receive=TO_RUNTIME, send=FROM_RUNTIME)
    seen: dict = {}
    admitted = []
    try:
        thread = runtime_side(server, seen)
        with pytest.raises(ChannelError):
            serve_once(server.address, os.getpid(), consumed=recorder.journal(), audit=recorder.audit,
                       owner_check=lambda pid: True, identity_check=lambda pid: False,
                       verify_secret=recorder.secret, admit=lambda: admitted.append(True))
        thread.join(15)
    finally:
        server.close()
    assert admitted == [] and recorder.events == [] and not recorder.consumed_dir.exists()
    assert not recorder.audit_file.exists() and "hello" not in seen


def test_a_pipe_that_releases_no_key_never_reaches_admission(tmp_path):
    """A pipe whose server is not an admitted bridge's broker session (no key released) is refused first."""
    recorder = Recorder(tmp_path)
    server = ChannelServer("action-broker", new_key(), receive=TO_RUNTIME, send=FROM_RUNTIME)
    seen: dict = {}
    admitted = []
    try:
        thread = runtime_side(server, seen, release_key=False)
        with pytest.raises(ChannelError):
            serve(server, recorder, lambda: admitted.append(True))
        thread.join(15)
    finally:
        server.close()
    assert admitted == [] and recorder.events == [] and not recorder.consumed_dir.exists()
