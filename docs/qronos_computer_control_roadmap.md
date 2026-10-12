# Qronos Computer Control Roadmap

## Product goal

Qronos must turn Persian or English voice/text requests into verified local
computer actions. It is not limited to one hard-coded demo. A basic house in
Paint is an acceptance test for bounded mouse control; Telegram retrieval and
printing are multi-step acceptance tests for grounded workflows.

## Safety architecture

```text
Voice/Text
  -> deterministic intent and entity extraction
  -> typed action plan
  -> permission gate
  -> app/window binding
  -> Vision/OCR grounding
  -> bounded mouse/keyboard or app adapter
  -> observe and verify postcondition
  -> audit, retry, or recover
```

The planner may propose actions but cannot execute shell commands, arbitrary
programs, or unbounded coordinates. Every executable step uses a typed schema.
Coordinates must be inside the verified target window and expire when the
window, screenshot, scale, or layout changes.

Qronos is offline-first. All intelligence and automation run locally. Public
internet access is browser-only: visible browser control behind permission and
audit. Core modules may not call cloud APIs or fetch public URLs directly.
Loopback IPC with Qronos-owned local sidecars remains allowed.

Paired mobile remote control is an explicit additional network exception.
Device Link must support internet access beyond the LAN while keeping all AI
and computer execution on the PC. Preserve authenticated pairing, revocation,
capability checks and encrypted transport across an untrusted relay. Existing
Layer 1 is implemented; Layer 2 and the phone application still require work.
No relay has been deployed. See QRONOS_DEVICE_LINK.md for the existing design.

Current implementation status: app/location fixes have automated coverage.
Paint intent parsing and permission categorization are prototypes. Native drawing
is disabled pending verified canvas/tool grounding, cancellation, focus-change
guards and visual postcondition checks. Telegram and printing are roadmap items,
not implemented capabilities. Legacy direct web transport is disabled pending
integration of the user-supplied browser workflow.

## Stage 4A - reliable computer and file control

- Open, close, focus, minimize, and maximize installed applications.
- Open redirected known folders and explicitly named local drives.
- Resolve exact/partial files and explicit folder names with ambiguity stops.
- Create, copy, move, rename, and recoverably delete files/folders through the
  existing permission-gated File Control executor.
- Preserve the single-sidecar mutex and Job Object containment.

## Stage 4B - grounded mouse and keyboard primitives

- Bind every action to a verified process and window handle.
- Capture only the target window after permission.
- Use Qronos Vision/OCR to return candidate UI elements and confidence.
- Add typed primitives: click target, double-click target, scroll a bounded
  amount, type bounded text, send an allowlisted hotkey, and wait for a state.
- Re-capture after each state-changing step and verify the expected result.
- Abort on low confidence, multiple matches, focus changes, unexpected dialogs,
  or coordinates outside the target window.

The first acceptance test is: open Paint, focus it, draw a simple house, and
verify that non-empty strokes appeared in the canvas.

## Stage 4C - app adapters and workflows

Generic Vision actions are the fallback, not the preferred integration. Each
high-value application gets a small adapter with app-specific invariants.

### Telegram: download the latest file from one named contact

1. Open/focus Telegram and verify its identity.
2. Search the exact requested contact.
3. Stop on duplicate/ambiguous contact names.
4. Open the verified chat.
5. Inspect messages from newest to oldest.
6. Select the first message that is a file attachment from that contact.
7. Download only that attachment.
8. Verify completion and report the exact local path.

The workflow must never download every attachment, choose a fuzzy contact
silently, or send a message unless that is a separate approved request.

### Print one exact file

1. Resolve one exact file; stop on ambiguity.
2. Check printer availability, default/selected printer, queue, and status.
3. Show the exact file, printer, page/range, copies, and color mode for approval.
4. Submit one print job and verify it appears in the queue.
5. Apply bounded recovery for offline queue, paused job, spooler retry, paper or
   consumable warnings, then report unresolved physical intervention.

Software cannot press a physical power button. Qronos may use only behavior
already exposed through the local Windows driver/spooler (including a printer's
normal local wake behavior). It must not use a vendor cloud API or online smart
device service.

## Stage 5 - multi-step agent

- Plan workflows from the typed action catalog.
- Maintain a short-lived state machine with explicit pre/postconditions.
- Use Vision for grounding and verification, not unrestricted execution.
- Limit retries and never turn an error-recovery loop into repeated destructive
  actions, repeated downloads, duplicate messages, or duplicate print jobs.

## Definition of done

- Unit, integration, security, and regression tests pass from an isolated test
  environment.
- Live validation passes in increasing-risk order.
- Every external side effect is visible, permission-gated, audited, and checked.
- A failed or ambiguous step stops safely and explains what user input is needed.
