"""
Layer 2 sessions, end to end on loopback: server, dialler, relay, phone.

Every test uses fresh keys in an in-memory registry and binds port 0. The relay
is the disposable ``FakeRelay``, which can also be made hostile, because what a
hostile relay cannot do is the point of the design.
"""

from __future__ import annotations

import socket
import threading
import unittest

from core.link_audit import AuditEvent
from core.link_capability import LinkOp, LinkScope
from core.link_client import LinkClientError, new_command_id
from core.link_handlers import ping_handler
from core.link_protocol import Request, encode_frame, parse_frame, read_frame
from core.link_relay_wire import route_id_for_secret
from core.link_server import LinkSettings, RejectReason, SessionRule
from core.link_devices import DeviceStatus
from core.link_transport import reader_for
from tests.fixtures.link_harness import ok_handler, wait_for
from tests.fixtures.link_remote_rig import RemoteRig


class Counter:
    """A handler that counts how many times it really ran."""

    def __init__(self) -> None:
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, session, request):
        with self.lock:
            self.calls += 1

        return {"answer": f"done:{request.params.get('question', '')}"}


def events(rig: RemoteRig, event: AuditEvent) -> list:
    return [r for r in rig.audit.records() if r.event is event]


def raw_call(client, request: Request):
    """Send one frame exactly as given, bypassing the client's command ids."""

    connection = client._connection
    connection.sendall(encode_frame(request))
    payload = read_frame(reader_for(connection))

    return None if payload is None else parse_frame(payload)


class RemoteTestCase(unittest.TestCase):
    handlers: dict = {}
    settings: LinkSettings | None = None

    def setUp(self) -> None:
        self.counter = Counter()
        handlers = {
            LinkOp.PING: ping_handler,
            LinkOp.STATUS: ok_handler,
            LinkOp.ASK: self.counter,
            LinkOp.SEARCH: ok_handler,
            LinkOp.WRITE_FILE: ok_handler,
        }
        handlers.update(self.handlers)
        self.rig = RemoteRig(handlers=handlers, settings=self.settings)
        self.addCleanup(self.rig.close)


class TestServedThroughTheRelay(RemoteTestCase):
    def test_a_remote_device_gets_the_narrow_profile_and_nothing_more(self) -> None:
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            ping = phone.call("ping")
            self.assertTrue(ping.ok)
            self.assertEqual(ping.result["scope"], LinkScope.REMOTE_TUNNEL.value)
            self.assertEqual(
                ping.result["capabilities"], ["ask", "read_status", "search_web"]
            )

            self.assertTrue(phone.call("status").ok)
            self.assertEqual(phone.call("write_file").code, "out_of_scope")
            self.assertEqual(phone.call("list_devices").code, "console_only")

    def test_the_session_inside_the_relay_is_tls13_with_the_pairing_key(self) -> None:
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            self.assertTrue(phone.tls_summary().startswith("TLSv1.3"))

    def test_the_relay_carries_only_ciphertext(self) -> None:
        self.rig.relay.tap = True
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            response = phone.call("ask", question="کلید مخفی من چیست")

        self.assertTrue(response.ok)

        seen = bytes(self.rig.relay.tapped[0])

        self.assertGreater(len(seen), 0)
        self.assertNotIn("کلید مخفی".encode("utf-8"), seen)
        self.assertNotIn(b'"op"', seen)
        self.assertNotIn(b"ask", seen)

    def test_the_audit_log_records_the_remote_scope(self) -> None:
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            phone.call("status")

        handshakes = events(self.rig, AuditEvent.HANDSHAKE_OK)

        self.assertEqual(len(handshakes), 1)
        self.assertEqual(handshakes[0].scope, LinkScope.REMOTE_TUNNEL)
        self.assertEqual(handshakes[0].device_id, record.device_id)

    def test_the_pc_parks_again_so_the_phone_can_reconnect(self) -> None:
        record = self.rig.remote_device()

        for _ in range(3):
            with self.rig.phone(record) as phone:
                self.assertTrue(phone.call("status").ok)

            self.assertTrue(self.rig.wait_parked(record))

        self.assertEqual(len(events(self.rig, AuditEvent.SESSION_OPENED)), 3)


class TestTheRelayCannotEnrolOrImpersonate(RemoteTestCase):
    def test_a_pending_device_cannot_pair_through_the_relay(self) -> None:
        # The relay sits on loopback here, so its address looks local. Pairing
        # must still be refused: over the relay it is the path that decides.
        parked = self.rig.remote_device("parked phone")
        payload = self.rig.pairing.open("new phone")
        newcomer = self.rig.registry.get(payload.device_id)

        self.rig.relay.misroute_to = route_id_for_secret(parked.secret)

        with self.assertRaises(LinkClientError):
            with self.rig.phone(newcomer) as phone:
                phone.call("ping")

        self.assertTrue(
            wait_for(lambda: len(events(self.rig, AuditEvent.PAIRING_REFUSED)) == 1)
        )
        refusal = events(self.rig, AuditEvent.PAIRING_REFUSED)[0]

        self.assertEqual(refusal.reason, RejectReason.PAIRING_OVER_RELAY.value)
        self.assertIs(self.rig.registry.get(newcomer.device_id).status, DeviceStatus.PENDING)
        self.assertTrue(self.rig.pairing.is_open)

    def test_a_device_without_an_opt_in_is_refused_even_when_joined(self) -> None:
        parked = self.rig.remote_device("opted in")
        other = self.rig.registry.create("not opted in")
        self.rig.registry.activate(other.device_id)

        self.rig.relay.misroute_to = route_id_for_secret(parked.secret)

        with self.assertRaises(LinkClientError):
            with self.rig.phone(other) as phone:
                phone.call("ping")

        self.assertTrue(
            wait_for(lambda: len(events(self.rig, AuditEvent.SCOPE_REFUSED)) == 1)
        )
        self.assertEqual(
            events(self.rig, AuditEvent.SCOPE_REFUSED)[0].reason,
            RejectReason.REMOTE_NOT_ENABLED.value,
        )

    def test_without_the_key_the_handshake_cannot_complete(self) -> None:
        record = self.rig.remote_device()
        self.rig.relay.misroute_to = route_id_for_secret(record.secret)

        with self.assertRaises(LinkClientError):
            with self.rig.phone(record, secret=b"x" * 32) as phone:
                phone.call("ping")

        self.assertTrue(
            wait_for(lambda: len(events(self.rig, AuditEvent.HANDSHAKE_REFUSED)) == 1)
        )
        self.assertEqual(self.counter.calls, 0)


class TestRevocationReachesOpenSessions(RemoteTestCase):
    def test_revoking_ends_an_open_remote_session_at_once(self) -> None:
        record = self.rig.remote_device()
        phone = self.rig.phone(record)
        phone.connect()
        self.addCleanup(phone.close)
        self.assertTrue(phone.call("status").ok)

        self.rig.server.revoke(record.device_id)

        # No request needed: the session is closed from the PC's side.
        self.assertTrue(wait_for(lambda: self.rig.server.live_sessions(record.device_id) == 0))

        with self.assertRaises(LinkClientError):
            phone.call("status")

        ended = events(self.rig, AuditEvent.SESSION_ENDED)
        self.assertEqual([e.reason for e in ended], [SessionRule.REVOKED.value])

        # And it cannot come back: the parker is gone, so the relay has no PC
        # to join it to, and the key would not resolve if it did.
        self.rig.dialler.refresh()
        self.assertTrue(self.rig.wait_unparked(record))

        with self.assertRaises(LinkClientError):
            self.rig.phone(record).connect()

    def test_a_revocation_made_behind_the_servers_back_applies_at_the_next_request(self) -> None:
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            self.assertTrue(phone.call("ask", question="one").ok)

            self.rig.registry.revoke(record.device_id)

            with self.assertRaises(LinkClientError):
                phone.call("ask", question="two")

        self.assertEqual(self.counter.calls, 1)

    def test_withdrawing_remote_access_spares_the_local_session(self) -> None:
        record = self.rig.remote_device()
        local = self.rig.local_phone(record)
        remote = self.rig.phone(record)
        local.connect()
        remote.connect()
        self.addCleanup(local.close)
        self.addCleanup(remote.close)

        self.assertTrue(remote.call("status").ok)
        self.assertTrue(local.call("status").ok)

        self.rig.server.set_remote_access(record.device_id, False)

        with self.assertRaises(LinkClientError):
            remote.call("status")

        self.assertTrue(local.call("status").ok)

    def test_an_opt_in_that_expires_mid_session_ends_the_session(self) -> None:
        record = self.rig.remote_device(seconds=60)

        with self.rig.phone(record) as phone:
            self.assertTrue(phone.call("status").ok)

            self.rig.clock.advance(61)

            with self.assertRaises(LinkClientError):
                phone.call("status")

        ended = events(self.rig, AuditEvent.SESSION_ENDED)
        self.assertEqual([e.reason for e in ended], [SessionRule.REMOTE_WITHDRAWN.value])

        self.rig.dialler.refresh()
        self.assertTrue(self.rig.wait_unparked(record))

    def test_revoking_ends_a_local_session_too(self) -> None:
        # Layer 1 gains the same guarantee: revocation is not only a handshake
        # rule any more.
        record = self.rig.remote_device()
        local = self.rig.local_phone(record)
        local.connect()
        self.addCleanup(local.close)
        self.assertTrue(local.call("status").ok)

        self.rig.server.revoke(record.device_id)

        self.assertTrue(wait_for(lambda: self.rig.server.live_sessions(record.device_id) == 0))

        with self.assertRaises(LinkClientError):
            local.call("status")


class TestSessionLimits(RemoteTestCase):
    settings = LinkSettings(remote_burst=3, remote_refill_per_second=0.0001)

    def test_a_remote_session_ends_at_its_lifetime(self) -> None:
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            self.assertTrue(phone.call("status").ok)

            self.rig.clock.advance(self.rig.server.settings.remote_session_lifetime + 1)

            with self.assertRaises(LinkClientError):
                phone.call("status")

        ended = events(self.rig, AuditEvent.SESSION_ENDED)
        self.assertEqual([e.reason for e in ended], [SessionRule.LIFETIME_REACHED.value])

        # The phone simply reconnects: a fresh handshake, fresh keys.
        with self.rig.phone(record) as phone:
            self.assertTrue(phone.call("status").ok)

    def test_a_remote_session_has_a_request_budget(self) -> None:
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            codes = [phone.call("status").code for _ in range(4)]

        self.assertEqual(codes, ["", "", "", SessionRule.RATE_LIMITED.value])

    def test_local_sessions_have_no_request_budget(self) -> None:
        record = self.rig.remote_device()

        with self.rig.local_phone(record) as phone:
            self.assertTrue(all(phone.call("status").ok for _ in range(6)))


class TestCommandIds(RemoteTestCase):
    def test_remote_requests_without_a_command_id_are_refused(self) -> None:
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            response = raw_call(phone, Request(id=7, op="ask", params={"question": "q"}))

        self.assertEqual(response.code, SessionRule.COMMAND_ID_REQUIRED.value)
        self.assertEqual(self.counter.calls, 0)

    def test_ping_needs_no_command_id(self) -> None:
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            self.assertTrue(raw_call(phone, Request(id=1, op="ping")).ok)

    def test_a_layer_one_request_without_a_command_id_still_works_locally(self) -> None:
        record = self.rig.remote_device()

        with self.rig.local_phone(record) as phone:
            response = raw_call(phone, Request(id=1, op="ask", params={"question": "q"}))

        self.assertTrue(response.ok)

    def test_a_malformed_command_id_ends_the_session_as_a_protocol_fault(self) -> None:
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            connection = phone._connection
            connection.sendall(_frame_with_bad_cid())
            self.assertIsNone(read_frame(reader_for(connection)))

        self.assertTrue(
            wait_for(lambda: len(events(self.rig, AuditEvent.PROTOCOL_FAULT)) == 1)
        )


def _frame_with_bad_cid() -> bytes:
    import json

    body = json.dumps(
        {"kind": "request", "id": 1, "op": "ask", "params": {}, "cid": "NOT-HEX"}
    ).encode("utf-8")

    return len(body).to_bytes(4, "big") + body


class TestDuplicateCommands(RemoteTestCase):
    def test_a_retry_after_the_answer_was_lost_is_answered_not_rerun(self) -> None:
        # The realistic failure: the PC does the work, and the connection dies
        # before the answer reaches the phone.
        def run_then_lose_the_connection(session, request):
            result = self.counter(session, request)
            session.connection.shutdown(socket.SHUT_RDWR)

            return result

        self.rig.server.handle(LinkOp.ASK, run_then_lose_the_connection)
        record = self.rig.remote_device()

        phone = self.rig.phone(record)
        self.addCleanup(phone.close)
        waits: list[float] = []

        def pause(seconds: float) -> None:
            # Instead of sleeping: wait until the PC has parked again.
            waits.append(seconds)
            self.rig.wait_parked(record)

        response = phone.call_with_retry("ask", question="once", sleep=pause)

        self.assertTrue(response.ok)
        self.assertEqual(response.result, {"answer": "done:once"})
        self.assertEqual(self.counter.calls, 1)
        self.assertEqual(len(events(self.rig, AuditEvent.REQUEST_DUPLICATE)), 1)
        self.assertGreaterEqual(len(waits), 1)

    def test_a_retry_on_a_second_connection_while_running_is_told_to_wait(self) -> None:
        release = threading.Event()
        started = threading.Event()

        def slow(session, request):
            started.set()
            release.wait(5)

            return self.counter(session, request)

        self.rig.server.handle(LinkOp.ASK, slow)
        record = self.rig.remote_device()
        cid = new_command_id()
        first_result: list = []

        first = self.rig.phone(record)
        first.connect()
        self.addCleanup(first.close)

        thread = threading.Thread(
            target=lambda: first_result.append(first.call("ask", _command_id=cid, question="q"))
        )
        thread.start()
        self.assertTrue(started.wait(3))
        self.assertTrue(self.rig.wait_parked(record))

        with self.rig.phone(record) as second:
            waiting = second.call("ask", _command_id=cid, question="q")
            self.assertEqual(waiting.code, SessionRule.COMMAND_IN_PROGRESS.value)

            release.set()
            thread.join(5)

            answered = second.call("ask", _command_id=cid, question="q")

        self.assertTrue(first_result[0].ok)
        self.assertEqual(answered.result, first_result[0].result)
        self.assertEqual(self.counter.calls, 1)

    def test_reusing_a_command_id_for_a_different_request_is_refused(self) -> None:
        record = self.rig.remote_device()
        cid = new_command_id()

        with self.rig.phone(record) as phone:
            self.assertTrue(phone.call("ask", _command_id=cid, question="first").ok)
            conflict = phone.call("ask", _command_id=cid, question="second")

        self.assertEqual(conflict.code, SessionRule.COMMAND_CONFLICT.value)
        self.assertEqual(self.counter.calls, 1)

    def test_revocation_forgets_the_devices_commands(self) -> None:
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            phone.call("ask", question="q")

        self.assertEqual(self.rig.server.ledger.size(record.device_id), 1)

        self.rig.server.revoke(record.device_id)

        self.assertEqual(self.rig.server.ledger.size(record.device_id), 0)


class TestHostileRelay(RemoteTestCase):
    def test_a_tampered_byte_ends_the_session_and_runs_nothing(self) -> None:
        armed = threading.Event()

        def flip_last_byte(data: bytes) -> bytes:
            if not armed.is_set():
                return data

            return data[:-1] + bytes([data[-1] ^ 0x01])

        self.rig.relay.tamper = flip_last_byte
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            armed.set()

            with self.assertRaises(LinkClientError):
                phone.call("ask", question="altered in flight")

        self.assertEqual(self.counter.calls, 0)
        self.assertEqual(len(events(self.rig, AuditEvent.REQUEST_ALLOWED)), 0)

    def test_a_recorded_session_replayed_into_the_pc_runs_nothing(self) -> None:
        self.rig.relay.tap = True
        record = self.rig.remote_device()

        with self.rig.phone(record) as phone:
            self.assertTrue(phone.call("ask", question="once only").ok)

        self.assertEqual(self.counter.calls, 1)
        self.assertTrue(self.rig.wait_parked(record))

        recording = bytes(self.rig.relay.tapped[0])
        self.rig.relay.replay_to_parked_host(route_id_for_secret(record.secret), recording)

        self.assertTrue(
            wait_for(lambda: len(events(self.rig, AuditEvent.HANDSHAKE_REFUSED)) == 1)
        )
        self.assertEqual(self.counter.calls, 1)
        self.assertEqual(len(events(self.rig, AuditEvent.REQUEST_ALLOWED)), 1)


class TestServerStop(RemoteTestCase):
    def test_stopping_the_server_ends_open_sessions_promptly(self) -> None:
        record = self.rig.remote_device()
        phone = self.rig.phone(record)
        phone.connect()
        self.addCleanup(phone.close)
        self.assertTrue(phone.call("status").ok)

        self.rig.dialler.stop(timeout=2.0)
        self.rig.server.stop(timeout=2.0)

        self.assertEqual(self.rig.server.live_sessions(record.device_id), 0)
        self.assertEqual(
            [e.reason for e in events(self.rig, AuditEvent.SESSION_ENDED)],
            [SessionRule.SERVER_STOPPING.value],
        )


if __name__ == "__main__":
    unittest.main()
