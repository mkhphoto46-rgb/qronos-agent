# Qronos Security Monitor (requirement D9)

`core/security_monitor.py`. It is deterministic and has no model and no network access.
It is required before Device Link remote access (Layer 2) is offered to users.

**State:** implemented and tested on loopback fixtures. It is not yet independently
reviewed. It is not yet wired into the runtime or desktop; Control holds that work.
It has never run with a real phone, relay or user registry.

## The ladder

This is the escalation the product architecture specifies, applied only to
Qronos's own Device Link.

| Response | What happens | Triggered by |
|---|---|---|
| Reject | The action is refused; the monitor counts it | Every signal below its threshold |
| Throttle | One principal is slowed or paused for a set time | A typed policy's threshold within its window |
| Contain | Remote access stops: the dialler parks nothing, remote sessions end, relayed connections are refused. The LAN keeps working. Latched until reset | Deterministic evidence only: tampering inside an authenticated relayed session, or an unreadable device registry |
| Quarantine | The whole link stops, LAN included. Latched until reset | 3 containments within 24 hours |
| Safe shutdown | The runtime's `request_safe_shutdown` hook is asked to stop Qronos | Quarantine, only when `shutdown_on_quarantine` is set. Off by default |

Rule of thumb: one malformed packet, or a message claiming an attack, can
never contain or quarantine. Two signals can contain, and each is evidence
by construction:

- The inner TLS rejected a record inside a session that had already
  authenticated through the relay. TCP and the outer TLS protect the bytes
  as far as the relay, so the alteration happened past it.
- The device registry cannot be read. Nobody can then say which devices are
  trusted.

A byte altered *during* a handshake cannot be told apart from a wrong key. It
is an unauthenticated failure, counted against the source, and never contains.

The monitor never shuts down Windows, never touches another program and never
touches user files. Safe shutdown means asking Qronos's own runtime to stop.
The link never does that by itself, and does not ask by default: link signals
cannot prove the whole PC is compromised, and quarantine already removes the
link.

## Who a signal is held against

| Principal | Meaning | Throttle effect |
|---|---|---|
| `lan_peer:<ip>` | A LAN address before it proved a key | Slowed, not banned: one attempt per 5 s, so a phone sharing the address still gets in |
| `relay` | Anything the relay delivers before a key is proved | The dialler stops parking; relayed connections are refused. The LAN is unaffected |
| `device:<id>@<scope>` | A device that proved its key, in one scope | That scope only. Abuse over the relay limits remote use, not LAN use |
| `system:qronos` | The PC's own integrity checks | — |

A claimed identity that was not proved is never a principal. Example: the
device id offered in a failed handshake. Otherwise anyone could get a real
phone refused by failing in its name. Relayed failures are never held
against a LAN address, even when the relay shares one. Each policy names the
principal kinds it accepts. Attributing a signal to the wrong kind is a
programming error and raises.

## Policies (defaults)

| Signal | Principal | Threshold / window | Response |
|---|---|---|---|
| handshake_failed | LAN peer, relay | 10 / 60 s | throttle 5 min |
| pairing_over_relay | relay | 3 / 1 h | throttle 1 h |
| unauthenticated_splice | relay | 10 / 10 min | throttle 15 min |
| relay_misbehaved | relay | 5 / 10 min | throttle 15 min |
| tampering | relay | 1 | contain |
| request_refused | device | 20 / 10 min | throttle 15 min |
| rate_limited | device | 10 / 10 min | throttle 15 min |
| command_conflict | device | 5 / 10 min | throttle 15 min |
| protocol_fault | device | 5 / 10 min | throttle 15 min |
| registry_unreadable | system | 1 | contain |

Windows and throttles use a monotonic clock. Only the 24-hour quarantine
history uses wall time, because it must survive restarts.

A command still running, or already answered, is a phone retrying
correctly. It never counts against the device.

## State, records, recovery

- **Memory is bounded, in two tables that never evict each other.** LAN
  addresses: at most 1024 windows and 1024 throttles. The relay, the system
  and devices: at most 256 of each. So the total is 1280 of each. Each
  window holds no more entries than its threshold. When a table is full,
  expired throttles go first, then its least recently seen entry. LAN churn
  can therefore only ever forgive another LAN address early. It cannot
  lift an active device or relay throttle, or wipe a device's partial count
  (review finding D9-1, repaired).
- **Latches persist.** `data/link_security.json` holds only the latches and
  the containment history. An unreadable file reads as contained.
- **Incident log.** `logs/link_incidents.jsonl` is hash-chained and
  size-capped. It gets one line per decision or reset, never one per
  signal, so a flood cannot rotate history away. `verify_chain` detects an
  edited or removed line.
- **Nothing secret is recorded.** No keys, no route tokens, no request
  contents.
- **Recovery.** Throttles end by themselves, and counting then starts
  afresh. Containment and quarantine end only through
  `LinkRuntime.reset_security()`. That is a console action at the PC, and
  device management is never reachable over the link.

## Where it is enforced

- **`LinkServer`**
  - Refuses to start with the remote scope and no monitor.
  - Every remote-scoped connection meets `remote_admission()`.
  - Throttled LAN peers and a throttled relay are refused before any TLS work; a throttled
    device is refused as soon as it has proved its key.
  - Device throttles and containment end sessions.
  - Every request re-checks quarantine, containment and the device throttle.
  - Relayed and LAN pause keys are kept apart.
- **`RelayDialler`**
  - Will not start unless remote access is admitted.
  - Parks nothing while contained.
  - Waits while the relay is throttled.
  - Resumes when the monitor is reset.
  - Reports unauthenticated splices and relay misbehaviour.
- **`LinkRuntime`**
  - Owns one persisted monitor.
  - Does not start a quarantined link.
  - Starts remote access only when admitted.
  - Stops the link on quarantine.
  - Exposes `reset_security()` and `status()["security"]`.

Tests: `tests/test_security_monitor.py` covers the rules, bounds, latches and
chain. `tests/test_link_security_integration.py` covers enforcement on
loopback. `tests/test_link_relay_abuse.py` covers hostile relays.
