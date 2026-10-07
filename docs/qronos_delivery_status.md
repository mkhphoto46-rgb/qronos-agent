# Qronos Delivery Status

Last updated: 2026-10-07

The desktop is an unsigned Windows review candidate with a bundled backend.
The August prototype estimate no longer describes this checkout. A percentage
would mix implemented components with untested live workflows, so this report
uses concrete verification and release gates instead.

## Implemented and verified in development

- A standalone backend serves the desktop, settings, permissions, Library,
  memory and audio services without discovering a development Python install
  or source checkout. Complete runtime manifests are verified before packaging.
- Desktop service calls use bounded asynchronous IPC: a closed set of bundled
  services, input/output size limits and a finite deadline for every operation,
  with cleanup that kills only the exact spawned child. There is no automatic
  retry; a failure after a possibly mutating call starts reports the outcome as
  unknown. Runtime status checks never wait behind a blocked writer: a busy
  status slot is reported as unavailable, never as a stopped runtime. See
  `bounded-desktop-service-ipc.md`.
- Local setup diagnostics report missing configured models and voice assets.
  They inspect local files and model names without starting, downloading or
  loading models. Model weights and user data are provisioned separately.
- Conversation work runs through a durable Smart Queue. Restart recovery,
  pause/resume, cancellation, committed-answer deduplication and storage failure
  handling have automated coverage.
- Stop, Pause and shutdown cancel foreground and queued inference through their
  own transport tokens. Native actions check cancellation at step boundaries.
  Closing a model request does not promise immediate GPU release.
- Screen, Paint and file approvals have scoped, expiring, single-use identities.
  Screen and Paint prompts use a revocable in-app dialog instead of a blocking
  browser confirm: Stop, Pause, revocation or the runtime exiting dismisses the
  prompt without answering, and late replies cannot authorize another action.
  Keyboard focus stays inside the dialog while a decision is being sent. If
  delivery of a decision cannot be confirmed, the dialog says so and nothing is
  replayed automatically. Paint undo is limited to a verified reversible Qronos
  action.
- Library mutation approvals bind the exact operation and artifacts. The local
  approval secret is stored as a salted scrypt verifier, with persisted attempt
  limits. Changed or replaced artifacts are refused; edits require registration
  again. Large legacy records without enough identity evidence fail closed.
- Storage roots are validated against protected program/state paths. Invalid
  defaults can relocate outside the source checkout, with diagnostics, without
  moving existing user files or silently overwriting preferences. Location
  changes are serialised by an operating-system lock on a persistent lock file:
  a waiting process gives up after a bounded time with a "busy" result, an
  owner that exits or is killed releases the lock, and an active owner is never
  displaced. Real concurrent Windows processes exercise this behaviour.
- A new GPU model load is deferred when it would not fit the video memory that
  is free now. The check uses the model's real footprint where known (its
  observed resident size, otherwise its installed size), never less than the
  configured estimate. If that capacity cannot be confirmed, a cold GPU load is
  deferred with an explanation: no request is sent and nothing is unloaded.
  Already-resident (warm) reuse, systems without GPU telemetry and legacy
  integrations without a size reader keep their previous behaviour. A model
  that can never fit the card entirely keeps the earlier partial-offload check,
  which does not protect other applications' video memory. Model tags,
  quantization, context, generation and keep-alive settings are unchanged.
- Local sidecar communication has bounded loopback, ownership and lifecycle
  checks. Model downloads and cloud forwarding are refused. Public web access
  requires the approved visible browser workflow; saved links offer Copy Link
  while a reviewed browser launcher is unavailable.

## Local models and voice assets on the development machine

The configured Fast and Vision models were installed with their exact
specified tags into the existing local model store; the specified Heavy model
was already present and reused. No model tag or setting was changed and
existing models were preserved. Synthetic, bounded checks on the 16 GB
development GPU gave correct answers with these latencies:

| Role | Cold | Warm or reload |
| --- | ---: | ---: |
| Fast | 29.34 s (a later cold load: 5.32 s) | 0.33 s warm |
| Vision | 12.58 s | 2.6 s reload (the model unloads after each request) |
| Heavy | 39.69 s (first cold request) | not measured |

Heavy occupied 12.74 GB of video memory, total GPU use reached 15,392 of
16,303 MiB and another application's video memory shrank meanwhile. That
observation led to the capacity deferral above. A second Heavy load was avoided,
so warm Heavy latency was not measured. These are single synthetic runs, not
target-hardware or long-running resource acceptance.

For wake-word development, the three openWakeWord feature models
(`melspectrogram.onnx`, `embedding_model.onnx` and `silero_vad.onnx`) were
provisioned from the official openWakeWord v0.5.1 release that the pinned
`openwakeword` 0.6.0 package declares. They passed interface and shape checks
with synthetic audio only. The custom Qronos wake head
(`models/wake_word/qronos.onnx` and its external data) is still absent, so the
wake word cannot work yet. Installed builds need their assets placed
separately; the installer contains no model weights, speech binaries or
private data.

## Evidence and its limits

The combined Windows development suite for this checkpoint's source passed
**3,749 tests**, with **7 skips** and **1,198 passing subtests**. 42 frontend
behaviour tests, strict TypeScript and the Vite production build passed. 25
Rust tests passed; one frozen-bundle transport test is ignored by default and
runs only against an explicitly supplied trusted bundle. Debug and release
checks of all Rust targets and 33 native, gate and approval contract tests
(46 subtests) passed.

The previous review build's actual frozen backend passed isolated settings,
permissions, Library, memory, setup diagnostics and bridge
startup/ping/shutdown checks outside the source checkout, and its NSIS
installer built locally. This checkpoint changes that source, so it needs a
fresh frozen runtime, smoke test and installer build; that result is recorded
separately and is not implied by this document. GitHub built the preceding
combined desktop on Windows and Linux. After correcting the CI-discovered
file identity and fixture issues, final checks were blocked before runner
allocation by the GitHub account's billing/spending restriction. Final Linux
source validation remains outstanding.

These checks use temporary state and fake native action boundaries. They do
not establish microphone quality, wake-word accuracy, model performance, native
GUI behavior or acceptance on a clean Windows machine. Browser layout previews
are not native GUI acceptance. The installer is unsigned and has not been
approved for public release. Source review chunks remain open as pull
requests rather than merged release code.

## Remaining desktop release gates

1. Supply the custom wake head and its external data, then test wake accuracy
   and Persian speech live. STT/TTS asset presence must be followed by live
   testing. The persistent recognizer server is optional and requires its
   separate executable. Place the required assets for installed builds.
2. Measure cold/warm model latency (including warm Heavy), memory/VRAM pressure,
   interruption, recovery and Fast/Heavy switching on the target hardware. Test
   resource protection while the user is working in other applications,
   including partial offload, growth of other applications' memory use and
   other same-user processes.
3. Test voice, screen approval, focus changes, native actions, Paint drawing and
   undo, recoverable file operations, queue recovery, pause and shutdown with
   real applications and a disposable test profile.
4. Install and uninstall on a clean Windows machine without development Python
   or Node. Check user-data persistence, errors for missing assets and upgrade
   behavior. Review redistribution licenses for every shipped binary and model.
5. Resolve the GitHub billing/spending restriction and run current-head Windows
   and Linux checks. Review and merge the focused pull requests in dependency
   order after those checks pass. Establish installer signing and an
   authenticated update and rollback process before public distribution.

## Broader product work still open

- Integrate and validate the user's permission-gated visible browser workflow.
  Browser research and saved-link navigation are currently unavailable.
- Implement and validate the Telegram exact-contact/latest-file and printer
  workflows described in the computer-control roadmap.
- Complete Device Link's internet transport, relay operations and native phone
  application. The relay has not been deployed; local pairing components do
  not establish completed remote mobile access.
- Complete later Device Hub and additional application adapters, with explicit
  capability limits and live acceptance for each supported workflow.

These items are separate milestones, not capabilities demonstrated by the
desktop review installer. See `standalone-runtime-packaging.md`,
`bounded-desktop-service-ipc.md`, `qronos_computer_control_roadmap.md` and
`QRONOS_DEVICE_LINK.md` for the relevant contracts and scope.
