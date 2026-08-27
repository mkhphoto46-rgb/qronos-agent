"""
Does the device link work on this machine?

Run this first, on the PC, before trying anything from a phone. It answers the
one question that decides whether Layer 1 can run at all on Windows: does the
OpenSSL that this Python links support TLS 1.3 with pre-shared keys?

Everything happens on this machine over loopback. Nothing listens on the
network, nothing is written to disk, and no phone is involved. It either prints
PASS at the end or tells you exactly what failed.

    python tools\\link_selfcheck.py
"""

from __future__ import annotations

import platform
import ssl
import sys
import traceback
from pathlib import Path


# Allow running this file directly from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.link_audit import AuditLog  # noqa: E402
from core.link_capability import LinkOp, LinkScope, scope_for_peer  # noqa: E402
from core.link_client import LinkClient  # noqa: E402
from core.link_devices import DeviceRegistry, DeviceStatus  # noqa: E402
from core.link_handlers import default_handlers  # noqa: E402
from core.link_pairing import PairingService, local_address  # noqa: E402
from core.link_server import LinkServer, LinkSettings  # noqa: E402
from core.link_transport import (  # noqa: E402
    TlsPskUnavailable,
    psk_support_report,
    require_psk_support,
)


PASS = "PASS"
FAIL = "FAIL"

results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    mark = " OK " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -  {detail}" if detail else ""))

    return ok


def heading(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def main() -> int:
    print("=" * 68)
    print("  Qronos device link - self check")
    print("=" * 68)

    heading("1. This machine")

    print(f"  python   {sys.version.split()[0]}")
    print(f"  platform {platform.system()} {platform.release()}")
    print(f"  openssl  {ssl.OPENSSL_VERSION}")

    heading("2. Can this Python do TLS 1.3 with pre-shared keys?")

    print(f"  {psk_support_report()}")
    print()

    try:
        require_psk_support()
        record("TLS-PSK is available", True)
    except TlsPskUnavailable as exc:
        record("TLS-PSK is available", False)
        print()
        print("  This is the one failure that stops Layer 1 working here.")
        print()
        print(f"  {exc}")
        print()
        print("  Report this output. Nothing else in this script will work.")

        return 1

    heading("3. Does a phone-shaped client actually connect?")

    registry = DeviceRegistry(path=None)
    pairing = PairingService(registry, host="127.0.0.1", port=0)
    audit = AuditLog(path=None)

    server = LinkServer(
        registry=registry,
        pairing=pairing,
        audit=audit,
        handlers=default_handlers(
            status=lambda: {"link": "ok", "selfcheck": True}
        ),
        settings=LinkSettings(host="127.0.0.1", port=0),
    )

    try:
        server.start()
    except Exception:
        record("the link server starts", False)
        traceback.print_exc()

        return 1

    ok = True

    try:
        port = server.address[1]
        pairing.port = port
        record("the link server starts", True, f"listening on port {port}")

        payload = pairing.open("self check")
        record(
            "a pairing window opens",
            len(payload.to_uri()) < 200,
            f"QR payload is {len(payload.to_uri())} characters",
        )

        client = LinkClient.from_payload(
            payload, host="127.0.0.1", port=port, timeout=10.0
        )

        try:
            client.connect()
        except Exception as exc:
            record("the TLS handshake completes", False, str(exc))

            return 1

        try:
            ok &= record(
                "the TLS handshake completes", True, client.tls_summary()
            )
            ok &= record(
                "TLS 1.3 was negotiated",
                "TLSv1.3" in client.tls_summary(),
                client.tls_summary(),
            )

            reply = client.call("ping")
            ok &= record("a request gets a reply", reply.ok)

            if reply.ok:
                capabilities = reply.result.get("capabilities", [])
                ok &= record(
                    "the session reports its capabilities",
                    "search_web" in capabilities,
                    f"{len(capabilities)} capabilities on the local network",
                )

            ok &= record("a status request works", client.call("status").ok)

            device_id = client.device_id
        finally:
            client.close()

        record(
            "the device is now paired",
            registry.get(device_id).status is DeviceStatus.ACTIVE,
        )

        heading("4. Are the refusals working?")

        record(
            "an unknown request is refused",
            _code(registry, port, device_id, "nonsense") == "unknown_op",
        )
        record(
            "device management is refused over the link",
            _code(registry, port, device_id, "list_devices") == "console_only",
        )
        record(
            "running an application needs approval",
            _code(registry, port, device_id, "run_app") == "needs_approval",
        )

        heading("5. Is this machine's own address usable?")

        address = local_address()
        scope = scope_for_peer(address)

        record(
            "this machine has a local network address",
            scope is LinkScope.LOCAL_NETWORK and address != "127.0.0.1",
            f"{address}"
            + (
                ""
                if address != "127.0.0.1"
                else "  (no network - a phone cannot reach this machine)"
            ),
        )
    finally:
        server.stop(timeout=3.0)

    heading("Result")

    failures = [row for row in results if row[0] == FAIL]

    if failures:
        print(f"  {len(failures)} check(s) FAILED:")

        for _, name, detail in failures:
            print(f"    - {name} {detail}")

        print()
        print("  Send this whole output back.")

        return 1

    print(f"  All {len(results)} checks passed.")
    print()
    print("  The link works on this machine.")
    print("  Next: run tools/link_reachability.py so a phone can be tested.")

    return 0


def _code(registry: DeviceRegistry, port: int, device_id: str, op: str) -> str:
    """Reconnect as the paired device and return the refusal code for one op."""

    record_ = registry.get(device_id)

    client = LinkClient(
        host="127.0.0.1",
        port=port,
        device_id=device_id,
        secret=record_.secret,
        timeout=10.0,
    )

    with client:
        return client.call(op).code


if __name__ == "__main__":
    raise SystemExit(main())
