/**
 * The activity history: what Qronos was asked to do, what was allowed, what happened, and what can actually be
 * undone (core/activity_history.py via the runtime). Persian, right to left.
 *
 * Only an entry the runtime marks "available" gets an undo button; every other entry says why it cannot be undone
 * here. An undo needs this panel's single-use confirmation (and the approval secret when the permission policy
 * asks for it). The secret lives only in the open confirmation and is cleared on submit, cancel and close; closing
 * the panel declines an open confirmation.
 */

import { useEffect, useState } from "react";

import {
  categoryText,
  OUTCOME_TEXT,
  reversalText,
  UNDO_TEXT,
} from "../historyModel";
import type { HistoryAction } from "../historyModel";
import {
  answerUndo,
  closeUndoConversation,
  dismissHistoryNotice,
  refreshHistory,
  requestUndo,
  useHistoryStore,
} from "../historyStore";

import "./HistoryDrawer.css";

type HistoryDrawerProps = { hidden: boolean };

const TIME = new Intl.DateTimeFormat("fa-IR", { dateStyle: "short", timeStyle: "short" });

function approvalText(action: HistoryAction): string {
  if (action.events.some((event) => event.event === "denied_by_user")) return "شما رد کردید";
  if (action.events.some((event) => event.event === "approved")) return "با تأیید شما";
  if (action.decision === "allow") return "بدون نیاز به تأیید";
  if (action.decision === "deny") return "طبق سیاست مجوزها ممنوع";
  return "";
}

function HistoryDrawer({ hidden }: HistoryDrawerProps) {
  const [open, setOpen] = useState(false);
  const [secret, setSecret] = useState("");
  const history = useHistoryStore();
  const { prompt } = history;

  useEffect(() => {
    if (open) {
      void refreshHistory();
    } else {
      setSecret("");
      void closeUndoConversation();
    }
  }, [open]);

  useEffect(() => {
    if (!prompt) setSecret(""); // a stale, answered or replaced prompt never keeps a secret
  }, [prompt]);

  useEffect(() => {
    if (hidden && open) setOpen(false);
  }, [hidden, open]);

  if (hidden) return null;

  const answer = (approved: boolean) => {
    const typed = secret;
    setSecret("");
    void answerUndo(approved, typed);
  };

  return (
    <div className="history-drawer" dir="rtl">
      <button
        type="button"
        className={"history-drawer-toggle" + (open ? " history-drawer-toggle-open" : "")}
        aria-expanded={open}
        aria-controls="qronos-activity-history"
        onClick={() => setOpen((value) => !value)}
      >
        تاریخچهٔ کارها
      </button>

      {open && (
        <section id="qronos-activity-history" className="history-drawer-panel" role="dialog"
          aria-label="تاریخچهٔ کارها">
          <header className="history-drawer-header">
            <strong>تاریخچهٔ کارها</strong>
            <span>کارهایی که Qronos انجام داد یا رد کرد، و آنچه واقعاً قابل بازگرداندن است.</span>
          </header>

          {history.notice && (
            <p className="history-drawer-notice" role="status">
              {history.notice}
              <button type="button" onClick={dismissHistoryNotice} aria-label="بستن پیام">×</button>
            </p>
          )}

          {prompt && (
            <div className="history-drawer-confirm" role="alertdialog" aria-label="تأیید بازگرداندن">
              <p>{reversalText(prompt.reversal, prompt.name, prompt.readsSource)}</p>
              {prompt.needsSecret && (
                <label>
                  رمز تأیید
                  <input type="password" autoComplete="off" value={secret}
                    onChange={(event) => setSecret(event.target.value)} />
                </label>
              )}
              <div className="history-drawer-actions">
                <button type="button" onClick={() => answer(true)} disabled={prompt.needsSecret && !secret}>
                  بازگردان
                </button>
                <button type="button" onClick={() => answer(false)}>لغو</button>
              </div>
            </div>
          )}

          {!history.loaded && <p className="history-drawer-empty">در حال دریافت…</p>}
          {history.loaded && history.actions.length === 0 && (
            <p className="history-drawer-empty">هنوز کاری ثبت نشده است.</p>
          )}

          <ul className="history-drawer-list">
            {history.actions.map((action) => (
              <li key={action.actionId} className="history-drawer-item">
                <div className="history-drawer-line">
                  <strong>
                    {action.kind === "undo_request" ? "درخواست بازگرداندن" : categoryText(action.category)}
                  </strong>
                  {action.name && <span className="history-drawer-name" dir="auto">{action.name}</span>}
                  <time dateTime={new Date(action.lastAt).toISOString()}>{TIME.format(action.lastAt)}</time>
                </div>
                <div className="history-drawer-line">
                  <span className={"history-drawer-outcome history-drawer-outcome-" + (action.outcome || "unknown")}>
                    {OUTCOME_TEXT[action.outcome] ?? OUTCOME_TEXT[""]}
                  </span>
                  {approvalText(action) && <span>{approvalText(action)}</span>}
                </div>
                {action.undo.state !== "none" && (
                  <div className="history-drawer-line">
                    <span className="history-drawer-undo">{UNDO_TEXT[action.undo.state]}</span>
                    {action.undo.state === "available" && (
                      <button type="button" onClick={() => void requestUndo(action.actionId)}
                        disabled={Boolean(prompt) || history.requested !== null}>
                        بازگرداندن…
                      </button>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
          {history.truncated && <p className="history-drawer-empty">فقط ۲۰۰ کار اخیر نشان داده می‌شود.</p>}
        </section>
      )}
    </div>
  );
}

export default HistoryDrawer;
