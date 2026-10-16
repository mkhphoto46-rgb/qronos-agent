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

**Composed after the freeze** (`2d92e8b`, integrated by Root):
- C97 `27c424a`: the runtime asset registry (`build_support/runtime_assets.json`) registers the cached SigLIP 2
  files as verified. It also corrects `docs/qronos_project_context.md`.
- A0 and A101: this document, plus the test fixture and network inventory repairs.
- C98, C99 and C101: the delivery-gates doc, the Rust receipt and one test.
- Product Python, TypeScript and Rust code is unchanged from `1755aa52`.
- The asset registry JSON is a source-only release-checker file. C102 and A102 independently proved it is **not**
  bundled: the installed product code and assets are unchanged from `1755aa52` to `31fc2ad`.
- The `31fc2ad` rebuild is therefore a fresh, source-bound candidate, not a runtime feature change.
- The installer bundles no model weights.

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
- **C101 (the other 22):** focused follow-up, 22 passed (accepted by Root).
- Root causes resolved:
  - the checkout's `tauri.svg` was restored to the committed 2599-byte blob (no source change);
  - the pinned PDF dependencies were installed in the fixture environment;
  - the image-embedding test now expects the verified SigLIP 2 entries with exactly the publisher pins.
- The pinned `defusedxml`: installed into the fixture environment as a pinned fixture dependency, with its metadata
  verified (C102).
- All 26 originally failed node IDs are covered by these focused follow-ups (A101 also ran one extra policy check).
  The 26 failures above remain the record of A100.
- The broad gate has **not** been rerun, so there is no broad or full pass claim.

**Remaining software gate** (A104–A106, after the asset-root isolation fix):
- The fix: the conftest now points `QRONOS_ASSET_ROOT` into each test's own profile, so an inherited user-level
  asset root (installed assets) is never used by tests or their children.
- Regression `test_software_profile_redirects_an_inherited_asset_root`: failed on the old fixture, passed after the
  fix.
- 9 of the 13 excluded files were source-audited and then run:
  - A104, 5 files (3 whole; 2 minus 6 functions, which expand to 17 parametrized cases): exit 0, 261 passed,
    3 skipped, 17 deselected.
  - A105, the supervisor and supervisor IPC tests (controlled stdlib and named-pipe fixture children): exit 0,
    73 passed.
  - A106, the bridge-process and supervisor-relay tests: exit 0, 80 passed, 1 skipped (the live-marked real
    end-to-end node). They ran after confirming no Qronos process held the session bridge mutex.
- Not run: the 4 files that run native NSIS installers or uninstallers, or write the HKCU registry, plus the 6
  deselected functions. These are deferred to a disposable Windows machine. The live-device nodes have not run
  either. This is not the full suite.

**Desktop:**
- `npm test`: exit 0, 183 of 183 passed across the 12 node unit-test files (A100). They ran on byte-identical
  desktop sources.
- TypeScript typecheck, Vite build and the Rust release build: compiled by the C95 installer build on `1755aa52`.
- Rust unit tests:
  - C99: 72 passed, 0 failed, 2 ignored, on the local Rust 1.98.0.
  - C105: the private toolchain pinned to the CI version 1.98.1 passed its check.
  - C106, on that pinned 1.98.1 toolchain: 72 passed, 0 failed, 2 ignored.

**Android:**
- The Gradle linkcore vector tests are reused; the linkcore tree is unchanged.
- The debug APK was accepted statically (A89). It has not been run on a phone.

**Installer:**
- An unsigned review candidate from `1755aa52`, accepted on static evidence (A98).
- An unsigned review candidate from `31fc2ad` (C102 receipt; installer sha256 `5def1112…`), accepted on static
  evidence (A102).
- An unsigned review candidate from `31fc2ad`, built with the pinned Rust 1.98.1 (C106; installer sha256
  `96647219…`, 318,601,298 bytes):
  - It reuses the unchanged C102 backend.
  - It was accepted on static evidence (A107): the payload equals the C102 backend (3096 files), and the install
    manifest, prerequisite pins, UI assets and Apache notices are unchanged.
- Product code is unchanged since then, so no rebuild is necessary after doc-only changes.
- This is build proof only, not installation or quality proof.

## Not verified (explicit limits)

- **No broad-gate rerun:** the gate has not been rerun since A100; the fixes were checked by focused nodes only.
- **Excluded tests:** 4 of the 13 originally excluded Python files (native installers and the registry), the 6
  deselected functions above, and the live-device nodes. The 2 ignored Rust tests did not run.
- **Hosted CI:** not run.
- **Deferred:** human and live tests on the PC, phone, GPU, microphone, devices and user archives.
- **Provisioning:**
  - Runtime model assets are provisioned separately and are not bundled.
  - In the registry: the 3 SigLIP 2 files are verified against publisher hashes, the 2 wake assets are present locally and unverified (C109),
    and the 2 voice assets are unverified.
  - C100: the original wake model was not found in the authoritative sources or the targeted local locations. No
    generic replacement was substituted. The owner later supplied the pair (C109); identity and detection quality remain unproven.
- **External:**
  - no genuine Authenticode signing, reviewed trusted root or same-artifact signing receipt. (The owner's update
    PUBLIC key is already admitted in `config/update_trusted_keys.json`, keyId `b0566b68…`, PR #68; that is a separate
    trust purpose and proves no private-key availability, signed update or release clearance.)
  - the relay is not deployed;
  - no real-phone LAN/WAN acceptance;
  - no clean-Windows installation or launch;
  - public visibility, signing submissions and relay deployment each need explicit owner authorisation.

## This document

The latest update is a docs-only commit on top of the composed `2d92e8b`. The tests and builds above ran on:
- `27c424a`: the Python broad gate;
- A101, C101 and C99: their own focused follow-up checks;
- `1755aa52`: the desktop and installer builds.

A docs-only change does not invalidate them.
