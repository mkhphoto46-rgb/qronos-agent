"""
The device link inside the running Qronos process: one object, one authority.

The link has several moving parts — the device registry, the pairing window,
the audit log, the listener, and for Layer 2 the relay dialler — and the
desktop needs to do a handful of things with them: switch the link on and
off, pair a phone, list devices, revoke one, give one remote access for a
while, and say where the relay is. ``LinkRuntime`` owns the parts and offers
exactly those actions, so the runtime bridge and the desktop never assemble
the link themselves and never reach past it.

Being the single authority is what makes revocation immediate. A phone
revoked through ``LinkRuntime.revoke`` loses its open sessions at once and its
parked relay connection within moments; the registry's cross-process
freshness covers anything changed behind its back, at the next request.

Nothing starts by itself:

    The link starts only when ``link_enabled`` is set, which mirrors
    ``CONFIG.security.link_enabled`` (default off).
    Remote access additionally needs ``remote_access_enabled`` (default off)
    and a relay endpoint the user entered. Without both, the server accepts
    only the local network and no outbound connection is made.

What answers questions is injected through ``LinkServices``, because those are
the runtime's models, not the link's. ``remote_chat`` is the only thing a phone
outside the house can reach, wrapped in ``ToolFreeAsk``; until the runtime
supplies an assistant path with on-screen approval for the local network,
local questions use the same answer-only path.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.config import CONFIG
from core.link_audit import DEFAULT_AUDIT_PATH, AuditEvent, AuditLog
from core.link_capability import LinkScope
from core.link_devices import DEFAULT_DEVICE_PATH, DeviceRecord, DeviceRegistry
from core.link_handlers import (
    ChatFunction,
    SearchFunction,
    StatusProvider,
    ToolFreeAsk,
    default_handlers,
)
from core.link_pairing import DEFAULT_PORT, PairingService
from core.link_server import LinkServer, LinkSettings
from core.link_tunnel import (
    Connector,
    RelayConfigError,
    RelayDialler,
    RelayEndpoint,
    connect_relay,
)


DEFAULT_RELAY_PATH = CONFIG.paths.data / "link_relay.json"

RELAY_SCHEMA_VERSION = 1


# ------------------------------------------------------------ relay settings


def load_relay_endpoint(path: Path | None) -> RelayEndpoint | None:
    """
    The relay the user configured, or ``None``.

    A file that exists but cannot be understood raises rather than reading as
    "no relay": a silent fallback would hide a configuration the user believes
    is in force.
    """

    if path is None or not Path(path).exists():
        return None

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RelayConfigError(f"cannot read the relay settings: {exc}") from exc

    if not isinstance(raw, dict) or raw.get("version") != RELAY_SCHEMA_VERSION:
        raise RelayConfigError("the relay settings are not in a known format")

    relay = raw.get("relay")

    if relay is None:
        return None

    if not isinstance(relay, dict):
        raise RelayConfigError("the relay settings are malformed")

    try:
        return RelayEndpoint(
            host=str(relay["host"]),
            port=int(relay.get("port", 443)),
            server_name=None if relay.get("server_name") is None else str(relay["server_name"]),
            ca_file=None if relay.get("ca_file") is None else str(relay["ca_file"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RelayConfigError(f"the relay settings are malformed: {exc}") from exc


def save_relay_endpoint(path: Path, endpoint: RelayEndpoint | None) -> None:
    """Write the relay settings. Nothing in them is secret."""

    payload: dict[str, Any] = {"version": RELAY_SCHEMA_VERSION, "relay": None}

    if endpoint is not None:
        payload["relay"] = {
            "host": endpoint.host,
            "port": endpoint.port,
            "server_name": endpoint.server_name,
            "ca_file": endpoint.ca_file,
        }

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)


# ------------------------------------------------------------------ settings


@dataclass(frozen=True)
class LinkRuntimeSettings:
    """Where the link keeps its files and which of its parts are switched on."""

    link_enabled: bool = False
    remote_access_enabled: bool = False
    port: int = DEFAULT_PORT
    bind_host: str = "0.0.0.0"

    device_path: Path | None = DEFAULT_DEVICE_PATH
    audit_path: Path | None = DEFAULT_AUDIT_PATH
    relay_path: Path | None = DEFAULT_RELAY_PATH

    # The address put in the QR code. ``None`` picks the PC's own LAN address.
    pairing_host: str | None = None

    @classmethod
    def from_config(cls, config: Any = CONFIG, **overrides: Any) -> LinkRuntimeSettings:
        """The documented switches, both off unless the user turned them on."""

        values: dict[str, Any] = {
            "link_enabled": bool(config.security.link_enabled),
            "remote_access_enabled": bool(config.security.remote_access_enabled),
        }
        values.update(overrides)

        return cls(**values)


@dataclass(frozen=True)
class LinkServices:
    """
    What the runtime lends the link for answering. All optional.

    ``remote_chat`` — ``chat(messages) -> text`` on the brain's plain chat call,
    with the existing Fast model and settings — becomes a ``ToolFreeAsk``. It
    must not route through the task router, the orchestrator or the queue.
    ``transcribe`` and ``speak`` are the PC's speech-to-text and text-to-speech
    for push-to-talk. ``local_ask``, if given, is the assistant path for the
    local network; it must raise approvals on the PC screen as typed turns do.
    """

    status: StatusProvider | None = None
    remote_chat: ChatFunction | None = None
    local_ask: SearchFunction | None = None
    transcribe: Any = None
    speak: Any = None


@dataclass(frozen=True)
class DeviceView:
    """A device as the desktop may show it. The key is not in it."""

    device_id: str
    name: str
    status: str
    created_at: float
    last_seen_at: float | None
    paired_from: str
    remote_enabled: bool
    remote_expires_at: float | None
    remote_allowed: bool

    @classmethod
    def of(cls, record: DeviceRecord, now: float) -> DeviceView:
        return cls(
            device_id=record.device_id,
            name=record.name,
            status=record.status.value,
            created_at=record.created_at,
            last_seen_at=record.last_seen_at,
            paired_from=record.paired_from,
            remote_enabled=record.remote_enabled,
            remote_expires_at=record.remote_expires_at,
            remote_allowed=record.remote_allowed(now),
        )

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


# ------------------------------------------------------------------- runtime


class LinkRuntime:
    """The link's lifecycle and the actions the desktop may take on it."""

    def __init__(
        self,
        settings: LinkRuntimeSettings,
        services: LinkServices | None = None,
        clock: Callable[[], float] | None = None,
        relay_connector: Connector = connect_relay,
        tunnel_settings: Any = None,
    ) -> None:
        self.settings = settings
        self.services = services if services is not None else LinkServices()
        self.clock = clock if clock is not None else time.time
        self.relay_connector = relay_connector
        self.tunnel_settings = tunnel_settings

        self.registry = DeviceRegistry(settings.device_path, clock=self.clock)
        self.audit = AuditLog(settings.audit_path, clock=self.clock)

        self.server: LinkServer | None = None
        self.pairing: PairingService | None = None
        self.dialler: RelayDialler | None = None
        self.relay: RelayEndpoint | None = None

    # -------------------------------------------------------------- queries

    @property
    def running(self) -> bool:
        return self.server is not None

    @property
    def remote_active(self) -> bool:
        return self.dialler is not None

    def status(self) -> dict[str, Any]:
        """For the desktop's link page. No keys, no addresses of phones."""

        remote_reason = None

        if not self.settings.remote_access_enabled:
            remote_reason = "remote_access_off"
        elif self.relay is None:
            try:
                configured = self._configured_relay()
            except RelayConfigError:
                configured = None
                remote_reason = "relay_settings_unreadable"

            if configured is None and remote_reason is None:
                remote_reason = "no_relay_configured"

        return {
            "enabled": self.settings.link_enabled,
            "running": self.running,
            "port": None if self.server is None else self.server.address[1],
            "remote_active": self.remote_active,
            "remote_unavailable": remote_reason,
            "parked_devices": (
                0 if self.dialler is None else len(self.dialler.parked_devices())
            ),
            "pairing_open": self.pairing is not None and self.pairing.is_open,
        }

    def devices(self) -> list[DeviceView]:
        now = self.clock()

        return [DeviceView.of(record, now) for record in self.registry.all()]

    # ------------------------------------------------------------ lifecycle

    def start(self) -> bool:
        """
        Start the link if it is switched on. Returns whether it is running.

        Remote access is added only when it is switched on and a relay is
        configured; otherwise the server is Layer 1 alone and nothing dials out.
        """

        if self.server is not None:
            return True

        if not self.settings.link_enabled:
            return False

        relay = None

        if self.settings.remote_access_enabled:
            try:
                relay = self._configured_relay()
            except RelayConfigError:
                # The local link still starts; remote stays off and the status
                # says why. Guessing at a damaged relay setting is not an option.
                relay = None

        scopes = {LinkScope.LOCAL_NETWORK}

        if relay is not None:
            scopes.add(LinkScope.REMOTE_TUNNEL)

        server = LinkServer(
            registry=self.registry,
            audit=self.audit,
            handlers=self._handlers(relay),
            settings=LinkSettings(
                host=self.settings.bind_host,
                port=self.settings.port,
                allowed_scopes=frozenset(scopes),
            ),
            clock=self.clock,
        )
        server.start()

        pairing = PairingService(
            self.registry,
            host=self.settings.pairing_host,
            port=server.address[1],
            clock=self.clock,
        )
        server.pairing = pairing

        self.server = server
        self.pairing = pairing
        self.relay = relay

        if relay is not None:
            self._start_dialler(relay)

        return True

    def stop(self) -> None:
        dialler, self.dialler = self.dialler, None

        if dialler is not None:
            dialler.stop()

        server, self.server = self.server, None

        if server is not None:
            server.stop()

        if self.pairing is not None:
            self.pairing.cancel()

        self.pairing = None
        self.relay = None

    def restart(self) -> bool:
        self.stop()

        return self.start()

    # -------------------------------------------------------------- pairing

    def open_pairing(self, name: str) -> str:
        """
        Open a pairing window and return the QR code's contents.

        The caller shows it and does nothing else with it: it holds the new
        device's key, so it is never logged, stored or sent anywhere.
        """

        if self.pairing is None:
            raise RuntimeError("the link is not running")

        payload = self.pairing.open(name.strip() or "phone")
        self.audit.record(AuditEvent.PAIRING_OPENED, device_id=payload.device_id)

        return payload.to_uri()

    def cancel_pairing(self) -> None:
        if self.pairing is not None and self.pairing.window is not None:
            device_id = self.pairing.window.device_id
            self.pairing.cancel()
            self.audit.record(AuditEvent.PAIRING_CANCELLED, device_id=device_id)

    # -------------------------------------------------------------- devices

    def revoke(self, device_id: str) -> DeviceView:
        """Revoke now: sessions end, the parked relay connection goes."""

        if self.server is not None:
            record = self.server.revoke(device_id)
        else:
            record = self.registry.revoke(device_id)
            self.audit.record(AuditEvent.DEVICE_REVOKED, device_id=device_id)

        if self.dialler is not None:
            self.dialler.refresh()

        return DeviceView.of(record, self.clock())

    def rename(self, device_id: str, name: str) -> DeviceView:
        return DeviceView.of(self.registry.rename(device_id, name), self.clock())

    def set_remote_access(
        self,
        device_id: str,
        enabled: bool,
        days: float | None = None,
    ) -> DeviceView:
        """Give one phone remote access for ``days`` (default 30), or take it away."""

        expires_at = None if days is None else self.clock() + days * 24 * 3600.0

        if self.server is not None:
            record = self.server.set_remote_access(device_id, enabled, expires_at)
        else:
            record = self.registry.set_remote_enabled(device_id, enabled, expires_at)

        if self.dialler is not None:
            self.dialler.refresh()

        return DeviceView.of(record, self.clock())

    # ---------------------------------------------------------------- relay

    def relay_endpoint(self) -> RelayEndpoint | None:
        return self._configured_relay()

    def set_relay_endpoint(self, endpoint: RelayEndpoint | None) -> bool:
        """
        Save where the relay is, and apply it. Returns whether the link runs.

        The endpoint is part of what the handlers tell phones, so a change
        restarts the link; open sessions end and phones reconnect.
        """

        if self.settings.relay_path is None:
            raise RelayConfigError("this runtime has nowhere to keep relay settings")

        save_relay_endpoint(self.settings.relay_path, endpoint)

        if self.server is None:
            return False

        return self.restart()

    # -------------------------------------------------------------- helpers

    def _configured_relay(self) -> RelayEndpoint | None:
        return load_relay_endpoint(self.settings.relay_path)

    def _handlers(self, relay: RelayEndpoint | None) -> dict:
        services = self.services
        remote_ask = (
            None if services.remote_chat is None else ToolFreeAsk(services.remote_chat)
        )
        local_ask = services.local_ask if services.local_ask is not None else remote_ask

        return default_handlers(
            status=services.status,
            search=None,
            ask=local_ask,
            relay=relay,
            remote_ask=remote_ask,
            transcribe=services.transcribe,
            speak=services.speak,
        )

    def _start_dialler(self, relay: RelayEndpoint) -> None:
        assert self.server is not None

        kwargs: dict[str, Any] = {"connector": self.relay_connector, "clock": self.clock}

        if self.tunnel_settings is not None:
            kwargs["settings"] = self.tunnel_settings

        dialler = RelayDialler(self.server, relay, **kwargs)
        dialler.start()
        self.dialler = dialler

    def __enter__(self) -> LinkRuntime:
        self.start()

        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
