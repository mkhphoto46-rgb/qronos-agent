/**
 * The Smart Queue as the desktop sees it.
 *
 * Owns the latest whole-queue snapshot from the runtime and the commands that
 * change it. Whole state rather than deltas, as the runtime sends it: a
 * mangled or missed event repairs itself on the next one.
 *
 * Commands resolve when they are written, not when they take effect. The
 * effect always arrives as a new snapshot, so the panel never shows a state
 * the runtime has not confirmed.
 */

import {
  useEffect,
  useSyncExternalStore,
} from "react";

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

import type {
  QueueRefusal,
  QueueSnapshot,
} from "./components/SmartQueuePanel";

type RuntimeEvent = {
  eventType: string;
  status: string;
  message: string;
};

type RuntimeStatus = {
  running: boolean;
};

export type QueueStoreSnapshot = {
  queue: QueueSnapshot | null;
  refusal: QueueRefusal | null;
  // The last command the runtime refused, in its own words.
  notice: string | null;
};

type Subscriber = () => void;

const subscribers = new Set<Subscriber>();

let snapshot: QueueStoreSnapshot = {
  queue: null,
  refusal: null,
  notice: null,
};

let unlistenRuntime: (() => void) | null = null;
let listenerPromise: Promise<void> | null = null;

function publish(next: QueueStoreSnapshot) {
  snapshot = next;

  for (const subscriber of subscribers) {
    subscriber();
  }
}

function subscribe(subscriber: Subscriber) {
  subscribers.add(subscriber);

  return () => {
    subscribers.delete(subscriber);
  };
}

function getSnapshot() {
  return snapshot;
}

function parse<T>(message: string): T | null {
  try {
    return JSON.parse(message) as T;
  } catch {
    return null;
  }
}

function isQueueSnapshot(value: unknown): value is QueueSnapshot {
  if (!value || typeof value !== "object") {
    return false;
  }

  const candidate = value as Partial<QueueSnapshot>;

  return (
    typeof candidate.revision === "number" &&
    typeof candidate.paused === "boolean" &&
    Array.isArray(candidate.tasks)
  );
}

function handleRuntimeEvent(event: RuntimeEvent) {
  switch (event.eventType) {
    case "runtime_ready":
      // A fresh runtime: ask for the queue, which is also what restores
      // whatever an earlier run left waiting or interrupted.
      void refreshQueue();
      return;

    case "queue_changed": {
      const parsed = parse<unknown>(event.message);

      if (isQueueSnapshot(parsed)) {
        publish({ ...snapshot, queue: parsed });
      }
      return;
    }

    case "queue_override_refused": {
      const parsed = parse<QueueRefusal>(event.message);

      if (parsed) {
        publish({ ...snapshot, refusal: parsed });
      }
      return;
    }

    case "queue_task_refused":
    case "queue_cancel_refused": {
      const parsed = parse<{ detail?: unknown }>(event.message);

      publish({
        ...snapshot,
        notice:
          parsed && typeof parsed.detail === "string"
            ? parsed.detail
            : "Qronos could not do that.",
      });
      return;
    }

    default:
      return;
  }
}

async function ensureRuntimeListener() {
  if (unlistenRuntime) {
    return;
  }

  if (listenerPromise) {
    await listenerPromise;
    return;
  }

  listenerPromise = listen<RuntimeEvent>(
    "qronos://runtime-event",
    (event) => {
      handleRuntimeEvent(event.payload);
    },
  )
    .then((cleanup) => {
      unlistenRuntime = cleanup;
      void refreshQueue();
    })
    .finally(() => {
      listenerPromise = null;
    });

  await listenerPromise;
}

async function send(command: string, args?: Record<string, unknown>) {
  try {
    await invoke(command, args);
  } catch (error) {
    publish({
      ...snapshot,
      notice: error instanceof Error ? error.message : String(error),
    });
  }
}

export function useQueueStore() {
  const current = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  useEffect(() => {
    void ensureRuntimeListener().catch((error) => {
      console.error("[Qronos Queue Store] runtime listener failed:", error);
    });
  }, []);

  return current;
}

export async function refreshQueue() {
  try {
    const status = await invoke<RuntimeStatus>("get_runtime_status");

    if (!status.running) {
      return;
    }

    await invoke("queue_list");
  } catch {
    // The runtime may still be starting; runtime_ready asks again.
  }
}

export function setQueueHeld(held: boolean) {
  return send("queue_set_paused", { paused: held });
}

export function cancelQueueTask(taskId: string) {
  return send("queue_cancel", { taskId });
}

export function pauseQueueTask(taskId: string) {
  return send("queue_pause_task", { taskId });
}

export function resumeQueueTask(taskId: string) {
  return send("queue_resume_task", { taskId });
}

export function overrideQueueTask(taskId: string) {
  return send("queue_override", { taskId });
}

export function dismissQueueRefusal() {
  publish({ ...snapshot, refusal: null });
}

export function dismissQueueNotice() {
  publish({ ...snapshot, notice: null });
}
