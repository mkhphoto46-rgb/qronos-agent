# Computer-control verification — 2026-09-18

Active checkout: `H:\Project Qronos Agent`.

## Implemented in this pass

- The named-file resolver accepts a quoted absolute local document path.
- The location resolver accepts a quoted absolute local directory path.
- A quoted filename plus a quoted directory restricts lookup to that directory.
- An exact direct child of an explicitly named drive/known folder has precedence
  over recursive matches. The measured project-folder resolution was 4.19 ms;
  this is resolver time, not end-to-end voice or window-opening latency.
- Recursive discovery streams directory entries and checks a shared two-second
  search budget across roots. This is a cooperative budget; a blocked OS disk
  operation cannot be interrupted by this check.
- Photoshop Persian aliases include both فتوشاپ and فوتوشاپ.
- A matching App Paths executable and version-labelled Start Menu shortcut no
  longer create false ambiguity. Different executable installations remain
  ambiguous, even when their process basenames are identical.

## Example commands

- `پوشه "H:\Project Qronos Agent" را باز کن`
- `فایل "H:\Project Qronos Agent\README.md" را باز کن`
- `فایل "README" داخل پوشه "H:\Project Qronos Agent" را باز کن`
- `فوتوشاپ را باز کن`

The examples use existing permission/execution gates. Opening an executable as
a document remains prohibited; installed programs use the application catalog.

## Outstanding work / limits

- No new persistent whole-system filename index was implemented in this pass.
  Broad searches can still exhaust their budget and ask for a narrower scope.
- Installed-app discovery does not guarantee coverage of every portable app or
  every packaged app.
- Existing uncommitted input, window-binding, grounding and sketch modules were
  preserved. Their presence is not evidence of working end-to-end drawing.
- The production default GUI action still refuses drawing pending integration
  with canvas/tool grounding and verified execution. Photoshop drawing is not
  implemented by this pass.
- Native desktop UI inspection was unavailable in the active tool session.
  Live voice-to-window acceptance and mouse/keyboard takeover remain unverified.
- A running Python runtime must reload/restart to pick up source changes.

Do not describe this checkpoint as the final assistant or as bug-free.

## Validation

The full working-tree suite reported 2656 passed, 4 skipped, 1122 subtests,
and one failure: the gate-discipline inventory does not account for the existing
uncommitted `window_binding`, `windows_app_activation`, and
`windows_input_native` modules. This was not bypassed by blanket-whitelisting
the native input layer. Its production permission integration needs review.
