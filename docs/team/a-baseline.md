# Track A/C baseline and product freeze (A0)

This records which history the A/C tracks work from, who owns what, and what has and has not been verified. It
is the A0 deliverable. It changes no code.

## Selected history

- `main` is `03cd331` ("Merge reviewed Qronos software integration and validation repairs (#77)").
- The accepted line `0890d0d` (signing readiness) and `main` split at `ad89443`.
- `main` has three commits the accepted line lacked: `c5baf85`, `42116ed` and the merge `03cd331`. They touch
  tests and docs only.
- Baseline: `0890d0d`. `main` was merged into it as `052fb20`, reconciling the four shared test files by hand. Then
  the accepted licence candidate `cc2d103` and image candidate `189069a` were replayed. The result, `6599af1`, is
  the head of PR96.
- Nothing from `main` is lost: `03cd331` is an ancestor of every later commit below.

## Review stack

Each PR is based on the one before it. All of them are open; none is merged to `main`.

| PR | Head | Content |
|----|------|---------|
| 96 | `6599af1` | Apache-2.0 adoption and real image retrieval, on the baseline with `main` merged |
| 97 | `041fb94` | Runtime startup failures shown, with a safe retry (A84) |
| 98 | `53dd571` | Android build with pinned standard tooling (C80/C81/C84/C85) |
| 99 | `1755aa5` | Default-off paired-phone remote access in Settings (A90/A93, C87–C92) |
| 100 | `91551fa` | The official Gradle wrapper exception in the mobile source check (A97, test only) |

**Product freeze:** `1755aa52`. The fresh unsigned review installer was built from this commit and passed a static
peer review (C93/C95, A98). PR100 changes only `tests/test_link_mobile_contract.py`.

**Pending publication:** `27c424a` (C97). It corrects the runtime asset registry
(`build_support/runtime_assets.json`, which registers the cached SigLIP 2 files as verified) and
`docs/qronos_project_context.md`. It is not yet in a PR. It changes no Python, desktop or Rust code. The installer
bundles no model assets, so the review installer above is unaffected.

## Ownership

- **A:** the runtime, `core/runtime_bridge.py`, shared contracts (`docs/team/shared-contracts.md`),
  `core/update_stores.py`, and the central `desktop/src/App.tsx` and Rust IPC.
- **C:** `core/link_*`, `mobile/`, `relay/`, packaging, update, installer and locks.
- **B:** a separate track. A/C only consume its explicit, reviewed handoffs.
- Only one writer per shared file. Changes that cross owners go through a small integration request. Root
  integrates and publishes; workers do not push, merge or deploy.

## Verification of record

**Python broad gate** (A100), run on `27c424a`:
- It is the source-audited suite **minus 13 excluded files**, not the full suite.
- The excluded files run NSIS installers, makensis builds, PowerShell, real service or supervisor child processes,
  or the benchmark child.
- It ran with `QRONOS_RUN_LIVE_DEVICE_TESTS=0`, so the live-device nodes skip.
- Result: exit 1. 6594 passed, 26 failed, 111 skipped, 1577 subtests passed, in 551 s.
- The failures had five causes:
  - **7, the artwork check:** the checkout's working copy of `desktop/public/tauri.svg` has CRLF line endings
    instead of the committed LF bytes.
  - **14, PDF reports:** the pinned `fpdf2==2.8.9` is missing from the fixture environment.
  - **3, the Ollama bootstrap/cancellation tests:** they assume auto-start, but the gate environment sets
    `QRONOS_OLLAMA_AUTO_START=0`.
  - **1, a stale test:** an image-embedding test still expects the SigLIP 2 registry entries to be `missing` after
    C97.
  - **1, the offline network policy:** `core/link_runtime.py` imports `ssl` (only to validate a CA PEM) without a
    reviewed exception.
- The full list is in the A100 report, a working record outside the repository.

**Repairs:**
- **A101 (4 of the 26):** each of the 3 Ollama tests now sets auto-start itself; its start stays faked and the
  guards are unchanged. The network policy gains an exact `core.link_runtime -> {ssl}` entry for relay CA parsing.
  - Focused check: 5 nodes, run with `QRONOS_OLLAMA_AUTO_START=0` in the parent environment.
  - Result: exit 0, 5 passed.
  - No product code changed.
- **C101 (the other 22):** the artwork, PDF and stale-asset repairs are **pending** until Root confirms them.
- The broad gate has not been rerun after these repairs.

**Desktop:**
- `npm test`: exit 0, 183 of 183 passed across the 12 node unit-test files (A100). They ran on byte-identical
  desktop sources.
- TypeScript typecheck, Vite build and the Rust release build: compiled by the C95 installer build on `1755aa52`.
- Rust unit tests (C99): 72 passed and 2 ignored, using the local Rust 1.98.0. CI pins 1.98.1.

**Android:**
- The Gradle linkcore vector tests are reused; the linkcore tree is unchanged.
- The debug APK was accepted statically (A89). It has not been run on a phone.

**Installer:**
- An unsigned review candidate from `1755aa52`, accepted on static evidence (A98).
- This is build proof only, not installation or quality proof.

## Not verified (explicit limits)

- **Open failures:** the 22 broad-gate failures C101 is repairing, pending Root's confirmation.
- **Excluded tests:** the 13 excluded Python files. The 2 ignored Rust tests did not run.
- **Deferred:** human and live tests on the PC, phone, GPU, microphone, devices and user archives.
- **Provisioning:** runtime model assets are provisioned separately and are not bundled.
- **External:**
  - no genuine Authenticode signing, reviewed trusted root, update key or signing receipt;
  - the relay is not deployed;
  - no real-phone LAN/WAN acceptance;
  - no clean-Windows installation or launch;
  - public visibility, signing submissions and relay deployment each need explicit owner authorisation.

## This document

This is a docs-only commit on top of `27c424a`. The tests and builds above ran on `27c424a` (Python) and `1755aa52`
(desktop and installer). A docs-only change does not invalidate them.
