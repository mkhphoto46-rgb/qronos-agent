# Qronos software wrap-up status

Date: 2026-10-09. Owner: independent review worker.

This is an audit record, not a release claim. Nothing here is production-ready
or live-ready. Live PC and phone acceptance is still deferred, and the external
gates below remain closed. The requirement keys (A8, D3, H1, and so on) refer
to `docs/qronos_software_phase_requirements.md`.

## Accepted software helper boundaries

All verdicts are for exact commits, checked in exported immutable snapshots.
The live-device gate was on (`QRONOS_RUN_LIVE_DEVICE_TESTS=0`) and no real
device, model, screen, audio, printer, GPU or phone was used.

| Area | Accepted source | What it is | What it is not |
|---|---|---|---|
| Installer update/ownership repairs | chain through `d19cbda` (PR #62 head `1f9e4ee`) | Owned-file manifest, link-safe removal, install root resolved before overlap checks | No current full installer build has been reviewed; clean-machine upgrade/uninstall is deferred |
| Authenticated update helper (A8) | `core/local_update.py`, `core/ed25519.py` | Offline verification of owner-signed manifests and a locked, typed store | A library only. It has no update UI, no launch/restart flow, no store-migration architecture and no runtime, desktop or installer caller. Shipped trust is empty, and the owner signing key is a gate |
| Runtime resource signals (B3) | `5868262` + `bf76118` (PR #63 head `53efed8`) | Bounded resource signals | — |
| Supervisor and Elevated Action Broker (D8) | `3a2bacf`..`4b8252e`; composition `0308669` | Supervisor, broker and a named-pipe channel with deadline-bounded sends and finite timeouts | `PRODUCTION_ACTIONS` is empty and no product caller or launcher is wired. It elevates nothing today |
| Remote chat profile (L4) | `e97736b` | The remote chat call uses exactly the configured Fast profile | — |
| Deterministic Security Monitor and WAN gate (D9) | gate `0feeea0`; D9-1 fix `ff3937c` (Root Link final `d983251`, the same patch) | Per-principal reject/throttle/contain/quarantine. The fix bounds LAN peers separately (1024) from relay, device and system principals (256), so LAN churn cannot evict them | The WAN UI is hard-off and no relay is deployed. WAN is not enabled |
| Control/desktop Device Link wiring (E2) | through `2e75722`; composition `25c9946` | Desktop pairing panel and runtime bridge | Live phone pairing is deferred |
| Android companion | source only | Source plus unsigned, SDK-free evidence | Not built with the SDK, not signed, never run on a phone |

## Fixtures by scope

| Scope | Source | Result | Run by |
|---|---|---|---|
| D9-1 reviewer reproducer (churn, partial counter, bounds, monotonic expiry, LAN fairness) | `0feeea0` | 4 failed, 3 passed (reproduces D9-1) | reviewer |
| Same | `ff3937c`, `25c9946` | 7 passed each | reviewer |
| `test_security_monitor.py` + `test_link_security_integration.py` | `25c9946` | 50 passed, 21 subtests | reviewer |
| Network inventory + live-device gate + `test_supervisor_ipc.py` | `0308669` | 175 passed | reviewer |
| IPC + gate + inventory | `0308669` | 125 passed, 60 subtests | Root (not rerun) |
| E2 desktop tests; Vite build | `25c9946` | 98 passed; build passed | Root (not rerun) |
| Installer independent harness; installer tests | `d19cbda` | 64/64; 278 passed | reviewer (round 2) |
| B3 / L4+D9 / D8 / E2 Python, desktop, cargo `--offline` | accepted SHAs above | 66 / 97 / 153 / 213, 29, 36 passed | reviewer (round 2) |

Each figure comes only from the test files run for that scope. No broad suite
was run in this wrap-up.

## Source identity

- `core/security_monitor.py` (`ab03fc6`) and `tests/test_security_monitor.py`
  (`0fcd713`) are byte-identical at `ff3937c`, `d983251` and `25c9946`. At
  `25c9946`, the E2 files are identical to `2e75722`, and the Link core files
  (`link_server`, `link_tunnel`, `link_runtime`, `orchestrator`) are identical
  to `d983251`.
- At `0308669`, `core/supervisor_ipc.py`, `supervisor.py`, `action_broker.py`,
  `action_broker_client.py`, `uac_launch.py` and `pipe_peer.py` are
  byte-identical to `4b8252e`, and the head commit has the same patch-id as
  `4b8252e`. No newer `supervisor_ipc` commit exists.
- On `0308669`, the inventory keeps `PENDING_EXCEPTIONS = {"core.link_tunnel"}`.
  This is correct for a D8-only branch without Link:
  - The canonical marker gives no violations.
  - An empty marker reports `core.link_tunnel` as an exception for a module
    that does not exist.
  - Marking `core.supervisor_ipc` pending reports that it has landed.
  - The marker becomes empty only where Link and D8 are composed together.
- `config/brains.json` is `4f4bcc3c` on every snapshot: specified models and
  generation settings are unchanged. The paired Device Link and local-pipe
  grants are recorded in the audited network inventory. The existing
  `code_analysis` permission policy is preserved.

## Work still left

Software (M/P):
- D8: register reviewed production actions, and wire the supervisor and broker
  callers into the runtime and desktop.
- A8: update UI, launch/restart flow and store-migration architecture.
- D9/WAN: a WAN UI and a deployed relay. WAN stays off until both are reviewed.
- D3: fresh window/process/screenshot binding with postconditions.
- D4: native Paint drawing.
- D10: pre-model content gate, output inspection and a measured red-team.
- D11: activity/approval/error/undo history screen.
- H1: Device Hub (no source).
- A6: a fresh installer built from reviewed current source, with provenance.

External gates (G), which code cannot close:
- A12: CI is blocked by billing.
- A8: owner signing keys.
- A10: Microsoft runtime redistribution rights, plus other owner licence rights.
- C2: the custom wake-word head (`qronos.onnx` + `.data`).

Deferred (D):
- All live PC and phone acceptance: clean-machine install, upgrade and
  uninstall, Android build, signing and on-device use, Device Link pairing
  over LAN/WAN, and elevated actions.
