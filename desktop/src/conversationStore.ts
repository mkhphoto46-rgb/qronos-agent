import {
  useEffect,
  useSyncExternalStore,
} from "react";

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export type ConversationAttachment =
  | {
      id: string;
      type: "file";
      title: string;
      meta: string;
      unread?: boolean;
    }
  | {
      id: string;
      type: "image";
      title: string;
      meta: string;
      unread?: boolean;
    }
  | {
      id: string;
      type: "link";
      title: string;
      meta: string;
      href: string;
      unread?: boolean;
    };

export type ConversationMessage = {
  id: string;
  role: "user" | "qronos";
  time: string;
  text: string;
  source?: "text" | "voice";
  timestamp?: number;
  attachments?: ConversationAttachment[];
  requestId?: string;
  pending?: boolean;
  error?: boolean;
  // Set while the request waits in, or runs from, the Smart Queue. The
  // bubble offers Cancel only while this is present.
  queueTaskId?: string;
};

type RuntimeEvent = {
  eventType: string;
  status: string;
  message: string;
};

type RuntimeStatus = {
  running: boolean;
  status: string;
  message: string;
};

type ConversationRuntimeMessage = {
  id?: unknown;
  role?: unknown;
  text?: unknown;
  source?: unknown;
  timestamp?: unknown;
  requestId?: unknown;
};

type ConversationSnapshotPayload = {
  messages?: unknown;
};

type TextRuntimePayload = {
  requestId?: unknown;
  text?: unknown;
  error?: unknown;
};

type QueueConversationPayload = {
  taskId?: unknown;
  requestId?: unknown;
  phase?: unknown;
  text?: unknown;
  error?: unknown;
};

type ConversationSnapshot = {
  messages: ConversationMessage[];
  pendingRequestId: string | null;
};

// What a queued request's bubble says while it has no answer yet, and what
// it says when it will never get one. The answer itself always arrives as a
// canonical conversation_message_added for the same requestId.
const QUEUE_WAITING_TEXT: Record<string, string> = {
  queued:
    "در صف Qronos — وقتی دستگاه آزاد شد پاسخ می‌دهم.",
  deferred:
    "هنوز در صف — دستگاه مشغول است؛ بعداً دوباره تلاش می‌کنم.",
  running:
    "در حال پردازش از صف...",
};

const QUEUE_ENDED_TEXT: Record<string, string> = {
  cancelled:
    "لغو شد — پاسخی ثبت نشد.",
  interrupted:
    "Qronos پیش از پاسخ بسته شد و این درخواست دوباره اجرا نمی‌شود؛ لطفاً دوباره بپرسید.",
};

type Subscriber = () => void;

const HISTORY_REQUEST_ACTION =
  "qronos.conversation_history_request";

const subscribers = new Set<Subscriber>();

let snapshot: ConversationSnapshot = {
  messages: [],
  pendingRequestId: null,
};

let unlistenRuntime:
  | (() => void)
  | null = null;

let listenerPromise:
  | Promise<void>
  | null = null;

function formatMessageTime(
  date = new Date(),
) {
  return new Intl.DateTimeFormat(
    "en-GB",
    {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    },
  ).format(date);
}

function formatTimestamp(
  timestamp: number,
) {
  return formatMessageTime(
    new Date(
      timestamp * 1000,
    ),
  );
}

function friendlyRuntimeErrorText(
  raw: string,
) {
  const value =
    raw.trim();

  const lowered =
    value.toLowerCase();

  if (
    lowered.includes(
      "local brain model is not installed",
    ) ||
    lowered.includes(
      "model is not installed",
    )
  ) {
    return "??? ???? ???????? Qronos ??? ??? ????? ??? ????.";
  }

  if (
    lowered.includes(
      "local brain is unavailable",
    ) ||
    lowered.includes(
      "local brain request failed",
    ) ||
    lowered.includes(
      "could not send request to model",
    ) ||
    lowered.includes(
      "connectionerror",
    ) ||
    lowered.includes(
      "httpconnectionpool",
    ) ||
    lowered.includes(
      "127.0.0.1:11434",
    ) ||
    lowered.includes(
      "winerror 10061",
    )
  ) {
    return "??? ???? Qronos ?? ????? ????? ????? ??? ???? ???? ???.";
  }

  return value;
}

function runtimeErrorText(
  error: unknown,
) {
  return friendlyRuntimeErrorText(
    error instanceof Error
      ? error.message
      : String(error),
  );
}

function publish(
  next: ConversationSnapshot,
) {
  snapshot = next;

  for (const subscriber of subscribers) {
    subscriber();
  }
}

function updateSnapshot(
  updater: (
    current: ConversationSnapshot,
  ) => ConversationSnapshot,
) {
  publish(
    updater(snapshot),
  );
}

function subscribe(
  subscriber: Subscriber,
) {
  subscribers.add(
    subscriber,
  );

  return () => {
    subscribers.delete(
      subscriber,
    );
  };
}

function getSnapshot() {
  return snapshot;
}

function parseCanonicalMessage(
  raw: ConversationRuntimeMessage,
): ConversationMessage | null {
  const id =
    typeof raw.id === "string"
      ? raw.id.trim()
      : "";

  const role =
    raw.role === "user" ||
    raw.role === "qronos"
      ? raw.role
      : null;

  const text =
    typeof raw.text === "string"
      ? raw.text.trim()
      : "";

  const timestamp =
    typeof raw.timestamp === "number" &&
    Number.isFinite(raw.timestamp)
      ? raw.timestamp
      : null;

  const source =
    raw.source === "voice" ||
    raw.source === "text"
      ? raw.source
      : undefined;

  const requestId =
    typeof raw.requestId === "string"
      ? raw.requestId.trim()
      : "";

  if (
    !id ||
    !role ||
    !text ||
    timestamp === null
  ) {
    return null;
  }

  return {
    id,
    role,
    text,
    source,
    timestamp,
    time: formatTimestamp(
      timestamp,
    ),
    requestId:
      requestId || undefined,
  };
}

function sortMessages(
  messages: ConversationMessage[],
) {
  return [...messages].sort(
    (left, right) => {
      const leftTime =
        left.timestamp ?? 0;
      const rightTime =
        right.timestamp ?? 0;

      if (
        leftTime !== rightTime
      ) {
        return leftTime - rightTime;
      }

      return left.id.localeCompare(
        right.id,
      );
    },
  );
}

function mergeCanonicalMessage(
  canonical: ConversationMessage,
) {
  updateSnapshot(
    (current) => {
      if (
        current.messages.some(
          (message) =>
            message.id ===
            canonical.id,
        )
      ) {
        return current;
      }

      let replaced = false;

      const nextMessages =
        current.messages.map(
          (message) => {
            if (
              !canonical.requestId ||
              message.requestId !==
                canonical.requestId ||
              message.role !==
                canonical.role
            ) {
              return message;
            }

            if (
              canonical.role ===
                "qronos" &&
              !message.pending
            ) {
              return message;
            }

            replaced = true;
            return canonical;
          },
        );

      if (!replaced) {
        nextMessages.push(
          canonical,
        );
      }

      return {
        messages: sortMessages(
          nextMessages,
        ),
        pendingRequestId:
          canonical.role ===
            "qronos" &&
          canonical.requestId &&
          canonical.requestId ===
            current.pendingRequestId
            ? null
            : current.pendingRequestId,
      };
    },
  );
}

function replacePendingFallback(
  requestId: string,
  responseText: string,
  isError: boolean,
) {
  updateSnapshot(
    (current) => {
      if (
        current.pendingRequestId !==
        requestId
      ) {
        return current;
      }

      const nextMessages =
        current.messages.map(
          (message) => {
            if (
              message.requestId !==
                requestId ||
              !message.pending
            ) {
              return message;
            }

            return {
              ...message,
              text: isError
                ? `درخواست اجرا نشد: ${responseText}`
                : responseText,
              pending: false,
              error: isError,
            };
          },
        );

      return {
        messages: nextMessages,
        pendingRequestId: null,
      };
    },
  );
}

/**
 * Move a queued request's bubble to where the queue says it is.
 *
 * Correlated only by requestId, and independent of pendingRequestId: a
 * queued request deliberately stops blocking the composer, so the user can
 * keep talking while it waits. Never invents an answer — "answered" only
 * resolves the bubble when the canonical message did not already.
 */
function applyQueueConversationUpdate(
  payload: QueueConversationPayload,
) {
  const requestId =
    typeof payload.requestId === "string"
      ? payload.requestId.trim()
      : "";
  const taskId =
    typeof payload.taskId === "string"
      ? payload.taskId.trim()
      : "";
  const phase =
    typeof payload.phase === "string"
      ? payload.phase
      : "";

  if (!requestId || !phase) {
    return;
  }

  let replacement: Partial<ConversationMessage> | null = null;

  if (phase in QUEUE_WAITING_TEXT) {
    replacement = {
      text: QUEUE_WAITING_TEXT[phase],
      pending: true,
      error: false,
      queueTaskId: taskId || undefined,
    };
  } else if (phase === "failed") {
    replacement = {
      text: `درخواست اجرا نشد: ${friendlyRuntimeErrorText(
        typeof payload.error === "string"
          ? payload.error
          : "Qronos نتوانست این درخواست را اجرا کند.",
      )}`,
      pending: false,
      error: true,
      queueTaskId: undefined,
    };
  } else if (phase in QUEUE_ENDED_TEXT) {
    replacement = {
      text: QUEUE_ENDED_TEXT[phase],
      pending: false,
      error: phase === "interrupted",
      queueTaskId: undefined,
    };
  } else if (
    phase === "answered" &&
    typeof payload.text === "string" &&
    payload.text.trim()
  ) {
    // Normally already resolved by the canonical message. This covers an
    // answer whose history write failed, so it is still shown.
    replacement = {
      text: payload.text.trim(),
      pending: false,
      error: false,
      queueTaskId: undefined,
    };
  }

  if (!replacement) {
    return;
  }

  const update = replacement;

  updateSnapshot((current) => {
    let found = false;

    const nextMessages = current.messages.map((message) => {
      if (
        message.requestId !== requestId ||
        message.role !== "qronos" ||
        !message.pending
      ) {
        return message;
      }

      found = true;
      return { ...message, ...update };
    });

    if (!found && update.pending !== false) {
      // A request queued without a pending bubble of its own, such as one
      // submitted to the queue directly.
      const now = new Date();

      nextMessages.push({
        id: `queued-${requestId}`,
        role: "qronos",
        time: formatMessageTime(now),
        timestamp: now.getTime() / 1000,
        source: "text",
        text: update.text ?? "",
        requestId,
        pending: true,
        queueTaskId: update.queueTaskId,
      });
    } else if (
      !found &&
      phase !== "answered" &&
      !current.messages.some(
        (message) =>
          message.requestId === requestId &&
          message.role === "qronos",
      )
    ) {
      // The bubble is gone (the history was reloaded), but the request
      // still deserves to say why it has no answer.
      const now = new Date();

      nextMessages.push({
        id: `queued-${phase}-${requestId}`,
        role: "qronos",
        time: formatMessageTime(now),
        timestamp: now.getTime() / 1000,
        source: "text",
        text: update.text ?? "",
        requestId,
        error: update.error,
      });
    }

    return {
      messages: sortMessages(nextMessages),
      pendingRequestId:
        current.pendingRequestId === requestId
          ? null
          : current.pendingRequestId,
    };
  });
}

function applyHistorySnapshot(
  payload: ConversationSnapshotPayload,
) {
  if (
    snapshot.pendingRequestId
  ) {
    return;
  }

  if (
    !Array.isArray(
      payload.messages,
    )
  ) {
    return;
  }

  const messages =
    payload.messages
      .map((item) =>
        parseCanonicalMessage(
          (
            item &&
            typeof item === "object"
              ? item
              : {}
          ) as ConversationRuntimeMessage,
        ),
      )
      .filter(
        (
          message,
        ): message is ConversationMessage =>
          message !== null,
      );

  publish({
    messages: sortMessages(
      messages,
    ),
    pendingRequestId: null,
  });
}

async function requestHistorySnapshot() {
  try {
    const status =
      await invoke<RuntimeStatus>(
        "get_runtime_status",
      );

    if (!status.running) {
      return;
    }

    await invoke(
      "send_runtime_action",
      {
        actionId:
          HISTORY_REQUEST_ACTION,
      },
    );
  } catch {
    // Runtime startup can race the first UI subscription.
    // runtime_ready will request the snapshot again.
  }
}

function handleRuntimeEvent(
  event: RuntimeEvent,
) {
  if (
    event.eventType ===
    "runtime_ready"
  ) {
    void requestHistorySnapshot();
    return;
  }

  if (
    event.eventType ===
    "conversation_history_snapshot"
  ) {
    try {
      applyHistorySnapshot(
        JSON.parse(
          event.message,
        ) as ConversationSnapshotPayload,
      );
    } catch {
      return;
    }

    return;
  }

  if (
    event.eventType ===
    "conversation_message_added"
  ) {
    try {
      const canonical =
        parseCanonicalMessage(
          JSON.parse(
            event.message,
          ) as ConversationRuntimeMessage,
        );

      if (canonical) {
        mergeCanonicalMessage(
          canonical,
        );
      }
    } catch {
      return;
    }

    return;
  }

  if (
    event.eventType ===
      "queue_conversation_update" ||
    event.eventType ===
      "text_deferred"
  ) {
    let payload: QueueConversationPayload;

    try {
      payload = JSON.parse(
        event.message,
      ) as QueueConversationPayload;
    } catch {
      return;
    }

    applyQueueConversationUpdate(
      event.eventType === "text_deferred"
        ? { ...payload, phase: "queued" }
        : payload,
    );
    return;
  }

  if (
    event.eventType !==
      "text_response" &&
    event.eventType !==
      "text_error"
  ) {
    return;
  }

  let payload: TextRuntimePayload;

  try {
    payload = JSON.parse(
      event.message,
    ) as TextRuntimePayload;
  } catch {
    return;
  }

  const requestId =
    typeof payload.requestId ===
    "string"
      ? payload.requestId.trim()
      : "";

  if (!requestId) {
    return;
  }

  const isError =
    event.eventType ===
    "text_error";

  const rawResponse =
    isError
      ? typeof payload.error ===
          "string"
        ? payload.error
        : "Qronos نتوانست این درخواست را اجرا کند."
      : typeof payload.text ===
          "string"
        ? payload.text
        : "";

  const responseText =
    friendlyRuntimeErrorText(
      rawResponse,
    );

  if (!responseText) {
    return;
  }

  // Canonical conversation_message_added normally resolves the pending
  // bubble first. This only acts as a correlated fallback if persistence
  // or event delivery failed; it can never append a stale response.
  replacePendingFallback(
    requestId,
    responseText,
    isError,
  );
}

async function ensureRuntimeListener() {
  if (unlistenRuntime) {
    return;
  }

  if (listenerPromise) {
    await listenerPromise;
    return;
  }

  listenerPromise =
    listen<RuntimeEvent>(
      "qronos://runtime-event",
      (event) => {
        handleRuntimeEvent(
          event.payload,
        );
      },
    )
      .then((cleanup) => {
        unlistenRuntime =
          cleanup;

        void requestHistorySnapshot();
      })
      .finally(() => {
        listenerPromise =
          null;
      });

  await listenerPromise;
}

export function useConversationStore() {
  const current =
    useSyncExternalStore(
      subscribe,
      getSnapshot,
      getSnapshot,
    );

  useEffect(() => {
    void ensureRuntimeListener()
      .catch((error) => {
        console.error(
          "[Qronos Conversation Store] runtime listener failed:",
          error,
        );
      });
  }, []);

  return current;
}

/**
 * Cancel a queued request. The outcome arrives as a queue event: either
 * "cancelled", or a refusal when the answer was already delivered.
 */
export async function cancelQueuedConversation(
  taskId: string,
) {
  const cleaned = taskId.trim();

  if (!cleaned) {
    return;
  }

  try {
    await invoke("queue_cancel", {
      taskId: cleaned,
    });
  } catch (error) {
    console.error(
      "[Qronos Conversation Store] queue cancel failed:",
      runtimeErrorText(error),
    );
  }
}

export async function sendConversationMessage(
  rawText: string,
) {
  const text =
    rawText.trim();

  if (
    !text ||
    snapshot.pendingRequestId
  ) {
    return null;
  }

  const now = new Date();
  const time =
    formatMessageTime(now);
  const requestId =
    `text-${now.getTime()}-${Math.random()
      .toString(36)
      .slice(2, 10)}`;

  publish({
    messages: [
      ...snapshot.messages,
      {
        id:
          `optimistic-user-${requestId}`,
        role: "user",
        time,
        timestamp:
          now.getTime() / 1000,
        source: "text",
        text,
        requestId,
      },
      {
        id:
          `pending-${requestId}`,
        role: "qronos",
        time,
        timestamp:
          now.getTime() / 1000 +
          0.000001,
        source: "text",
        text: "در حال پردازش...",
        requestId,
        pending: true,
      },
    ],
    pendingRequestId:
      requestId,
  });

  try {
    await ensureRuntimeListener();

    const status =
      await invoke<RuntimeStatus>(
        "get_runtime_status",
      );

    if (!status.running) {
      await invoke<RuntimeStatus>(
        "start_runtime",
      );
    }

    await invoke(
      "send_text_message",
      {
        text,
        requestId,
      },
    );
  } catch (error) {
    replacePendingFallback(
      requestId,
      runtimeErrorText(error),
      true,
    );
  }

  return requestId;
}
