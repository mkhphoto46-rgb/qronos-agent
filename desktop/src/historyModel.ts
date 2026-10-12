/**
 * The activity history as the desktop shows it (backend: core/activity_history.py through the bridge).
 *
 * Pure and import-free: validation of every runtime event field, the state reducer and the Persian texts.
 * - Only allow-listed enum values and bounded names are kept; anything else is dropped, never shown raw.
 * - An undo prompt is accepted only for the undo this panel asked for; an answer is accepted only for the
 *   prompt that is open. Unknown, stale or late events change nothing.
 * - The approval secret is never stored here: it goes straight from the input into one decision.
 */

export const HISTORY_REQUEST_ACTION = "qronos.history_request";
export const HISTORY_UNDO_REQUEST_ACTION = "qronos.history_undo_request";
export const HISTORY_UNDO_DECISION_ACTION = "qronos.history_undo_decision";

export type UndoState =
  | "available" | "done" | "failed" | "expired" | "irreversible" | "nothing_to_undo" | "not_latest"
  | "paint_flow" | "earlier_session" | "changed_since" | "unverifiable" | "unsupported" | "none";

export type HistoryEvent = { event: string; at: number };

export type HistoryAction = {
  actionId: string;
  category: string;
  kind: "action" | "undo_request";
  name: string;
  firstAt: number;
  lastAt: number;
  events: HistoryEvent[];
  outcome: string;
  decision: string;
  undo: { state: UndoState; reversal: string; reason: string };
};

export type UndoPrompt = {
  actionToken: string;
  actionId: string;
  reversal: string;
  name: string;
  needsSecret: boolean;
  /** The confirmation also covers reading the copy's original once more (round 34). */
  readsSource: boolean;
};

export type HistoryState = {
  actions: HistoryAction[];
  truncated: boolean;
  loaded: boolean;
  /** The action whose undo this panel asked for and is waiting to hear about. */
  requested: string | null;
  prompt: UndoPrompt | null;
  notice: string | null;
};

export const INITIAL_HISTORY: HistoryState = {
  actions: [], truncated: false, loaded: false, requested: null, prompt: null, notice: null,
};

const UNDO_STATES: ReadonlySet<string> = new Set([
  "available", "done", "failed", "expired", "irreversible", "nothing_to_undo", "not_latest", "paint_flow",
  "earlier_session", "changed_since", "unverifiable", "unsupported", "none",
]);
const EVENTS: ReadonlySet<string> = new Set(["decided", "approved", "denied_by_user", "completed", "undone"]);
const OUTCOMES: ReadonlySet<string> = new Set([
  "", "succeeded", "failed", "refused", "awaiting_approval", "cancelled", "undone", "denied_by_user",
]);
const REVERSALS: ReadonlySet<string> = new Set([
  "", "nothing_to_undo", "restore_content", "move_back", "delete_created", "stop_started", "app_undo_steps",
  "irreversible",
]);
const ENUM = /^[a-z_]{0,48}$/;
const ID = /^[A-Za-z0-9_.:-]{1,64}$/;
const TOKEN = /^[A-Za-z0-9_-]{16,64}$/;
const NAME = /^[^\u0000-\u001f\u007f\\/]{0,80}$/;
const MAX_ACTIONS = 200;

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function parseJson(message: string): Record<string, unknown> | null {
  try {
    return object(JSON.parse(message));
  } catch {
    return null;
  }
}

function time(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function parseAction(value: unknown): HistoryAction | null {
  const item = object(value);
  const undo = object(item?.undo);
  if (!item || !undo) return null;
  const firstAt = time(item.firstAt);
  const lastAt = time(item.lastAt);
  if (typeof item.actionId !== "string" || !ID.test(item.actionId)
      || typeof item.category !== "string" || !ENUM.test(item.category)
      || (item.kind !== "action" && item.kind !== "undo_request")
      || typeof item.name !== "string" || !NAME.test(item.name)
      || firstAt === null || lastAt === null
      || typeof item.outcome !== "string" || !OUTCOMES.has(item.outcome)
      || typeof item.decision !== "string" || !ENUM.test(item.decision)
      || typeof undo.state !== "string" || !UNDO_STATES.has(undo.state)
      || typeof undo.reversal !== "string" || !REVERSALS.has(undo.reversal)
      || typeof undo.reason !== "string" || !ENUM.test(undo.reason)
      || !Array.isArray(item.events) || item.events.length > 12) {
    return null;
  }
  const events: HistoryEvent[] = [];
  for (const raw of item.events) {
    const entry = object(raw);
    const at = time(entry?.at);
    if (!entry || typeof entry.event !== "string" || !EVENTS.has(entry.event) || at === null) return null;
    events.push({ event: entry.event, at });
  }
  return {
    actionId: item.actionId, category: item.category, kind: item.kind, name: item.name, firstAt, lastAt, events,
    outcome: item.outcome, decision: item.decision,
    undo: { state: undo.state as UndoState, reversal: undo.reversal, reason: undo.reason },
  };
}

/** A whole snapshot, or null when it is not understood (the previous one is then kept). */
export function parseSnapshot(message: string): { actions: HistoryAction[]; truncated: boolean } | null {
  const data = parseJson(message);
  if (!data || data.schemaVersion !== 1 || !Array.isArray(data.actions) || data.actions.length > MAX_ACTIONS
      || typeof data.truncated !== "boolean") {
    return null;
  }
  const actions: HistoryAction[] = [];
  for (const raw of data.actions) {
    const action = parseAction(raw);
    if (action) actions.push(action); // a single malformed row is dropped, never shown raw
  }
  return { actions, truncated: data.truncated };
}

export function parsePrompt(message: string): UndoPrompt | null {
  const data = parseJson(message);
  if (!data || typeof data.actionToken !== "string" || !TOKEN.test(data.actionToken)
      || typeof data.actionId !== "string" || !ID.test(data.actionId)
      || typeof data.reversal !== "string" || !REVERSALS.has(data.reversal)
      || typeof data.name !== "string" || !NAME.test(data.name) || typeof data.needsSecret !== "boolean"
      || (data.readsSource !== undefined && typeof data.readsSource !== "boolean")) {
    return null;
  }
  return { actionToken: data.actionToken, actionId: data.actionId, reversal: data.reversal, name: data.name,
           needsSecret: data.needsSecret, readsSource: data.readsSource === true };
}

export const CATEGORY_TEXT: Record<string, string> = {
  create_or_edit_file: "تغییر فایل",
  open_application: "باز کردن",
  gui_automation: "کار با برنامه‌ها",
  read_screen: "خواندن صفحه",
  read_non_sensitive: "خواندن",
  external_communication: "ارسال پیام",
  device_control: "کنترل دستگاه",
  install_software: "نصب نرم‌افزار",
  delete_recoverable: "حذف قابل بازیابی",
  system_configuration: "تنظیمات سیستم",
};

export const OUTCOME_TEXT: Record<string, string> = {
  succeeded: "انجام شد",
  failed: "ناموفق",
  refused: "رد شد (مجوز)",
  awaiting_approval: "در انتظار تأیید",
  cancelled: "لغو شد",
  undone: "بازگردانده شد",
  denied_by_user: "شما رد کردید",
  "": "نامشخص",
};

export const UNDO_TEXT: Record<UndoState, string> = {
  available: "قابل بازگرداندن",
  done: "بازگردانده شده",
  failed: "بازگرداندن ناموفق بود",
  expired: "منقضی شده",
  irreversible: "Qronos نمی‌تواند این را بازگرداند",
  nothing_to_undo: "چیزی تغییر نکرده بود",
  not_latest: "ابتدا تغییرهای بعدی باید بازگردانده شوند",
  paint_flow: "فقط با درخواست بازگرداندن نقاشی در Paint",
  earlier_session: "مربوط به اجرای قبلی Qronos؛ قابل بازگرداندن نیست",
  changed_since: "فایل‌ها پس از آن تغییر کرده‌اند؛ بازگرداندن امن نیست",
  unverifiable: "شواهد لازم برای بازگرداندن امن این مورد ثبت نشده است",
  unsupported: "بازگرداندن این نوع تغییر پشتیبانی نمی‌شود",
  none: "",
};

const REASON_TEXT: Record<string, string> = {
  paused: "Qronos متوقف موقت است؛ چیزی تغییر نکرد.",
  permission_denied: "سیاست مجوزها این بازگرداندن را اجازه نمی‌دهد؛ چیزی تغییر نکرد.",
};

export function categoryText(category: string): string {
  return CATEGORY_TEXT[category] ?? "کار دیگر";
}

/** The reversal in words, for the confirmation. */
export function reversalText(reversal: string, name: string, readsSource = false): string {
  if (reversal === "move_back") return `«${name}» به جای قبلی‌اش برگردانده می‌شود.`;
  if (reversal === "delete_created" && readsSource) {
    return `«${name}» که Qronos ساخته بود حذف می‌شود؛ پیش از آن فایل اصلی یک بار دیگر خوانده می‌شود تا مطمئن شویم هنوز همان محتوا را دارد (اصل فایل دست نمی‌خورد).`;
  }
  if (reversal === "delete_created") return `«${name}» که Qronos ساخته بود حذف می‌شود (اصل فایل دست نمی‌خورد).`;
  return "";
}

function refusalText(reason: string): string {
  return REASON_TEXT[reason] ?? UNDO_TEXT[reason as UndoState] ?? "بازگرداندن انجام نشد؛ چیزی تغییر نکرد.";
}

/** Apply one runtime event; anything not for this panel or not current returns the same state. */
export function reduceHistory(state: HistoryState, eventType: string, message: string): HistoryState {
  if (eventType === "history_snapshot") {
    const parsed = parseSnapshot(message);
    return parsed ? { ...state, actions: parsed.actions, truncated: parsed.truncated, loaded: true } : state;
  }
  if (eventType === "history_undo_approval_required") {
    const prompt = parsePrompt(message);
    if (!prompt || state.requested !== prompt.actionId) return state; // not the undo this panel asked for
    return { ...state, requested: null, prompt, notice: null };
  }
  const data = parseJson(message);
  if (!data) return state;
  if (eventType === "history_undo_refused") {
    if (state.requested === null || data.actionId !== state.requested) return state;
    return { ...state, requested: null, notice: refusalText(String(data.reason ?? "")) };
  }
  const forOpenPrompt = state.prompt !== null && data.actionToken === state.prompt.actionToken;
  if (!forOpenPrompt) return state; // late, unknown or another prompt's answer
  switch (eventType) {
    case "history_undo_declined":
      return { ...state, prompt: null, notice: "بازگرداندن لغو شد؛ چیزی تغییر نکرد." };
    case "history_undo_stale":
      return { ...state, prompt: null, notice: "این درخواست دیگر معتبر نیست؛ چیزی تغییر نکرد." };
    case "history_undo_secret_rejected":
      return { ...state, prompt: null, notice: "رمز تأیید درست نبود؛ چیزی تغییر نکرد. دوباره درخواست دهید." };
    case "history_undo_result":
      return {
        ...state,
        prompt: null,
        notice: data.outcome === "undone" ? "بازگردانده شد." : data.outcome === "failed"
          ? "بازگرداندن ناموفق بود؛ وضعیت فایل‌ها را بررسی کنید." : refusalText(String(data.outcome ?? "")),
      };
    default:
      return state;
  }
}

/** The one decision for the open prompt. The secret travels only with an approval that needs it. */
export function undoDecision(prompt: UndoPrompt, approved: boolean, secret: string): {
  actionId: string; data: { actionToken: string; approved: boolean; secret?: string };
} {
  const data: { actionToken: string; approved: boolean; secret?: string } = { actionToken: prompt.actionToken,
                                                                              approved };
  if (approved && prompt.needsSecret) data.secret = secret;
  return { actionId: HISTORY_UNDO_DECISION_ACTION, data };
}
