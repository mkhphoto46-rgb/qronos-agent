import { useEffect, useState } from "react";

import { setApprovalSecret } from "../permissionStore";
import type { ApprovalSecretStatus } from "../permissionStore";

type ApprovalSecretPanelProps = {
  status: ApprovalSecretStatus | null;
  visible: boolean;
  disabled: boolean;
};

const MIN_SECRET_CHARS = 8;

// Set or change the approval secret that TYPED_SECRET actions require.
// Typed values live only in this component's transient state: they are cleared
// on submit, on cancel and whenever the Permissions view is hidden, and they
// are never written to a store, a snapshot, storage or the conversation.
function ApprovalSecretPanel({ status, visible, disabled }: ApprovalSecretPanelProps) {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const clearSecrets = () => {
    setCurrent("");
    setNext("");
    setRepeat("");
  };

  const close = () => {
    clearSecrets();
    setOpen(false);
  };

  useEffect(() => {
    if (!visible) {
      clearSecrets();
      setOpen(false);
    }
  }, [visible]);

  const configured = Boolean(status?.configured);
  const available = status?.available !== false;
  const locked = Boolean(status && status.lockedUntil * 1000 > Date.now());
  const tooShort = next.length < MIN_SECRET_CHARS;
  const mismatch = repeat.length > 0 && next !== repeat;
  const canSubmit =
    !busy && !tooShort && next === repeat && (!configured || current.length > 0);

  const submit = async () => {
    if (!canSubmit) return;

    const newSecret = next;
    const currentSecret = configured ? current : undefined;
    clearSecrets();
    setBusy(true);

    try {
      await setApprovalSecret(newSecret, currentSecret);
      setMessage(configured ? "Approval secret changed." : "Approval secret set.");
      setOpen(false);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const state = !status
    ? "…"
    : !available
      ? "UNAVAILABLE"
      : locked
        ? "LOCKED"
        : configured
          ? "CONFIGURED"
          : "NOT SET";

  return (
    <div className="permissions-backend-note permissions-approval-secret">
      <span className="permissions-ltr">APPROVAL SECRET</span>
      <strong className="permissions-ltr">{state}</strong>
      <p className="permissions-ltr">
        Typed-secret actions, such as deleting from the Library, are approved
        only with this secret. Qronos keeps a salted scrypt hash on this PC,
        never the secret. There is no reset inside Qronos.
      </p>
      {!available && status?.error && (
        <p className="permissions-ltr">{status.error}</p>
      )}
      {message && (
        <p className="permissions-ltr" role="status">
          {message}
        </p>
      )}

      {open ? (
        <form
          className="permissions-approval-secret-form"
          dir="ltr"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          {configured && (
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="Current secret"
              value={current}
              onChange={(event) => setCurrent(event.target.value)}
            />
          )}
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder={`New secret (${MIN_SECRET_CHARS}+ characters)`}
            value={next}
            onChange={(event) => setNext(event.target.value)}
          />
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder="Repeat new secret"
            value={repeat}
            onChange={(event) => setRepeat(event.target.value)}
          />
          {mismatch && <small>The new secrets do not match.</small>}
          <div className="permissions-approval-secret-actions">
            <button
              type="button"
              className="permissions-reset-button"
              disabled={busy}
              onClick={close}
            >
              CANCEL
            </button>
            <button
              type="submit"
              className="permissions-reset-button"
              disabled={!canSubmit}
            >
              {busy ? "SAVING..." : "SAVE SECRET"}
            </button>
          </div>
        </form>
      ) : (
        <button
          type="button"
          className="permissions-reset-button"
          disabled={disabled || busy || !available || locked}
          onClick={() => {
            setMessage(null);
            clearSecrets();
            setOpen(true);
          }}
        >
          {configured ? "CHANGE SECRET" : "SET SECRET"}
        </button>
      )}
    </div>
  );
}

export default ApprovalSecretPanel;
