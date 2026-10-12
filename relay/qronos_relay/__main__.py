"""
Run a Qronos relay.

    python -m qronos_relay --cert fullchain.pem --key privkey.pem

Run from the ``relay`` directory, or with it on ``PYTHONPATH``. Listens on
0.0.0.0:443 unless told otherwise. A certificate is required: the relay does
not run without TLS, because in the clear every route would be visible to the
network.

Prints a line of counts every ``--stats-every`` seconds, and nothing else: no
route ids, no addresses, no contents.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys

from .server import RelayLimits, RelayServer, server_context


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="qronos_relay", description=__doc__.split("\n\n")[0])
    parser.add_argument("--listen", default="0.0.0.0", help="address to bind (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=443, help="port to bind (default 443)")
    parser.add_argument("--cert", required=True, help="PEM certificate chain for the relay's name")
    parser.add_argument("--key", required=True, help="PEM private key for that certificate")
    parser.add_argument("--max-connections", type=int, default=RelayLimits.max_connections)
    parser.add_argument("--max-per-address", type=int, default=RelayLimits.max_per_address)
    parser.add_argument("--stats-every", type=float, default=300.0, help="seconds between count lines; 0 for none")

    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    limits = RelayLimits(
        max_connections=args.max_connections,
        max_per_address=args.max_per_address,
    )
    relay = RelayServer(server_context(args.cert, args.key), limits)
    host, port = await relay.start(args.listen, args.port)

    print(f"qronos relay listening on {host}:{port}", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)

        if sig is not None:
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass  # Windows: Ctrl+C still raises KeyboardInterrupt.

    try:
        while not stop.is_set():
            timeout = args.stats_every if args.stats_every > 0 else None

            try:
                await asyncio.wait_for(stop.wait(), timeout)
            except asyncio.TimeoutError:
                print(json.dumps(relay.stats(), sort_keys=True), flush=True)
    finally:
        await relay.close()


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
