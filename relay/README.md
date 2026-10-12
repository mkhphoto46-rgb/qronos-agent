# Qronos relay

The relay lets a paired phone reach its Qronos PC from outside the house
without a listening port on the PC, port forwarding or a VPN app on the phone.
The PC dials out and parks; the phone dials in; the relay joins the two and
copies bytes. See `docs/QRONOS_DEVICE_LINK.md` §5 for the design.

**Status: software only.** Tested on loopback with throwaway certificates. No
relay has been deployed, and no default relay exists: Qronos uses one only
when the user configures its address. Whether a relay is reachable and stable
from Iranian mobile networks is an open question (§5.9) that this code does
not answer.

## What the relay can and cannot do

The phone and the PC run a TLS 1.3 session keyed by their pairing key *inside*
the relay connection. The relay never has that key.

| A relay operator, or anyone who takes over the relay, can | cannot |
|---|---|
| see that a PC and a phone connect, when, and how much data moves | read requests or answers |
| refuse service, or join a phone to the wrong PC (the handshake then fails) | alter or inject a request — tampering ends the session |
| learn one opaque route per remote-enabled device | learn the pairing key, or enrol a phone (pairing is local-network only) |

The relay keeps no contents and logs no route ids or addresses. It prints
counts — connections, parkings, joins, refusals by reason — every few minutes.

## Requirements

- A Linux machine reachable from the internet on TCP 443 (any port works; 443
  looks like every other HTTPS connection).
- Python 3.11 or newer. Nothing else: standard library only.
- A certificate for the relay's DNS name, for example from Let's Encrypt, or a
  private CA whose certificate you give to Qronos as the relay's pinned CA.

## Running it

From the `relay` directory:

```sh
python3 -m qronos_relay --cert /etc/qronos-relay/fullchain.pem \
                        --key  /etc/qronos-relay/privkey.pem
```

Options: `--listen` (default `0.0.0.0`), `--port` (default `443`),
`--max-connections`, `--max-per-address`, `--stats-every` (seconds; `0` turns
the count lines off).

Binding port 443 needs either root or `CAP_NET_BIND_SERVICE`. A minimal
systemd unit, running as an unprivileged user:

```ini
[Unit]
Description=Qronos relay
After=network-online.target

[Service]
User=qronos-relay
WorkingDirectory=/opt/qronos-relay
ExecStart=/usr/bin/python3 -m qronos_relay --cert /etc/qronos-relay/fullchain.pem --key /etc/qronos-relay/privkey.pem
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Limits

Every resource is bounded (`RelayLimits` in `qronos_relay/server.py`):

| Limit | Default |
|---|---|
| Connections in total / per source address, counted from accept - before any TLS work, so unfinished handshakes count | 1024 / 32 |
| Routes with a parked PC | 4096 |
| PCs parked per route | 2 |
| Time to finish the TLS handshake / then to send the 37-byte hello | 10 s / 10 s |
| Time a phone waits for its PC to park | 10 s |
| Keepalive to a parked PC | every 25 s |
| Time a PC may stay parked before it must dial again | 15 min |
| Idle time / total time for a joined pair | 10 min / 4 h |
| Bytes in flight per direction | 16 KiB, with back-pressure |

## Wire format

The only bytes the relay reads: after the outer TLS handshake, each side sends
`"QRL1"`, a role byte (1 = PC, 2 = phone) and a 32-byte token. The relay
answers with one-byte statuses: `0x00` parked, `0x01` joined, `0x02` keepalive,
`0x10` no PC for that route, `0x11` route full, `0x12` bad hello, `0x13` over
capacity, `0x14` parking expired. After `0x01`, every byte belongs to the other
side.

A PC's token is its route key; the relay files it under `SHA-256("qronos relay
route id v1" ‖ key)`. A phone's token is that route id. The definition is
`core/link_relay_wire.py` in the Qronos repository; `qronos_relay/wire.py` is a
copy, so the relay can be deployed without the rest of Qronos, and the Qronos
test suite fails if the copy drifts.

## Not done

- No deployment, no operator, no default relay address.
- No multi-address or domain-fronting strategy if a relay address is blocked;
  §5.9 question 2 has to be measured first.
- No rate limiting of hellos beyond the per-address connection cap.
