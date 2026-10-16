# Shared runtime contracts (Track A, A1)

A short reference to the contracts that already exist. Nothing here adds a schema or changes a policy; B and C
consume these types as they are. Writer: Track A (see the team coordination rules).

## ActionRequest (`core/actions.py`)

One thing Qronos has been asked to do, before it is allowed: `category` (an `ActionCategory`), `target` (in the
user's terms), `summary` (one line for the person deciding), `parameters`, and a generated `action_id`. An empty
target or summary, or a non-`ActionCategory` category, is refused with `InvalidAction`. Every request is decided by
`security.gate.evaluate`; a request never grants itself anything.

## RuntimeEvent (`core/runtime_bridge.py`; Rust `desktop/src-tauri/src/runtime/mod.rs`)

`{event_type, status, message}`. It is sent as JSON lines on the bridge's stdout and delivered to the desktop on
`qronos://runtime-event` as `{eventType, status, message}`. Rust drops pairing keys from the log and routes
`link_pairing` separately.

Runtime lifecycle events the desktop relies on:

| eventType | Meaning | Desktop use |
|---|---|---|
| `runtime_ready` | a new bridge reached its command loop | connection **ready**; Wake start is sent |
| `runtime_pong` | an already running bridge answered `ping_runtime` | connection **ready** |
| `runtime_error` | a diagnostic (bridge error, stderr traceback, closed stream, unconfirmed command) | startup: **failed**; after ready: **degraded**. Valid audio playback is never stopped by it |
| `runtime_stopping` | the bridge is ending (Stop/restart) | **reconnecting**; the supervisor restarts it, the desktop spawns nothing |
| `voice_turn_failed` | a voice turn ended in an error; emitted once after its `runtime_error`, with the fixed message `{"reason": "voice_turn_failed"}` | a Persian voice notice and a setup re-check; its retry is one explicit new push-to-talk turn. Cleared by the next `voice_listening` |

Spawning (`start_runtime`) is never "ready". The person sees only fixed Persian texts
(`desktop/src/runtimeConnection.ts`), never a message, path or provider. Retry is offered only after a failure or an
error, one attempt at a time. It only checks, starts or pings the runtime: it never replays a user action and never
starts inference or a microphone (the separate voice notice's retry is the person's own new push-to-talk). Late results of an older attempt, or of a disposed view, are ignored.

## Device Link WAN wiring (A90 with C; approved interface 2026-10-16)

- Setting `link.remote_access_enabled` (bool, default false), changed only at this PC through the settings
  save/reload. It is effective only with `link.enabled`; `_link_runtime_settings` passes their conjunction.
- `qronos.link_relay_set`:
  - set is `{host, port, serverName?, caPem?, requestNonce}`, with exact keys and bounded types: host and serverName
    at most 253 characters, port an int 1..65535, caPem at most 16 KiB of UTF-8, and never a CA path or key;
  - clear is `{clear: true, requestNonce}`;
  - C's `relay_endpoint_from_request(data, data_dir)` validates the request and stages it before any save;
  - saving while remote access is off is stored, never dialled;
  - a quarantined link refuses a new endpoint; clearing is always allowed.
- `qronos.link_remote`:
  - on is `{deviceId, enabled: true, days: int 1..365, requestNonce}`. It needs the setting, a configured relay,
    `monitor.remote_admission().allowed` and an unrevoked device;
  - off is `{deviceId, enabled: false, requestNonce?}` and is always allowed;
  - `qronos.link_revoke` is `{deviceId, requestNonce?}`;
  - older callers without a nonce are unchanged.
- `link_operation_result`:
  - `{requestNonce, action, status: "applied" | "refused", code?}` is emitted once per mutation that carried a safe
    nonce, after the actual commit or refusal;
  - the nonce correlates; it grants nothing;
  - codes: invalid_request, relay_invalid, quarantined, remote_access_off, relay_not_configured, device_revoked,
    unknown_device, failed;
  - `link_status`/`link_devices` only refresh the view, and `link_error` stays as before.
- Not done by this wiring: deployment, a relay operator or certificate, a live remote enable, or real-phone LAN/WAN
  acceptance.

## ApprovalLedger (`core/approval_ledger.py`)

These are single-use identities for prompts that wait on a person (screen read, Paint, file actions):

- an identity is bound to the exact prepared action;
- `take` consumes it once (a second answer gives `StaleApproval`), and a wrong kind is refused and left untouched;
- Stop, Pause and shutdown revoke every open identity;
- identities expire, and after `close` nothing is issued or taken.

The ledger grants nothing: an accepted answer still passes the action's own gate re-evaluation.

## Retained policy (PR94, `core/code_policy.py`)

Read-only code analysis is permitted (`CODE_ANALYSIS`, AUTO_ALLOW). Code generation, code modification and script
execution are forbidden. The chat checks requests before any Brain call and answers afterwards, deterministically.
The decision and its audit record come from `security.gate.evaluate`. Any change to this policy is the owner's
decision, not this document's.
