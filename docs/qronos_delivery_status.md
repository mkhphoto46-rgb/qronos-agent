# Qronos Delivery Status

Last updated: 2026-10-07

The desktop now has an unsigned Windows review installer with a bundled backend.
The August prototype estimate no longer describes this checkout. A percentage
would mix implemented components with untested live workflows, so this report
uses concrete verification and release gates instead.

## Implemented and verified in development

- A standalone backend serves the desktop, settings, permissions, Library,
  memory and audio services without discovering a development Python install
  or source checkout. Complete runtime manifests are verified before packaging.
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
  Stop/Pause/shutdown revoke pending approvals; late replies cannot authorize
  another action. Paint undo is limited to a verified reversible Qronos action.
- Library mutation approvals bind the exact operation and artifacts. The local
  approval secret is stored as a salted scrypt verifier, with persisted attempt
  limits. Changed or replaced artifacts are refused; edits require registration
  again. Large legacy records without enough identity evidence fail closed.
- Storage roots are validated against protected program/state paths. Invalid
  defaults can relocate outside the source checkout, with diagnostics, without
  moving existing user files or silently overwriting preferences.
- Local sidecar communication has bounded loopback, ownership and lifecycle
  checks. Model downloads and cloud forwarding are refused. Public web access
  requires the approved visible browser workflow; saved links offer Copy Link
  while a reviewed browser launcher is unavailable.

## Evidence and its limits

The combined Windows development suite passed **3,698 tests**, with **7 skips**
and **1,197 passing subtests**. Six frontend behavior tests, strict TypeScript,
the Vite production build, ten Rust library tests and locked native checks
passed. The production npm dependency audit reported no vulnerabilities.

The actual frozen backend passed isolated settings, permissions, Library,
memory, setup diagnostics and bridge startup/ping/shutdown checks outside the
source checkout. The NSIS installer built locally. GitHub built the preceding
combined desktop on Windows and Linux; the final source checks are being run
after correcting the CI-discovered file identity and fixture issues.

These checks use temporary state and fake native action boundaries. They do
not establish microphone quality, wake-word accuracy, model performance, native
GUI behavior or acceptance on a clean Windows machine. The installer is
unsigned and has not been approved for public release. Source review chunks
remain open as pull requests rather than merged release code.

## Remaining desktop release gates

1. Provision and verify the configured Fast and Vision models and all wake-word
   assets. STT/TTS asset presence must be followed by live testing. The persistent
   recognizer server is optional and requires its separate executable.
2. Measure cold/warm model latency, memory/VRAM pressure, interruption, recovery
   and Fast/Heavy switching on the target hardware. Test resource protection
   while the user is working in other applications.
3. Test voice, screen approval, focus changes, native actions, Paint drawing and
   undo, recoverable file operations, queue recovery, pause and shutdown with
   real applications and a disposable test profile.
4. Install and uninstall on a clean Windows machine without development Python
   or Node. Check user-data persistence, errors for missing assets and upgrade
   behavior. Review redistribution licenses for every shipped binary and model.
5. Review and merge the focused pull requests in dependency order after their
   final checks pass. Establish installer signing and an authenticated update
   and rollback process before public distribution.

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
`qronos_computer_control_roadmap.md` and `QRONOS_DEVICE_LINK.md` for the relevant
contracts and scope.

