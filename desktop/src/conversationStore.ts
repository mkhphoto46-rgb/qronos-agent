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
  // Where the queue says this request is. Present only on bubbles the queue
  // manages, never on a canonical answer.
  queuePhase?: string;
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
  resumable?: unknown;
};

type QueueSnapshotTask = {
  taskId?: unknown;
  requestId?: unknown;
  kind?: unknown;
  state?: unknown;
  heldReason?: unknown;
  detail?: unknown;
  resumable?: unknown;
  inFlight?: unknown;
};

type QueueSnapshotPayload = {
  tasks?: unknown;
  recent?: unknown;
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

// Waiting for the user, not for the machine: no pulse, and the bubble offers
// Resume.
const QUEUE_HELD_TEXT: Record<string, string> = {
  paused:
    "در صف متوقف شده — برای ادامه «ادامه» را بزنید.",
  interrupted:
    "Qronos هنگام پردازش این درخواست بسته شد. خودکار دوباره اجرا نمی‌شود؛ برای تلاش دوباره «ادامه» را بزنید.",
};

const QUEUE_ENDED_TEXT: Record<string, string> = {
  cancelled:
    "لغو شد — پاسخی ثبت نشد.",
  interrupted:
    "Qronos پیش از پاسخ بسته شد و این درخواست دوباره اجرا نمی‌شود؛ لطفاً دوباره بپرسید.",
};

// Holds that mean "the machine said not yet", as opposed to waiting its turn.
const DEFERRED_HOLDS = new Set([
  "sustained_load",
  "safety_floor",
  "activity_mode",
]);

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

function text(value: unknown) {
  return typeof value === "string" ? value.trim() : "";
}

/**
 * What a queued request's bubble should say for one queue phase, or null
 * when the phase changes nothing it shows.
 */
function queueBubbleFor(
  payload: QueueConversationPayload,
): Partial<ConversationMessage> | null {
  const phase = text(payload.phase);
  const taskId = text(payload.taskId) || undefined;

  if (phase in QUEUE_WAITING_TEXT) {
    return {
      text: QUEUE_WAITING_TEXT[phase],
      pending: true,
      error: false,
      queueTaskId: taskId,
      queuePhase: phase,
    };
  }

  if (
    phase === "paused" ||
    (phase === "interrupted" && payload.resumable !== false)
  ) {
    return {
      text: QUEUE_HELD_TEXT[phase],
      pending: false,
      error: phase === "interrupted",
      queueTaskId: taskId,
      queuePhase: phase,
    };
  }

  if (phase === "failed") {
    return {
      text: `درخواست اجرا نشد: ${friendlyRuntimeErrorText(
        text(payload.error) ||
          "Qronos نتوانست این درخواست را اجرا کند.",
      )}`,
      pending: false,
      error: true,
      queueTaskId: undefined,
      queuePhase: "ended",
    };
  }

  if (phase in QUEUE_ENDED_TEXT) {
    return {
      text: QUEUE_ENDED_TEXT[phase],
      pending: false,
      error: phase === "interrupted",
      queueTaskId: undefined,
      queuePhase: "ended",
    };
  }

  if (phase === "answered" && text(payload.text)) {
    // Normally already resolved by the canonical message. This covers an
    // answer whose history write failed, so it is still shown.
    return {
      text: text(payload.text),
      pending: false,
      error: false,
      queueTaskId: undefined,
      queuePhase: undefined,
    };
  }

  return null;
}

/**
 * Move queued requests' bubbles to where the queue says they are.
 *
 * Correlated only by requestId, and independent of pendingRequestId: a
 * queued request deliberately stops blocking the composer, so the user can
 * keep talking while it waits. Never invents an answer, and never touches a
 * canonical answer: only a pending bubble or one the queue manages moves.
 * A request with no bubble at all — after a restart, say — gets one placed
 * right after the user's own message, so the outcome reads where it was
 * asked.
 */
function applyQueueConversationUpdates(
  payloads: QueueConversationPayload[],
) {
  const updates = payloads
    .map((payload) => ({
      requestId: text(payload.requestId),
      phase: text(payload.phase),
      bubble: queueBubbleFor(payload),
      // "Answered" with no text: the answer was already in the history
      // before this run, so the queue's own bubble is simply retired.
      retire: text(payload.phase) === "answered" && !text(payload.text),
    }))
    .filter((item) => Boolean(item.requestId && (item.bubble || item.retire)));

  if (updates.length === 0) {
    return;
  }

  updateSnapshot((current) => {
    let messages = current.messages;
    let pendingRequestId = current.pendingRequestId;

    for (const { requestId, phase, bubble: maybeBubble, retire } of updates) {
      if (retire || !maybeBubble) {
        messages = messages.filter(
          (message) =>
            message.requestId !== requestId ||
            message.role !== "qronos" ||
            !(message.pending || message.queuePhase),
        );

        if (pendingRequestId === requestId) {
          pendingRequestId = null;
        }

        continue;
      }

      const bubble = maybeBubble;
      let found = false;

      messages = messages.map((message) => {
        if (
          message.requestId !== requestId ||
          message.role !== "qronos" ||
          !(message.pending || message.queuePhase)
        ) {
          return message;
        }

        found = true;
        return { ...message, ...bubble };
      });

      const answered = messages.some(
        (message) =>
          message.requestId === requestId &&
          message.role === "qronos" &&
          !message.pending &&
          !message.queuePhase,
      );

      if (!found && !answered && phase !== "answered") {
        const asked = messages.find(
          (message) =>
            message.requestId === requestId && message.role === "user",
        );
        const now = new Date();
        const timestamp =
          asked?.timestamp !== undefined
            ? asked.timestamp + 0.000001
            : now.getTime() / 1000;

        messages = [
          ...messages,
          {
            id: `queued-${requestId}`,
            role: "qronos",
            time: formatTimestamp(timestamp),
            timestamp,
            source: "text",
            text: bubble.text ?? "",
            requestId,
            pending: bubble.pending,
            error: bubble.error,
            queueTaskId: bubble.queueTaskId,
            queuePhase: bubble.queuePhase,
          },
        ];
      }

      if (pendingRequestId === requestId) {
        pendingRequestId = null;
      }
    }

    return {
      messages: sortMessages(messages),
      pendingRequestId,
    };
  });
}

function applyQueueConversationUpdate(
  payload: QueueConversationPayload,
) {
  applyQueueConversationUpdates([payload]);
}

/**
 * Bring every queued request's bubble in line with a whole-queue snapshot.
 *
 * The snapshot is whole state, so this repairs anything a missed event left
 * behind — and it is how outcomes reappear on their requests after the
 * desktop or the runtime has restarted.
 */
function applyQueueSnapshot(
  payload: QueueSnapshotPayload,
) {
  const tasks = Array.isArray(payload.tasks)
    ? (payload.tasks as QueueSnapshotTask[])
    : [];
  const recent = Array.isArray(payload.recent)
    ? (payload.recent as QueueSnapshotTask[])
    : [];
  const updates: QueueConversationPayload[] = [];

  for (const task of [...recent, ...tasks]) {
    if (!task || typeof task !== "object" || !text(task.requestId)) {
      continue;
    }

    if (task.kind !== undefined && task.kind !== "conversation") {
      continue;
    }

    const state = text(task.state);
    let phase = "";

    if (state === "queued") {
      phase = DEFERRED_HOLDS.has(text(task.heldReason))
        ? "deferred"
        : "queued";
    } else if (state === "running" && task.inFlight !== true) {
      phase = "running";
    } else if (
      state === "paused" ||
      state === "interrupted" ||
      state === "failed" ||
      state === "cancelled"
    ) {
      phase = state;
    }

    if (!phase) {
      continue;
    }

    updates.push({
      taskId: task.taskId,
      requestId: task.requestId,
      phase,
      error: task.detail,
      resumable: task.resumable,
    });
  }

  applyQueueConversationUpdates(updates);
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

  // A history snapshot holds only canonical messages. Bubbles the queue
  // manages for requests that have no answer yet are kept, or a reload
  // would make an interrupted request look as if it had never been asked.
  const answered = new Set(
    messages
      .filter(
        (message) =>
          message.role === "qronos" && message.requestId,
      )
      .map((message) => message.requestId),
  );
  const queueBubbles = snapshot.messages.filter(
    (message) =>
      message.role === "qronos" &&
      message.queuePhase &&
      message.requestId &&
      !answered.has(message.requestId),
  );

  publish({
    messages: sortMessages(
      [...messages, ...queueBubbles],
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
    "queue_changed"
  ) {
    try {
      applyQueueSnapshot(
        JSON.parse(
          event.message,
        ) as QueueSnapshotPayload,
      );
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

/**
 * Resume a paused or interrupted queued request. Runs it again only through
 * the queue, with every check made afresh.
 */
export async function resumeQueuedConversation(
  taskId: string,
) {
  const cleaned = taskId.trim();

  if (!cleaned) {
    return;
  }

  try {
    await invoke("queue_resume_task", {
      taskId: cleaned,
    });
  } catch (error) {
    console.error(
      "[Qronos Conversation Store] queue resume failed:",
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
