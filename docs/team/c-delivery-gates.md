# Delivery gates (Track C, C0)

Where delivery stands, and what each open gate needs from whom. It records existing evidence only; it adds no
policy. Writer: Track C. Status as of 2026-10-16.

## Toolchain

| Use | Tool | Note |
|---|---|---|
| Test fixtures | project `.venv`, Python 3.13.13 | pytest, the registry checker, synthetic fixtures |
| Packaging | private CPython 3.14.8 venv, PyInstaller 6.22.3 | frozen runtime; freeze unchanged since r73 |
| Desktop | Tauri CLI 2.11.4, Node v24.19.0, NSIS from the Tauri cache | WebView2 and VC++ prerequisites pinned in `build_support/*_prerequisite.json` |
| Rust | local rustc/cargo **1.98.0** | CI pins **1.98.1** (`.github/workflows/tests.yml`); 1.98.1 is not installed locally. The pin is not changed |
| Android | Gradle 9.8.1 wrapper, SDK unpacked from the pinned archives (`mobile/android/buildcheck/sdk-packages.json`) | owner approved the SDK licence; built through `sdk_setup.py` |

## Artifacts and sources

These are distinct. Do not mix their identities.

- **Desktop installer**, built from product source `1755aa52`. It is UNSIGNED and a review candidate only.
  - File: `qronos-desktop_0.1.0_x64-setup.exe`, 318,605,942 B, sha256 `425cdf19…52c2`.
  - Location: `E:/qronos-release-ac-2026-10-16/delivery-1755aa52`, with its provenance, install manifest and
    notices.
  - It is accurate for `1755aa52` only.
- **Asset registry source**, `27c424a` (on `91551fa`). It changes `build_support/runtime_assets.json`, which is a
  packaged input, so the `1755aa52` installer does NOT contain it. A matching build is a separate, later decision.
- **Android APK**, the standard debug build from source `53dd571`.
  - File: `qronos-remote-0.1.0-debug.apk`, 4,022,723 B, sha256 `d0f006b4…9af8`.
  - It is signed with the Android **debug** key only, which is not release signing.
  - Receipt: `E:/qronos-delivery/ac-2026-10-16/android-build-receipt.json`.

## Runtime assets

Assets are never bundled. They live under `<LocalAppData>/Qronos/RuntimeAssets` and are checked read-only with
`python -m build_support.runtime_assets --root <RuntimeAssets>`.

- **SigLIP 2 image search** (3 files): **verified** in the registry (`27c424a`), equal to the publisher's Hugging Face
  identity at revision `ba1f3b08`.
  - Staged copies: `E:/qronos-delivery/ac-2026-10-16/RuntimeAssets`. That folder is supplemental. It holds ONLY these
    three files, and the checker reports the other 31 registry entries as missing there.
  - Image-retrieval quality is not measured.
- **Custom wake head** `models/wake_word/qronos.onnx` and `.data`: **missing**.
  - They are not on this machine or E:, and were never in Git. The owner must supply them; without them the wake word
    cannot work.
- **Voice (speech synthesis)** `crispasr.exe` and `t3-fa-q4_k.gguf`: **unverified**. No published identity matches
  the local files, so the registry holds no hash for them.
- **Registry totals**: 30 verified, 2 unverified, 2 missing.

## Open gates

| Gate | State | Owner | Next concrete action |
|---|---|---|---|
| Asset licence and provenance | owner review is required for the openWakeWord feature models (CC BY-NC-SA 4.0 question), the SigLIP 2 ONNX conversion (base Apache-2.0; the conversion states no licence) and the Persian Chatterbox fine-tune | owner | decide each licence; the registry text then changes as a reviewed change |
| Custom wake head | missing | owner | supply both files and their origin; register them as `unverified` unless an authoritative identity exists |
| Voice assets identity | unverified | owner / Track C | provide a publisher identity (size and SHA-256) or accept them as unverified |
| Genuine signing | none: no certificate, provider, trust root, update key or same-artifact signing receipt. Free-signing preparation is in `docs/signing` | owner | choose a provider and certificate; then one signing receipt on the exact artifact |
| Publication | the GitHub repository is private; no worker has authority to publish | owner | decide visibility and release |
| CI | hosted Actions billing is unresolved; CI runs `cargo check --locked --all-targets` with 1.98.1 but no `cargo test` | owner (billing), Root | restore billing; Rust tests: see below |
| Rust 1.98.1 | not installed locally; local evidence is 1.98.0 | Root / owner | approve installing the pinned toolchain, or accept 1.98.0 evidence as local only |
| Device Link relay | no operator, host, TLS certificate or deployment authority; WAN is default off | owner | name the operator and host; then a deployment review |
| Human and device tests | deferred: real-phone LAN/WAN, clean-Windows install/upgrade/uninstall, voice quality and resource use | owner (human tester) | schedule them on the exact artifacts above |

## Rust tests (`desktop/src-tauri`, 74 `#[test]`)

Classified from source:

- **Pure, in process** (no child process):
  - `runtime::backend::tests`: temp-dir fake bundle;
  - `runtime::lifecycle::tests` except `reader`: fake launcher;
  - `runtime::runtime_status_tests`, `runtime_reader_tests`, `screen_decision_tests`, `pairing_route_tests`;
  - the in-process `runtime_input_tests`.
- **Controlled fixture children** (no real runtime, service, device, model or network):
  - `runtime::job::tests`, `runtime::lifecycle::tests::reader`, `runtime::stop_proof_tests` and
    `runtime::service_ipc::tests` spawn `tests/fixtures/lifecycle_child.py` or `service_ipc_child.py`. These scripts
    use only stdlib, temp-dir markers and their own sub-children.
  - The interpreter is `QRONOS_TEST_PYTHON`. Without it, whatever `python` is on PATH runs, so it must be set.
  - Two `runtime_input_tests` re-run the test binary itself (`current_exe`, an `#[ignore]` helper that sleeps).
  - One lifecycle test names a non-existent `.exe` and launches nothing.
- **Real runtime, never by default**: `service_ipc::tests::frozen_closed_services_use_private_transport_with_isolated_profile`
  is `#[ignore]` and needs `QRONOS_TEST_BUNDLED_RUNTIME_DIRECTORY`. Do not pass `--ignored` or `--include-ignored`.

Proposed, in `desktop/src-tauri`, in two steps. None has been run.

1. Pure only, no child process:

   ```
   cargo test --locked --offline --lib -- --skip runtime::job::tests --skip runtime::lifecycle::tests::reader
     --skip runtime::stop_proof_tests --skip runtime::service_ipc::tests
     --skip runtime::runtime_input_tests::an_owned_child_that_never_reads_cannot_hold_commands_or_its_stop
     --skip runtime::runtime_input_tests::stop_and_app_exit_are_bounded_by_an_owned_child_that_never_reads
   ```

2. Controlled fixtures. Set `QRONOS_TEST_PYTHON=<project .venv python>` first; never leave it unset. Then run:

   ```
   cargo test --locked --offline --lib -- runtime::job::tests runtime::lifecycle::tests::reader
     runtime::stop_proof_tests runtime::service_ipc::tests runtime::runtime_input_tests::an_owned_child
     runtime::runtime_input_tests::stop_and_app_exit --test-threads=1
   ```

Use `--offline` with the existing Cargo download cache. Pass the toolchain explicitly (`+1.98.0` locally); a CI-equal
run needs 1.98.1, which is not installed. The ignored frozen-runtime test stays ignored.
