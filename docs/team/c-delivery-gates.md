# Delivery gates (Track C, C0)

Where delivery stands, and what each open gate needs from whom. It records existing evidence only; it adds no
policy. Writer: Track C. Status as of 2026-10-16.

## Toolchain

| Use | Tool | Note |
|---|---|---|
| Test fixtures | project `.venv`, Python 3.13.13 | pytest, the registry checker, synthetic fixtures |
| Packaging | private CPython 3.14.8 venv, PyInstaller 6.22.3 | frozen runtime; freeze unchanged since r73 |
| Desktop | Tauri CLI 2.11.4, Node v24.19.0, NSIS from the Tauri cache | WebView2 and VC++ prerequisites pinned in `build_support/*_prerequisite.json` |
| Rust | pinned **1.98.1** in a private `RUSTUP_HOME` (`E:/qronos-toolchains/rustup-1.98.1`, C105); global `stable` is still 1.98.0 | equals the CI pin (`.github/workflows/tests.yml`). Installed with `rustup toolchain install 1.98.1 --profile minimal --no-self-update` from the official channel; the global rustup settings, toolchains, cargo proxies, default and PATH are unchanged |
| Android | Gradle 9.8.1 wrapper, SDK unpacked from the pinned archives (`mobile/android/buildcheck/sdk-packages.json`) | owner approved the SDK licence; built through `sdk_setup.py` |

## Artifacts and sources

These are distinct. Do not mix their identities.

- **Previous accepted desktop installer**, built from product source `1755aa52`. It is UNSIGNED and a review
  candidate only.
  - File: `qronos-desktop_0.1.0_x64-setup.exe`, 318,605,942 B, sha256 `425cdf19…52c2`.
  - Location: `E:/qronos-release-ac-2026-10-16/delivery-1755aa52`, with its provenance, install manifest and
    notices.
  - It is accurate for `1755aa52` only.
- **Asset registry**, `build_support/runtime_assets.json` (`27c424a`): a source-side review record, read only by the
  `build_support.runtime_assets` checker. It is NOT bundled in the runtime or the installer (C102: no `build_support`
  module in the frozen runtime, no copy in the payload, and the install manifest is unchanged). Correcting it is not a
  runtime feature, and both installers contain the same product code.
- **Current personal installer**, a fresh backend and desktop build of source `2835768f` with rustc/cargo 1.98.1
  (C118), including the voice-failure fix (A111/A112). It is UNSIGNED. A static peer accepted it (A113): the packed
  `core.runtime_bridge` equals its source, the payload manifest equals the backend's, and the voice notice is in the
  desktop assets. Installation and human acceptance remain deferred.
  - File: `E:/qronos-delivery/personal-2026-10-17-2835768f/qronos-desktop_0.1.0_x64-setup.exe`, 318,611,167 B,
    sha256 `ac441a6d…e85b`.
- **Previous pinned desktop installer**, built from source `31fc2adc` with rustc/cargo 1.98.1 (C106). It is UNSIGNED
  and a review candidate only. Its independent static desktop-delta review passed (A107); Root confirmed both CLI exits were zero. Installation and human acceptance remain deferred.
  - File: `qronos-desktop_0.1.0_x64-setup.exe`, 318,601,298 B, sha256 `96647219…1ef9`.
  - Location: `E:/qronos-release-pinned-2026-10-16/delivery-31fc2adc`, with its provenance (recording 1.98.1),
    install manifest and notices.
  - It reuses the unchanged C102 backend (3096 files, byte-equal); only the desktop executable was recompiled.
- **Earlier candidates** (kept): `31fc2adc` built with 1.98.0 (C102, `5def1112…7de1`) and `1755aa52` built with
  1.98.0 (C95, `425cdf19…52c2`).
- **Android APK**, the standard debug build from source `53dd571`.
  - File: `qronos-remote-0.1.0-debug.apk`, 4,022,723 B, sha256 `d0f006b4…9af8`.
  - It is signed with the Android **debug** key only, which is not release signing.
  - Receipt: `E:/qronos-delivery/ac-2026-10-16/android-build-receipt.json`.

## Runtime assets

Assets are never bundled. An installed Qronos reads them from `QRONOS_ASSET_ROOT` when it is set, and otherwise from
`<LocalAppData>/Qronos/RuntimeAssets` (`core/config.py`). A development checkout ignores that variable and uses the
repository root. Check an asset root read-only with `python -m build_support.runtime_assets --root <RuntimeAssets>`.

- **Installed asset root (C103)**: `E:/qronos-runtime-assets`. It was new, and the user-level `QRONOS_ASSET_ROOT`
  points to it; the variable was previously unset, and the Machine value is unset. It takes effect for later
  installed launches; no app was started.
  - It holds the registry's 30 verified files, each copied by exact path and checked against its pinned size and
    SHA-256. The sources were the r35 runtime-assets zip (whisper, ffmpeg), the SigLIP 2 cache on E, and the
    development root for 6 model files. Nothing was downloaded.
  - Local voice files (C108): the owner's existing `crispasr.exe`, `t3-fa-q4_k.gguf` and the unregistered companions
    beside the exe (`crispasr.dll`, `ggml.dll`, `ggml-base.dll`, `ggml-cpu.dll`, `ggml-vulkan.dll`, `whisper.dll`,
    `LICENSE`, `THIRD_PARTY_NOTICES.txt`) were copied from the development root at the same paths. Their local hashes are
    a work record only, not provenance; nothing was executed.
  - Recovered wake head (C109): the owner-supplied `qronos.onnx` and `.onnx.data` were copied to `models/wake_word` here
    and in the development root (git-ignored there).
  - The read-only check reports all 34 registry files present: 30 verified, 4 unverified (2 voice, 2 wake), 0 mismatch,
    0 missing. `allVerified` is false, so the release gate stays closed.
  - Local provisioning does not clear the licence, provenance or release gates. The first voice run and hardware
    (Vulkan GPU) acceptance are deferred, and redistribution of the voice files stays closed.

- **SigLIP 2 image search** (3 files): **verified** in the registry (`27c424a`), equal to the publisher's Hugging Face
  identity at revision `ba1f3b08`.
  - Also installed in the asset root above. The earlier supplemental copy `E:/qronos-delivery/ac-2026-10-16/RuntimeAssets`
    holds only these three files.
  - Image-retrieval quality is not measured.
- **Custom wake head** `models/wake_word/qronos.onnx` and `.data`: **unverified, present locally** (C109).
  - The owner supplied them from a recovered backup. Earlier targeted searches (C96, C100) had not found them, and no
    release, export or published identity exists; they stay out of Git.
  - Checked read-only, without running them: ONNX opset 18, input `[1, 16, 96]` float, output `[1, 1]` sigmoid, with
    the external tensors exactly covering `qronos.onnx.data`. The metadata shape/type matches the openWakeWord classifier contract and the
    recorded settings (threshold 0.660167, 1280-sample frames, 16 kHz, external data required).
  - That the files are the original export, and how well they detect, is not independently proven. The recorded
    quality is `experimental_not_production_ready`; live wake acceptance is deferred, and production wake is not
    claimed.
- **Voice (speech synthesis)** `crispasr.exe` and `t3-fa-q4_k.gguf`: **unverified**. No published identity matches
  the local files, so the registry holds no hash for them. They are installed locally (C108) and block only the
  release gate (`--require-verified`), not local use. The companion DLLs and notices are not in the registry, and the
  t3-fa licence is not established.
- **Registry totals**: 30 verified, 4 unverified, 0 missing.

## Gates

Qronos is private and for personal use, and the repository stays PRIVATE (owner decision). Setup steps are in
`docs/personal-use.md`.

### Personal use: a software candidate is available

The source shows no signing or hosted-CI gate on installation or startup. An actual install and startup have not
been tested yet (see Deferred).

| Item | State | Owner | Next concrete action |
|---|---|---|---|
| Installer and startup | the unsigned installer installs per user and the desktop finds its bundled backend by manifest only; no signature, `allVerified` or CI result is consulted at startup (C112) | owner | install and launch (see Deferred) |
| Runtime assets | all 34 present in `E:/qronos-runtime-assets` (30 verified, 4 unverified); `QRONOS_ASSET_ROOT` set for the user | — | none |
| CI | the hosted PR #101 jobs never started: billing failed / spending limit (annotations 116733368374 and 116733367898), not a code failure. CI runs `cargo check --locked --all-targets` with 1.98.1 but no `cargo test` | — | none for development: checks run locally (`docs/local-validation.md`); locally, the same check and the Rust unit tests pass on 1.98.1 (see below) |
| Rust 1.98.1 | closed locally: installed privately (C105); `cargo +1.98.1 check --locked --offline --all-targets` exit 0 at `31fc2adc` (C105); unit tests 72 passed, 0 failed, 2 ignored (C106) | — | none locally; a hosted run still needs CI billing |
| Update key (separate purpose) | the owner's Ed25519 update PUBLIC key is admitted in `config/update_trusted_keys.json` (keyId `b0566b68ca6ee2887a8db62b3aff00e8`, PR #68, matching the 2026-10-09 public receipt). This does not prove the private key is available or usable, that any update is signed, or release clearance | owner | none for the public key; a signed update needs the owner's key and a reviewed release |

### Deferred live acceptance (owner, on the real PC and phone)

| Item | State | Owner | Next concrete action |
|---|---|---|---|
| Human and device tests | deferred: real-phone LAN/WAN, clean-Windows install/upgrade/uninstall, voice quality and resource use | owner (human tester) | schedule them on the exact artifacts above |
| Custom wake head | present, unverified (owner-supplied recovered backup) | owner | licence/ownership review; live wake acceptance on the target hardware |
| Voice assets identity | unverified | owner / Track C | provide a publisher identity (size and SHA-256) or accept them as unverified |

### Optional: public distribution and access from outside the home network

Needed only to give Qronos to others or to reach it over the internet; not needed for personal use. A public release and signing policy would be a separate owner decision; this table gives no legal clearance either way. Updates still require the owner's signature.

| Item | State | Owner | Next concrete action |
|---|---|---|---|
| Asset licence and provenance | owner review is required for the openWakeWord feature models (CC BY-NC-SA 4.0 question), the SigLIP 2 ONNX conversion (base Apache-2.0; the conversion states no licence) and the Persian Chatterbox fine-tune | owner | decide each licence; the registry text then changes as a reviewed change |
| Genuine signing (Authenticode) | none: no certificate, provider, approved trust root or same-artifact signing receipt. Free-signing preparation is in `docs/signing` | owner | choose a provider and certificate; then one signing receipt on the exact artifact |
| Public distribution | not planned: the repository stays PRIVATE. Making it public or releasing to others would be a new owner decision; no worker may do either. Root-authorised PR pushes are separate | owner | none while private |
| Device Link relay | no operator, host, TLS certificate or deployment authority; WAN is default off | owner | name the operator and host; then a deployment review |

## Test evidence

- **Broad Python gate (A100, at `27c424a`)**: 6594 passed, 26 failed, 111 skipped, 1577 subtests passed.
  - All 26 failures are covered by focused checks: A101 (5 nodes, for 4 failures) and C101 (the other 22 nodes).
    Both exited 0.
  - The broad gate has **not** been rerun after these repairs.
- **Fixture PDF dependencies**: the project `.venv` now holds the `requirements.txt` pins fpdf2 2.8.9,
  fonttools 4.66.1, uharfbuzz 0.56.3 and defusedxml 0.7.1 (C101, C102).
- **Node**: `npm test` passed 183 of 183 (A100).
- **Rust**: 72 passed, 0 failed, 2 ignored on the pinned 1.98.1 (C106) and earlier on 1.98.0 (C99, below);
  `cargo +1.98.1 check --locked --offline --all-targets` exit 0 (C105).

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

Run once on 2026-10-16 (C99), at source `6fda950`, in `desktop/src-tauri`, with a 900 s timeout:

```
cargo +stable test --locked --offline --release --lib --target-dir E:/qronos-release-r45/t -- --test-threads=1
```

- Environment:
  - `QRONOS_TEST_PYTHON` was the project `.venv` Python 3.13.13; there was no PATH fallback.
  - `QRONOS_RUN_LIVE_DEVICE_TESTS=0`.
  - `QRONOS_TEST_BUNDLED_RUNTIME_DIRECTORY` was unset.
  - TEMP, TMP, APPDATA, LOCALAPPDATA and the profile pointed to a fresh isolated workspace.
  - `RUSTUP_HOME` and `CARGO_HOME` were explicit, and `RUSTUP_AUTO_INSTALL=0`.
- Result: **exit 0. 72 passed, 0 failed, 2 ignored** (74 in total).
  - The 2 ignored are the `#[ignore]` no-read helper, which runs only as its own test's child, and the frozen-runtime
    test.
  - The release build reused the C95 cache: 1 m 16 s compile, then 16.6 s of tests.
  - Nothing was downloaded, and the fixture temp folder was empty afterwards.
- Limit: this run is local `stable` = rustc/cargo **1.98.0** evidence (historical).

C106 repeated the same command at `31fc2adc` with the pinned toolchain (`cargo.exe +1.98.1 test ...`, private
`RUSTUP_HOME`, the same isolation): **exit 0, 72 passed, 0 failed, 2 ignored**. The Rust sources are unchanged since
C99. This is local evidence, not a hosted CI run.
