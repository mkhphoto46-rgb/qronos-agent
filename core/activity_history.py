"""
The desktop's activity history: what Qronos was asked to do, what was allowed, what happened, and what can
actually be undone.

Nothing new is collected. The history is read from the two records Qronos already keeps: the action audit trail
(:mod:`core.action_audit`) and the undo journal (:mod:`core.undo_journal`). Both are bounded on read and redacted
on the way out:

- only enum values (category, event, outcome, decision, reversal, state) and times leave this module;
- an action's target leaves as its last path component only (a file or folder name), never a full path;
- summaries, restoration data, tokens and secrets never leave it.

Undo is offered only where it is real. An entry is undoable from the history only when it is a file change made
in THIS session (the journal is not reloaded after a restart), it is the most recent change still standing (undo is
a stack), its reversal has an executor (moving a file back, removing a created copy or folder), and an exact check
passes right now. A copy is removed only while it is still byte-identical to its source (size and SHA-256), so a
copy the person edited is never deleted. Everything else is shown with the honest reason it cannot be undone here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Mapping

from core.append_only_log import AppendOnlyLog
from core.file_executor import bounded_sha256, file_identity
from core.path_safety import is_link_or_reparse_point, is_network_or_device_path
from core.undo_journal import EntryState, Reversal, UndoEntry

#: Bounds: how many audit records are read, and how many actions are returned.
MAX_AUDIT_RECORDS = 4000
MAX_ACTIONS = 200
MAX_NAME_CHARS = 80

#: The summary of the gate request a history undo makes (Qronos-written, matched, never shown).
UNDO_REQUEST_SUMMARY = "Undo one exact earlier file change."

AUDIT_EVENTS = ("decided", "approved", "denied_by_user", "completed", "undone")
OUTCOMES = ("succeeded", "failed", "refused", "awaiting_approval", "cancelled", "")
REVERSALS = tuple(reversal.value for reversal in Reversal)
#: Reasons an IRREVERSIBLE entry may carry (core.computer_executor.undo_unavailable_restoration).
IRREVERSIBLE_REASONS = ("automatic_undo_not_supported",)

UNDO_STATES = (
    "available",        # can be undone from the history now
    "done",             # already undone
    "failed",           # an undo was attempted and failed (terminal)
    "expired",          # too old, superseded or discarded
    "irreversible",     # Qronos cannot reverse it
    "nothing_to_undo",  # it changed nothing
    "not_latest",       # a later change still stands on top of it
    "paint_flow",       # a Paint drawing: undone through the Paint undo request only
    "earlier_session",  # recorded before this Qronos session; not undoable after a restart
    "changed_since",    # the files are no longer exactly as Qronos left them
    "unverifiable",     # recorded without the evidence that proves which object it changed
    "unsupported",      # no executor for this reversal
    "none",             # no undo record (not a change, or not journaled)
)

FILE_REVERSALS = (Reversal.MOVE_BACK, Reversal.DELETE_CREATED)


def _name(target: object) -> str:
    """The last path component of a target, bounded. Never a full path."""
    text = " ".join(str(target or "").split())
    if not text:
        return ""
    name = PurePath(text.replace("\\", "/")).name or text
    if "://" in text:
        name = text.split("://", 1)[0] + "://…"
    return name if len(name) <= MAX_NAME_CHARS else name[: MAX_NAME_CHARS - 1] + "…"


def _safe_local(path: Path) -> bool:
    """An absolute local path with no link or reparse point anywhere on it."""
    text = str(path)
    if not path.is_absolute() or is_network_or_device_path(text):
        return False
    probe = path
    while True:
        if os.path.lexists(probe) and is_link_or_reparse_point(probe):
            return False
        if probe.parent == probe:
            return True
        probe = probe.parent


def evidence_state(entry: UndoEntry) -> str:
    """Metadata only (no file content is read): "ok", "unverifiable" or "changed_since"."""
    restoration = entry.restoration
    recorded = str(restoration.get("identity", ""))
    if not recorded or (entry.reversal is Reversal.DELETE_CREATED and recorded.startswith("v1f:")
                        and not restoration.get("sha256")):
        return "unverifiable"  # an older record, or a copy too large to have kept its hash
    try:
        if entry.reversal is Reversal.MOVE_BACK:
            moved, original = Path(str(restoration["from"])), Path(str(restoration["to"]))
            if not (_safe_local(moved) and _safe_local(original)):
                return "changed_since"
            ok = (file_identity(moved) == recorded and not os.path.lexists(original)
                  and original.parent.is_dir())
            return "ok" if ok else "changed_since"
        created = Path(str(restoration["path"]))
        if not _safe_local(created) or file_identity(created) != recorded:
            return "changed_since"
        if recorded.startswith("v1d:"):
            with os.scandir(created) as listing:
                return "changed_since" if next(listing, None) is not None else "ok"  # empty folders only
        source = Path(entry.target)
        if source == created or not _safe_local(source) or not source.is_file() \
                or source.stat().st_size != created.stat().st_size:
            return "changed_since"  # never remove a copy whose source is gone or differs
        return "ok"
    except (KeyError, OSError, ValueError):
        return "changed_since"


def source_read_path(entry: UndoEntry) -> str | None:
    """The source file an undo must read (a created copy's source), or None when the undo reads no source."""
    if entry.reversal is Reversal.DELETE_CREATED and str(entry.restoration.get("identity", "")).startswith("v1f:"):
        return str(entry.target)
    return None


def content_check(entry: UndoEntry, *, source: bool = True) -> bool:
    """The content reads, only after the gates allowed them: a created copy still hashes to what Qronos wrote, AND
    (with ``source``) its source still holds those same original bytes, so removing the copy loses nothing (both
    bounded). ``source`` is False only before a confirmation the source read still requires; the answer reads it."""
    if entry.reversal is Reversal.DELETE_CREATED and str(entry.restoration.get("identity", "")).startswith("v1f:"):
        recorded = str(entry.restoration.get("sha256", ""))
        if not recorded or bounded_sha256(Path(str(entry.restoration["path"]))) != recorded:
            return False
        return not source or bounded_sha256(Path(str(entry.target))) == recorded
    return True


@dataclass(frozen=True)
class UndoView:
    state: str
    reversal: str = ""
    reason: str = ""


def undo_view(action_id: str, live: Mapping[str, UndoEntry], top_action: str | None,
              earlier: Mapping[str, Mapping[str, Any]]) -> UndoView:
    """The honest undo state of one action (see UNDO_STATES)."""
    entry = live.get(action_id)
    if entry is None:
        old = earlier.get(action_id)
        if old is None:
            return UndoView("none")
        reversal = str(old.get("reversal", ""))
        reversal = reversal if reversal in REVERSALS else ""
        if reversal in (Reversal.IRREVERSIBLE.value, Reversal.NOTHING_TO_UNDO.value):
            return UndoView("irreversible" if reversal == Reversal.IRREVERSIBLE.value else "nothing_to_undo", reversal)
        state = str(old.get("state", ""))
        if state == EntryState.UNDONE.value:
            return UndoView("done", reversal)
        return UndoView("earlier_session", reversal)
    reversal = entry.reversal.value
    if entry.state is EntryState.UNDONE:
        return UndoView("done", reversal)
    if entry.state is EntryState.FAILED:
        return UndoView("failed", reversal)
    if entry.state is EntryState.EXPIRED:
        return UndoView("expired", reversal)
    if entry.reversal is Reversal.IRREVERSIBLE:
        reason = str(entry.restoration.get("reason", ""))
        return UndoView("irreversible", reversal, reason if reason in IRREVERSIBLE_REASONS else "")
    if entry.reversal is Reversal.NOTHING_TO_UNDO:
        return UndoView("nothing_to_undo", reversal)
    if entry.reversal is Reversal.APP_UNDO_STEPS:
        return UndoView("paint_flow", reversal)
    if entry.reversal not in FILE_REVERSALS or entry.category != "create_or_edit_file":
        return UndoView("unsupported", reversal)
    if top_action != action_id:
        return UndoView("not_latest", reversal)
    state = evidence_state(entry)
    if state != "ok":
        return UndoView(state, reversal)
    return UndoView("available", reversal)


def _audit_records(audit: Any) -> list[Mapping[str, Any]]:
    """The bridge's audit trail: its live file when it has one, else this process's records."""
    if audit is None:
        return []
    path = getattr(audit, "path", None)
    if path is not None:
        return list(AppendOnlyLog(path).read_records())[-MAX_AUDIT_RECORDS:]
    return [record.to_json() for record in audit.records()][-MAX_AUDIT_RECORDS:]


def _earlier_journal(journal: Any) -> dict[str, Mapping[str, Any]]:
    path = getattr(journal, "path", None)
    if path is None:
        return {}
    latest: dict[str, Mapping[str, Any]] = {}
    for record in AppendOnlyLog(path).read_records()[-MAX_AUDIT_RECORDS:]:
        action_id = record.get("actionId")
        if isinstance(action_id, str) and action_id:
            latest[action_id] = record
    return latest


def live_journal(journal: Any) -> tuple[dict[str, UndoEntry], str | None]:
    """This session's journal entries by action, and the action a plain undo would reverse (stack top)."""
    if journal is None:
        return {}, None
    entries = {entry.action_id: entry for entry in journal.entries()}
    top = journal.next_undoable()
    return entries, (top.action_id if top is not None else None)


def snapshot(audit: Any, journal: Any) -> dict[str, Any]:
    """The bounded, redacted history the desktop shows (newest action first)."""
    live, top = live_journal(journal)
    earlier = _earlier_journal(journal)
    actions: dict[str, dict[str, Any]] = {}
    for record in _audit_records(audit):
        action_id = record.get("actionId")
        event = record.get("event")
        if not isinstance(action_id, str) or not action_id or event not in AUDIT_EVENTS:
            continue
        try:
            at_ms = int(float(record.get("at", 0)) * 1000)
        except (TypeError, ValueError):
            continue
        outcome = record.get("outcome") if record.get("outcome") in OUTCOMES else ""
        decision = str(record.get("decision", ""))
        item = actions.get(action_id)
        if item is None:
            item = actions[action_id] = {
                "actionId": action_id[:64],
                "category": str(record.get("category", ""))[:48],
                "kind": "undo_request" if record.get("summary") == UNDO_REQUEST_SUMMARY else "action",
                "name": _name(record.get("target", "")),
                "firstAt": at_ms,
                "lastAt": at_ms,
                "events": [],
                "outcome": "",
                "decision": "",
            }
        item["lastAt"] = max(item["lastAt"], at_ms)
        if len(item["events"]) < 12:
            item["events"].append({"event": event, "at": at_ms})
        if event in ("completed", "undone") or (event == "decided" and outcome == "refused"):
            item["outcome"] = "undone" if event == "undone" else outcome
        if event == "decided" and decision.isidentifier():
            item["decision"] = decision[:40]
        if event == "denied_by_user":
            item["outcome"] = "denied_by_user"
    ordered = sorted(actions.values(), key=lambda item: item["lastAt"], reverse=True)
    out = []
    for item in ordered[:MAX_ACTIONS]:
        view = undo_view(item["actionId"], live, top, earlier) if item["kind"] == "action" else UndoView("none")
        out.append({**item, "undo": {"state": view.state, "reversal": view.reversal, "reason": view.reason}})
    return {"schemaVersion": 1, "actions": out, "truncated": len(ordered) > MAX_ACTIONS}


def undoable_entry(journal: Any, action_id: str, *, content: bool = False,
                   source: bool = True) -> tuple[UndoEntry | None, str]:
    """The live entry for ``action_id`` when it can be undone now, else (None, the honest state).

    Metadata only, unless ``content`` (allowed only after the gates permitted it): then a created copy's bytes are
    hashed against the evidence recorded when Qronos made it, and (``source``) its source's bytes too.
    """
    live, top = live_journal(journal)
    earlier = _earlier_journal(journal)
    view = undo_view(str(action_id or ""), live, top, earlier)
    if view.state != "available":
        return None, view.state
    entry = live[str(action_id)]
    if content and not content_check(entry, source=source):
        return None, "changed_since"
    return entry, "available"


def target_for(entry: UndoEntry) -> str:
    """The exact path an undo of ``entry`` writes (for the gate request; never sent to the desktop)."""
    restoration = entry.restoration
    if entry.reversal is Reversal.MOVE_BACK:
        return str(restoration.get("to", ""))
    return str(restoration.get("path", ""))

