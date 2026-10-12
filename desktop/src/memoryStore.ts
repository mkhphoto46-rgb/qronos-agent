import { useSyncExternalStore } from "react";
import { invoke } from "@tauri-apps/api/core";

export type MemoryStatus =
  | "PENDING_REVIEW"
  | "PENDING_EMBEDDING"
  | "ACTIVE"
  | "SUPERSEDED";

export type MemoryRecord = {
  id: string;
  memoryKey: string;
  value: string;
  category: string;
  scopeType: string;
  scopeKey: string;
  confidence: number;
  evidenceType: string;
  sensitivity: string;
  status: MemoryStatus;
  sourceKind: string;
  sourceConversationId: string;
  sourceMessageId: string;
  sourceRequestId: string;
  createdAt: number;
  updatedAt: number;
  lastConfirmedAt: number;
  lastUsedAt: number;
  embeddingModel: string;
  embeddingVersion: string;
  embeddingDimensions: number;
  supersedesId: string;
};

export type MemoryCounts = {
  active: number;
  pendingEmbedding: number;
  pendingReview: number;
  superseded: number;
  total: number;
};

export type MemoryProviderStatus = {
  ready: boolean;
  reason: string;
  model: string;
  version: string;
  localOnly: boolean;
};

export type MemoryIndexStatus = {
  count: number;
  dimensions: number;
  model: string;
  version: string;
  path: string;
  healthy: boolean;
  error: string;
};

type MemorySnapshot = {
  enabled: boolean;
  counts: MemoryCounts;
  provider: MemoryProviderStatus;
  index: MemoryIndexStatus;
  memories: MemoryRecord[];
  databasePath: string;
  vectorPath: string;
  loading: boolean;
  error: string | null;
  revision: number;
};

const emptySnapshot: MemorySnapshot = {
  enabled: true,
  counts: {
    active: 0,
    pendingEmbedding: 0,
    pendingReview: 0,
    superseded: 0,
    total: 0,
  },
  provider: {
    ready: false,
    reason: "loading",
    model: "",
    version: "",
    localOnly: true,
  },
  index: {
    count: 0,
    dimensions: 0,
    model: "",
    version: "",
    path: "",
    healthy: true,
    error: "",
  },
  memories: [],
  databasePath: "",
  vectorPath: "",
  loading: true,
  error: null,
  revision: 0,
};

let snapshot = emptySnapshot;
const subscribers = new Set<() => void>();
let inFlight = false;

function publish(next: MemorySnapshot) {
  snapshot = next;
  subscribers.forEach((fn) => fn());
}

function getSnapshot() {
  return snapshot;
}

function subscribe(fn: () => void) {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

async function command<T = Record<string, unknown>>(
  operation: string,
  payload: Record<string, unknown> = {},
) {
  return invoke<T>("memory_command", {
    operation,
    payload,
  });
}

type MemoryBackendSnapshot = Omit<
  MemorySnapshot,
  "loading" | "error" | "revision"
> & {
  ok: boolean;
};

export async function refreshMemory() {
  if (inFlight) return;

  inFlight = true;

  try {
    const result = await command<MemoryBackendSnapshot>("snapshot");

    if (!result.ok) {
      throw new Error("Memory snapshot failed.");
    }

    publish({
      enabled: result.enabled,
      counts: result.counts,
      provider: result.provider,
      index: result.index,
      memories: result.memories,
      databasePath: result.databasePath,
      vectorPath: result.vectorPath,
      loading: false,
      error: null,
      revision: snapshot.revision + 1,
    });
  } catch (error) {
    publish({
      ...snapshot,
      loading: false,
      error: error instanceof Error ? error.message : String(error),
      revision: snapshot.revision + 1,
    });
  } finally {
    inFlight = false;
  }
}

export async function setMemoryEnabled(enabled: boolean) {
  await command("set_enabled", { enabled });
  await refreshMemory();
}

export async function deleteMemory(id: string) {
  await command("delete", { id });
  await refreshMemory();
}

export async function approveMemory(id: string) {
  await command("approve", { id });
  await refreshMemory();
}

export async function supersedeMemory(id: string, value: string) {
  await command("supersede", {
    id,
    value,
    sourceKind: "user_correction",
    evidenceType: "explicit",
    confidence: 1,
  });
  await refreshMemory();
}

export async function retryPendingMemory() {
  await command("retry_pending");
  await refreshMemory();
}

export async function rebuildMemoryIndex() {
  await command("reindex");
  await refreshMemory();
}

export function useMemoryStore() {
  return useSyncExternalStore(
    subscribe,
    getSnapshot,
    getSnapshot,
  );
}
