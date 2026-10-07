import { useSyncExternalStore } from "react";
import { invoke } from "@tauri-apps/api/core";

export type SetupSnapshot = {
  schemaVersion: 1;
  packaged: boolean;
  assetRoot: string;
  dataRoot: string;
  features: Record<string, { assetsPresent: boolean; missing: string[] }>;
  modelServer: {
    status: "unchecked" | "available" | "unavailable";
    configured: Record<string, string>;
    missingRoles?: string[] | null;
  };
};
type SetupState = { snapshot: SetupSnapshot | null; loading: boolean; error: string | null };
let state: SetupState = { snapshot: null, loading: true, error: null };
let pending: Promise<void> | null = null;
const listeners = new Set<() => void>();
const notify = () => { for (const listener of listeners) listener(); };
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };

export function refreshSetup(): Promise<void> {
  if (pending) return pending;
  state = { ...state, loading: true, error: null };
  notify();
  pending = invoke<SetupSnapshot>("runtime_setup_snapshot")
    .then(snapshot => { state = { snapshot, loading: false, error: null }; })
    .catch(() => { state = { snapshot: null, loading: false, error: "گزارش راه‌اندازی در دسترس نیست. نصب کامل کرونوس را بررسی کنید." }; })
    .finally(() => { pending = null; notify(); });
  return pending;
}
export function setupNeedsAttention(snapshot: SetupSnapshot): boolean {
  return ["wakeWord", "speechRecognition", "speechSynthesis"].some(key => !snapshot.features[key]?.assetsPresent)
    || snapshot.modelServer.status !== "available" || Boolean(snapshot.modelServer.missingRoles?.length);
}
export const useSetupStore = () => useSyncExternalStore(subscribe, () => state);
