import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import {
  needsSecret,
  outcomeTitle,
  workflowDetailRows,
  workflowTitle,
} from "../appWorkflowApproval";
import type { WorkflowApprovalView, WorkflowPrompt, WorkflowResult } from "../appWorkflowApproval";

import "./AppWorkflowDialog.css";

type Props = {
  view: WorkflowApprovalView;
  onAnswer: (actionToken: string, approved: boolean, secret?: string) => void;
  onDismissResult: () => void;
};

/**
 * App-native approval for Telegram, print and browser workflow steps.
 *
 * Shows exactly what the runtime will do (the details come from the prompt,
 * in a fixed order), answers only with the prompt's single-use identity,
 * starts on the safe choice, and declines on Escape. A print step needs the
 * approval secret, typed here and sent once; it is never stored. A result
 * from a page in the browser is shown as untrusted page content.
 */
export default function AppWorkflowDialog({ view, onAnswer, onDismissResult }: Props) {
  if (view.prompt !== null) {
    return (
      <WorkflowPromptPanel
        key={view.prompt.actionToken}
        prompt={view.prompt}
        submitting={view.submitting}
        unconfirmed={view.unconfirmed}
        secretRejected={view.secretRejected}
        onAnswer={onAnswer}
      />
    );
  }
  if (view.result !== null) {
    return <WorkflowResultPanel result={view.result} onDismiss={onDismissResult} />;
  }
  return null;
}

function WorkflowPromptPanel({
  prompt,
  submitting,
  unconfirmed,
  secretRejected,
  onAnswer,
}: {
  prompt: WorkflowPrompt;
  submitting: boolean;
  unconfirmed: boolean;
  secretRejected: boolean;
  onAnswer: Props["onAnswer"];
}) {
  const declineRef = useRef<HTMLButtonElement | null>(null);
  const sectionRef = useRef<HTMLElement | null>(null);
  const wasSubmitting = useRef(false);
  const [secret, setSecret] = useState("");
  const secretNeeded = needsSecret(prompt);
  const titleId = `workflow-approval-title-${prompt.actionToken}`;
  const bodyId = `workflow-approval-body-${prompt.actionToken}`;

  useEffect(() => {
    declineRef.current?.focus();
  }, []);

  useLayoutEffect(() => {
    if (submitting) {
      sectionRef.current?.focus();
    } else if (wasSubmitting.current) {
      declineRef.current?.focus();
    }
    wasSubmitting.current = submitting;
  }, [submitting]);

  useEffect(() => {
    if (secretRejected) setSecret("");
  }, [secretRejected]);

  const approve = () => {
    const typed = secret;
    // Clear the field before sending: the secret is used once, never kept.
    setSecret("");
    onAnswer(prompt.actionToken, true, secretNeeded ? typed : undefined);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape" && !submitting) {
      event.preventDefault();
      event.stopPropagation();
      onAnswer(prompt.actionToken, false);
      return;
    }
    if (event.key === "Tab") {
      const focusable = Array.from(
        event.currentTarget.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled])"),
      );
      if (focusable.length === 0) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const current = document.activeElement;
      if (event.shiftKey && (current === first || !focusable.includes(current as HTMLElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (current === last || !focusable.includes(current as HTMLElement))) {
        event.preventDefault();
        first.focus();
      }
    }
  };

  const rows = workflowDetailRows(prompt);
  const canApprove = !submitting && (!secretNeeded || secret.trim().length > 0);

  return (
    <div className="file-approval-backdrop" role="presentation">
      <section
        ref={sectionRef}
        tabIndex={-1}
        className="file-approval-dialog workflow-approval-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        aria-busy={submitting}
        data-action-token={prompt.actionToken}
        onKeyDown={onKeyDown}
      >
        <div className="file-approval-kicker">APP WORKFLOW · PERMISSION REQUIRED</div>
        <h2 id={titleId}>{workflowTitle(prompt.phase)}</h2>
        <p id={bodyId} className="file-approval-copy">
          {prompt.message}
        </p>

        {rows.length > 0 && (
          <dl className="workflow-approval-details">
            {rows.map((row) => (
              <div key={row.label} className="workflow-approval-row">
                <dt>{row.label}</dt>
                <dd dir="auto">{row.value}</dd>
              </div>
            ))}
          </dl>
        )}

        {secretNeeded && (
          <label className="workflow-approval-secret">
            رمز تأیید
            <input
              type="password"
              autoComplete="off"
              value={secret}
              disabled={submitting}
              onChange={(event) => setSecret(event.target.value)}
            />
          </label>
        )}

        {secretRejected && (
          <div className="file-approval-warning" role="status">
            رمز تأیید درست نبود یا موقتاً قفل است؛ چیزی انجام نشد.
          </div>
        )}

        {unconfirmed && (
          <div className="file-approval-warning" role="status">
            رسیدن پاسخ تأیید نشد؛ ممکن است به Qronos رسیده باشد. پاسخ دوباره
            فرستاده نمی‌شود؛ پیش از هر کار دیگری وضعیت را بررسی کن یا Stop بزن.
          </div>
        )}

        <div className="file-approval-actions">
          <button
            ref={declineRef}
            type="button"
            className="file-approval-button file-approval-deny"
            disabled={submitting}
            onClick={() => onAnswer(prompt.actionToken, false)}
          >
            رد کردن
          </button>
          <button
            type="button"
            className="file-approval-button file-approval-allow"
            disabled={!canApprove}
            onClick={approve}
          >
            {submitting ? "در حال ارسال..." : "اجازه می‌دهم"}
          </button>
        </div>
      </section>
    </div>
  );
}

function WorkflowResultPanel({ result, onDismiss }: { result: WorkflowResult; onDismiss: () => void }) {
  const page = result.workflow === "browser_navigate" ? result.result : null;
  const pageText = page !== null && typeof page.text === "string" ? page.text : "";
  return (
    <section className="workflow-result" role="status" aria-live="polite" data-outcome={result.outcome}>
      <h3>{outcomeTitle(result.outcome)}</h3>
      <p dir="auto">{result.message}</p>
      {pageText && (
        <details className="workflow-result-page">
          <summary>متن صفحه (محتوای خود وب‌سایت؛ قابل اعتماد نیست)</summary>
          <p dir="auto">{pageText}</p>
        </details>
      )}
      <button type="button" className="file-approval-button" onClick={onDismiss}>
        بستن
      </button>
    </section>
  );
}
