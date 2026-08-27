"""
Can a phone on this network actually reach this PC?

This is NOT the device link. It is a plain HTTP page, with no encryption and no
authentication, served for a few minutes so a phone's browser can prove it can
open a TCP connection to this machine on the link's port.

A browser cannot test the real link. The link uses TLS 1.3 with a pre-shared
key and a binary frame protocol; browsers can do neither. What this does test
is the half that a browser can, and it happens to be the half nobody has
verified yet:

    Does the phone's VPN break access to the local network? A full-tunnel
    configuration with no private-range exception swallows traffic to
    192.168.x.x, and if that is how the users' VPNs are set up then Layer 1 is
    much less useful than it looks.

    Is the port reachable at all - no firewall block, no access-point
    isolation between wireless clients?

    Does the PC see the phone as being on the local network? The page reports
    this using the link's own classifier, so it is the real decision the link
    would make about that phone.

The server stops on its own after a few minutes. Run it, do the test, let it
close.

    python tools\\link_reachability.py
    python tools\\link_reachability.py --minutes 20 --port 47711
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.link_capability import LinkScope, scope_for_peer  # noqa: E402
from core.link_pairing import DEFAULT_PORT, local_address  # noqa: E402


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Qronos reachability test</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    margin: 0; padding: 1.5rem;
    background: #0f1115; color: #e8eaed;
    -webkit-text-size-adjust: 100%;
  }}
  .card {{
    max-width: 34rem; margin: 0 auto;
    background: #171a21; border: 1px solid #262b36;
    border-radius: 14px; padding: 1.25rem 1.4rem;
  }}
  h1 {{ font-size: 1.5rem; margin: 0 0 .25rem; }}
  .verdict {{
    font-size: 2.6rem; font-weight: 700; letter-spacing: -.02em;
    margin: .6rem 0 .1rem;
  }}
  .ok {{ color: #4ade80; }}
  .warn {{ color: #fbbf24; }}
  .fa {{ direction: rtl; font-size: 1.15rem; color: #9aa4b2; margin-bottom: 1rem; }}
  table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; }}
  td {{ padding: .5rem 0; border-bottom: 1px solid #262b36; font-size: .95rem; }}
  td:first-child {{ color: #9aa4b2; width: 45%; }}
  code {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .9rem; }}
  .note {{
    font-size: .85rem; color: #9aa4b2; line-height: 1.5;
    background: #12151b; border-left: 3px solid #3b4252;
    padding: .7rem .9rem; border-radius: 0 8px 8px 0;
  }}
  #latency {{ color: #e8eaed; }}
</style>
</head>
<body>
<div class="card">
  <h1>Qronos</h1>
  <div class="verdict {css}">{verdict}</div>
  <div class="fa">{verdict_fa}</div>

  <table>
    <tr><td>Your phone's address</td><td><code>{peer}</code></td></tr>
    <tr><td>Seen as local network</td><td><code>{is_local}</code></td></tr>
    <tr><td>This PC's address</td><td><code>{host}</code></td></tr>
    <tr><td>Port</td><td><code>{port}</code></td></tr>
    <tr><td>Round trip</td><td><code id="latency">measuring&hellip;</code></td></tr>
  </table>

  <p class="note">
    This page is a reachability test only. It is plain HTTP with no encryption,
    and it is <strong>not</strong> the Qronos link &mdash; a browser cannot
    speak that protocol. What it proves is that your phone can open a
    connection to this PC on this port, with your VPN running as it normally
    does.
  </p>

  <p class="note" style="margin-top:.8rem">
    Read the numbers above back to whoever asked you to run this, then close
    the page. The PC will stop the test server on its own.
  </p>
</div>

<script>
(async () => {{
  const times = [];
  for (let i = 0; i < 6; i++) {{
    const started = performance.now();
    try {{
      await fetch('ping?n=' + i, {{ cache: 'no-store' }});
      times.push(performance.now() - started);
    }} catch (e) {{ /* ignore a single failure */ }}
    await new Promise(r => setTimeout(r, 120));
  }}
  const el = document.getElementById('latency');
  if (!times.length) {{ el.textContent = 'could not measure'; return; }}
  times.sort((a, b) => a - b);
  const median = times[Math.floor(times.length / 2)];
  el.textContent = `${{times[0].toFixed(0)}} ms best, ${{median.toFixed(0)}} ms typical`;
}})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "QronosReachability/1"
    hits = 0

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        peer = self.client_address[0]
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path == "/ping":
            self._send(
                200,
                "application/json",
                json.dumps({"pong": True, "at": round(time.time(), 3)}),
            )

            return

        if path != "/":
            self._send(404, "text/plain", "not found")

            return

        scope = scope_for_peer(peer)
        is_local = scope is LinkScope.LOCAL_NETWORK

        Handler.hits += 1

        print(f"  hit {Handler.hits}: {peer}", end="")
        print("  -  seen as LOCAL NETWORK" if is_local
              else f"  -  NOT local ({scope})")

        if not is_local:
            print("       The link would refuse this address in Layer 1.")
            print("       Most likely the phone reached the PC through a VPN")
            print("       or a mobile connection rather than the local Wi-Fi.")

        page = PAGE.format(
            verdict="Connected" if is_local else "Connected, but not local",
            verdict_fa=(
                "اتصال برقرار شد" if is_local
                else "اتصال برقرار شد، اما از شبکه محلی نیست"
            ),
            css="ok" if is_local else "warn",
            peer=peer,
            is_local="yes" if is_local else "no",
            host=local_address(),
            port=self.server.server_address[1],
        )

        self._send(200, "text/html; charset=utf-8", page)

    def _send(self, status: int, content_type: str, body: str) -> None:
        encoded = body.encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        try:
            self.wfile.write(encoded)
        except OSError:
            pass

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet. The handler prints what matters itself."""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Serve a reachability test page for a phone on this network."
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--minutes",
        type=float,
        default=10.0,
        help="stop on its own after this long (default 10)",
    )
    arguments = parser.parse_args()

    address = local_address()

    if address == "127.0.0.1":
        print("This machine has no network address, so no phone can reach it.")
        print("Connect it to the same Wi-Fi as the phone and try again.")

        return 1

    try:
        server = ThreadingHTTPServer(("0.0.0.0", arguments.port), Handler)
    except OSError as exc:
        print(f"Could not listen on port {arguments.port}: {exc}")
        print("Something else may be using it. Try --port 47712.")

        return 1

    url = f"http://{address}:{arguments.port}/"

    print("=" * 68)
    print("  Qronos reachability test")
    print("=" * 68)
    print()
    print("  On the phone, on the SAME Wi-Fi, open a browser and go to:")
    print()
    print(f"      {url}")
    print()
    if arguments.minutes >= 1:
        print(f"  Stopping on its own in {arguments.minutes:.0f} minutes.")
    else:
        print(f"  Stopping on its own in {arguments.minutes * 60:.0f} seconds.")
    print("  Press Ctrl+C to stop sooner.")
    print()
    print("  Waiting for the phone...")
    print()

    stop = threading.Timer(arguments.minutes * 60.0, server.shutdown)
    stop.daemon = True
    stop.start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print()
        print("  Stopped.")
    finally:
        stop.cancel()
        server.server_close()

    print()

    if Handler.hits:
        print(f"  The phone reached this PC {Handler.hits} time(s).")
    else:
        print("  The phone never reached this PC.")
        print()
        print("  Things to check, in this order:")
        print("    1. Is the phone on the same Wi-Fi, not mobile data?")
        print("    2. Turn the phone's VPN off and reload. If it works with")
        print("       the VPN off, the VPN is swallowing local traffic - say")
        print("       so, it is the single most useful result of this test.")
        print("    3. Did Windows ask to allow Python through the firewall?")
        print("       If it was blocked, allow it on Private networks.")
        print("    4. Some routers isolate wireless clients from each other.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
