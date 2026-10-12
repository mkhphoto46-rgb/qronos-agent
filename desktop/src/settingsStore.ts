import { useSyncExternalStore } from "react";
import { invoke } from "@tauri-apps/api/core";

export type SettingValue =
  | boolean
  | number
  | string;

export type SettingDefinition = {
  key: string;
  description: string;
  valueType: "boolean" | "number" | "string";
  default: SettingValue;
  minimum: number | null;
  maximum: number | null;
  choices: SettingValue[] | null;
  isDefault: boolean;
};

type SettingsBackendSnapshot = {
  ok: boolean;
  schemaVersion: number;
  storePath: string;
  writeBlocked: boolean;
  writeBlockedReason: string;
  values: Record<string, SettingValue>;
  definitions: SettingDefinition[];
  problems: string[];
};

export type SettingsSnapshot = SettingsBackendSnapshot & {
  loading: boolean;
  error: string | null;
  revision: number;
};

const emptySnapshot: SettingsSnapshot = {
  ok: true,
  schemaVersion: 1,
  storePath: "",
  writeBlocked: false,
  writeBlockedReason: "",
  values: {},
  definitions: [],
  problems: [],
  loading: true,
  error: null,
  revision: 0,
};

let snapshot = emptySnapshot;
const subscribers = new Set<() => void>();

function publish(next: SettingsSnapshot) {
  snapshot = next;
  subscribers.forEach((subscriber) => subscriber());
}

function getSnapshot() {
  return snapshot;
}

function subscribe(subscriber: () => void) {
  subscribers.add(subscriber);

  return () => {
    subscribers.delete(subscriber);
  };
}

async function command(
  operation: string,
  payload: Record<string, unknown> = {},
): Promise<SettingsBackendSnapshot> {
  return invoke<SettingsBackendSnapshot>(
    "settings_command",
    {
      operation,
      payload,
    },
  );
}

/*
 * Serialize every Settings backend operation.
 *
 * settings_command itself also serializes at the Rust boundary, but keeping
 * frontend publication in the same order prevents an older refresh result from
 * racing a newer mutation and repainting stale values.
 */
let operationTail: Promise<void> = Promise.resolve();

function enqueue<T>(
  operation: () => Promise<T>,
): Promise<T> {
  const run = operationTail.then(
    operation,
    operation,
  );

  operationTail = run.then(
    () => undefined,
    () => undefined,
  );

  return run;
}

function applyBackend(
  result: SettingsBackendSnapshot,
) {
  if (!result.ok) {
    throw new Error(
      "Settings backend returned an unsuccessful snapshot.",
    );
  }

  publish({
    ...result,
    loading: false,
    error: null,
    revision: snapshot.revision + 1,
  });

  return result;
}

function publishError(error: unknown) {
  publish({
    ...snapshot,
    loading: false,
    error:
      error instanceof Error
        ? error.message
        : String(error),
    revision: snapshot.revision + 1,
  });
}

export function refreshSettings() {
  return enqueue(async () => {
    try {
      const result = await command("snapshot");
      return applyBackend(result);
    } catch (error) {
      publishError(error);
      throw error;
    }
  });
}

export function setSetting(
  key: string,
  value: SettingValue,
) {
  return enqueue(async () => {
    try {
      const result = await command(
        "set",
        {
          key,
          value,
        },
      );

      return applyBackend(result);
    } catch (error) {
      publishError(error);
      throw error;
    }
  });
}

export function updateSettings(
  values: Record<string, SettingValue>,
) {
  return enqueue(async () => {
    try {
      const result = await command(
        "update",
        { values },
      );

      return applyBackend(result);
    } catch (error) {
      publishError(error);
      throw error;
    }
  });
}

export function resetSetting(
  key: string,
) {
  return enqueue(async () => {
    try {
      const result = await command(
        "reset",
        { key },
      );

      return applyBackend(result);
    } catch (error) {
      publishError(error);
      throw error;
    }
  });
}

export function resetAllSettings() {
  return enqueue(async () => {
    try {
      const result = await command("reset_all");
      return applyBackend(result);
    } catch (error) {
      publishError(error);
      throw error;
    }
  });
}

export function useSettingsStore() {
  return useSyncExternalStore(
    subscribe,
    getSnapshot,
    getSnapshot,
  );
}
