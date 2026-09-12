import { useEffect, useSyncExternalStore } from "react";
import { invoke } from "@tauri-apps/api/core";

export type PermissionLevel =
  | "AUTO_ALLOW"
  | "VOICE_CONFIRMATION"
  | "UI_CONFIRMATION"
  | "TYPED_SECRET"
  | "FORBIDDEN";

export type UserPermissionPolicy =
  | "DENY"
  | "ALWAYS_ASK"
  | "SESSION"
  | "ALLOW"
  | "CUSTOM";

export type PermissionPolicyRecord = {
  category: string;
  level: PermissionLevel;
  levelValue: number;
  decision: string;
  reversible: boolean;
  description: string;
};

export type UserPermissionRecord = {
  id: string;
  title: string;
  policy: UserPermissionPolicy;
  defaultPolicy: UserPermissionPolicy;
  customRules: Record<string, Exclude<UserPermissionPolicy, "CUSTOM">>;
  categories: string[];
  lockedCategories: string[];
  configured: boolean;
};

export type PermissionAuditRecord = {
  schemaVersion?: number;
  at: number;
  event: string;
  actionId: string;
  category?: string;
  target?: string;
  summary?: string;
  outcome?: string;
  decision?: string;
};

type PermissionBackendSnapshot = {
  ok: boolean;
  mutable: boolean;
  policySource: string;
  policyStoreSource: string;
  auditSource: string;
  policies: PermissionPolicyRecord[];
  userPermissions: UserPermissionRecord[];
  audit: PermissionAuditRecord[];
  error?: string;
};

type Snapshot = {
  policies: PermissionPolicyRecord[];
  userPermissions: UserPermissionRecord[];
  audit: PermissionAuditRecord[];
  mutable: boolean;
  policySource: string;
  policyStoreSource: string;
  auditSource: string;
  loading: boolean;
  error: string | null;
  revision: number;
};

let snapshot: Snapshot = {
  policies: [],
  userPermissions: [],
  audit: [],
  mutable: false,
  policySource: "security/permissions.py",
  policyStoreSource: "",
  auditSource: "action_audit.jsonl",
  loading: true,
  error: null,
  revision: 0,
};

const subscribers = new Set<() => void>();
let inFlight = false;

function publish(next: Snapshot) {
  snapshot = next;
  subscribers.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  subscribers.add(listener);

  if (subscribers.size === 1 && snapshot.revision === 0 && !inFlight) {
    void refreshPermissions();
  }

  return () => {
    subscribers.delete(listener);
  };
}

function getSnapshot() {
  return snapshot;
}

async function command<T>(
  operation: string,
  payload: Record<string, unknown> = {},
) {
  return invoke<T>("permissions_command", { operation, payload });
}

export async function refreshPermissions() {
  if (inFlight) return;
  inFlight = true;

  publish({ ...snapshot, loading: true, error: null });

  try {
    const result = await command<PermissionBackendSnapshot>("snapshot");

    if (!result.ok) {
      throw new Error(result.error || "Permissions snapshot failed.");
    }

    publish({
      policies: result.policies,
      userPermissions: result.userPermissions,
      audit: result.audit,
      mutable: result.mutable,
      policySource: result.policySource,
      policyStoreSource: result.policyStoreSource,
      auditSource: result.auditSource,
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

async function mutate(
  operation: string,
  payload: Record<string, unknown>,
) {
  try {
    await command<{ ok: boolean; error?: string }>(operation, payload);
    await refreshPermissions();
  } catch (error) {
    publish({
      ...snapshot,
      loading: false,
      error: error instanceof Error ? error.message : String(error),
      revision: snapshot.revision + 1,
    });
    throw error;
  }
}

export async function setPermissionPolicy(
  permissionId: string,
  policy: UserPermissionPolicy,
) {
  await mutate("set_policy", { permissionId, policy });
}

export async function setPermissionCustomRule(
  permissionId: string,
  category: string,
  policy: Exclude<UserPermissionPolicy, "CUSTOM">,
) {
  await mutate("set_custom_rule", { permissionId, category, policy });
}

export async function resetPermission(permissionId: string) {
  await mutate("reset_permission", { permissionId });
}

export function usePermissionStore() {
  const value = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  useEffect(() => {
    if (value.loading && !inFlight) {
      void refreshPermissions();
    }
  }, [value.loading]);

  return value;
}
