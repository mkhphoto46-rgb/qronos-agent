# Qronos Device Link — Layer 1 and Layer 2

**Status**

| Layer | Name | State |
|---|---|---|
| Layer 1 | Local Link — phone and PC on the same network | Implemented; verified on Windows 11 |
| Layer 2 | Relay Link — phone reaches the PC over the internet | PC-side transport, self-hostable relay and the Android protocol core implemented and tested on loopback with real outer TLS (§5.3, §5.4, §5.8). Android app compiled and packaged unsigned without the SDK, never run; no iOS. No relay deployed; not wired into the desktop; no phone has used it |

Layer 1 ships first and is useful on its own. Layer 2 is a transport change on
top of it, not a redesign. The point of this document is to make that true: the
parts that are hard to change later — identity, pairing, capability limits — are
settled in Layer 1 so Layer 2 adds only a pipe.

---

## 1. Scope

**In scope, both layers.** The phone is a remote control. Voice goes in, an
answer comes back. All reasoning, all file access and all computer control stay
on the PC. The phone never runs a model and never holds project data.

**Layer 1 covers** the PC-side link service: transport, device identity,
pairing, capability limits, the wire protocol, and the audit log. It includes a
reference client in Python, which is both the test harness and the specification
the phone app should follow.

**Layer 1 does not cover** the pairing screen (Tauri, desktop app), the phone
app itself, or network discovery. Discovery is deliberately left out — see §4.9.

---

## 2. The constraint that shapes everything

Most Iranian users already run a VPN on the phone for reasons that have nothing
to do with Qronos.

On both iOS and Android **only one application can hold the VPN tunnel at a
time**. Starting a second VPN client stops the first. So a design that puts
WireGuard, Tailscale or Netbird on the phone would switch off the connection the
user actually depends on. That is not an inconvenience to be documented; it is a
reason those options are unavailable.

The consequence, which drives the whole of Layer 2: **the phone must never claim
a VPN interface.** It speaks ordinary TLS to an ordinary port. Whatever VPN is
running carries that traffic without knowing what it is.

Layer 1 inherits a related hazard, and it is no longer a hypothesis.

**Measured on 27 August 2026, on a phone in Iran: the local network path works
with the VPN off and fails with the VPN on.** The phone reached the PC at
`192.168.0.3:47711` in 13 ms with the VPN disconnected, and could not reach it
at all with the VPN connected. The VPN is capturing traffic to `192.168.0.0/16`.

**What that result does and does not settle.** It was measured through a mobile
browser, and a browser has no say in which network interface its sockets use. A
native app does. On Android an app can ask for the Wi-Fi network specifically —
`NetworkRequest` with `TRANSPORT_WIFI`, then `Network.bindSocket` or
`Network.getSocketFactory` — and that socket then uses Wi-Fi even while a VPN
holds the default route. So the browser failure is evidence about browsers, not a
verdict on the phone app.

Two caveats on that route, both worth knowing before relying on it:

- Android's always-on VPN with "block connections without VPN" (lockdown mode)
  does defeat it. A user in that configuration cannot reach the LAN from any app.
- The iOS equivalent is `NWParameters.requiredInterfaceType = .wifi`, plus the
  local-network permission prompt. Whether it bypasses an active VPN there is
  **unverified**, and iOS is the stricter platform of the two.

So the honest position: Layer 1 over a browser is blocked by a VPN, Layer 1 from
a native Android app probably is not, and nobody has tested the second because
the app does not exist yet. §7 carries this forward.

---

## 3. Threat model

**Defended against**

| Threat | Defence |
|---|---|
| Another device on the same Wi-Fi reading the session | TLS 1.3, authenticated encryption |
| Another device impersonating the PC | Mutual authentication from the shared pairing key |
| Another device impersonating a paired phone | Same; an unpaired device cannot complete a handshake |
| A guessed or brute-forced key | 256-bit key from `secrets.token_bytes` |
| A lost phone retaining access | Revocation at the PC, enforced during the handshake |
| A paired phone exceeding its remit | Capability ceiling per scope, intersected with per-device grants |
| A recorded session decrypted later after a key leak | Forward secrecy — measured, §4.1 |
| A phone enrolling another phone | Device management is console-only, in no scope profile |
| Pairing intercepted over the network | The key travels screen-to-camera, never over the network |
| Pairing performed remotely by an attacker | Pairing requires a local-network peer address |

**Not defended against, stated plainly**

- **A compromised or unlocked phone.** It holds a valid key. The answer is
  revocation after the fact, not prevention.
- **A compromised PC.** The link is not a boundary inside the machine.
- **Traffic analysis.** In Layer 2 a relay and an ISP can see that a connection
  exists, when, and roughly how much data moved. Contents stay private.
- **Someone reading the QR code off the screen.** Physical presence at the PC
  during a 120-second window is assumed to be the user.

---

## 4. Layer 1 — Local Link

### 4.1 Transport: TLS 1.3 with an external pre-shared key

Pairing produces a shared secret. TLS 1.3 can authenticate a connection from a
pre-shared key directly, with no certificates, no certificate authority and no
pinning logic. Python exposes this through `ssl.SSLContext.set_psk_server_callback`
and `set_psk_client_callback`.

This was tested on this machine before the design was settled, not taken from
documentation:

| Property | Result |
|---|---|
| Handshake completes | `TLSv1.3`, `TLS_CHACHA20_POLY1305_SHA256` |
| Server learns which device connected | Yes — the identity reaches the callback |
| Identity type | `str`, **not** `bytes` — a bytes-keyed lookup silently fails |
| Known identity, wrong key | Handshake fails on both sides |
| Unknown or revoked identity | Handshake fails; server still learns the attempted identity, so it can be logged |
| Forward secrecy | **Yes.** ServerHello carries `key_share`, group `X25519MLKEM768` |

The last row was measured by tapping the handshake and parsing the ServerHello
extensions. A `key_share` in the ServerHello means the exchange is `psk_dhe_ke`
rather than `psk_ke`: an ephemeral key exchange ran, so recorded traffic is not
readable even if the pairing key later leaks. The negotiated group is the hybrid
post-quantum one, which is OpenSSL 3.6's default. A different OpenSSL build may
choose plain `x25519`; forward secrecy holds either way.

**Why this rather than mutual TLS with certificates.** Certificate generation
needs a third-party library, which the project does not currently depend on, and
pinning is a step that is easy to implement wrongly. PSK removes both. The
trade-off is real and is recorded in §4.11.

**Port and switch.** The listener defaults to TCP 47711 and binds all
interfaces; the defence against a connection from the wrong place is the peer
check, not the bind address, because binding one interface breaks on a machine
with several and gives a false sense of protection either way. Nothing starts by
itself: `CONFIG.security.link_enabled` is the documented switch and defaults to
`False`, and the server has no module-level instance and no import side effect.

**One context per connection.** The PSK callback receives no handle for the
connection it belongs to, so a shared context cannot tell two simultaneous
handshakes apart. Each accepted socket therefore gets its own `SSLContext` whose
callback closes over that socket's own state. Cheap, and it removes the race
outright.

### 4.2 Identity and pairing

A device is identified by a random 16-hex-character string, not by its name.
Renaming a phone does not change who it is.

Pairing runs entirely on the local network and only while a window is open:

1. The user opens the pairing screen on the PC.
2. The PC generates a device id and a 256-bit key, stores the device as
   `PENDING`, and displays a QR code.
3. The phone scans it and connects using that key.
4. The first successful handshake from a local-network address inside the window
   promotes the device to `ACTIVE`.

The QR payload is a URI so a scanner can deep-link into the app:

```
qronos://pair?v=1&h=<host>&p=<port>&d=<device-id>&k=<key-base64url>&e=<expiry>
```

Four properties matter, and each is enforced in code rather than by convention:

- **Out of band.** The key goes from screen to camera. It is never transmitted
  over the network, so there is no handshake to intercept and nothing to
  man-in-the-middle.
- **Time limited.** 120 seconds, then the window closes and the pending device
  is discarded.
- **Single use.** One window pairs one device. Opening a second window cancels
  the first, so a forgotten window cannot sit open.
- **Local only.** The completing peer must be on one of an explicit list of
  local networks. In Layer 2 pairing over the relay is refused outright.

  The list is spelled out — `10/8`, `172.16/12`, `192.168/16`, `169.254/16`,
  `127/8`, and the IPv6 equivalents — rather than asked of
  `ipaddress.is_private`, which answers a broader question than the one being
  asked. It returns True for the documentation ranges, the benchmarking range
  and `240/4`, none of which is a home network. The first implementation used
  that predicate and accepted a peer at `203.0.113.7` as local; the demo caught
  it. Carrier-grade NAT space (`100.64/10`) is deliberately absent too: that is
  the address a phone gets from a mobile carrier, not from a home router.

The last property is the load-bearing one. Because a device can only ever be
enrolled from the local network, a compromised relay in Layer 2 cannot add a
phone. That is what makes an untrusted relay acceptable.

The QR payload is never written to disk and never logged.

### 4.3 Device registry and revocation

Devices live in `data/link_devices.json`, mode `0600`. `data/` is already in
`.gitignore`, so the file holding the keys is outside version control by
construction rather than by remembering.

| Field | Purpose |
|---|---|
| `device_id` | Stable identity, also the TLS PSK identity |
| `name` | Human label, freely changeable |
| `secret` | 256-bit key, base64 |
| `status` | `pending`, `active` or `revoked` |
| `created_at`, `last_seen_at` | For the device list in the UI |
| `paired_from` | The address it was paired from, for the audit trail |
| `grants` | Optional narrowing of what this device may do |
| `remote_enabled` | Layer 2 opt-in, per device, default off |

Two rules:

- **Revocation is enforced during the handshake, not after it.** A revoked
  device's key stops resolving, so the TLS handshake itself fails. There is no
  window in which a revoked phone holds an open session.
- **A corrupt registry raises rather than starting empty.** Silently starting
  with no devices would look like "nothing is paired" and would invite
  re-pairing over whatever the corruption was. Same rule as the artifact
  ownership store.

Records redact the key in `repr`, so a stray debug print cannot leak it.

**More than one process uses the file** — the link service, and whatever the
user revokes a phone from — so no registry trusts its own memory. Every lookup
checks whether the file has been replaced (modification time, size, file id)
and re-reads it if so; every change takes a lock shared between processes,
re-reads the file under it, and writes it back whole. A revocation written by
another process therefore reaches the next handshake and the next request of
an open session, and two processes changing the file at once cannot undo each
other. A file that cannot be read fails closed: handshakes are refused and open
sessions end until it is readable again. Tested with separate Python processes
on a disposable file.

### 4.4 Capability model

This is the part Layer 2 depends on, and the reason to build it now.

A **scope** is where the connection came from. A **capability** is a thing a
session may ask for.

| Capability | `local_network` | `remote_tunnel` |
|---|---|---|
| `ask` — put a question to the assistant | yes | yes |
| `search_web` | yes | yes |
| `read_status` — resource and storage state | yes | yes |
| `read_files` | yes | no |
| `write_files` | yes | no |
| `run_application` | yes | no |
| `control_system` | yes | no |
| `delete_files` | yes | no |
| `manage_devices` — pair or revoke | **no** | **no** |

`manage_devices` appears in no profile. Enrolling and revoking devices happens
at the PC, never over the link, in either layer.

Three rules make this safe to extend:

1. **Effective capability is the intersection** of the scope profile and the
   device's own grants. A per-device grant can only narrow, never widen. There
   is no code path by which a device grant raises a ceiling.
2. **The link never widens permissions.** It sits in front of the existing
   permission engine in `security/permissions.py`; that engine still runs and
   can still demand approval or deny. Two independent gates, and the stricter
   one wins.
3. **The scope resolver is injected.** The server takes the peer-classifying
   function as a dependency, defaulting to the real one. That is what lets the
   Layer 2 paths — a remote-scope connection, and the per-device `remote_enabled`
   gate — be exercised by tests today, with no internet-facing socket. Layer 2
   does not replace it; it adds `REMOTE_TUNNEL` to the allowed scopes and lets
   the real classifier return it.
4. **Unmapped is denied.** An operation with no capability mapping is refused.
   A test asserts every operation has a mapping and every capability appears in
   at least one profile or in the console-only list, so a policy cannot quietly
   become unreachable. That bug class already appeared once in this project, in
   `ActivityMode.IDLE`, and this is the guard against a repeat.

### 4.5 Wire protocol

Length-prefixed JSON frames: a four-byte big-endian length, then UTF-8 JSON.

```
+--------+------------------------+
| len(4) | JSON payload           |
+--------+------------------------+
```

Three frame kinds:

- `request` — `{"kind":"request","id":N,"op":"...","params":{...}}`
- `response` — `{"kind":"response","id":N,"ok":bool,"result":...|"error":...}`
- `event` — `{"kind":"event","topic":"...","data":{...}}`, server to client,
  unsolicited, for streaming progress out of the research pipeline

The length prefix is checked against a maximum **before** any allocation, so an
oversized declared length is refused rather than reserved. Malformed frames
produce a typed refusal, never an exception escaping to the socket loop.

Events exist so the phone can show what the existing `ResearchEvent` stream
already reports — searching, reading, answering, with honest progress that is
`None` when there is no meaningful fraction.

### 4.6 Audit log and data minimisation

Append-only JSONL at `logs/link_audit.jsonl`. One line per handshake, pairing
action, and authorisation decision.

The log records **what was asked for and what was decided** — timestamp, event,
device id, peer address, operation name, decision. It does **not** record request
parameters, utterance text, answers, or keys. There is no free-form field through
which content could reach it, so this is a property of the interface rather than
a rule someone has to follow.

That is deliberate. The traffic here is a person's voice commands in their own
home. The log needs to answer "which device did what, and when" for an intrusion
or a mistake. It does not need the contents to do that.

Size is capped with a single rollover to `.1`, and the file sits inside the
existing `LOGS_AND_TEMP` storage budget component.

### 4.7 Abuse resistance, and why it is deliberately small

The key is 256 bits from `secrets.token_bytes`. Brute force is not a threat that
throttling improves.

What repeated handshake attempts can do is waste CPU. So the controls are sized
for that and no larger: a cap on concurrent connections, a minimum interval
after a failed handshake, and an idle timeout on sessions. Failures are logged,
because a burst of them on a home network is worth seeing.

Adding a per-address token bucket here would look thorough and defend against
nothing that matters.

### 4.8 Module map

| Module | Responsibility |
|---|---|
| `core/link_protocol.py` | Frames, request/response/event, size limits, typed refusals |
| `core/link_capability.py` | Scopes, capabilities, profiles, peer-address classification, authorisation |
| `core/link_devices.py` | Device records, registry file, revocation |
| `core/link_pairing.py` | Pairing window, QR payload, local-only enforcement |
| `core/link_transport.py` | PSK TLS contexts, support probe, socket reads |
| `core/link_audit.py` | Append-only audit log |
| `core/link_server.py` | Listener, per-connection handshake, dispatch |
| `core/link_client.py` | Reference client — the specification for the phone app |
| `core/link_handlers.py` | Default operation handlers, dependencies injected |
| `core/link_relay_wire.py` | Layer 2: the 37-byte relay hello, status bytes, route derivation |
| `core/link_tunnel.py` | Layer 2: relay endpoint, outer TLS, the PC's outbound parking dialler |
| `core/link_commands.py` | Layer 2: command-id ledger against duplicate execution, request budget |

Every module follows the existing conventions in `core/`: frozen dataclasses,
enums over string literals, pure decision functions where the decision can be
separated from the effect, injected clocks and dependencies so tests need no
sockets, no timers and no network.

Two scripts under `tools/` exist to verify a real deployment:

| Script | What it answers |
|---|---|
| `tools/debug/link_selfcheck.py` | Does the link work on this machine at all? Runs the real server and client over loopback and reports the TLS-PSK verdict first. |
| `tools/debug/link_reachability.py` | Can a phone on this network reach this PC on the link's port, with the user's VPN running as it normally does? Plain HTTP, because a browser cannot speak the link protocol. |

The second is the one that answered the VPN question in §2, and it reports its
verdict using the link's own peer classifier, so the answer is the real decision
the link would make about that phone.

Both live under `tools/debug/`, are listed in `release-exclude.txt`, and refuse
to run if they detect a release build. The reachability tool opens an
unauthenticated HTTP socket, which is fine on a developer's machine for ten
minutes and is not fine in software a user installs, so
`tests/test_release_hygiene.py` fails the suite if that marking ever falls out of
step with the code.

### 4.9 Deliberately excluded from Layer 1

**Network discovery.** The phone needs the PC's address. The options are mDNS,
which means a new dependency (`zeroconf`), or typing an address, or reading it
from the QR code. The QR code already carries the host and port, so pairing
needs no discovery at all. Discovery only helps on reconnection when the PC's
DHCP lease has changed.

That is a real gap but a small one, and it belongs in the desktop app: the Tauri
side already owns the pairing screen and can advertise from Rust without adding
a Python dependency. Left out rather than half-built.

**A typed pairing code as an alternative to the QR.** A 256-bit key cannot be
typed. Doing it properly needs a password-authenticated key exchange such as
SPAKE2, and therefore a crypto library. Deferred, with the limitation stated:
**Layer 1 pairing requires a camera.**

### 4.10 Verification status

Verified on this machine (macOS, Python 3.14.7, OpenSSL 3.6.3): everything in
§4.1, plus the full pipeline through the reference client over loopback.

307 tests cover the nine modules, and the suite as a whole is 1131 tests with no
regressions. What the socket-level tests cover that the pure ones cannot: a real
TLS 1.3 handshake with the right key, a wrong key, and a revoked device; pairing
completing on a first connection; the connection cap; the post-failure pause;
the idle timeout; malformed and oversized frames closing a session; and a
handler's exception message not reaching the phone.

The whole stack was also run together — pairing, handshake, framing,
authorisation, then the real web research pipeline with fake providers and a
scripted model — and delivered a cited Persian answer to the client with its
progress events and source list. That run found a defect the unit tests had
missed: the adapter read `cited_urls` from the research result rather than from
the answer inside it, so the phone received an empty source list while the
rendered text claimed a source. The stub in the test encoded the same wrong
assumption, which is why it passed. The stub now mirrors the real shape.

**Python 3.13 or newer is required.** `ssl.SSLContext.set_psk_server_callback`
does not exist before then. Measured here: Python 3.12.5 has neither the
callbacks nor `ssl.HAS_PSK`, and 3.9.6 likewise. The probe was run under 3.12
and refuses cleanly, naming exactly what is missing and what the fallback is, so
an older interpreter produces an actionable message rather than a confusing
handshake failure. This is a real deployment constraint: nothing else in the
project needs 3.13.

Two things cannot be verified from here.

1. **Does Windows Python support TLS-PSK?** Even on 3.13+, support depends on
   how the OpenSSL that CPython links was built. The code probes `ssl.HAS_PSK`
   and TLS 1.3 at startup and refuses to start with a clear message rather than
   failing obscurely mid-handshake — but whether it passes on the reference PC
   is unknown. **If it fails, Layer 1's transport has to become mutual TLS with
   certificates, which means adding `cryptography` as a dependency.** This is
   the single highest-value check on the list.
2. **Does the LAN path survive the VPN the users actually run?** Partly
   answered. Through a mobile browser, no — measured on a phone in Iran, see §2.
   From a native app, unknown and probably yes, because an app can pin its
   socket to the Wi-Fi network and a browser cannot.

   Two cheap tests remain, neither needing the finished app:

   - A VPN client's own bypass-LAN or split-tunnelling setting. One toggle, one
     reload of the same test page. If it works, users keep their VPN and Layer 1
     works today.
   - A throwaway Android build that requests `TRANSPORT_WIFI` and connects to
     the link. This is the one that actually settles it, and it is the first
     thing the phone app should prove.

Item 1 has been answered too. Windows 11, Python 3.14.7, OpenSSL 3.5.7: all
thirteen self-check checks pass, and the handshake negotiates
`TLSv1.3 / TLS_CHACHA20_POLY1305_SHA256` on a different OpenSSL build than the
one used during development. **TLS-PSK works on Windows.** No fallback to
certificates, and no `cryptography` dependency.

### 4.11 Trade-offs accepted

| Decision | Cost accepted |
|---|---|
| PSK instead of certificates | The key is symmetric, so it cannot live in the phone's Secure Enclave as a non-extractable key the way an EC private key could. Mitigated by keychain storage with biometric protection, and by revocation. Revisit in Layer 2 if hardware-backed keys become important. |
| Threads, not async | Simpler, and matches a codebase with no asyncio. Fine for one to three devices; wrong for hundreds. |
| QR-only pairing | Requires a camera. |
| No discovery | Reconnection after the PC's address changes needs re-entry or a re-scan. |

---

## 5. Layer 2 — Relay Link (PC side implemented; not deployed)

### 5.1 Shape

**The PC dials out. The phone dials in. A relay copies bytes between them.**

The PC holds an outbound connection to a relay on port 443. The phone connects
to the same relay. The relay splices the two together and understands nothing
about what passes through.

What this buys:

- **No listening port on the PC.** Nothing to port-scan, no router
  configuration, no UPnP.
- **Works behind carrier-grade NAT**, which is close to universal on Iranian
  home broadband. Port forwarding would not work for most users even if it were
  acceptable.
- **No VPN interface on the phone.** It is an ordinary TLS connection to an
  ordinary host, so it rides inside whatever VPN is already running. This is the
  requirement from §2, satisfied directly.

### 5.2 The relay is untrusted by design

The phone and the PC keep the Layer 1 TLS session **end to end, through the
relay**. The relay carries an already-encrypted stream.

So the relay operator cannot read the traffic, cannot alter it, and cannot inject
into it. Its compromise yields connection metadata and nothing else. Combined
with the Layer 1 rule that **pairing only happens on the local network**, a
hostile relay also cannot enrol a device.

This is what keeps the local-first claim honest. The relay address must be a
configuration value with a documented self-hosting path, not a hard-coded
endpoint. A default may be provided; depending on it must be optional.

### 5.3 Rejected alternatives

| Option | Why not |
|---|---|
| Tailscale, Netbird, plain WireGuard | Claims the phone's VPN interface. §2. |
| Cloudflare Tunnel, ngrok | US providers with Iran sanctions screening. Sign-up and connectivity fail from Iranian addresses. A dependency that fails for the entire target audience is not a dependency. |
| Mojeek-style third-party hosting generally | Same class of problem: the PC side runs inside Iran and must dial out. |
| WebRTC with hole punching | Behind carrier-grade NAT hole punching usually fails and falls back to a TURN relay. Much more complexity for the same outcome. |
| Port forwarding on the router | Needs a public address the user does not have, exposes a listening port, and requires router access. |
| A polling queue instead of a live tunnel | Simple and censorship-resistant, but latency makes voice unusable and a third party sees request timing. Possible fallback, not the primary design. |

For the relay itself: `rathole` (Rust, MIT/Apache-2.0, fits the Tauri stack) or
`frp` (Apache-2.0). Both do outbound-only reverse tunnelling. It is also a few
hundred lines to write, since the relay's whole job is to copy bytes.

**Built:** the few hundred lines, in `relay/` — Python standard library and
asyncio, so it runs on any small Linux machine with Python 3.11+ and nothing
installed, and so it speaks the Layer 2 hello natively instead of needing
rathole's or frp's own protocol on the PC. Outer TLS is mandatory; every
resource is bounded; it logs counts only, never routes or addresses.
`relay/README.md` is the self-hosting guide. It is not deployed and has no
default address.

### 5.4 What was built (8 October 2026)

The proposal above was implemented on the PC side, with one deliberate change
to it and several rules it did not have. Everything here is tested on loopback
with fresh keys; none of it has carried a real phone's traffic.

**Two TLS sessions, one inside the other.**

| Layer | Between | Authenticated by | What it is for |
|---|---|---|---|
| Outer | each end and the relay | the relay's certificate, verified like HTTPS (system trust store, or a pinned CA for a self-hosted relay) | Looks like any HTTPS connection; hides the route from the network; rides inside the phone's VPN |
| Inner | phone and PC, end to end | the pairing key — the unchanged Layer 1 TLS 1.3 PSK handshake with an ephemeral key exchange | Everything the relay must not read, alter or inject into |

Python's `wrap_socket` cannot run TLS inside TLS, so the inner session runs
over OpenSSL memory BIOs (`TlsStream` in `core/link_transport.py`), presenting
the same socket methods the session loop already used.

**Relay wire format** (`core/link_relay_wire.py`). After the outer handshake each
side sends one 37-byte hello — `"QRL1"`, a role byte, a 32-byte token — and the
relay answers with status bytes (`PARKED`, `KEEPALIVE`, `SPLICED`, or a terminal
refusal). Every byte after `SPLICED` is the inner session.

- One route per remote-enabled device. The PC's token is the *route key*,
  `HMAC-SHA256(pairing key, "qronos relay route key v1")`; the phone's token is
  the *route id*, `SHA-256("qronos relay route id v1" ‖ route key)`.
- The relay files a host under the hash of what it presented, so a host can
  only park under a route whose key it holds, and a phone's hello cannot be
  replayed as a host.
- What the relay learns: one opaque route per device, the route key once the
  PC has parked, timing and volume. It cannot derive the pairing key (HMAC is
  one-way). With the route key it can pretend to be the PC to the phone, and
  that fails at the inner handshake.

**The change from §5.4 as proposed: scope comes from the path, not the address.**
The proposal had peer classification return `REMOTE_TUNNEL`. That would have
meant the listening port accepting internet addresses. Instead
`scope_for_peer` still refuses every non-local address, and only the relay path
— `LinkServer.serve_relayed`, fed by the dialler — produces a remote session.
The PC still has no internet-facing listener.

**The dialler** (`RelayDialler` in `core/link_tunnel.py`) keeps one outbound
parked connection per device that is active and inside a remote opt-in, re-read
from the registry every two seconds and on demand. A revoked, opted-out or
expired device loses its parked connection. Reconnects use exponential backoff
with full jitter, 1 to 60 seconds. A relay that goes silent past 60 seconds is
abandoned and redialled. Losing and regaining the relay is audited once per
transition, not once per retry. It will not start unless the server's
`allowed_scopes` includes `REMOTE_TUNNEL`. No relay address is built in.

**Rules for remote sessions, as enforced:**

| Rule | Where |
|---|---|
| Off by default, per device, **with an expiry**: default 30 days, at most 365, renewed at the PC. A record with no expiry reads as lapsed | `DeviceRecord.remote_allowed`, `set_remote_enabled` |
| **No pairing over the relay**, decided by the path. A relay on the local network does not make pairing local | `_authorise_session` |
| Opt-in checked at the handshake **and before every request** | `_still_valid` |
| **Revocation reaches open sessions** in both layers: `LinkServer.revoke` closes them at once; a revocation written straight to the registry applies at the next request | `revoke`, `_still_valid` |
| **Short-lived sessions**: 30 minutes, then a fresh handshake with fresh ephemeral keys | `remote_session_lifetime` |
| **Request budget**: a burst of 10, then one every two seconds | `RequestBudget` |
| **Duplicate-command safety**: every remote request but `ping` carries a 128-bit command id (`cid`); a retry is answered from a bounded per-device ledger, never run twice; the same id with different parameters is refused | `core/link_commands.py` |
| The capability ceiling is not negotiable: three of nine capabilities, unchanged | `SCOPE_PROFILES` |

**What a hostile relay can make the PC do** (review finding L3, repaired). As
first built, a relay answering every parking with `SPLICED` and a hang-up made
the PC redial 312–374 times in three seconds, with an audit line each, while
those connections shared the LAN's four slots. Now, measured with the same
reproducer and production settings: 3 connections and 3 audit lines in three
seconds. The rules:

| Rule | Where |
|---|---|
| A splice is a success only when the server reports an **authenticated** session; anything else is a failure with exponential backoff (1 s doubling to 60 s, jittered) | `serve_relayed(..., on_outcome)`, `_Parker._await_verdict` |
| Each device has a **splice quota**: a burst of 20, then one every 5 s, authenticated or not | `TunnelSettings.splice_burst` |
| A `PARK_EXPIRED` sooner than 30 s after parking, or keepalives faster than about one a second, is **misbehaviour**, answered with backoff | `min_park_seconds`, `keepalive_per_second` |
| Relayed connections have **their own four slots**; they cannot crowd out the phone on the LAN | `LinkSettings.max_relay_connections` |
| Identical failure lines are **coalesced**: written once, then counted, then one summary line with `repeated` per minute; at most 256 failure keys counted at once; the in-memory copy is a window of 4096 lines | `core/link_audit.py` |

**Replay, stated precisely.** TLS already makes recorded traffic useless: a
recorded session replayed into a new connection fails the handshake, because
the server's half of the key exchange is new (tested: the replay is refused and
nothing runs). What TLS cannot stop is the phone itself retrying a command after
a dropped connection; that is what the command ledger is for. The ledger is in
memory only, so a retry that arrives after the PC restarts runs again. The
remote profile holds only `ask`, `search` and `status`, so that costs
computation, not safety.

**Measured while building it.** On Windows 11 with Python 3.13,
`socket.shutdown(SHUT_RDWR)` from another thread does not wake a blocked `recv`
— with or without a timeout — until the peer reacts; `close()` wakes it at
once. Ending a session (revocation, opt-out, stop) therefore closes the
transport, so a revoked phone that simply goes quiet cannot hold its session
thread until the idle timeout.

**The relay address reaches the phone over the local link.** `ping` on the
local network includes the relay endpoint (and a pinned CA, if any) for a
device inside an opt-in. The QR format is unchanged.

**Not done here:** the desktop has no switch, device toggle or relay field for
any of this yet; nothing constructs a `RelayDialler` in production. The
requested wiring is: start `LinkServer` with `REMOTE_TUNNEL` in
`allowed_scopes` and a `RelayDialler` only when
`CONFIG.security.remote_access_enabled` is on and a relay endpoint is
configured; revoke through `LinkServer.revoke` and change opt-ins through
`LinkServer.set_remote_access`, then call `RelayDialler.refresh`.

### 5.5 Additional rules for remote sessions

- **Off by default, per device, with an expiry.** Turning it on is a decision
  taken at the PC for one named phone, not a global setting. Implemented, §5.4.
- **Push-to-talk, not always-listening.** Bandwidth — Opus speech is 16–24 kbit/s
  against 256 kbit/s for raw audio — and an always-open remote microphone is a
  bad idea on its own terms. Implemented on the PC side, §5.6.
- **The capability ceiling is not negotiable at runtime.** A remote session
  cannot request elevation. If the user wants to run an application, they do it
  on the local network.
- **`ask` cannot smuggle actions past the ceiling.** `ask` is natural language,
  so "open Notepad" or "delete the report" is just a question to the link. The
  remote profile would be hollow if a remote question reached the full
  assistant. So the handler picks the answering path by authenticated scope:
  local sessions reach the assistant as before; remote sessions reach only a
  `ToolFreeAsk`, which holds one `chat(messages)` callable and nothing that can
  open, write, delete, print, browse or queue. A plain function offered as the
  remote path is refused when the handler is built. On top, for honesty: a
  request that reads as an action (the task router's computer, browser or
  vision routes, plus printing, which the router does not catch) gets a fixed
  Persian refusal without reaching the model, and every remote answer carries
  `performed_actions: []`, which the phone shows. Tested with a hostile remote
  session against a stand-in for the full assistant, in Persian and English.

### 5.6 Push-to-talk protocol (`core/link_voice.py`)

Three operations, all under the `ask` capability, so the remote ceiling does not
move:

| Operation | Phone sends | PC answers |
|---|---|---|
| `voice_begin` | `format: "pcm_s16le"`, `sample_rate: 16000`, `channels: 1`, `language: fa/en/auto` | `voice_id`, `max_seconds` (30), `max_chunk_bytes` (48 KiB) |
| `voice_chunk` | `voice_id`, `seq` 0,1,2…, `data` (base64 PCM) | bytes received so far |
| `voice_end` | `voice_id`, `reply: text/audio` | `transcript`, `answer`, `refused_action`, `performed_actions`; for `audio`, `voice_reply` events first, carrying the spoken answer as WAV in order |

Raw 16 kHz PCM is 32 kB/s — more than Opus, but it needs no codec on either side
and nothing to decode before the PC's speech-to-text. `format` is a field so a
codec can be added later without a protocol change.

Bounded: one upload per device, 30 seconds, chunks in order and at most 48 KiB,
an idle upload dropped after a minute, a spoken reply of at most 4 MiB.
Chunks carry command ids, so a chunk resent after a dropped connection is
acknowledged rather than appended twice, and an upload survives a reconnect.

The recording is the user's voice. It is kept in memory while uploading, in a
private temporary WAV only while it is transcribed, then deleted; the
synthesised reply file is deleted once read. Neither audio nor transcript is
logged. A transcript is answered through the same scope rule as a typed
question.

The PC's speech-to-text and text-to-speech are injected: `transcribe(wav_path,
language) -> text` and `speak(text) -> wav_path`. Without `transcribe`, the
voice operations refuse with `not_configured`. Wiring them to the existing
Whisper and Chatterbox runtimes is part of the runtime contract in §5.7; it is
not done here, and nothing in this protocol has run against a real microphone,
model or phone.

### 5.7 Runtime contract (`core/link_runtime.py`) — wiring requested, not applied

`LinkRuntime` is the one object the Qronos runtime holds for the link. It owns
the registry, audit log, server, pairing window and relay dialler, and offers
only the actions the desktop needs. It is tested on its own (pairing, device
list, revocation ending sessions, remote opt-in through the fake relay, relay
settings). It is **not yet constructed by the runtime**; the shared files are
owned elsewhere, so the wiring below is a request to Root, not a change made.

Construction, in `QronosRuntime.__init__` — the whole of it:

```python
self.link = LinkRuntime(
    LinkRuntimeSettings.from_config(
        link_enabled=bool(self.settings.get("link.enabled")),                        # default off
        remote_access_enabled=bool(self.settings.get("link.remote_access_enabled")),  # default off
    ),
    services=runtime_services(self),
)
```

`runtime_services` (in `core/link_runtime.py`, tested on a stand-in runtime
whose planner and queue raise if touched) builds the services from what the
runtime already has, with its existing models and settings:

| Service | Existing call | Not used |
|---|---|---|
| remote chat (the only remote answer path, wrapped in `ToolFreeAsk`) | `orchestrator.answer_web_prompt(prompt)` — the Fast brain behind the orchestrator's resource and activity guard, prompt only | `execute_plan`, workers, tools, the queue |
| speech-to-text | `speech_runtime.transcribe_file(path, language)` | — |
| text-to-speech | `voice_output.speak_to_file(text).audio_path` | — |

Each refuses with `busy` while the runtime is busy or paused, so a phone never
queues behind the person at the PC and nothing waits to run after a restart.
Local phones get the same answer-only path until an assistant path with
on-screen approval and `LinkOrigin` propagation exists.

The exact diffs for `core/runtime_bridge.py` (Control's) and
`core/settings_service.py` — two default-off settings, the construction above,
`start` after `runtime_prepared`, `stop` first in `_close`, restart on settings
reload, and a `handle_link_action` for the desktop actions below — were
generated against this branch's base, checked to apply and compile, and handed
to Root. They are not applied here.

Lifecycle: `self.link.start()` once the runtime is prepared (it returns False
and does nothing when the link is off); `self.link.stop()` at the start of
`_close`, before computer actions close, so no phone request can arrive
during shutdown. A settings reload that changes either switch calls
`self.link.restart()` with new settings.

Threading and load: the link calls `remote_chat`, `transcribe` and `speak` on
its own session threads. They must be thread-safe, must respect the runtime's
foreground lifecycle and resource governor, and when the runtime is busy or
paused must raise `LinkOperationError("busy", ...)` rather than queue: a
remote question must never wait in, or be replayed from, the persistent queue.

Desktop actions (through `handle_action`), each a thin call on `self.link`:

| Action | Payload | Call | Event back |
|---|---|---|---|
| `link_status` | — | `status()` | link status (no keys, no phone addresses) |
| `link_pair_open` | `name` | `open_pairing(name)` | the `qronos://pair` URI, **shown as a QR code only — never logged, stored or forwarded** |
| `link_pair_cancel` | — | `cancel_pairing()` | status |
| `link_devices` | — | `devices()` | `DeviceView` list, no keys |
| `link_revoke` | `device_id` | `revoke(id)` | updated device |
| `link_remote` | `device_id`, `enabled`, `days` | `set_remote_access(...)` | updated device |
| `link_relay_set` | `host`, `port`, `server_name?`, `ca_file?` or empty to clear | `set_relay_endpoint(...)` | status |

Revocation and opt-in changes must go through these calls, not through a
registry of the desktop's own, so open sessions end at once; the registry's
cross-process checks are the backstop, not the path.

### 5.8 The phone app (`mobile/`)

**Android: source and protocol core; iOS: nothing.** Stated precisely in
`mobile/README.md`; in short:

| Part | State |
|---|---|
| Protocol core, Kotlin (`mobile/android/linkcore`) | Compiled and unit-tested here (kotlinc 2.4.21 and the committed Gradle build); interoperates with the real PC server directly and through the real relay |
| Android app (`mobile/android/app`) | Compiled against the Android 16 API and packaged as an **unsigned** APK without the Android SDK (aapt2 9.4.1, AOSP-built `android-all`, D8 9.4.28; `mobile/android/tools/check_without_sdk.py`). Not built through Gradle — that needs the SDK licence, which was not accepted — and never run on a phone or emulator |
| iOS | Not implemented, not supported |

One finding changes §4's "the reference client is the specification the phone
app should follow" in practice: **Android's platform TLS cannot speak the
link.** The Android `SSLSocket` reference and Conscrypt's `CAPABILITIES.md`
list PSK cipher suites only for TLS 1.0–1.2; the PC requires TLS 1.3 with an
external PSK and `psk_dhe_ke`. The app therefore uses BouncyCastle's TLS
(`bctls` 1.86.2), which implements TLS 1.3 external PSKs, for the link itself,
and the platform's `SSLSocket` only for the outer connection to the relay. The
same question is open on iOS: Apple's documentation of
`sec_protocol_options_add_pre_shared_key` does not say it supports TLS 1.3
external PSKs, and that must be settled on a Mac before any iOS work.

Measured on loopback (`mobile/android/linkcore/interop/run_interop.py`, Kotlin
client against `core/link_server.py`): TLS 1.3 with
`TLS_CHACHA20_POLY1305_SHA256` both directly and through the relay with its
outer certificate verified against a pinned CA; Persian text round-trips
exactly; the remote ceiling and the action refusal hold; push-to-talk uploads
16 kHz audio and plays back a spoken reply; a connection cut after the PC ran a
command is retried and answered from the ledger with exactly one execution; a
wrong key is refused at the handshake and a revoked device gets `NO_HOST` from
the relay.

What the app does that §2 asked for: it never declares a `VpnService`; on Wi-Fi
it opens the PC connection from `requestNetwork(TRANSPORT_WIFI)`'s socket
factory so a VPN holding the default route is bypassed rather than replaced;
away from home it uses the relay as an ordinary TLS connection inside whatever
VPN runs. None of that has run on a phone.

### 5.9 Must be measured before Layer 2 is relied on

The code exists now; these questions are still open, and code does not answer
them.

1. **Is a European VPS reachable and stable from Iranian mobile networks under
   load?** The whole design assumes a long-lived outbound TLS connection stays
   up. Measure before committing.
2. **Does a long-lived TLS connection to a fixed foreign host attract blocking?**
   This is an availability question, not a confidentiality one, and it decides
   whether the relay needs multiple addresses.
3. **Latency budget end to end**, phone to relay to PC and back, with a
   circumvention VPN in the path. If a voice round trip is unusable, the
   push-to-talk model needs rethinking.
4. **Who runs and pays for the relay**, and what happens when it is down. A
   dependency with no owner is not a plan.

---

## 6. Failure modes

| Failure | Effect | Handling |
|---|---|---|
| Python older than 3.13 | Layer 1 cannot start | Probed at startup; verified to refuse cleanly on 3.12 |
| Windows Python lacks TLS-PSK | Layer 1 cannot start | Probed at startup, clear message; fallback is certificate-based mTLS |
| User's VPN captures the private range | LAN link unreachable **from a browser** — measured. From a native app, unproven | The app pins its socket to the Wi-Fi network (`TRANSPORT_WIFI`). Defeated only by Android lockdown mode |
| PC has a VPN adapter holding the default route | The QR code could advertise an address the phone cannot reach | Fixed: active physical Ethernet/Wi-Fi private addresses outrank VPN/TUN/TAP adapters; link-local and loopback candidates are rejected, with loopback retained only as a visible no-network failure |
| Registry file corrupted | Cannot authenticate any device | Raises rather than starting empty; user re-pairs deliberately |
| Registry file deleted | All devices unpaired | Re-pair; the failure is safe in the right direction |
| Phone lost | Attacker holds a valid key | Revoke at the PC; enforced at the next handshake |
| Pairing window left open | 120-second exposure window | Expires; a new window cancels the old one |
| Two phones pair at once | Second window cancels the first | Second phone fails and re-pairs |
| Malicious frame, huge declared length | Memory exhaustion | Length checked before allocation |
| Handshake flood on the LAN | CPU waste | Connection cap, post-failure interval, logged |
| Relay compromised (Layer 2) | Metadata exposure; denial of service | End-to-end encryption; pairing refused over the relay by path, not address. Tested: tampered bytes end the session with nothing run; a recorded session replayed into the PC fails the handshake |
| Relay unavailable (Layer 2) | No remote access | Local network still works; relay address configurable; dialler backs off to once a minute and audits the outage once |
| Phone retries after a dropped connection (Layer 2) | The same command could run twice | Command id; the retry is answered from the PC's ledger. In memory only: a retry after a PC restart runs again |
| Device revoked or opted out mid-session | An open session outliving the decision | `LinkServer.revoke` closes it at once; every request re-reads the record |

---

## 7. Recommendation and confidence

**Revised 27 August 2026, after testing on real hardware in Iran.**

**Layer 1 is correct and it works.** The transport is sound — measured on
Windows, forward-secret, no dependencies — and every failure mode enumerated here
behaves as designed. Build the phone app on it.

**One question is open, and it belongs to the phone app rather than to this
layer.** A mobile browser cannot reach the PC while a VPN is running, because a
browser cannot choose its network interface. A native Android app can, so this is
very likely an artefact of how it was tested rather than a limit on the design —
but it is not yet proven, and the platform that could prove it does not exist
yet.

What follows:

- **Make interface selection the phone app's first milestone.** Before any voice
  handling, before any UI: request `TRANSPORT_WIFI`, bind the socket, connect to
  the link with the VPN running. That is a day of work and it answers the last
  real question about Layer 1.
- **Try the VPN client's bypass-LAN setting in the meantime.** One toggle and a
  page reload, using the test tooling that already exists. A positive result
  means Layer 1 works for those users today, with no app changes.
- **Fix `local_address()` before a phone app parses the payload.** A PC with a
  VPN adapter can put an address in the QR code that the phone cannot reach, and
  pairing then fails with nothing on screen to explain it. Carrying every
  candidate address and letting the client try each removes the guess; the format
  change is free now and will not be once an app ships.
- **Layer 2 remains the answer for reaching the PC from outside the house.** It
  is not a workaround for the VPN problem, and this result neither raises nor
  lowers its priority.

**Layer 2 as specified: the shape is right, the operational questions are open.**
Outbound-only through an untrusted relay is the correct answer to the constraint
in §2, and I would not expect a better one to emerge. What is unresolved is not
the architecture but whether a relay can be run reliably and by whom — §5.9.
Those are answerable with measurement, and should be answered before code.

**What I would not do:** ship Layer 2 without the per-device remote opt-in and
the reduced capability profile. A tunnel into a process that can delete files and
control the machine is the highest-risk surface in this project, and the
narrowing is what makes it proportionate.
