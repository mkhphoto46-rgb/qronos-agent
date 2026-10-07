/**
 * Where the Smart Queue lives in the app: a small toggle, and the panel.
 *
 * The toggle stays visible on the home and conversation views and shows how
 * much is waiting. It draws attention when something needs the user — an
 * interrupted request waiting for Resume, or a queue that is not being saved —
 * because neither of those fixes itself.
 *
 * "Show message" opens the conversation at the request a task belongs to, so
 * the outcome is read where it was asked rather than only counted here.
 */

import { useMemo, useState } from "react";

import { useConversationStore } from "../conversationStore";
import {
  cancelQueueTask,
  dismissQueueNotice,
  dismissQueueRefusal,
  overrideQueueTask,
  pauseQueueTask,
  resumeQueueTask,
  setQueueHeld,
  useQueueStore,
} from "../queueStore";
import SmartQueuePanel from "./SmartQueuePanel";

import "./QueueDrawer.css";

type QueueDrawerProps = {
  hidden: boolean;
  onShowConversation: (messageId: string) => void;
};

const ACTIVE_STATES = new Set(["queued", "paused", "running", "interrupted"]);

function QueueDrawer({ hidden, onShowConversation }: QueueDrawerProps) {
  const [open, setOpen] = useState(false);
  const { queue, refusal, notice } = useQueueStore();
  const { messages } = useConversationStore();

  const active = useMemo(
    () => (queue?.tasks ?? []).filter((task) => ACTIVE_STATES.has(task.state)),
    [queue],
  );
  const needsAttention =
    active.some((task) => task.state === "interrupted") ||
    (queue?.storage != null && !queue.storage.durable);

  const showRequest = (requestId: string) => {
    // The user's own message is the anchor: it exists for every queued
    // request, answered or not.
    const anchor =
      messages.find(
        (message) =>
          message.requestId === requestId && message.role === "user",
      ) ??
      messages.find((message) => message.requestId === requestId);

    if (anchor) {
      setOpen(false);
      onShowConversation(anchor.id);
    }
  };

  const describeRequest = (requestId: string) => {
    const asked = messages.find(
      (message) =>
        message.requestId === requestId && message.role === "user",
    );

    if (!asked) {
      return null;
    }

    const flattened = asked.text.replace(/\s+/g, " ").trim();

    return flattened.length > 120
      ? `${flattened.slice(0, 119).trimEnd()}…`
      : flattened;
  };

  if (hidden) {
    return null;
  }

  return (
    <div className="queue-drawer" dir="rtl">
      <button
        type="button"
        className={
          "queue-drawer-toggle" +
          (needsAttention ? " queue-drawer-toggle-attention" : "") +
          (open ? " queue-drawer-toggle-open" : "")
        }
        aria-expanded={open}
        aria-controls="qronos-smart-queue"
        onClick={() => setOpen((value) => !value)}
      >
        <span className="queue-drawer-toggle-label">SMART QUEUE</span>
        <span className="queue-drawer-toggle-count">{active.length}</span>
      </button>

      {open && (
        <div
          id="qronos-smart-queue"
          className="queue-drawer-panel"
          role="dialog"
          aria-label="Smart Queue"
        >
          <SmartQueuePanel
            snapshot={queue}
            refusal={refusal}
            notice={notice}
            onOverride={(taskId) => void overrideQueueTask(taskId)}
            onCancel={(taskId) => void cancelQueueTask(taskId)}
            onPause={(taskId) => void pauseQueueTask(taskId)}
            onResume={(taskId) => void resumeQueueTask(taskId)}
            onTogglePaused={(held) => void setQueueHeld(held)}
            onDismissRefusal={dismissQueueRefusal}
            onDismissNotice={dismissQueueNotice}
            onShowRequest={showRequest}
            describeRequest={describeRequest}
          />
        </div>
      )}
    </div>
  );
}

export default QueueDrawer;
