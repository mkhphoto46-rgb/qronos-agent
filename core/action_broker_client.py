"""
The user runtime's side of the Elevated Action Broker.

The runtime holds the approval: the permission gate's ``ActionGrant`` for one
exact ``ActionRequest``, issued after the person approved it. That grant is an
in-process object on a monotonic clock and cannot cross into another process,
so ``BrokerSession`` turns it into an approval envelope the broker can check:

    action id, category and target exactly as approved; the SHA-256 digest of
    the canonical typed action; issued/expiry wall-clock times no longer than
    the grant has left (and never more than the broker's 180 s limit); a
    fresh nonce; and an HMAC over all of it with this session's key.

Then, for that one action:

    1. listen on a fresh named pipe (first instance, unguessable name) with a
       fresh session key;
    2. launch the broker through the injected launcher — in production
       ``core/uac_launch.py``, the normal UAC prompt; the person can say no.
       Its command line carries only the pipe name and this process's id,
       never the key;
    3. accept one connection, only from the exact process launched (process id
       checked on Windows), release the session key to it on the pipe, run
       the mutual handshake, and require its hello to name that process;
    4. send the request — the typed action, the approval envelope and the
       person's typed approval secret, which the broker checks itself —
       wait for the result, and wait for the broker to exit.

Cancellation (Stop, Pause, shutdown) is checked at every handoff before the
request leaves: before launching, after the launcher returns (the UAC prompt
may have been up for a while), and immediately before sending. A cancelled
session sends nothing (``BrokerCancelled``); the broker it may have started
finds no key and exits. Once the request has left, nothing is cancelled: the
outcome is reported, or is unknown.

A grant is used once by this session: a second attempt with the same grant
is refused here before anything is launched, and the broker's own single-use
journal refuses a replay anyway.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol

from core.action_broker import (
    FROM_RUNTIME as BROKER_RECEIVES,
    MAX_APPROVAL_SECONDS,
    TO_RUNTIME as BROKER_SENDS,
    action_digest,
    approval_mac,
)
from core.actions import ActionRequest
from security.gate import ActionGrant
from core.supervisor_ipc import (
    PROTOCOL_VERSION,
    ChannelError,
    ChannelServer,
    new_key,
)


class BrokerUnavailable(RuntimeError):
    """The broker could not be asked; nothing was performed by this session."""


class BrokerCancelled(BrokerUnavailable):
    """Stop, Pause or shutdown came first; the request was never sent."""


class BrokerOutcomeUnknown(BrokerUnavailable):
    """
    The request was sent but its answer was lost: the broker may have acted.

    A subclass, so a caller that only knows ``BrokerUnavailable`` still
    refuses; a caller that tells them apart must report "outcome unknown"
    and never retry automatically.
    """


class ElevatedProcess(Protocol):
    pid: int

    def wait(self, timeout: float) -> Optional[int]:
        """The exit code, or None if still running after ``timeout``."""

    def close(self) -> None:
        """Release the process handle, if any; idempotent."""


class BrokerLauncher(Protocol):
    def launch(self, address: str, runtime_pid: int) -> ElevatedProcess:
        """Start the broker so that it connects to ``address``; the key comes over the pipe."""


@dataclass(frozen=True)
class TypedAction:
    """An exact broker action: a registered kind and its parameters."""

    kind: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class BrokerResult:
    ok: bool
    outcome: str
    detail: str


def build_approval(
    action: TypedAction,
    request: ActionRequest,
    grant: ActionGrant,
    key: bytes,
    wall_clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """The approval envelope for one live grant of one exact request."""
    if (grant.action_id, grant.category, grant.target) != (request.action_id, request.category, request.target):
        raise BrokerUnavailable("The grant does not belong to this action request.")

    remaining = grant.expires_at - monotonic()

    if remaining <= 0:
        raise BrokerUnavailable("The approval has expired.")

    now = wall_clock()
    approval = {
        "actionId": request.action_id,
        "category": request.category.value,
        "target": request.target,
        "digest": action_digest(action.kind, action.params),
        "issuedAt": now,
        "expiresAt": now + min(remaining, MAX_APPROVAL_SECONDS),
        "nonce": secrets.token_hex(16),
    }
    approval["mac"] = approval_mac(key, approval)
    return approval


class BrokerSession:
    """Runs exactly one approved action through a freshly launched broker."""

    def __init__(
        self,
        launcher: BrokerLauncher,
        accept_timeout_seconds: float = 120.0,
        reply_timeout_seconds: float = 60.0,
        exit_timeout_seconds: float = 10.0,
        peer_pid=None,
    ) -> None:
        self._launcher = launcher
        self._accept_timeout = accept_timeout_seconds
        self._reply_timeout = reply_timeout_seconds
        self._exit_timeout = exit_timeout_seconds
        self._peer_pid = peer_pid
        self._used: set[str] = set()

    def perform(
        self,
        action: TypedAction,
        request: ActionRequest,
        grant: ActionGrant,
        secret: str = "",
        cancelled: Callable[[], bool] = lambda: False,
    ) -> BrokerResult:
        if not isinstance(grant, ActionGrant):
            raise BrokerUnavailable("Only a permission-gate grant can authorize the broker.")

        if request.action_id in self._used:
            raise BrokerUnavailable("This approval has already been used.")

        if cancelled():
            raise BrokerCancelled("Cancelled before the broker was started; nothing was sent.")

        key = new_key()
        approval = build_approval(action, request, grant, key)
        self._used.add(request.action_id)

        server = ChannelServer("action-broker", key, receive=BROKER_SENDS, send=BROKER_RECEIVES)
        process = None

        try:
            try:
                process = self._launcher.launch(server.address, os.getpid())
            except Exception as error:
                raise BrokerUnavailable(f"The broker was not started ({type(error).__name__}).") from error

            if cancelled():
                # The UAC prompt may have been up a long time. The broker it
                # started gets no key and exits on its own deadline.
                raise BrokerCancelled("Cancelled while the broker was being started; nothing was sent.")

            try:
                channel = server.accept(self._accept_timeout, expected_pid=process.pid, peer_pid=self._peer_pid,
                                        release_key=True)
            except ChannelError as error:
                raise BrokerUnavailable(f"The broker did not connect ({type(error).__name__}).") from error

            sent = False
            try:
                hello = channel.receive(self._reply_timeout)
                if hello["kind"] != "hello" or hello["pid"] != process.pid or hello["protocol"] != PROTOCOL_VERSION:
                    raise BrokerUnavailable("The broker introduced itself wrongly.")

                if cancelled():
                    raise BrokerCancelled("Cancelled just before the request was sent; nothing was sent.")

                # From here the request may have left, so a failure means the
                # broker may have acted: the outcome is unknown, not "refused".
                sent = True
                channel.send("request", action={"kind": action.kind, "params": dict(action.params)},
                             approval=approval, secret=secret)
                reply = channel.receive(self._reply_timeout)
                if reply["kind"] != "result":
                    raise BrokerOutcomeUnknown("The broker answered wrongly; the outcome is unknown.")
            except ChannelError as error:
                if not sent:
                    raise BrokerUnavailable(f"The broker did not introduce itself ({type(error).__name__}).") from error
                raise BrokerOutcomeUnknown(
                    f"The broker's answer was lost ({type(error).__name__}); the outcome is unknown."
                ) from error
            finally:
                channel.close()

            process.wait(self._exit_timeout)
            return BrokerResult(ok=reply["ok"], outcome=reply["outcome"], detail=reply["detail"])
        finally:
            server.close()
            close = getattr(process, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def broker_arguments(address: str, runtime_pid: int) -> list[str]:
    """
    The broker's exact command-line arguments; nothing else is ever passed.

    Only public identifiers: the pipe's name and the runtime's process id.
    The session key reaches the broker over the pipe, after both process ids
    are checked.
    """
    return ["action-broker", "--pipe", address, "--runtime-pid", str(int(runtime_pid))]


def bundled_executable() -> Path:
    import sys

    return Path(sys.executable)
