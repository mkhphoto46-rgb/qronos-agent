# Qronos software-phase requirements and evidence map

Last updated: 2026-10-08. Source base: `8c7c1cea50300a330701bba79fa3df061f3c33c7`.
Owner: independent review worker. This is an audit map, not a release claim.

The user deferred live PC and phone acceptance until after the current
software phase. Software completion therefore means: the capability is
implemented in the product source, connected to the path a user reaches,
and covered by automated tests using isolated fixtures. It never means
production or live readiness. Those still need the deferred acceptance
and the external gates below.

## Status keys

| Key | Meaning |
|---|---|
| **V** | Implemented, wired into the product path and verified by automated tests. |
| **P** | Partly implemented: some code is tested, but a required part is absent or not wired. |
| **M** | Missing software. Writing code can close it. |
| **G** | External gate: an owner, licence, signing, billing, infrastructure or asset decision. Code cannot close it. |
| **D** | Live PC/phone acceptance, explicitly deferred by the user. |

A gate (**G**) or a deferral (**D**) is never evidence of completion. Moving
an item to a document does not change its key.

## Source and verification baseline

- `8c7c1ce` is PR #55's head, the desktop third-party notices. Its parent
  chain contains `7e12e2b`, PR #54's head (backend notices). PR #54's base
  is `6d6d47d`. Both PRs are open and unmerged.
- `7e12e2b` has the same tree as the reviewed backend `bfadee6` plus a
  2-line `.gitattributes`. `8c7c1ce` contains the reviewed desktop
  `4842d45`, rebased onto `7e12e2b`, plus one extra 3-line metadata-text fix.
- Checks at `8c7` (Root logs, hashes recorded in
  `work/notice-final-integration-evidence.json`):
  - full Python suite: 3967 passed, 10 skipped
  - focused tests: 273 passed, 4 skipped
  - desktop toolchain tests: 125 passed, 1 skipped
  - frozen backend at `7e12`: settings/permissions/Library/memory smoke and
    the private-pipe transport test (1 passed)
- No NSIS installer has been built from `7e12` or `8c7`.
- The newest installer (`7584bb5`, unsigned) is historical.
- GitHub CI is blocked by billing before any job runs (`runner_id` 0,
  `step_count` 0).
- No Linux or Python 3.14 result exists for the current head.

## Requirement map

### A. Packaging, installer and release

| # | Requirement (source) | Key | Evidence / gap | Owner now |
|---|---|---|---|---|
| A1 | Standalone frozen backend with a closed service set (packaging doc) | V | `build_support/runtime_entry.py` `SERVICES`; frozen smoke at `7e12` | — |
| A2 | Bounded desktop↔service IPC (IPC doc) | V | Suite + private-pipe test | — |
| A3 | Backend third-party notices (PR #54) | P | pip packages and PortAudio are covered. Interpreter-bundled native components (OpenSSL, SQLite, Tcl/Tk, zlib, XZ, Expat, mpdecimal, HACL\*, libb2, mimalloc) have no matched full texts. Integration evidence marks them `namedInLicenseText:false` | runtime worker |
| A4 | Desktop npm/Rust notices (PR #55) | P | 5 npm + 254 Rust packages, 485 files, offline dry run. Extraction from a fresh NSIS installer is not verified | installer worker |
| A5 | Offline install when WebView2 is absent (offline-first rule) | P | Installer `ecfaeaa`/`89d047b` (pending integration): `webviewInstallMode` skip plus an NSIS hook that embeds a pinned, hash- and signature-verified x64 Evergreen Standalone Installer (154.0.4258.62). The hook compiles in a probe script; no full current-head NSIS build exists yet. Clean-machine install: D | installer worker |
| A6 | Fresh installer from reviewed current source, with provenance | M | None since `7584bb5` | installer worker, then Root |
| A7 | Safe upgrade/uninstall that preserves user data | M | Behaviour is not specified or tested. The storage-lock protocol change requires closing older instances | installer worker |
| A8 | Authenticated update and rollback | M + G | No update channel in the source. A signing key is an owner gate | installer worker / owner |
| A9 | Code signing, public release | G | Owner | owner |
| A10 | Microsoft runtime (VC++/WebView2) redistribution rights | G | Never assumed | owner |
| A11 | Pinned packaging interpreter | M | The packaging doc and README say Python 3.14, but the verified bundle was built with 3.13.13. `build_runtime.py` records the version and does not enforce it | runtime/installer, Root |
| A12 | Current-head CI on Windows and Linux | G | Billing restriction | owner |
| A13 | Clean-machine install/uninstall | D | — | deferred |

### B. Models and resources

| # | Requirement | Key | Evidence / gap |
|---|---|---|---|
| B1 | Exact configured models: Fast `hf.co/unsloth/gemma-3n-E4B-it-GGUF:Q5_K_M`, Heavy `gpt-oss:20b`, Vision `qwen3-vl:4b-instruct`, with their generation/context/keep-alive values | V | `config/brains.json` sha256 `519757ac…a59a08` at `8c7`. Any worker diff to this file is a review failure |
| B2 | Fresh resource checks, sustained load signal, safe queue, hard floor, VRAM capacity deferral | V | Suite; delivery status |
| B3 | Disk-pressure and battery/power signals in the resource sentinel (architecture) | M | No battery/power signal in `core/` (the only match is an intent word). Storage guards exist for disk *space*, not as a load signal |
| B4 | Cancellation of running and queued inference (Stop/Pause/shutdown) | V | Suite; the cancellation doc |
| B5 | Sustained latency, warm Heavy, VRAM coexistence | D | Single synthetic runs only |
| B6 | Routing: deterministic → Fast → Heavy handoff, with a fresh resource re-check before generation; Vision describes and Heavy reasons | V | `runtime_bridge._select_production_task_type`, orchestrator, vision worker; suite |
| B7 | Read-only setup diagnostics for missing models/assets (no load, no download) | V | `core/runtime_preflight.py` (`doctor` service); frozen smoke lists missing assets |
| B8 | Error surfacing: "outcome unknown" after a possibly mutating IPC call, storage-failure and busy-lock results, no automatic retry | V | IPC doc; suite |

### C. Voice

| # | Requirement | Key | Evidence / gap |
|---|---|---|---|
| C0 | Voice pipeline wired end to end in the runtime: wake → VAD → STT → normaliser → intent gate → router → brain → chunked TTS playback, with interruption/echo barrier | V | `runtime_bridge` wake-capture preparation; `test_voice_pipeline`, `test_runtime_bridge*`, voice continuity/echo tests. Live: D |
| C1 | Wake-word adapter, trigger state machine, nothing runs before wake | V | `core/openwakeword_engine.py`, `voice_trigger.py`, suite |
| C2 | Custom Qronos wake head `qronos.onnx` + `.data` | G | Absent; the source must come from the user. The wake word cannot work without it |
| C3 | Local STT (whisper.cpp, `fa`) | V | Code + suite. Quality: D |
| C4 | Local Persian TTS (Chatterbox/CrispASR) | V | Code + suite. Quality is not accepted: synthetic 5/6 cases had errors, 14/48 words wrong at `7584` |
| C5 | Human Persian voice evaluation | D/G | Needs an approved human reference |
| C6 | Chatterbox weight licence (CC BY-NC origin) before redistribution | G | Weights are not shipped |

### D. Computer control and app workflows (roadmap Stage 4–5)

| # | Requirement | Key | Evidence / gap | Owner now |
|---|---|---|---|---|
| D1 | App/folder/file control with ambiguity stops, recoverable delete | V | Suite; live: D | — |
| D2 | Typed action contract, scoped single-use approvals, audit, honest undo | V | Approval ledger, action audit, undo journal; suite | — |
| D3 | Fresh window/process/screenshot binding; coordinates expire on change; postconditions | M | Roadmap Stage 4B; `computer_control_implementation_status.md` item 3 | workflows worker |
| D4 | Native Paint drawing | M | Deliberately disabled: `_default_gui_action` raises "grounding" (`tests/test_offline_network_policy.py`). Undo exists only for a verified drawing | workflows worker |
| D5 | Telegram: exact contact → newest incoming attachment → one verified download | P | `core/telegram_workflow.py` (`12b4536`) and intents (`d5b2ddf`) exist with fake-UI tests. Not imported by the runtime, approval ledger or desktop, so no user can reach it. Journal defects W1–W6 are open | workflows worker |
| D6 | Print one exact file → one verified spool job, bounded non-duplicating recovery | P | `core/print_workflow.py` (`15a6234`) exists with fake-spooler tests; not wired. The native submitter waits up to 120 s without cancellation (P1) | workflows worker |
| D7 | Visible, permission-gated, audited browser workflow (the only public-internet route) | M | User-supplied browser source not in repo; legacy HTTP transport refuses (`core/web_providers.py`); `core/web_worker.py` is unregistered; saved links offer Copy only | workflows worker (report missing source to Root) |
| D8 | Elevated Action Broker / supervisor split (architecture) | M | Absent; no worker assigned | unassigned |
| D9 | Deterministic Security Monitor (reject/throttle/contain/safe-shutdown) | M | Absent. Required before Layer 2 remote access is enabled for users | unassigned (link-adjacent) |
| D10 | Layered runtime code-prohibition with measured red-team | P | `core/intent_gate.py` code markers; no measured red-team | unassigned |
| D11 | Activity/approval/error/undo history screen | P | Permission audit view exists; no general activity/undo history | unassigned |
| D12 | Live app/Paint/Telegram/printer acceptance | D | — | deferred |

### E. Device Link and phone

| # | Requirement | Key | Evidence / gap | Owner now |
|---|---|---|---|---|
| E1 | Layer 1 PC modules: TLS 1.3 PSK, pairing, revocation, capability ceilings, audit | V (modules) | `core/link_*`; suite; Windows self-check recorded in the Device Link doc §4.10 | — |
| E2 | Layer 1 reachable from the product | M | `runtime_entry.SERVICES` has no link service; `runtime_bridge` never starts `LinkServer`; desktop has no pairing/device/revocation UI. Device Link doc §1 excludes the pairing screen | link worker → Root allocation for shared surfaces |
| E3 | Layer 2 outbound-only relay, end-to-end through an untrusted relay, per-device remote opt-in with expiry, reduced capability profile | M | Proposed only (Device Link doc §5) | link worker |
| E4 | Native phone app (Android first: `TRANSPORT_WIFI` socket binding), Persian UI, safe key storage, never claims a VPN interface | M | No mobile source | link worker |
| E5 | Relay deployment, operator, cost; reachability from Iranian networks (§5.6) | G/D | Infrastructure decision and field measurement | owner |
| E6 | Phone signing/distribution | G | — | owner |
| E7 | Live phone acceptance with a VPN | D | — | deferred |
| E8 | Android build toolchain | G | No JDK, Gradle, Kotlin compiler or Android SDK on this machine (only `adb`); no global SDK installs are allowed. Source can be written; compilation is unproven until a toolchain is provided | owner |
| E9 | iOS app | G | Needs macOS/Xcode. Apple documentation does not establish TLS 1.3 external-PSK support | owner |
| E10 | Phone TLS 1.3 external PSK | P/G | Link worker's primary-source evidence: the Android platform stack offers PSK suites only up to TLS 1.2, so a bundled TLS library (Bouncy Castle) is planned. Its licence and notices need review before shipping | link worker / owner |

### H. Device Hub (broader device scope)

| # | Requirement | Key | Evidence / gap |
|---|---|---|---|
| H1 | Device Hub for printers, 3D printers and other local/LAN devices, with explicit per-device capability limits, permission, audit and live acceptance per workflow (project context; delivery status) | P | Printer and 3D chunks (docs/device_hub.md): Windows-installed local and LAN printers, driver capabilities, queue, own-job (user + this computer) pause/resume/cancel and exact-file print; 3D printers proven by their driver's PrintCapabilities with one exact .3mf handed to the Windows 3D print path (handoff only, never a printed-part claim); gated, approved with the typed secret and audited; Persian drawer. WIA 2.0 scanners: one gated, typed-secret scan through Windows' own dialog into one new PNG/JPEG. Proven local USB hardware cameras (Media Foundation, VIDCAP_HW_SOURCE): one gated, typed-secret still frame into one new PNG/JPEG, camera turned on only for the capture, no video/audio; a camera that has not closed blocks new captures. WIA 1.0 still cameras, virtual/network/phone/non-USB cameras, other local devices and live acceptance on real hardware remain REQUIRED |
| H2 | Local driver/spooler only; no vendor cloud or smart-device service | V (rule) | The printer and 3D chunks use only the Windows spooler, Print Ticket API and drivers (no socket/HTTP/vendor API, process or shell in core/device_hub.py or core/print3d_native.py); re-verify for each new device class |
| H3 | Wake-from-sleep via hardware capability or secure Wake-on-LAN, not a universal voice-wake claim | P | Hardware wake timer on THIS PC (docs/device_hub.md, core/power_wake.py): read-only capability (firmware RTC/Modern Standby, the power plan's Allow wake timers for AC/DC), gated single-use typed-secret approval, Windows-accepted claim only, Pause/Stop/close cancel; Persian drawer. No voice wake from sleep, no Wake-on-LAN (network scope). Live acceptance on real hardware remains REQUIRED |

### F. Cross-cutting invariants

| # | Requirement | Key | Evidence / gap |
|---|---|---|---|
| F1 | No undeclared direct-network path in product modules | V (pending integration) | R1 repaired in `52b5303`: per-module exact family grants, aliases, dynamic imports, asyncio/native/COM/subprocess/script probes |
| F2 | No cloud inference, telemetry, silent downloads | V | Model-download refusal and loopback tests; offline policy tests |
| F3 | Device Link is the only WAN exception; no direct Core web content | V (today) | Will need re-verification when E3/D7 land |
| F4 | Product UI copy stays Persian | V | Desktop source |

## Stale or conflicting guidance (do not act on these)

The current configuration (B1) and the Tauri 2 + NSIS desktop stack are
authoritative. The texts below predate them. Root should correct or annotate
them; this report does not edit shared docs.

1. `README.md`/`README.en.md`:
   - claim "Prototype", "~20%", "1,146 tests" and "no installable release"
   - say TTS "does not exist" and the desktop is "not connected to the backend"
   - mark web research ✅, though it is disabled in production
   - name `qwen3:4b-instruct`/`qwen3:14b` as the brains and Python 3.14
2. `docs/qronos_product_architecture.md`:
   - recommends PySide6/QML with PyInstaller + Inno Setup; the actual stack
     is Tauri 2 and NSIS
   - lists Qwen3 4B/14B as the brains
   - sizes the design for an 8 GB GPU
3. `docs/qronos_project_context.md`:
   - says `qronos.onnx` exists locally and loads; the current delivery
     status and the frozen doctor report it absent
   - lists Qwen3 brains and old branches
4. `docs/qronos_voice_output.md` says "read-only code analysis is
   permitted". The architecture doc and README forbid analysing code. A
   user decision is needed; do not change policy silently.
5. `docs/standalone-runtime-packaging.md` says build with Python 3.14, but
   the verified bundle is 3.13.13 (see A11).
6. `docs/QRONOS_DEVICE_LINK.md` §4.10 gives 1131 suite tests (historical),
   and §5.6 says to measure the relay before building it. The user's current
   instruction authorises building Layer 2 software without deployment. The
   measurement is still owed before users depend on it (E5).

## Independent review findings (current base)

**R1 — the offline network-import guard misses real egress paths. Repaired by this worker in `52b5303`; pending integration.**
- Location: `tests/test_offline_network_policy.py`,
  `test_product_network_imports_require_an_explicit_reviewed_exception`.
  `risky_roots` covers `requests`, `socket`, `aiohttp`, `httpx`,
  `websocket(s)` and `urllib`/`urllib.request`/`http.client`.
- Trigger: a new, undeclared module in `core/` that uses any of `urllib3`,
  `asyncio.open_connection`, `ssl`, `ftplib`, `smtplib`,
  `importlib.import_module('requests')`, or `subprocess` with
  `curl`/`Invoke-WebRequest`.
- Reproduced by `.cache/review/repro_network_guard.py`: 8 of 10 probes pass
  the guard. The `requests` control is caught, and so is
  `from http import client`. `webbrowser` is caught separately by
  `tests/test_library_link_open.py`.
- Impact: the guard the offline rule requires would not fail if a worker
  adds a relay client or browser helper outside the reviewed exception list.
  The Layer 2 and browser work now in progress make this concrete.
- Repair direction (Root to allocate, a shared test):
  - add `urllib3`, `ssl`, `asyncio` network calls, `ftplib`, `smtplib`,
    `imaplib`, `poplib`, `xmlrpc`, `http.server` and `socketserver` to the
    inventory
  - flag `importlib.import_module`/`__import__` with network module names
  - flag `subprocess` argument vectors that name `curl`, `wget`,
    `bitsadmin`, `certutil`, or PowerShell web cmdlets
  - keep per-module reviewed exceptions

**R2 — the production CSP allows the Vite dev origin (low). Fixed by installer `8b00f95` (`devCsp`), verified against locked tauri-codegen 2.6.3; pending integration.**
- Location: `desktop/src-tauri/tauri.conf.json`, `connect-src` includes
  `ws://localhost:1420` and `http://localhost:1420`.
- Impact: in a production webview, any local process listening on port
  1420 is an allowed connect target. Exploiting it also needs injected
  script, which `script-src 'self'` limits.
- Repair: use a dev-only CSP override. The installer worker owns this file.

## Worker review log

Commits are reviewed as they appear; exact reproducers and requested changes
are in `work/opus-five-review-status.json`.

- **Independent baseline at `8c7`:** 3968 passed, 9 skipped, run alongside the
  four active workers. The skip differences from Root's run are
  environmental: no `node_modules` in this worktree, and the Cargo registry
  is present here.
- **Workflows `61a6302`** (`core/app_workflow.py`): only safety rails are in
  place. Nothing in the product imports them yet. The single-shot side-effect
  journal is not safe across journal instances or processes:
  - Real two-process fixtures allowed two submissions for one content key
    and lost one record.
  - The shared fixed `.tmp` name in `core/json_store.py` corrupted the
    journal in 2 of 40 natural rounds.
  - Settled operation keys are evicted after 2048 entries, after which the
    same approved attempt can begin again.
  - Malformed entries raise untyped errors, or stop blocking (fail open).
  - These must be fixed before the Telegram or printer executors use the
    journal.

- **Installer `ecfaeaa`/`89d047b`/`8b00f95`:** independently verified the pinned
  WebView2 file (size, SHA-256 and `WinVerifyTrust`), the locked Tauri NSIS
  template (hook placement, the `NSISdl` downloader) and the `devCsp` fix.
  Open findings (status JSON I1–I4):
  - a failed post-build check leaves the unverified installer at Tauri's
    normal output path
  - the recorded signer thumbprint is never checked
  - the doc claims the build downloads nothing, but the locked bundler
    fetches the NSIS toolset when it is absent
  - the hook's `ExecWait` launch-failure path relies on a stale register
- **Workflows `15a6234`/`d5b2ddf`:** the printer and intent parsers are
  helper modules, not wired. `ShellPrintToSubmitter.submit` blocks up to
  120 s with no cancellation, and a job that finishes during that wait is
  reported as uncertain (P1). No W1–W6 repair has landed yet.

### Unassigned software, to be assigned after current chunks

These are missing software (M), not hardware tests:
- power/battery resource signals (B3)
- Elevated Action Broker / supervisor split (D8)
- deterministic Security Monitor (D9)
- runtime code-prohibition red-team (D10)
- general activity/undo history UI (D11)
- Device Hub (H1)
- Wake-on-LAN (H3)
