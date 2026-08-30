# Qronos Delivery Status

Last updated: 2026-08-24

This report separates product intent, prototypes, tested implementation, and
production readiness. Percentages are engineering estimates, not a promise of a
release date.

## Current Overall Estimate

- Full requested product scope: approximately **20% implemented**.
- Installable production-ready product: **not available yet**.
- Current state: backend prototypes and tested components, without an end-to-end
  application runtime or desktop UI.

The estimate is deliberately conservative. Unit-tested components count as
progress, but they do not count as complete features until they are connected,
tested end-to-end, packaged, secured, and validated on a clean Windows machine.

## Capability Status

| Capability | Product target | Prototype / component | Tested implementation | Production-ready |
|---|---|---|---|---|
| Wake word | Always-available local activation | Audio input, ONNX engine, trigger state machine | Component tests and real model load | No: poor model quality and no authorized live-mic test |
| STT / TTS / Voice Session | Natural local voice conversation | Not end-to-end | No | No |
| Fast Brain | Fast local conversation and routing | 4B integration and lifecycle policy | Component tests; real benchmark pending final model tag | No |
| Heavy Brain | On-demand deeper reasoning | 14B integration and offload policy | Component tests; real hardware benchmark pending | No |
| Resource protection | Always yield to user workload | CPU/RAM/GPU/VRAM/temperature checks, activity modes | Preflight and second fresh-check tests | No: process attribution and in-flight monitoring remain |
| Permission Engine | Five levels with Level 5 hard denial | Explicit category-policy map | Unit tested | No: not connected to an Action Broker/runtime |
| Intrusion response | Reject, contain, then safe shutdown | Architecture and policy defined | No gateway integration tests | No |
| Application/file/browser control | Structured approved actions with undo | Early folders/interfaces only | No end-to-end execution | No |
| Code prohibition | No runtime code generation/analysis/modification/execution | Level 5 categories and layered design | Policy-map tests | No: Content Gate and Action Broker enforcement remain |
| Desktop UI / system tray | Polished standalone Windows UX | Architecture choice only | No | No |
| Windows service / installer / updater | Signed installable application | Architecture choice only | No clean-machine test | No |
| Remote mobile PTT | Secure optional gateway | Target only | No | No |
| Printer / 3D printer / Device Hub | Later-stage device integrations | Target only | No | No |

## Time Range

Assuming one primary developer working consistently with AI assistance, fixed
requirements, and no major hardware/vendor blockers:

- First installable local alpha (UI shell, Fast Brain, basic voice path,
  resource panel, no broad computer control): roughly 2–4 months.
- Security-reviewed local beta (voice session, permission-gated limited tools,
  Action Broker, logging/undo, installer/update testing): roughly 5–9 months.
- Full requested scope including hardened remote access, broad computer/browser
  execution, recovery testing, and initial Device Hub: roughly 12–18+ months.

Part-time development, wake-word retraining, code-signing delays, extensive
device support, or major UX changes can extend these ranges. Security-sensitive
features must not be compressed merely to meet a date.

## Next Completion Gate

The next milestone is not “all features.” It is a measurable local runtime gate:

1. Verify the exact Fast and Heavy model artifacts on the development machine.
2. Benchmark cold load, warm response, generation, unload, and Fast recovery.
3. Implement process-aware continuous Resource Sentinel monitoring.
4. Connect the immutable Permission Policy to a structured Action Contract.
5. Build the first non-elevated desktop UI shell and system tray.

