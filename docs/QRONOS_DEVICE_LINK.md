# Qronos Device Link — Layer 1 and Layer 2

**Status**

| Layer | Name | State |
|---|---|---|
| Layer 1 | Local Link — phone and PC on the same network | Implemented, tested |
| Layer 2 | Relay Link — phone reaches the PC over the internet | Proposed, not built |

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

Layer 1 is not affected by this directly, but it inherits a related hazard: a
full-tunnel VPN configured with `0.0.0.0/0` and no private-range exception will
capture traffic to `192.168.x.x` as well, and break local access. See §4.10.

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

Every module follows the existing conventions in `core/`: frozen dataclasses,
enums over string literals, pure decision functions where the decision can be
separated from the effect, injected clocks and dependencies so tests need no
sockets, no timers and no network.

Two scripts under `tools/` exist to verify a real deployment:

| Script | What it answers |
|---|---|
| `tools/link_selfcheck.py` | Does the link work on this machine at all? Runs the real server and client over loopback and reports the TLS-PSK verdict first. |
| `tools/link_reachability.py` | Can a phone on this network reach this PC on the link's port, with the user's VPN running as it normally does? Plain HTTP, because a browser cannot speak the link protocol. |

The second is the one that answers the open question in §4.10 about VPN
interference, and it reports its verdict using the link's own peer classifier,
so the answer is the real decision the link would make about that phone.

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
2. **Does the LAN path survive the VPN the users actually run?** A full-tunnel
   configuration with no private-range exception will break local access
   entirely. Worth testing before the phone app is written, because if it fails,
   Layer 1's usefulness for the real audience is much smaller than it looks and
   Layer 2 becomes urgent rather than optional.

### 4.11 Trade-offs accepted

| Decision | Cost accepted |
|---|---|
| PSK instead of certificates | The key is symmetric, so it cannot live in the phone's Secure Enclave as a non-extractable key the way an EC private key could. Mitigated by keychain storage with biometric protection, and by revocation. Revisit in Layer 2 if hardware-backed keys become important. |
| Threads, not async | Simpler, and matches a codebase with no asyncio. Fine for one to three devices; wrong for hundreds. |
| QR-only pairing | Requires a camera. |
| No discovery | Reconnection after the PC's address changes needs re-entry or a re-scan. |

---

## 5. Layer 2 — Relay Link (proposed)

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

### 5.4 What actually changes in the code

Very little, which is the point of §4.4.

| Change | Size |
|---|---|
| Peer classification returns `REMOTE_TUNNEL` instead of refusing | One branch |
| Server accepts a scope other than `local_network` | One entry in `allowed_scopes`, behind `remote_access_enabled` |
| Per-device `remote_enabled` is checked | One check, already in the record |
| A tunnel dialler: outbound connection, reconnect with backoff | New, small |
| Relay: accept two sides, match them, copy | New, or adopted |

The capability profile for `remote_tunnel` already exists and already grants
three capabilities out of nine. The audit log already records scope. Nothing in
the protocol changes.

### 5.5 Additional rules for remote sessions

- **Off by default, per device, with an expiry.** Turning it on is a decision
  taken at the PC for one named phone, not a global setting.
- **Push-to-talk, not always-listening.** Bandwidth — Opus speech is 16–24 kbit/s
  against 256 kbit/s for raw audio — and an always-open remote microphone is a
  bad idea on its own terms.
- **The capability ceiling is not negotiable at runtime.** A remote session
  cannot request elevation. If the user wants to run an application, they do it
  on the local network.

### 5.6 Must be measured before Layer 2 is built

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
| User's VPN captures the private range | LAN link unreachable | Not detectable from the PC side; needs a diagnostic in the phone app |
| Registry file corrupted | Cannot authenticate any device | Raises rather than starting empty; user re-pairs deliberately |
| Registry file deleted | All devices unpaired | Re-pair; the failure is safe in the right direction |
| Phone lost | Attacker holds a valid key | Revoke at the PC; enforced at the next handshake |
| Pairing window left open | 120-second exposure window | Expires; a new window cancels the old one |
| Two phones pair at once | Second window cancels the first | Second phone fails and re-pairs |
| Malicious frame, huge declared length | Memory exhaustion | Length checked before allocation |
| Handshake flood on the LAN | CPU waste | Connection cap, post-failure interval, logged |
| Relay compromised (Layer 2) | Metadata exposure only | End-to-end encryption; pairing stays local-only |
| Relay unavailable (Layer 2) | No remote access | Local network still works; relay address configurable |

---

## 7. Recommendation and confidence

**Layer 1 as specified: build it, high confidence.** The transport primitive is
measured rather than assumed, the failure modes are enumerated, and it is useful
on its own with no external dependency and no server to run. The one open risk
is Windows PSK support, which is cheap to check and has a known fallback.

**Layer 2 as specified: the shape is right, the operational questions are open.**
Outbound-only through an untrusted relay is the correct answer to the constraint
in §2, and I would not expect a better one to emerge. What is unresolved is not
the architecture but whether a relay can be run reliably and by whom — §5.6.
Those are answerable with measurement, and should be answered before code.

**What I would not do:** ship Layer 2 without the per-device remote opt-in and
the reduced capability profile. A tunnel into a process that can delete files and
control the machine is the highest-risk surface in this project, and the
narrowing is what makes it proportionate.
