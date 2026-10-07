import { useEffect, useRef } from "react";
import type { KeyboardEvent } from "react";

import {
  screenApprovalKeyIntent,
  screenPromptTitle,
} from "../screenApproval";
import type { ScreenApprovalView } from "../screenApproval";

import "./ScreenApprovalDialog.css";

type Props = {
  view: ScreenApprovalView | null;
  onAnswer: (approvalId: string, approved: boolean) => void;
};

/**
 * App-native, nonblocking Screen/Paint approval.
 *
 * Replaces window.confirm so runtime events (including revocation) keep
 * being processed while a prompt is shown. Focus lands on the safe choice
 * (decline); Escape declines; both buttons are disabled while a decision is
 * in flight. An unconfirmed decision is reported honestly and never
 * replayed. Each prompt identity renders a fresh dialog instance.
 */
export default function ScreenApprovalDialog({ view, onAnswer }: Props) {
  if (view === null) {
    return null;
  }
  return (
    <ScreenApprovalPanel
      key={view.prompt.approvalId}
      view={view}
      onAnswer={onAnswer}
    />
  );
}

function ScreenApprovalPanel({
  view,
  onAnswer,
}: {
  view: ScreenApprovalView;
  onAnswer: Props["onAnswer"];
}) {
  const declineRef = useRef<HTMLButtonElement | null>(null);
  const { prompt, text, submitting, unconfirmed } = view;
  const titleId = `screen-approval-title-${prompt.approvalId}`;
  const bodyId = `screen-approval-body-${prompt.approvalId}`;

  useEffect(() => {
    declineRef.current?.focus();
  }, []);

  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (screenApprovalKeyIntent(event.key, submitting) === "decline") {
      // Handled here only; other keys (e.g. a Stop hotkey) still reach the
      // app's window-level handlers while the dialog is open.
      event.preventDefault();
      event.stopPropagation();
      onAnswer(prompt.approvalId, false);
      return;
    }
    if (event.key === "Tab") {
      // Keep focus inside the modal: cycle between its enabled buttons.
      const buttons = Array.from(
        event.currentTarget.querySelectorAll<HTMLButtonElement>(
          "button:not([disabled])",
        ),
      );
      if (buttons.length === 0) {
        event.preventDefault();
        return;
      }
      const first = buttons[0];
      const last = buttons[buttons.length - 1];
      const current = document.activeElement;
      if (event.shiftKey && (current === first || !buttons.includes(current as HTMLButtonElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (current === last || !buttons.includes(current as HTMLButtonElement))) {
        event.preventDefault();
        first.focus();
      }
    }
  };

  return (
    <div className="file-approval-backdrop" role="presentation">
      <section
        className="file-approval-dialog screen-approval-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        aria-busy={submitting}
        data-approval-id={prompt.approvalId}
        onKeyDown={onKeyDown}
      >
        <div className="file-approval-kicker">
          SCREEN CONTROL · PERMISSION REQUIRED
        </div>

        <h2 id={titleId}>{screenPromptTitle(prompt.kind)}</h2>

        <p id={bodyId} className="file-approval-copy screen-approval-copy">
          {text}
        </p>

        {unconfirmed && (
          <div className="file-approval-warning" role="status">
            رسیدن پاسخ تأیید نشد؛ ممکن است به Qronos رسیده و وضعیت تغییر
            کرده باشد. پیش از هر کار دیگری وضعیت را بررسی کن یا Stop بزن.
          </div>
        )}

        <div className="file-approval-actions">
          <button
            ref={declineRef}
            type="button"
            className="file-approval-button file-approval-deny"
            disabled={submitting}
            onClick={() => onAnswer(prompt.approvalId, false)}
          >
            رد کردن
          </button>

          <button
            type="button"
            className="file-approval-button file-approval-allow"
            disabled={submitting}
            onClick={() => onAnswer(prompt.approvalId, true)}
          >
            {submitting ? "در حال ارسال..." : "اجازه می‌دهم"}
          </button>
        </div>
      </section>
    </div>
  );
}
