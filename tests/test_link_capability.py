from __future__ import annotations

import unittest
from enum import Enum

from core.link_capability import (
    CONSOLE_ONLY,
    LOCAL_NETWORKS,
    OP_CAPABILITY,
    OP_PERMISSION,
    SCOPE_PROFILES,
    AuthReason,
    Capability,
    LinkOp,
    LinkScope,
    _reason_for,
    authorise,
    resolve_capabilities,
    scope_for_peer,
)
from security.permissions import PermissionDecision


class TestTablesAreComplete(unittest.TestCase):
    """
    The guards against a policy quietly becoming unreachable.

    This project already shipped one dead policy branch — ``ActivityMode.IDLE``,
    which nothing ever routed to. These tests exist so the same thing cannot
    happen to a capability or an operation.
    """

    def test_every_operation_has_a_capability_entry(self) -> None:
        self.assertEqual(set(OP_CAPABILITY), set(LinkOp))

    def test_every_operation_has_a_permission_level(self) -> None:
        self.assertEqual(set(OP_PERMISSION), set(LinkOp))

    def test_ping_is_the_only_operation_needing_no_capability(self) -> None:
        without = {op for op, cap in OP_CAPABILITY.items() if cap is None}

        self.assertEqual(without, {LinkOp.PING})

    def test_every_scope_has_a_profile(self) -> None:
        self.assertEqual(set(SCOPE_PROFILES), set(LinkScope))

    def test_every_capability_is_reachable_or_explicitly_console_only(
        self,
    ) -> None:
        granted = set().union(*SCOPE_PROFILES.values())

        for capability in Capability:
            with self.subTest(capability=capability):
                self.assertTrue(
                    capability in granted or capability in CONSOLE_ONLY,
                    f"{capability.value} is in no profile and is not marked "
                    "console-only, so nothing can ever exercise it",
                )

    def test_every_capability_is_used_by_some_operation(self) -> None:
        used = {cap for cap in OP_CAPABILITY.values() if cap is not None}

        self.assertEqual(used, set(Capability))


class TestProfiles(unittest.TestCase):
    def test_remote_is_strictly_narrower_than_local(self) -> None:
        local = SCOPE_PROFILES[LinkScope.LOCAL_NETWORK]
        remote = SCOPE_PROFILES[LinkScope.REMOTE_TUNNEL]

        self.assertTrue(remote < local)

    def test_remote_cannot_touch_files_or_the_machine(self) -> None:
        remote = SCOPE_PROFILES[LinkScope.REMOTE_TUNNEL]

        for capability in (
            Capability.READ_FILES,
            Capability.WRITE_FILES,
            Capability.DELETE_FILES,
            Capability.RUN_APPLICATION,
            Capability.CONTROL_SYSTEM,
        ):
            with self.subTest(capability=capability):
                self.assertNotIn(capability, remote)

    def test_device_management_is_in_no_profile(self) -> None:
        # A stolen phone must not be able to enrol its replacement or revoke
        # the owner's device.
        for scope, profile in SCOPE_PROFILES.items():
            with self.subTest(scope=scope):
                self.assertNotIn(Capability.MANAGE_DEVICES, profile)


class TestResolveCapabilities(unittest.TestCase):
    def test_no_grants_means_the_whole_profile(self) -> None:
        self.assertEqual(
            resolve_capabilities(LinkScope.LOCAL_NETWORK),
            SCOPE_PROFILES[LinkScope.LOCAL_NETWORK],
        )

    def test_grants_narrow_the_profile(self) -> None:
        resolved = resolve_capabilities(
            LinkScope.LOCAL_NETWORK,
            frozenset({Capability.ASK}),
        )

        self.assertEqual(resolved, frozenset({Capability.ASK}))

    def test_a_grant_cannot_widen_the_profile(self) -> None:
        # The invariant that makes a per-device setting safe: it can only ever
        # take away.
        resolved = resolve_capabilities(
            LinkScope.REMOTE_TUNNEL,
            frozenset({Capability.ASK, Capability.CONTROL_SYSTEM}),
        )

        self.assertNotIn(Capability.CONTROL_SYSTEM, resolved)
        self.assertEqual(resolved, frozenset({Capability.ASK}))

    def test_an_empty_grant_set_permits_nothing(self) -> None:
        self.assertEqual(
            resolve_capabilities(LinkScope.LOCAL_NETWORK, frozenset()),
            frozenset(),
        )

    def test_granting_only_console_capabilities_permits_nothing(self) -> None:
        self.assertEqual(
            resolve_capabilities(
                LinkScope.LOCAL_NETWORK,
                frozenset({Capability.MANAGE_DEVICES}),
            ),
            frozenset(),
        )


class TestAuthorise(unittest.TestCase):
    def test_ping_is_allowed_from_everywhere(self) -> None:
        for scope in LinkScope:
            with self.subTest(scope=scope):
                self.assertTrue(authorise("ping", scope).allowed)

    def test_ping_survives_an_empty_grant_set(self) -> None:
        # A keepalive that could be refused would make an idle session
        # indistinguishable from a broken one.
        decision = authorise("ping", LinkScope.LOCAL_NETWORK, frozenset())

        self.assertTrue(decision.allowed)

    def test_an_unknown_operation_is_refused(self) -> None:
        decision = authorise("drop_tables", LinkScope.LOCAL_NETWORK)

        self.assertIs(decision.reason, AuthReason.UNKNOWN_OP)
        self.assertTrue(decision.refused)
        self.assertIsNone(decision.op)

    def test_an_empty_operation_name_is_refused(self) -> None:
        self.assertIs(
            authorise("", LinkScope.LOCAL_NETWORK).reason,
            AuthReason.UNKNOWN_OP,
        )

    def test_device_management_is_refused_as_console_only(self) -> None:
        for scope in LinkScope:
            with self.subTest(scope=scope):
                self.assertIs(
                    authorise("revoke_device", scope).reason,
                    AuthReason.CONSOLE_ONLY,
                )

    def test_a_capability_outside_the_scope_is_out_of_scope(self) -> None:
        self.assertIs(
            authorise("run_app", LinkScope.REMOTE_TUNNEL).reason,
            AuthReason.OUT_OF_SCOPE,
        )

    def test_a_capability_the_device_was_not_granted_is_not_granted(
        self,
    ) -> None:
        # Distinct from out_of_scope: the scope allows it, this device does not
        # have it. The audit log should be able to tell them apart.
        decision = authorise(
            "search",
            LinkScope.LOCAL_NETWORK,
            frozenset({Capability.ASK}),
        )

        self.assertIs(decision.reason, AuthReason.NOT_GRANTED)

    def test_an_unknown_scope_refuses_rather_than_defaulting_open(self) -> None:
        # Fail-closed: a scope with no profile grants nothing.
        class Rogue:
            value = "rogue"

        decision = authorise("search", Rogue())  # type: ignore[arg-type]

        self.assertFalse(decision.allowed)


class TestPermissionEngineStillApplies(unittest.TestCase):
    def test_a_write_needs_approval_even_on_the_local_network(self) -> None:
        # The link permits it; the permission engine still wants a human.
        decision = authorise("write_file", LinkScope.LOCAL_NETWORK)

        self.assertTrue(decision.needs_approval)
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.refused)

    def test_deleting_a_file_needs_approval_rather_than_being_impossible(
        self,
    ) -> None:
        # Mapping delete to FORBIDDEN would deny it outright and make
        # DELETE_FILES a capability nothing could ever exercise.
        decision = authorise("delete_file", LinkScope.LOCAL_NETWORK)

        self.assertTrue(decision.needs_approval)

    def test_running_an_application_needs_approval(self) -> None:
        self.assertTrue(
            authorise("run_app", LinkScope.LOCAL_NETWORK).needs_approval
        )

    def test_a_read_is_allowed_outright(self) -> None:
        decision = authorise("read_file", LinkScope.LOCAL_NETWORK)

        self.assertTrue(decision.allowed)
        self.assertIs(decision.permission, PermissionDecision.ALLOW)

    def test_the_scope_check_runs_before_the_permission_check(self) -> None:
        # A remote request to run an application should read as out_of_scope,
        # not as needing approval, or the phone would offer to ask for
        # something that can never be granted.
        self.assertIs(
            authorise("run_app", LinkScope.REMOTE_TUNNEL).reason,
            AuthReason.OUT_OF_SCOPE,
        )


class TestTranslationFailsClosed(unittest.TestCase):
    """
    The guard on turning a permission decision into an answer.

    The permission engine grew from three decisions to five when it moved to
    the five authorization levels. Code that branched on the old three and let
    an ``else`` mean "allowed" would have silently granted both new decisions.
    A remote device is the wrong place to discover that, so the translation is
    checked here decision by decision.
    """

    def test_only_an_explicit_allow_allows(self) -> None:
        allowing = {
            decision
            for decision in PermissionDecision
            if _reason_for(decision) is AuthReason.ALLOWED
        }

        self.assertEqual(allowing, {PermissionDecision.ALLOW})

    def test_every_confirmation_decision_asks_for_approval(self) -> None:
        for decision in (
            PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
            PermissionDecision.REQUIRE_UI_CONFIRMATION,
            PermissionDecision.REQUIRE_TYPED_SECRET,
        ):
            with self.subTest(decision=decision):
                self.assertIs(
                    _reason_for(decision),
                    AuthReason.NEEDS_APPROVAL,
                )

    def test_deny_is_refused(self) -> None:
        self.assertIs(
            _reason_for(PermissionDecision.DENY),
            AuthReason.PERMISSION_DENIED,
        )

    def test_every_decision_the_engine_can_return_is_handled(self) -> None:
        # Not a tautology: it fails the day someone adds a sixth decision to
        # the permission engine without deciding what the link should do
        # with it.
        for decision in PermissionDecision:
            with self.subTest(decision=decision):
                self.assertIn(
                    _reason_for(decision),
                    {
                        AuthReason.ALLOWED,
                        AuthReason.NEEDS_APPROVAL,
                        AuthReason.PERMISSION_DENIED,
                    },
                )

    def test_a_decision_the_translation_has_never_seen_is_refused(
        self,
    ) -> None:
        # Stands in for a decision added later. Refusal is the only safe
        # answer to a value this module was not written against.
        class UnknownDecision(Enum):
            SOMETHING_NEW = "something_new"

        self.assertIs(
            _reason_for(UnknownDecision.SOMETHING_NEW),
            AuthReason.PERMISSION_DENIED,
        )


class TestManagingDevicesIsRefusedOverTheLink(unittest.TestCase):
    def test_listing_devices_is_refused(self) -> None:
        decision = authorise("list_devices", LinkScope.LOCAL_NETWORK)

        self.assertTrue(decision.refused)
        self.assertFalse(decision.needs_approval)

    def test_revoking_a_device_is_refused(self) -> None:
        # A phone that could revoke devices could revoke every device but
        # itself. The console is the only place this happens.
        self.assertTrue(
            authorise("revoke_device", LinkScope.LOCAL_NETWORK).refused
        )


class TestDecisionShape(unittest.TestCase):
    def test_allowed_needs_approval_and_refused_are_mutually_exclusive(
        self,
    ) -> None:
        for op in LinkOp:
            for scope in LinkScope:
                with self.subTest(op=op, scope=scope):
                    decision = authorise(op.value, scope)
                    flags = (
                        decision.allowed,
                        decision.needs_approval,
                        decision.refused,
                    )

                    self.assertEqual(sum(flags), 1, decision.describe())

    def test_describe_names_the_operation_and_the_outcome(self) -> None:
        text = authorise("run_app", LinkScope.REMOTE_TUNNEL).describe()

        self.assertIn("run_app", text)
        self.assertIn("out_of_scope", text)


class TestPeerClassification(unittest.TestCase):
    def test_private_ranges_are_local(self) -> None:
        for address in ("192.168.1.5", "10.0.0.7", "172.16.0.1", "172.31.255.254"):
            with self.subTest(address=address):
                self.assertIs(
                    scope_for_peer(address), LinkScope.LOCAL_NETWORK
                )

    def test_loopback_is_local(self) -> None:
        self.assertIs(scope_for_peer("127.0.0.1"), LinkScope.LOCAL_NETWORK)

    def test_link_local_is_local(self) -> None:
        self.assertIs(scope_for_peer("169.254.3.4"), LinkScope.LOCAL_NETWORK)

    def test_ipv6_loopback_link_local_and_unique_local_are_local(self) -> None:
        for address in ("::1", "fe80::1", "fd00::abcd"):
            with self.subTest(address=address):
                self.assertIs(
                    scope_for_peer(address), LinkScope.LOCAL_NETWORK
                )

    def test_public_addresses_are_refused(self) -> None:
        for address in ("8.8.8.8", "172.32.0.1", "2606:4700::1"):
            with self.subTest(address=address):
                self.assertIsNone(scope_for_peer(address))

    def test_documentation_ranges_are_not_local(self) -> None:
        # ipaddress.is_private answers True for these, which is a broader
        # question than "is this a home network". An earlier version of this
        # module used that predicate and accepted a peer at 203.0.113.7.
        for address in ("203.0.113.7", "192.0.2.1", "198.51.100.4"):
            with self.subTest(address=address):
                self.assertIsNone(scope_for_peer(address))

    def test_reserved_and_benchmark_ranges_are_not_local(self) -> None:
        for address in ("240.0.0.1", "198.18.0.1", "0.1.2.3", "255.255.255.255"):
            with self.subTest(address=address):
                self.assertIsNone(scope_for_peer(address))

    def test_carrier_grade_nat_is_not_local(self) -> None:
        # The address a phone gets from a mobile carrier. Private-looking, and
        # not on the user's network.
        self.assertIsNone(scope_for_peer("100.64.0.1"))

    def test_an_ipv4_mapped_address_is_judged_on_its_ipv4_form(self) -> None:
        # How an IPv4 peer appears on a dual-stack socket.
        self.assertIs(
            scope_for_peer("::ffff:192.168.1.5"), LinkScope.LOCAL_NETWORK
        )
        self.assertIsNone(scope_for_peer("::ffff:8.8.8.8"))

    def test_unparseable_input_is_refused_rather_than_assumed_local(
        self,
    ) -> None:
        for address in ("", "not an address", "192.168.1", "hostname.local", "  "):
            with self.subTest(address=address):
                self.assertIsNone(scope_for_peer(address))

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        self.assertIs(
            scope_for_peer("  192.168.1.5 "), LinkScope.LOCAL_NETWORK
        )

    def test_the_local_ranges_are_the_documented_ones(self) -> None:
        self.assertEqual(
            {str(network) for network in LOCAL_NETWORKS},
            {
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "169.254.0.0/16",
                "127.0.0.0/8",
            },
        )


if __name__ == "__main__":
    unittest.main()
