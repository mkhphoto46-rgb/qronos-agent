/**
 * The activity history store: the latest validated snapshot from the runtime and the undo conversation.
 *
 * The runtime is asked through the registered send_runtime_action route (core/runtime_bridge.py history
 * actions) and answers with runtime events; everything is validated by historyModel.ts. Nothing polls: the
 * history is asked for when the panel opens, when the runtime becomes ready, and after an undo result.
 */

import { useEffect, useSyncExternalStore } from "react";

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

import {
  HISTORY_REQUEST_ACTION,
  HISTORY_UNDO_REQUEST_ACTION,
  INITIAL_HISTORY,
  reduceHistory,
  undoDecision,
} from "./historyModel";
import type { HistoryState } from "./historyModel";

type RuntimeEvent = { eventType: string; status: string; message: string };

const subscribers = new Set<() => void>();
let state: HistoryState = INITIAL_HISTORY;
let unlisten: (() => void) | null = null;
let listening: Promise<void> | null = null;

function publish(next: HistoryState) {
  if (next === state) return;
  state = next;
  for (const subscriber of subscribers) subscriber();
}

async function send(actionId: string, data?: Record<string, unknown>) {
  try {
    await invoke("send_runtime_action", data ? { actionId, data } : { actionId });
  } catch {
    publish({ ...state, requested: null, notice: "Qronos در دسترس نیست؛ چیزی تغییر نکرد." });
  }
}

function onRuntimeEvent(event: RuntimeEvent) {
  if (event.eventType === "runtime_ready") {
    void refreshHistory();
    return;
  }
  if (event.eventType.startsWith("history_")) {
    publish(reduceHistory(state, event.eventType, event.message));
  }
}

async function ensureListener() {
  if (unlisten) return;
  if (!listening) {
    listening = listen<RuntimeEvent>("qronos://runtime-event", (event) => onRuntimeEvent(event.payload))
      .then((cleanup) => { unlisten = cleanup; })
      .finally(() => { listening = null; });
  }
  await listening;
}

export function useHistoryStore(): HistoryState {
  useEffect(() => {
    void ensureListener();
  }, []);
  return useSyncExternalStore(
    (subscriber) => {
      subscribers.add(subscriber);
      return () => subscribers.delete(subscriber);
    },
    () => state,
  );
}

export async function refreshHistory() {
  await ensureListener();
  await send(HISTORY_REQUEST_ACTION);
}

export async function requestUndo(actionId: string) {
  if (state.prompt || state.requested) return; // one undo conversation at a time
  publish({ ...state, requested: actionId, notice: null });
  await send(HISTORY_UNDO_REQUEST_ACTION, { actionId });
}

/** Answer the open prompt once; the secret is passed through, never kept. */
export async function answerUndo(approved: boolean, secret: string) {
  const prompt = state.prompt;
  if (!prompt) return;
  const decision = undoDecision(prompt, approved, secret);
  await send(decision.actionId, decision.data);
}

/** Closing the panel ends any open prompt (declined) and forgets the pending request. */
export async function closeUndoConversation() {
  const prompt = state.prompt;
  publish({ ...state, requested: null, notice: null });
  if (prompt) await answerUndo(false, "");
}

export function dismissHistoryNotice() {
  publish({ ...state, notice: null });
}
