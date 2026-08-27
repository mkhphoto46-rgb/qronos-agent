"""
What a linked device is allowed to ask for.

This module is the reason Layer 2 is a small change rather than a rewrite. It
separates two questions that are easy to conflate:

    Where did this connection come from?      ->  LinkScope
    What may a connection from there do?      ->  Capability

A connection over the local network and a connection through an internet relay
run identical code and differ only in scope. The remote profile grants three of
the nine capabilities, and nothing at runtime can widen it.

Three invariants, each covered by a test:

1. Effective capability is the *intersection* of the scope profile and any
   per-device grants. A device grant can narrow. There is no code path by which
   it widens.
2. The link never widens permissions. It sits in front of the existing
   permission engine in ``security/permissions.py``, which still runs and can
   still demand approval or refuse. The stricter of the two decides.
3. An operation with no capability mapping is refused. A test asserts the
   mapping is exhaustive, so a policy cannot quietly become unreachable the way
   ``ActivityMode.IDLE`` did.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from enum import Enum

from security.permissions import (
    PermissionDecision,
    PermissionLevel,
    evaluate_permission,
)


class LinkScope(Enum):
    """Where a connection reached Qronos from."""

    # Phone and PC on the same network. This is all of Layer 1.
    LOCAL_NETWORK = "local_network"

    # Through an outbound relay from the internet. Defined here, and honoured
    # by the capability tables below, but Layer 1's server refuses it. Layer 2
    # turns it on; the profile it will run under already exists.
    REMOTE_TUNNEL = "remote_tunnel"


class Capability(Enum):
    """A thing a linked session may be permitted to do."""

    ASK = "ask"
    SEARCH_WEB = "search_web"
    READ_STATUS = "read_status"
    READ_FILES = "read_files"
    WRITE_FILES = "write_files"
    RUN_APPLICATION = "run_application"
    CONTROL_SYSTEM = "control_system"
    DELETE_FILES = "delete_files"
    MANAGE_DEVICES = "manage_devices"


# Pairing and revoking happen at the PC, never over the link, in either layer.
# A phone that has been stolen must not be able to enrol its replacement or
# revoke the real owner's device. This set appears in no scope profile, and the
# exhaustiveness test knows that is deliberate rather than an omission.
CONSOLE_ONLY = frozenset({Capability.MANAGE_DEVICES})


SCOPE_PROFILES: dict[LinkScope, frozenset[Capability]] = {
    # On the user's own network the phone is a remote control for the machine
    # in the next room. It gets the same reach the person at the keyboard has,
    # and the permission engine still gates the dangerous parts.
    LinkScope.LOCAL_NETWORK: frozenset(
        {
            Capability.ASK,
            Capability.SEARCH_WEB,
            Capability.READ_STATUS,
            Capability.READ_FILES,
            Capability.WRITE_FILES,
            Capability.RUN_APPLICATION,
            Capability.CONTROL_SYSTEM,
            Capability.DELETE_FILES,
        }
    ),
    # From the internet the phone can ask questions and see how the machine is
    # doing. It cannot touch files, start programs or control anything. A phone
    # is lost or stolen far more often than a home network is breached, so the
    # remote profile is drawn for the phone that is no longer in the owner's
    # hand.
    LinkScope.REMOTE_TUNNEL: frozenset(
        {
            Capability.ASK,
            Capability.SEARCH_WEB,
            Capability.READ_STATUS,
        }
    ),
}


class LinkOp(Enum):
    """An operation the phone can name in a request."""

    PING = "ping"
    ASK = "ask"
    SEARCH = "search"
    STATUS = "status"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    DELETE_FILE = "delete_file"
    RUN_APP = "run_app"
    SYSTEM_CONTROL = "system_control"
    LIST_DEVICES = "list_devices"
    REVOKE_DEVICE = "revoke_device"


# Every LinkOp appears here. ``None`` means the operation needs an
# authenticated session but no capability, and PING is the only one: a
# keepalive that could be refused would make an idle session indistinguishable
# from a broken one.
OP_CAPABILITY: dict[LinkOp, Capability | None] = {
    LinkOp.PING: None,
    LinkOp.ASK: Capability.ASK,
    LinkOp.SEARCH: Capability.SEARCH_WEB,
    LinkOp.STATUS: Capability.READ_STATUS,
    LinkOp.READ_FILE: Capability.READ_FILES,
    LinkOp.WRITE_FILE: Capability.WRITE_FILES,
    LinkOp.DELETE_FILE: Capability.DELETE_FILES,
    LinkOp.RUN_APP: Capability.RUN_APPLICATION,
    LinkOp.SYSTEM_CONTROL: Capability.CONTROL_SYSTEM,
    LinkOp.LIST_DEVICES: Capability.MANAGE_DEVICES,
    LinkOp.REVOKE_DEVICE: Capability.MANAGE_DEVICES,
}


# What the existing permission engine is asked about. Deleting a file maps to
# CREATE_OR_EDIT rather than SENSITIVE on purpose: SENSITIVE is an outright
# refusal, which would make DELETE_FILES a capability that can never be
# exercised, and an unreachable policy is worse than a strict one. Approval is
# the honest answer, and it matches
# ``CONFIG.security.destructive_actions_require_approval``.
OP_PERMISSION: dict[LinkOp, PermissionLevel] = {
    LinkOp.PING: PermissionLevel.SAFE_READ,
    LinkOp.ASK: PermissionLevel.SAFE_READ,
    LinkOp.SEARCH: PermissionLevel.SAFE_READ,
    LinkOp.STATUS: PermissionLevel.SAFE_READ,
    LinkOp.READ_FILE: PermissionLevel.SAFE_READ,
    LinkOp.WRITE_FILE: PermissionLevel.CREATE_OR_EDIT,
    LinkOp.DELETE_FILE: PermissionLevel.CREATE_OR_EDIT,
    LinkOp.RUN_APP: PermissionLevel.RUN_APPLICATION,
    LinkOp.SYSTEM_CONTROL: PermissionLevel.CONTROL_SYSTEM,
    LinkOp.LIST_DEVICES: PermissionLevel.SENSITIVE,
    LinkOp.REVOKE_DEVICE: PermissionLevel.SENSITIVE,
}


class AuthReason(Enum):
    """Why a request was allowed or refused."""

    ALLOWED = "allowed"
    UNKNOWN_OP = "unknown_op"
    CONSOLE_ONLY = "console_only"
    OUT_OF_SCOPE = "out_of_scope"
    NOT_GRANTED = "not_granted"
    NEEDS_APPROVAL = "needs_approval"
    PERMISSION_DENIED = "permission_denied"


@dataclass(frozen=True)
class Authorisation:
    """The decision on one request."""

    op_name: str
    scope: LinkScope
    reason: AuthReason
    op: LinkOp | None = None
    capability: Capability | None = None
    permission: PermissionDecision | None = None

    @property
    def allowed(self) -> bool:
        """True only when the operation may run right now, unaided."""
        return self.reason is AuthReason.ALLOWED

    @property
    def needs_approval(self) -> bool:
        """
        True when the user must approve before this runs.

        Distinct from a refusal: the phone should offer to ask, not report a
        wall.
        """
        return self.reason is AuthReason.NEEDS_APPROVAL

    @property
    def refused(self) -> bool:
        """True when no amount of approval would help."""
        return not self.allowed and not self.needs_approval

    def describe(self) -> str:
        return f"{self.op_name} from {self.scope.value}: {self.reason.value}"


def resolve_capabilities(
    scope: LinkScope,
    grants: frozenset[Capability] | None = None,
) -> frozenset[Capability]:
    """
    What a session in this scope, on this device, may actually do.

    ``grants`` of ``None`` means "whatever the scope allows". Anything else is
    intersected with the scope profile, so a grant listing a capability the
    scope withholds has no effect. That direction is deliberate and is the
    reason a per-device setting can never become a privilege escalation.
    """

    profile = SCOPE_PROFILES.get(scope, frozenset())

    if grants is None:
        return profile

    return profile & frozenset(grants)


def authorise(
    op_name: str,
    scope: LinkScope,
    grants: frozenset[Capability] | None = None,
) -> Authorisation:
    """
    Decide one request.

    Fail-closed throughout: an operation nobody recognises, an operation with
    no capability mapping, or a scope with no profile all end in a refusal
    rather than in a default allowance.
    """

    try:
        op = LinkOp(op_name)
    except ValueError:
        return Authorisation(
            op_name=op_name,
            scope=scope,
            reason=AuthReason.UNKNOWN_OP,
        )

    if op not in OP_CAPABILITY:
        # Unreachable while the exhaustiveness test passes, and a refusal
        # rather than a crash if that test is ever deleted.
        return Authorisation(
            op_name=op_name,
            scope=scope,
            op=op,
            reason=AuthReason.UNKNOWN_OP,
        )

    capability = OP_CAPABILITY[op]

    if capability is not None:
        if capability in CONSOLE_ONLY:
            return Authorisation(
                op_name=op_name,
                scope=scope,
                op=op,
                capability=capability,
                reason=AuthReason.CONSOLE_ONLY,
            )

        if capability not in SCOPE_PROFILES.get(scope, frozenset()):
            return Authorisation(
                op_name=op_name,
                scope=scope,
                op=op,
                capability=capability,
                reason=AuthReason.OUT_OF_SCOPE,
            )

        if capability not in resolve_capabilities(scope, grants):
            return Authorisation(
                op_name=op_name,
                scope=scope,
                op=op,
                capability=capability,
                reason=AuthReason.NOT_GRANTED,
            )

    # The link has said yes. The permission engine still gets a say, and it is
    # allowed to be stricter.
    permission = evaluate_permission(OP_PERMISSION[op])

    if permission is PermissionDecision.DENY:
        reason = AuthReason.PERMISSION_DENIED
    elif permission is PermissionDecision.REQUIRE_APPROVAL:
        reason = AuthReason.NEEDS_APPROVAL
    else:
        reason = AuthReason.ALLOWED

    return Authorisation(
        op_name=op_name,
        scope=scope,
        op=op,
        capability=capability,
        permission=permission,
        reason=reason,
    )


# ------------------------------------------------------------ peer addresses


# The networks a phone in the same building can actually have an address on.
#
# Listed explicitly rather than asked of ``ipaddress.is_private``, which is a
# broader question than the one being asked here: it answers True for the
# documentation ranges (203.0.113.0/24 and friends), the benchmarking range,
# and 240.0.0.0/4, none of which are a home network. An early version of this
# module used that predicate and accepted a peer at 203.0.113.7 as local.
#
# Carrier-grade NAT space (100.64.0.0/10) is deliberately absent. It is the
# address a phone gets from a mobile carrier, not from a home router, so a
# connection from it is not on the user's network however private the range
# looks.
LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",        # RFC 1918
        "172.16.0.0/12",     # RFC 1918
        "192.168.0.0/16",    # RFC 1918
        "169.254.0.0/16",    # link-local, no DHCP server present
        "127.0.0.0/8",       # loopback, the desktop app talking to itself
    )
)

LOCAL_NETWORKS_V6 = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "::1/128",           # loopback
        "fe80::/10",         # link-local
        "fc00::/7",          # unique local, RFC 4193
    )
)


def scope_for_peer(address: str) -> LinkScope | None:
    """
    Classify the address a connection arrived from.

    Returns ``None`` for anything that is not recognisably a machine on the
    user's own network, including an address that cannot be parsed at all. The
    caller refuses on ``None``; there is no "assume local" branch.
    """

    try:
        parsed = ipaddress.ip_address(address.strip())
    except ValueError:
        return None

    # An IPv4 address arriving on a dual-stack socket looks like
    # ::ffff:192.168.1.5. Judge the address the packet really came from.
    mapped = getattr(parsed, "ipv4_mapped", None)

    if mapped is not None:
        parsed = mapped

    networks = (
        LOCAL_NETWORKS
        if isinstance(parsed, ipaddress.IPv4Address)
        else LOCAL_NETWORKS_V6
    )

    for network in networks:
        if parsed in network:
            return LinkScope.LOCAL_NETWORK

    return None


def main() -> None:
    """Show both profiles side by side."""

    print("capability            local  remote")

    for capability in Capability:
        local = capability in SCOPE_PROFILES[LinkScope.LOCAL_NETWORK]
        remote = capability in SCOPE_PROFILES[LinkScope.REMOTE_TUNNEL]
        mark = lambda flag: " yes " if flag else "  no "  # noqa: E731

        print(f"{capability.value:22}{mark(local)}{mark(remote)}")

    print()

    for op_name in ("ping", "search", "run_app", "revoke_device", "nonsense"):
        for scope in LinkScope:
            print(" ", authorise(op_name, scope).describe())


if __name__ == "__main__":
    main()
