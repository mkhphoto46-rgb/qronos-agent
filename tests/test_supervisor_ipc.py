"""
The authenticated local channel, including adversarial peers.

Every session test uses this test process's own fresh named pipes and its own
threads; nothing listens on a network or touches a device. Each adversarial
case asserts the wait it caused stayed within its deadline, so a regression
to an unbounded read fails here instead of hanging the suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Client, Listener
from pathlib import Path

import pytest

from core import supervisor_ipc as ipc
from core.supervisor_ipc import (
    MAX_MESSAGE_BYTES,
    ChannelError,
    ChannelServer,
    ChannelTimeout,
    MessageSchema,
    PeerRejected,
    connect,
    decode_message,
    encode_message,
    new_key,
    require_local_address,
)
from tests.fixtures.child_python import child_python

UP = MessageSchema({"hello": {"pid": "int", "protocol": "int"}, "note": {"text": "str"}})
DOWN = MessageSchema({"welcome": {"ok": "bool"}})
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="IPC sessions are Windows named pipes.")
ME = os.getpid()


# ------------------------------------------------------------------ schema


def test_a_message_has_exactly_its_declared_fields():
    assert UP.validate({"kind": "hello", "pid": 1, "protocol": 1})["pid"] == 1
    for bad in (
        {"kind": "hello", "pid": 1},
        {"kind": "hello", "pid": 1, "protocol": 1, "extra": 0},
        {"kind": "hello", "pid": "1", "protocol": 1},
        {"kind": "hello", "pid": True, "protocol": 1},
        {"kind": "unknown"},
        {"pid": 1},
        ["hello"],
    ):
        with pytest.raises(ChannelError):
            UP.validate(bad)


def test_schemas_refuse_invalid_kinds_and_types():
    with pytest.raises(ValueError):
        MessageSchema({"Bad Kind": {}})
    with pytest.raises(ValueError):
        MessageSchema({"ok": {"field": "list"}})


def test_oversized_non_json_and_non_finite_messages_are_refused():
    with pytest.raises(ChannelError):
        encode_message("note", {"text": "x" * MAX_MESSAGE_BYTES})
    with pytest.raises(ChannelError):
        decode_message(b"x" * (MAX_MESSAGE_BYTES + 1), UP)
    with pytest.raises(ChannelError):
        decode_message(b"\xff\xfe", UP)
    with pytest.raises(ChannelError):
        decode_message(b'{"kind":"hello","pid":NaN,"protocol":1}', UP)


def test_one_message_fits_an_empty_pipe_buffer():
    # Only the first send to an idle peer is helped by this; repeated sends
    # are bounded by the send deadline (see the "peer stops reading" tests).
    assert MAX_MESSAGE_BYTES < 8192 and ipc.DEFAULT_SEND_TIMEOUT_SECONDS > 0


# --------------------------------------------------------------- addresses


@windows_only
def test_only_qronos_local_addresses_are_accepted():
    address = ipc.new_address("supervisor")
    assert require_local_address(address) == address
    for bad in (("127.0.0.1", 8080), "localhost:8080", r"\\.\pipe\other", r"\\server\pipe\qronos-supervisor-" + "0" * 32, 7):
        with pytest.raises(ChannelError):
            require_local_address(bad)


def test_addresses_are_unguessable_and_purposes_are_checked():
    assert len({ipc.new_address("supervisor") for _ in range(20)}) == 20
    with pytest.raises(ValueError):
        ipc.new_address("Bad Purpose")


@windows_only
def test_keys_must_be_exactly_32_bytes():
    with pytest.raises(ChannelError):
        ChannelServer("supervisor", b"short", UP, DOWN)
    with pytest.raises(ChannelError):
        connect(ipc.new_address("supervisor"), b"short", DOWN, UP, expected_server_pid=ME)


def test_sessions_are_refused_off_windows(monkeypatch):
    monkeypatch.setattr(ipc.sys, "platform", "linux")
    with pytest.raises(ChannelError, match="Windows named pipes only"):
        ChannelServer("supervisor", new_key(), UP, DOWN)
    with pytest.raises(ChannelError, match="Windows named pipes only"):
        connect("/tmp/qronos-ipc-x/a.sock", new_key(), DOWN, UP, expected_server_pid=ME)


@windows_only
def test_connecting_to_a_non_local_address_never_opens_anything():
    with pytest.raises(ChannelError):
        connect(("127.0.0.1", 9), new_key(), DOWN, UP, expected_server_pid=ME)  # type: ignore[arg-type]


# --------------------------------------------------------------- helpers


class Peer(threading.Thread):
    """Runs ``body`` on its own thread and records what it returned or raised."""

    def __init__(self, body):
        super().__init__(daemon=True)
        self.body = body
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.body()
        except BaseException as error:  # noqa: BLE001 - recorded for the assertion
            self.error = error


def bounded(call, limit):
    """
    Run ``call`` on a watchdog thread; fail if it has not returned or raised
    within ``limit`` seconds. A wait that ignores its deadline therefore fails
    this test instead of hanging the run (the stuck daemon thread is abandoned).
    """
    worker = Peer(call)
    worker.start()
    worker.join(limit)
    assert not worker.is_alive(), "a channel wait outlived its deadline"
    if worker.error is not None:
        raise worker.error
    return worker.result


@pytest.fixture
def server():
    made = []

    def make(key=None):
        instance = ChannelServer("supervisor", key or new_key(), receive=UP, send=DOWN)
        made.append(instance)
        return instance

    yield make
    for instance in made:
        instance.close()


def client_of(srv, key=None, **kwargs):
    def body():
        channel = connect(srv.address, key or srv.key, receive=DOWN, send=UP, expected_server_pid=ME, **kwargs)
        channel.send("hello", pid=ME, protocol=1)
        reply = channel.receive(5)
        channel.close()
        return reply
    peer = Peer(body)
    peer.start()
    return peer


# ------------------------------------------------------- identity + auth


@windows_only
def test_the_launched_process_with_the_key_is_accepted(server):
    srv = server()
    peer = client_of(srv)
    channel = bounded(lambda: srv.accept(5, expected_pid=ME), 5)
    assert channel.receive(5) == {"kind": "hello", "pid": ME, "protocol": 1}
    channel.send("welcome", ok=True)
    peer.join(5)
    assert peer.result == {"kind": "welcome", "ok": True}
    channel.close()


@windows_only
def test_a_peer_without_the_key_is_refused(server):
    srv = server()
    peer = client_of(srv, key=new_key())
    with pytest.raises(ChannelError):
        bounded(lambda: srv.accept(3, expected_pid=ME), 4)
    peer.join(5)
    assert isinstance(peer.error, ChannelError)


@windows_only
def test_no_expected_process_means_nobody_is_accepted(server):
    srv = server()
    peer = client_of(srv)
    with pytest.raises(PeerRejected):
        bounded(lambda: srv.accept(1.0), 3)
    peer.join(5)


@windows_only
def test_the_real_pipe_query_refuses_a_different_process(server):
    srv = server()
    peer = client_of(srv)
    with pytest.raises(PeerRejected):
        bounded(lambda: srv.accept(1.0, expected_pid=ME + 1), 3)
    peer.join(5)


@windows_only
def test_an_unreadable_peer_identity_is_refused(server):
    srv = server()
    peer = client_of(srv)
    with pytest.raises(PeerRejected):
        bounded(lambda: srv.accept(1.0, expected_pid=ME, peer_pid=lambda connection: None), 3)
    peer.join(5)


@windows_only
def test_an_impostor_first_does_not_block_the_real_peer(server):
    srv = server()
    identities = iter([ME + 1, ME])
    impostor = Client(srv.address, family="AF_PIPE")  # connects first, holds the pipe
    peer = client_of(srv)
    try:
        channel = bounded(lambda: srv.accept(5, expected_pid=ME, peer_pid=lambda connection: next(identities)), 5)
        assert channel.receive(5)["kind"] == "hello" and srv.refused_peers == 1
        channel.send("welcome", ok=True)
        channel.close()
    finally:
        impostor.close()
        peer.join(5)


# ------------------------------------------------------ stalls and partial


@windows_only
def test_an_unauthenticated_peer_that_never_answers_cannot_stall_accept(server):
    srv = server()
    stalled = Client(srv.address, family="AF_PIPE")  # right process, never answers the challenge
    try:
        with pytest.raises(ChannelTimeout):
            bounded(lambda: srv.accept(1.0, expected_pid=ME), 2.5)
    finally:
        stalled.close()


@windows_only
def test_a_garbled_handshake_is_refused_at_once(server):
    srv = server()

    def garble():
        raw = Client(srv.address, family="AF_PIPE")
        raw.recv_bytes(256)
        raw.send_bytes(b"QIPC1-RESPONSE:" + b"\x00" * 64)
        time.sleep(1)
        raw.close()

    peer = Peer(garble)
    peer.start()
    with pytest.raises(ChannelError):
        bounded(lambda: srv.accept(5, expected_pid=ME), 3)
    peer.join(5)


@windows_only
def test_a_server_that_never_challenges_cannot_stall_connect():
    address = ipc.new_address("supervisor")
    listener = Listener(address, family="AF_PIPE")  # accepts, then says nothing
    held = Peer(listener.accept)
    held.start()
    try:
        with pytest.raises(ChannelTimeout):
            bounded(lambda: connect(address, new_key(), DOWN, UP, expected_server_pid=ME, timeout=1.0), 2.5)
    finally:
        held.join(5)
        if held.result is not None:
            held.result.close()
        listener.close()


@windows_only
@pytest.mark.parametrize("expected", [ME + 1, None])
def test_the_client_refuses_an_unexpected_or_unnamed_server(server, expected):
    srv = server()
    with pytest.raises(PeerRejected):
        bounded(lambda: connect(srv.address, srv.key, DOWN, UP, expected_server_pid=expected, timeout=1.0), 3)


@windows_only
def test_a_busy_pipe_cannot_hold_connect_past_its_deadline(server):
    srv = server()
    occupant = Client(srv.address, family="AF_PIPE")  # takes the only free instance
    try:
        with pytest.raises(ChannelTimeout):
            bounded(lambda: connect(srv.address, srv.key, DOWN, UP, expected_server_pid=ME, timeout=1.0), 2.5)
    finally:
        occupant.close()


@windows_only
def test_an_authenticated_peer_that_stays_silent_times_out(server):
    srv = server()
    holder = {}
    peer = Peer(lambda: holder.setdefault("channel", connect(srv.address, srv.key, DOWN, UP, expected_server_pid=ME)))
    peer.start()
    channel = srv.accept(5, expected_pid=ME)
    try:
        with pytest.raises(ChannelTimeout):
            bounded(lambda: channel.receive(0.3), 1.5)
    finally:
        channel.close()
        peer.join(5)
        holder["channel"].close()


@windows_only
def test_an_oversized_payload_is_refused_by_its_length_without_stalling(server):
    srv = server()
    holder = {}
    peer = Peer(lambda: holder.setdefault("channel", connect(srv.address, srv.key, DOWN, UP, expected_server_pid=ME)))
    peer.start()
    channel = srv.accept(5, expected_pid=ME)
    try:
        peer.join(5)
        holder["channel"].connection.send_bytes(b"x" * (MAX_MESSAGE_BYTES + 1))
        with pytest.raises(ChannelError):
            bounded(lambda: channel.receive(2), 2.5)
    finally:
        channel.close()
        holder["channel"].close()


@windows_only
def test_a_full_size_send_does_not_wait_for_a_peer_that_never_reads(server):
    srv = server()
    holder = {}
    peer = Peer(lambda: holder.setdefault("channel", connect(srv.address, srv.key, DOWN, UP, expected_server_pid=ME)))
    peer.start()
    channel = srv.accept(5, expected_pid=ME)
    try:
        peer.join(5)
        text = "x" * (MAX_MESSAGE_BYTES - 40)
        bounded(lambda: holder["channel"].send("note", text=text), 1.0)
        assert channel.receive(2)["text"] == text
    finally:
        channel.close()
        holder["channel"].close()


def idle_and_writer(srv):
    """An accepted channel that never reads, and its authenticated peer that writes to it."""
    holder = {}
    peer = Peer(lambda: holder.setdefault("channel", connect(srv.address, srv.key, DOWN, UP, expected_server_pid=ME)))
    peer.start()
    idle = srv.accept(5, expected_pid=ME)
    peer.join(5)
    return idle, holder["channel"]


@windows_only
def test_repeated_sends_to_a_peer_that_stops_reading_time_out_and_close(server):
    idle, writer = idle_and_writer(server())
    text = "x" * (MAX_MESSAGE_BYTES - 40)
    try:
        def flood():
            sent = 0
            while True:
                started = time.monotonic()
                try:
                    writer.send("note", timeout=0.5, text=text)  # the accepted side never reads
                except ChannelTimeout:
                    return sent, time.monotonic() - started
                sent += 1

        sent, waited = bounded(flood, 5)
        assert sent >= 1 and 0.4 <= waited < 2.0  # the buffer filled, then the wait kept its deadline
        with pytest.raises(ChannelError):
            bounded(lambda: writer.send("note", timeout=0.5, text="after"), 1.0)  # broken, refused at once
    finally:
        idle.close()
        writer.close()


@windows_only
def test_closing_cancels_a_send_that_is_waiting_on_a_full_buffer(server):
    idle, writer = idle_and_writer(server())
    text = "x" * (MAX_MESSAGE_BYTES - 40)

    def flood():
        while True:
            writer.send("note", timeout=30, text=text)

    sender = Peer(flood)
    sender.start()
    try:
        time.sleep(1.0)  # well past the two messages the buffer holds
        assert sender.is_alive()  # waiting on its 30 s deadline
        started = time.monotonic()
        bounded(writer.close, 1.0)
        sender.join(2.0)
        assert not sender.is_alive() and time.monotonic() - started < 2.0
        assert isinstance(sender.error, ChannelError) and not isinstance(sender.error, ChannelTimeout)
        assert writer.connection.closed  # released by the sender as it left
    finally:
        idle.close()
        writer.close()


@windows_only
def test_a_short_send_behind_a_blocked_send_keeps_its_own_deadline(server):
    idle, writer = idle_and_writer(server())
    text = "x" * (MAX_MESSAGE_BYTES - 40)

    def flood():
        while True:
            writer.send("note", timeout=5, text=text)

    first = Peer(flood)
    first.start()
    try:
        time.sleep(0.8)  # the first send now holds the channel, waiting on a full buffer
        assert first.is_alive()
        started = time.monotonic()
        with pytest.raises(ChannelTimeout, match="nothing was written"):
            bounded(lambda: writer.send("note", timeout=0.2, text="short"), 1.5)
        assert time.monotonic() - started < 1.0
        assert first.is_alive()  # the short send's timeout did not break the channel
    finally:
        writer.close()
        first.join(2.0)
        idle.close()
    assert not first.is_alive()


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), True, "1", None, ipc.MAX_TIMEOUT_SECONDS + 1])
def test_every_channel_timeout_must_be_a_finite_number_of_seconds(bad):
    with pytest.raises(ValueError):
        ipc._deadline(bad)
    assert ipc._deadline(-1) <= time.monotonic()  # a passed deadline is no time at all


@windows_only
@pytest.mark.parametrize("bad", [float("inf"), float("nan")])
def test_channel_calls_refuse_an_unending_timeout(server, bad):
    srv = server()
    with pytest.raises(ValueError):
        srv.accept(bad, expected_pid=ME)
    with pytest.raises(ValueError):
        connect(srv.address, srv.key, DOWN, UP, expected_server_pid=ME, timeout=bad)
    idle, writer = idle_and_writer(srv)
    try:
        with pytest.raises(ValueError):
            writer.send("note", timeout=bad, text="x")
        with pytest.raises(ValueError):
            writer.receive(bad)
    finally:
        idle.close()
        writer.close()


@windows_only
def test_closing_is_prompt_while_the_peer_stalls(server):
    srv = server()
    holder = {}
    peer = Peer(lambda: holder.setdefault("channel", connect(srv.address, srv.key, DOWN, UP, expected_server_pid=ME)))
    peer.start()
    channel = srv.accept(5, expected_pid=ME)
    peer.join(5)
    bounded(channel.close, 1.0)
    bounded(srv.close, 1.0)
    holder["channel"].close()


@windows_only
def test_nobody_connecting_times_out_and_frees_the_listener(server):
    srv = server()
    with pytest.raises(ChannelTimeout):
        bounded(lambda: srv.accept(0.3, expected_pid=ME), 2.5)


@windows_only
def test_a_send_outside_the_schema_is_refused_before_it_leaves(server):
    srv = server()
    holder = {}
    peer = Peer(lambda: holder.setdefault("channel", connect(srv.address, srv.key, DOWN, UP, expected_server_pid=ME)))
    peer.start()
    channel = srv.accept(5, expected_pid=ME)
    try:
        peer.join(5)
        with pytest.raises(ChannelError):
            channel.send("hello", pid=1, protocol=1)  # the server may not send hellos
    finally:
        channel.close()
        holder["channel"].close()


# ------------------------------------------- a separate process stops reading


@windows_only
def test_a_peer_process_that_stops_reading_cannot_hang_send_or_close():
    """
    The same scenarios across real processes, under a hard outer timeout.

    tests/fixtures/ipc_send_driver.py serves channels to disposable peer
    children (tests/fixtures/ipc_peer_child.py) that authenticate and then
    never read. A handle to each peer is opened the moment the driver reports
    it, so the cleanup check below is about that exact process even if the
    driver itself hung and was killed.
    """
    import _winapi

    driver_script = Path(__file__).with_name("fixtures") / "ipc_send_driver.py"
    executable, environment = child_python()
    driver = subprocess.Popen(
        [executable, "-B", str(driver_script)], stdout=subprocess.PIPE, env=environment,
    )
    peers, lines = [], []

    def read_driver():
        for raw in driver.stdout:
            line = raw.decode("utf-8", "replace").strip()
            lines.append(line)
            if line.startswith("PEER "):
                access = 0x100000 | 0x1000 | 0x0001  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION | TERMINATE
                peers.append(_winapi.OpenProcess(access, False, int(line.split()[1])))

    reader = Peer(read_driver)
    reader.start()
    hung = False
    try:
        driver.wait(timeout=60)
    except subprocess.TimeoutExpired:
        hung = True
        driver.kill()
        driver.wait(10)
    reader.join(10)

    left_running = 0
    for handle in peers:
        if _winapi.WaitForSingleObject(handle, 10_000) != _winapi.WAIT_OBJECT_0:
            left_running += 1
            _winapi.TerminateProcess(handle, 1)
            _winapi.WaitForSingleObject(handle, 10_000)
        _winapi.CloseHandle(handle)

    assert not hung, f"the driver outlived its hard timeout: {lines}"
    assert left_running == 0 and len(peers) == 3
    report = json.loads(lines[-1])
    repeated, closing, serialized = report["repeated"], report["close"], report["serialized"]

    # The buffer filled, then a send kept its 1 s deadline and broke the channel.
    assert repeated["sent_before_block"] >= 1 and repeated["timed_out"]
    assert 0.9 <= repeated["blocked_send_seconds"] < 2.5
    assert repeated["after_timeout"] == "refused"

    # Close returned at once and woke a send waiting on its 3 s deadline early.
    assert closing["close_seconds"] < 0.5 and closing["sender_finished"]
    assert closing["result"] == "error" and closing["sender_seconds"] < 2.5

    # A 0.2 s send behind a blocked 5 s send kept its own deadline.
    assert serialized["result"] == "timeout" and 0.15 <= serialized["short_seconds"] < 1.0
    assert serialized["first_still_waiting"]
    assert serialized["first_finished"] and serialized["first_released_seconds"] < 2.0
    assert all(peer["exited"] for peer in report["peers"])
