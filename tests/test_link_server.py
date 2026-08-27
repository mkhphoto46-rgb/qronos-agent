"""
End-to-end tests for the link server.

These run a real listener on loopback and drive it with the real client, so the
handshake, the framing and the authorisation path are exercised together rather
than in halves. Everything that can be decided without a socket is tested in
the other link test modules; what is here needs the whole thing running.
"""

from __future__ import annotations

import time
import unittest

from core.link_audit import AuditEvent
from core.link_capability import Capability, LinkOp, LinkScope
from core.link_client import LinkClient, LinkClientError
from core.link_devices import DeviceRegistry, DeviceStatus, new_secret
from core.link_protocol import Response, encode_frame
from core.link_server import (
    LinkOperationError,
    LinkServer,
    LinkSettings,
    RejectReason,
)
from tests.fixtures.link_harness import (
    FakeServerClock,
    ServerHarness,
    ok_handler,
    wait_for,
)


class ServerTestCase(unittest.TestCase):
    settings: LinkSettings | None = None
    handlers: dict | None = None
    scope_resolver = None
    with_pairing = True

    def setUp(self) -> None:
        self.harness = ServerHarness(
            settings=self.settings,
            handlers=self.handlers if self.handlers is not None else {
                LinkOp.PING: ok_handler,
                LinkOp.STATUS: ok_handler,
                LinkOp.SEARCH: ok_handler,
            },
            scope_resolver=self.scope_resolver,
            with_pairing=self.with_pairing,
        )

    def tearDown(self) -> None:
        self.harness.close()


class TestLifecycle(unittest.TestCase):
    def test_the_server_binds_an_ephemeral_port(self) -> None:
        harness = ServerHarness()

        try:
            self.assertGreater(harness.port, 0)
        finally:
            harness.close()

    def test_starting_twice_is_an_error(self) -> None:
        harness = ServerHarness()

        try:
            with self.assertRaises(RuntimeError):
                harness.server.start()
        finally:
            harness.close()

    def test_start_and_stop_are_recorded(self) -> None:
        harness = ServerHarness()
        harness.close()

        self.assertEqual(harness.audit.count(AuditEvent.LINK_STARTED), 1)
        self.assertEqual(harness.audit.count(AuditEvent.LINK_STOPPED), 1)

    def test_it_works_as_a_context_manager(self) -> None:
        registry = DeviceRegistry(path=None)
        server = LinkServer(
            registry=registry,
            settings=LinkSettings(host="127.0.0.1", port=0),
        )

        with server:
            self.assertGreater(server.address[1], 0)


class TestPairingOverTheWire(ServerTestCase):
    def test_a_phone_pairs_on_its_first_connection(self) -> None:
        with self.harness.pair() as client:
            self.assertTrue(client.call("ping").ok)

        record = self.harness.registry.all()[0]

        self.assertIs(record.status, DeviceStatus.ACTIVE)
        self.assertTrue(
            wait_for(
                lambda: self.harness.audit.count(
                    AuditEvent.PAIRING_COMPLETED
                ) >= 1
            )
        )

    def test_the_negotiated_session_is_tls_1_3(self) -> None:
        with self.harness.pair() as client:
            self.assertIn("TLSv1.3", client.tls_summary())

    def test_a_paired_phone_reconnects_without_a_window(self) -> None:
        with self.harness.pair() as client:
            # A completed call proves the server has finished pairing this
            # device; reading the registry before that races the session
            # thread.
            self.assertTrue(client.call("ping").ok)
            device_id, secret = client.device_id, client.secret

        assert self.harness.pairing is not None
        self.harness.pairing.cancel()

        with self.harness.client_for(device_id, secret) as client:
            self.assertTrue(client.call("ping").ok)

    def test_a_pending_device_with_no_window_is_refused(self) -> None:
        # The key is valid, so the handshake succeeds; the session does not.
        record = self.harness.registry.create("orphan")

        assert self.harness.pairing is not None
        self.harness.pairing.cancel()

        client = self.harness.client_for(record.device_id, record.secret)
        client.connect()

        with self.assertRaises(LinkClientError):
            client.call("ping")

        client.close()

        self.assertTrue(
            wait_for(
                lambda: self.harness.audit.count(
                    AuditEvent.PAIRING_REFUSED
                ) >= 1
            )
        )

    def test_pairing_the_wrong_device_through_an_open_window_is_refused(
        self,
    ) -> None:
        assert self.harness.pairing is not None
        self.harness.pairing.open("expected phone")

        other = self.harness.registry.create("other phone")

        client = self.harness.client_for(other.device_id, other.secret)
        client.connect()

        with self.assertRaises(LinkClientError):
            client.call("ping")

        client.close()


class TestAuthentication(ServerTestCase):
    def test_an_unpaired_device_cannot_complete_a_handshake(self) -> None:
        client = self.harness.client_for("ffffffffffffffff", new_secret())

        with self.assertRaises(LinkClientError):
            client.connect()

    def test_a_wrong_key_cannot_complete_a_handshake(self) -> None:
        with self.harness.pair() as client:
            self.assertTrue(client.call("ping").ok)
            device_id = client.device_id

        wrong = self.harness.client_for(device_id, new_secret())

        with self.assertRaises(LinkClientError):
            wrong.connect()

    def test_a_revoked_device_cannot_reconnect(self) -> None:
        # Revocation is enforced during the handshake: the key stops
        # resolving, so there is no window in which a revoked phone holds an
        # open session.
        with self.harness.pair() as client:
            self.assertTrue(client.call("ping").ok)
            device_id, secret = client.device_id, client.secret

        self.harness.registry.revoke(device_id)

        again = self.harness.client_for(device_id, secret)

        with self.assertRaises(LinkClientError):
            again.connect()

    def test_a_failed_handshake_is_recorded_with_the_attempted_identity(
        self,
    ) -> None:
        client = self.harness.client_for("ffffffffffffffff", new_secret())

        with self.assertRaises(LinkClientError):
            client.connect()

        def refusals() -> list:
            return [
                record
                for record in self.harness.audit.records()
                if record.event is AuditEvent.HANDSHAKE_REFUSED
            ]

        self.assertTrue(
            wait_for(lambda: len(refusals()) >= 1),
            "the server never recorded the failed handshake",
        )
        self.assertEqual(len(refusals()), 1)
        self.assertEqual(refusals()[0].device_id, "ffffffffffffffff")

    def test_the_server_does_not_say_why_it_refused(self) -> None:
        # An unknown device and a wrong key are indistinguishable from the
        # phone, on purpose.
        client = self.harness.client_for("ffffffffffffffff", new_secret())

        with self.assertRaises(LinkClientError) as caught:
            client.connect()

        message = str(caught.exception).lower()

        self.assertNotIn("unknown", message)
        self.assertNotIn("revoked", message)


class TestAuthorisation(ServerTestCase):
    def test_an_allowed_operation_reaches_its_handler(self) -> None:
        with self.harness.pair() as client:
            response = client.call("status")

        self.assertTrue(response.ok)
        self.assertEqual(response.result, {"seen": "status"})

    def test_an_operation_needing_approval_is_not_run(self) -> None:
        with self.harness.pair() as client:
            response = client.call("write_file", path="x", text="y")

        self.assertFalse(response.ok)
        self.assertEqual(response.code, "needs_approval")

    def test_device_management_is_refused_as_console_only(self) -> None:
        with self.harness.pair() as client:
            self.assertEqual(client.call("list_devices").code, "console_only")

    def test_an_unknown_operation_is_refused(self) -> None:
        with self.harness.pair() as client:
            self.assertEqual(client.call("rm_rf").code, "unknown_op")

    def test_an_operation_with_no_handler_says_so(self) -> None:
        with self.harness.pair() as client:
            self.assertEqual(client.call("read_file").code, "not_implemented")

    def test_a_narrowed_device_loses_the_capability(self) -> None:
        with self.harness.pair() as client:
            device_id = client.device_id
            self.assertTrue(client.call("status").ok)

        self.harness.registry.set_grants(device_id, {Capability.ASK})

        with self.harness.client_for(
            device_id, self.harness.registry.get(device_id).secret
        ) as client:
            self.assertEqual(client.call("status").code, "not_granted")

    def test_ping_survives_a_device_granted_nothing(self) -> None:
        with self.harness.pair() as client:
            self.assertTrue(client.call("ping").ok)
            device_id, secret = client.device_id, client.secret

        self.harness.registry.set_grants(device_id, set())

        with self.harness.client_for(device_id, secret) as client:
            self.assertTrue(client.call("ping").ok)

    def test_refusals_are_recorded(self) -> None:
        with self.harness.pair() as client:
            client.call("list_devices")

        self.assertTrue(
            wait_for(
                lambda: self.harness.audit.count(
                    AuditEvent.REQUEST_REFUSED
                ) >= 1
            )
        )


class TestScopeGate(ServerTestCase):
    def test_a_peer_outside_the_allowed_scopes_never_reaches_tls(self) -> None:
        harness = ServerHarness(
            settings=LinkSettings(allowed_scopes=frozenset()),
        )

        try:
            record = harness.registry.create("phone")
            client = harness.client_for(record.device_id, record.secret)

            with self.assertRaises(LinkClientError):
                client.connect()
                client.call("ping")

            self.assertTrue(
                wait_for(
                    lambda: harness.audit.count(AuditEvent.SCOPE_REFUSED) >= 1
                )
            )
        finally:
            harness.close()

    def test_a_remote_connection_needs_the_device_opted_in(self) -> None:
        # The Layer 2 gate, exercised now. Turning the scope on must not also
        # turn every paired device on.
        harness = ServerHarness(
            settings=LinkSettings(
                allowed_scopes=frozenset(
                    {LinkScope.LOCAL_NETWORK, LinkScope.REMOTE_TUNNEL}
                )
            ),
            handlers={LinkOp.PING: ok_handler},
            scope_resolver=lambda peer: LinkScope.REMOTE_TUNNEL,
            with_pairing=False,
        )

        try:
            record = harness.registry.create("phone")
            harness.registry.activate(record.device_id)

            client = harness.client_for(record.device_id, record.secret)
            client.connect()

            with self.assertRaises(LinkClientError):
                client.call("ping")

            client.close()

            self.assertTrue(
                wait_for(
                    lambda: harness.audit.count(AuditEvent.SCOPE_REFUSED) >= 1
                )
            )

            refusals = [
                r
                for r in harness.audit.records()
                if r.event is AuditEvent.SCOPE_REFUSED
            ]

            self.assertEqual(len(refusals), 1)
            self.assertEqual(
                refusals[0].reason, RejectReason.REMOTE_NOT_ENABLED.value
            )
        finally:
            harness.close()

    def test_an_opted_in_device_is_served_remotely_with_the_narrow_profile(
        self,
    ) -> None:
        harness = ServerHarness(
            settings=LinkSettings(
                allowed_scopes=frozenset({LinkScope.REMOTE_TUNNEL})
            ),
            handlers={LinkOp.PING: ok_handler, LinkOp.STATUS: ok_handler},
            scope_resolver=lambda peer: LinkScope.REMOTE_TUNNEL,
            with_pairing=False,
        )

        try:
            record = harness.registry.create("phone")
            harness.registry.activate(record.device_id)
            harness.registry.set_remote_enabled(record.device_id, True)

            with harness.client_for(record.device_id, record.secret) as client:
                self.assertTrue(client.call("ping").ok)
                self.assertTrue(client.call("status").ok)

                # Present in the local profile, absent from the remote one.
                self.assertEqual(
                    client.call("write_file").code, "out_of_scope"
                )
                self.assertEqual(client.call("read_file").code, "out_of_scope")
        finally:
            harness.close()


class TestHandlerFailures(ServerTestCase):
    def test_a_handler_refusal_reaches_the_phone(self) -> None:
        def refuse(session, request):
            raise LinkOperationError("no_model", "There is no model wired in.")

        harness = ServerHarness(handlers={LinkOp.STATUS: refuse})

        try:
            with harness.pair() as client:
                response = client.call("status")

            self.assertFalse(response.ok)
            self.assertEqual(response.code, "no_model")
            self.assertIn("no model", response.error)
        finally:
            harness.close()

    def test_an_unexpected_exception_does_not_leak_its_message(self) -> None:
        # An exception message can carry a path or a fragment of the user's
        # data. The phone gets a fixed code and the detail stays here.
        def explode(session, request):
            raise RuntimeError("C:/Users/amin/private/secrets.txt is missing")

        harness = ServerHarness(handlers={LinkOp.STATUS: explode})

        try:
            with harness.pair() as client:
                response = client.call("status")

            self.assertEqual(response.code, "internal_error")
            self.assertNotIn("secrets.txt", response.error)
            self.assertNotIn("amin", response.error)
            self.assertTrue(
                wait_for(
                    lambda: harness.audit.count(AuditEvent.REQUEST_FAILED) >= 1
                )
            )
        finally:
            harness.close()

    def test_a_failed_handler_does_not_end_the_session(self) -> None:
        def explode(session, request):
            raise RuntimeError("boom")

        harness = ServerHarness(
            handlers={LinkOp.STATUS: explode, LinkOp.PING: ok_handler}
        )

        try:
            with harness.pair() as client:
                self.assertEqual(client.call("status").code, "internal_error")
                self.assertTrue(client.call("ping").ok)
        finally:
            harness.close()


class TestEvents(ServerTestCase):
    def test_events_from_a_handler_reach_the_phone_before_the_response(
        self,
    ) -> None:
        def chatty(session, request):
            session.emit("searching")
            session.emit("reading", done=1, total=3)
            session.emit("reading", done=3, total=3)

            return {"done": True}

        harness = ServerHarness(handlers={LinkOp.SEARCH: chatty})

        try:
            with harness.pair() as client:
                response = client.call("search", query="x")

            self.assertTrue(response.ok)
            self.assertEqual(
                [event.topic for event in client.events],
                ["searching", "reading", "reading"],
            )
            self.assertEqual(client.events[-1].data, {"done": 3, "total": 3})
        finally:
            harness.close()

    def test_a_callback_sees_events_as_they_arrive(self) -> None:
        def chatty(session, request):
            session.emit("searching")

            return {}

        harness = ServerHarness(handlers={LinkOp.SEARCH: chatty})
        seen = []

        try:
            payload = harness.pairing.open("phone")
            client = LinkClient.from_payload(
                payload,
                host="127.0.0.1",
                port=harness.port,
                on_event=seen.append,
            )

            with client:
                client.call("search", query="x")

            self.assertEqual([event.topic for event in seen], ["searching"])
        finally:
            harness.close()

    def test_events_do_not_leak_between_calls(self) -> None:
        def chatty(session, request):
            session.emit("searching")

            return {}

        harness = ServerHarness(
            handlers={LinkOp.SEARCH: chatty, LinkOp.PING: ok_handler}
        )

        try:
            with harness.pair() as client:
                client.call("search", query="x")
                self.assertEqual(len(client.events), 1)

                client.call("ping")
                self.assertEqual(len(client.events), 0)
        finally:
            harness.close()


class TestProtocolFaults(ServerTestCase):
    def send_raw(self, client: LinkClient, blob: bytes) -> None:
        client._connection.sendall(blob)  # type: ignore[union-attr]

    def test_a_malformed_frame_closes_the_session(self) -> None:
        with self.harness.pair() as client:
            self.send_raw(client, b"\x00\x00\x00\x05{bad}")

            with self.assertRaises(LinkClientError):
                client.call("ping")

        self.assertTrue(
            wait_for(
                lambda: self.harness.audit.count(
                    AuditEvent.PROTOCOL_FAULT
                ) >= 1
            ),
            "the server never recorded the protocol fault",
        )

        faults = [
            r
            for r in self.harness.audit.records()
            if r.event is AuditEvent.PROTOCOL_FAULT
        ]

        self.assertEqual(len(faults), 1)
        self.assertEqual(faults[0].reason, "malformed_json")

    def test_a_huge_declared_length_is_refused(self) -> None:
        with self.harness.pair() as client:
            self.send_raw(client, (0xFFFFFFFF).to_bytes(4, "big"))

            with self.assertRaises(LinkClientError):
                client.call("ping")

        self.assertTrue(
            wait_for(
                lambda: self.harness.audit.count(
                    AuditEvent.PROTOCOL_FAULT
                ) >= 1
            ),
            "the server never recorded the protocol fault",
        )

        faults = [
            r
            for r in self.harness.audit.records()
            if r.event is AuditEvent.PROTOCOL_FAULT
        ]

        self.assertEqual(faults[0].reason, "oversize")

    def test_a_response_frame_from_the_phone_is_unexpected(self) -> None:
        # The phone does not send responses. One that does is confused or
        # hostile, and either way there is nothing to do with it.
        with self.harness.pair() as client:
            self.send_raw(client, encode_frame(Response.success(1, "hi")))

            with self.assertRaises(LinkClientError):
                client.call("ping")

        self.assertTrue(
            wait_for(
                lambda: self.harness.audit.count(
                    AuditEvent.PROTOCOL_FAULT
                ) >= 1
            ),
            "the server never recorded the protocol fault",
        )

        faults = [
            r
            for r in self.harness.audit.records()
            if r.event is AuditEvent.PROTOCOL_FAULT
        ]

        self.assertEqual(faults[0].reason, "unexpected_frame")


class TestLimits(unittest.TestCase):
    def test_too_many_connections_are_turned_away(self) -> None:
        harness = ServerHarness(
            settings=LinkSettings(max_connections=2),
            handlers={LinkOp.PING: ok_handler},
        )
        held: list[LinkClient] = []

        try:
            for index in range(2):
                record = harness.registry.create(f"phone {index}")
                harness.registry.activate(record.device_id)
                client = harness.client_for(record.device_id, record.secret)
                client.connect()
                self.assertTrue(client.call("ping").ok)
                held.append(client)

            extra = harness.registry.create("one too many")
            harness.registry.activate(extra.device_id)
            spare = harness.client_for(extra.device_id, extra.secret)

            with self.assertRaises(LinkClientError):
                spare.connect()
                spare.call("ping")

            self.assertTrue(
                wait_for(
                    lambda: harness.audit.count(
                        AuditEvent.CONNECTION_REJECTED
                    ) >= 1
                )
            )
        finally:
            for client in held:
                client.close()

            harness.close()

    def test_a_failed_handshake_pauses_the_next_attempt(self) -> None:
        # Denial-of-service protection, not authentication hardening: the key
        # is 256 bits, so repeated attempts cannot guess it.
        clock = FakeServerClock()
        harness = ServerHarness(
            settings=LinkSettings(failure_pause=60.0),
            handlers={LinkOp.PING: ok_handler},
            clock=clock,
        )

        try:
            record = harness.registry.create("phone")
            harness.registry.activate(record.device_id)

            bad = harness.client_for("ffffffffffffffff", new_secret())

            with self.assertRaises(LinkClientError):
                bad.connect()

            good = harness.client_for(record.device_id, record.secret)

            with self.assertRaises(LinkClientError):
                good.connect()
                good.call("ping")

            rejections = [
                r
                for r in harness.audit.records()
                if r.event is AuditEvent.CONNECTION_REJECTED
            ]

            self.assertTrue(rejections)
            self.assertEqual(
                rejections[-1].reason,
                RejectReason.TOO_SOON_AFTER_FAILURE.value,
            )

            clock.advance(61.0)

            with harness.client_for(record.device_id, record.secret) as client:
                self.assertTrue(client.call("ping").ok)
        finally:
            harness.close()

    def test_an_idle_session_is_closed(self) -> None:
        harness = ServerHarness(
            settings=LinkSettings(idle_timeout=0.1),
            handlers={LinkOp.PING: ok_handler},
        )

        try:
            with harness.pair() as client:
                self.assertTrue(client.call("ping").ok)

                time.sleep(0.3)

                with self.assertRaises(LinkClientError):
                    client.call("ping")
        finally:
            harness.close()

    def test_sessions_are_counted(self) -> None:
        harness = ServerHarness(handlers={LinkOp.PING: ok_handler})

        try:
            with harness.pair() as client:
                client.call("ping")

            self.assertEqual(harness.server.sessions_served, 1)
        finally:
            harness.close()


class TestSessionState(ServerTestCase):
    def test_a_session_knows_which_device_it_serves(self) -> None:
        captured = {}

        def capture(session, request):
            captured["device"] = session.device.device_id
            captured["scope"] = session.scope
            captured["described"] = session.describe()

            return {}

        harness = ServerHarness(handlers={LinkOp.PING: capture})

        try:
            with harness.pair("Amin's phone") as client:
                client.call("ping")

            self.assertEqual(captured["device"], client.device_id)
            self.assertIs(captured["scope"], LinkScope.LOCAL_NETWORK)
            self.assertIn("Amin's phone", captured["described"])
        finally:
            harness.close()

    def test_the_last_seen_time_is_updated_on_reconnection(self) -> None:
        with self.harness.pair() as client:
            self.assertTrue(client.call("ping").ok)
            device_id, secret = client.device_id, client.secret

        first = self.harness.registry.get(device_id).last_seen_at

        time.sleep(0.01)

        with self.harness.client_for(device_id, secret) as client:
            client.call("ping")

        second = self.harness.registry.get(device_id).last_seen_at

        assert first is not None and second is not None
        self.assertGreater(second, first)


if __name__ == "__main__":
    unittest.main()
