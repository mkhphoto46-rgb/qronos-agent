# Activity history (desktop)

The desktop's "تاریخچهٔ کارها" panel shows what Qronos was asked to do, what the permission gate or the person
allowed, what happened (succeeded, failed, refused, denied), and what can actually be undone. It sits beside the
Smart Queue toggle on the home and conversation views.

## Where the data comes from

Nothing new is collected. `core/activity_history.py` reads the two records Qronos already keeps:

- the action audit trail (`core/action_audit.py`): decisions, approvals, denials, completions and undos; the live
  file only, at most 4,000 records, at most 200 actions returned, newest first;
- the undo journal (`core/undo_journal.py`): this session's entries, plus earlier sessions' entries from its file
  (shown, never undoable: the journal is not reloaded after a restart).

What leaves for the desktop is enum values (category, event, outcome, decision, reversal, undo state), times, and a
target's last path component (a file or folder name) only. Full paths, summaries, restoration data, tokens and
secrets never do.

## Transport

The desktop uses the registered `send_runtime_action` route and the bridge answers with runtime events; no Rust
change was needed.

- `qronos.history_request` (read only, allowed while paused) -> `history_snapshot`.
- `qronos.history_undo_request {actionId}` -> `history_undo_approval_required` or `history_undo_refused`.
- `qronos.history_undo_decision {actionToken, approved, secret?}` -> one of:
  - `history_undo_result` (undone, failed, changed_since, refused, ...);
  - `history_undo_declined`;
  - `history_undo_stale`;
  - `history_undo_secret_rejected`.
  Every result is followed by a fresh `history_snapshot`.

## What can be undone, and when

Only a file change made by Qronos in this session, at the top of the undo stack, through the file executor:

- a move or rename moved back;
- a copy removed;
- a created empty folder removed.

Ownership evidence:
- Each such change records, when it happens, the exact object it left behind: volume and file id, and for a file
  its size and modification time; for a copy also its SHA-256, up to 256 MiB.
- An undo refuses when the object there is not that one: replaced (even with identical bytes), edited, or reached
  through a link or junction.
- It also refuses when a copy's source is gone or differs: before a created copy is removed, the source must still
  hold the original bytes (its bounded SHA-256 equals the hash recorded when Qronos made the copy), so removing the
  copy loses nothing. A same-size edit of the source refuses and keeps both files.
- Older entries recorded without this evidence are shown as "unverifiable" and never offered.

Checks and order:
- Opening the list reads metadata only, and only for the one stack-top candidate.
- An undo request first asks the permission gate (CREATE_OR_EDIT_FILE for the exact reversal path), and for a copy
  also the Files gate for reading its source (READ_NON_SENSITIVE, recorded under the undo request). Only if neither
  refuses are the copy and its source hashed.
- The answer re-checks, in order: metadata, the gate, the typed secret when the policy requires it, the source-read
  gate, then the content (copy and source) again.
- The typed secret is verified through the Library's borrowed check (core.library_executor.WorkflowSecretCheck); the
  bridge never opens the secret store itself. Only then does the executor undo it; the audit records APPROVED and UNDONE.
- Every undo needs the panel's single-use confirmation (an `ApprovalKind.HISTORY_UNDO` identity). It is revoked by
  Pause, Stop and shutdown; a late, unknown, used or wrong-kind answer does nothing.
- A wrong secret spends the confirmation (a new request is needed). The secret is never stored or echoed, and the
  panel clears it on submit, cancel, a stale prompt and closing.

Shown with the honest reason, never offered:
- irreversible actions (computer actions);
- changes that changed nothing;
- Paint drawings (undone only through the Paint undo request);
- earlier-session entries;
- anything that is not the latest change;
- changed or unverifiable entries;
- reversals without an executor (restore_content, stop_started).

## Limits

- Rotated-away audit history is not shown.
- A check-to-act gap remains between the final content check and the executor's move or delete. The executor
  re-checks identity itself immediately before acting.
- Verified with fixtures only (tmp folders, fake gate verdicts); no live desktop run.
