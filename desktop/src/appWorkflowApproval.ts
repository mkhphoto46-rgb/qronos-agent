/**
 * Telegram / print / browser workflow prompts from the runtime.
 *
 * Every prompt carries a single-use backend identity (actionToken). The
 * desktop answers with that identity only. Stop, Pause and shutdown revoke
 * open prompts in the runtime (app_workflow_stale), which dismisses them
 * here without answering. One decision is sent per prompt; an unconfirmed
 * send is reported honestly and never replayed automatically. The approval
 * secret is sent only with an approved print step and is never kept.
 */

export type WorkflowPhase =
  | "telegram_inspect"
  | "telegram_download"
  | "print"
  | "browser_navigate";

export type WorkflowPermission =
  | "require_voice_confirmation"
  | "require_ui_confirmation"
  | "require_typed_secret";

const PERMISSION_FOR_PHASE: Record<WorkflowPhase, WorkflowPermission> = {
  telegram_inspect: "require_voice_confirmation",
  telegram_download: "require_ui_confirmation",
  print: "require_typed_secret",
  browser_navigate: "require_voice_confirmation",
};

const ACTION_TOKEN = /^[A-Za-z0-9_-]{22,64}$/;
const MAX_TEXT = 2000;
const MAX_DETAIL_TEXT = 1000;

export const APP_WORKFLOW_DECISION_ACTION = "qronos.app_workflow_decision";

export type DetailValue = string | number | boolean | string[];

export type WorkflowPrompt = {
  actionToken: string;
  requestId: string;
  phase: WorkflowPhase;
  permission: WorkflowPermission;
  message: string;
  details: Record<string, DetailValue>;
};

export type WorkflowResult = {
  requestId: string;
  workflow: string;
  outcome: string;
  message: string;
  result: Record<string, unknown>;
};

export function isActionToken(value: unknown): value is string {
  return typeof value === "string" && ACTION_TOKEN.test(value);
}

function isPhase(value: unknown): value is WorkflowPhase {
  return (
    value === "telegram_inspect" ||
    value === "telegram_download" ||
    value === "print" ||
    value === "browser_navigate"
  );
}

function boundedText(value: unknown, limit: number): string | null {
  return typeof value === "string" && value.length <= limit ? value : null;
}

function cleanDetails(value: unknown): Record<string, DetailValue> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  const details: Record<string, DetailValue> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (typeof item === "string") {
      if (item.length > MAX_DETAIL_TEXT) return null;
      details[key] = item;
    } else if (typeof item === "number" && Number.isFinite(item)) {
      details[key] = item;
    } else if (typeof item === "boolean") {
      details[key] = item;
    } else if (
      Array.isArray(item) &&
      item.length <= 20 &&
      item.every((entry) => typeof entry === "string" && entry.length <= MAX_DETAIL_TEXT)
    ) {
      details[key] = item as string[];
    } else {
      return null;
    }
  }
  return details;
}

function parseObject(message: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(message);
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** Parse `app_workflow_approval_required`; null when anything is off. */
export function parseWorkflowPrompt(message: string): WorkflowPrompt | null {
  const record = parseObject(message);
  if (record === null) return null;
  const phase = record.phase;
  const requestId = boundedText(record.requestId, 128);
  const text = boundedText(record.message, MAX_TEXT);
  const details = cleanDetails(record.details ?? {});
  if (
    !isActionToken(record.actionToken) ||
    !isPhase(phase) ||
    record.permission !== PERMISSION_FOR_PHASE[phase] ||
    !requestId ||
    text === null ||
    details === null
  ) {
    return null;
  }
  return {
    actionToken: record.actionToken,
    requestId,
    phase,
    permission: PERMISSION_FOR_PHASE[phase],
    message: text,
    details,
  };
}

/** Parse `app_workflow_stale`; the revoked identity or null. */
export function parseWorkflowStale(message: string): string | null {
  const record = parseObject(message);
  return record !== null && isActionToken(record.actionToken) ? record.actionToken : null;
}

/** Parse `app_workflow_finished`. */
export function parseWorkflowFinished(message: string): WorkflowResult | null {
  const record = parseObject(message);
  if (record === null) return null;
  const requestId = boundedText(record.requestId, 128);
  const workflow = boundedText(record.workflow, 64);
  const outcome = boundedText(record.outcome, 32);
  const text = boundedText(record.message, MAX_TEXT);
  const result = record.result;
  if (!requestId || !workflow || !outcome || text === null) return null;
  return {
    requestId,
    workflow,
    outcome,
    message: text,
    result:
      typeof result === "object" && result !== null && !Array.isArray(result)
        ? (result as Record<string, unknown>)
        : {},
  };
}

export function needsSecret(prompt: WorkflowPrompt): boolean {
  return prompt.permission === "require_typed_secret";
}

export function workflowTitle(phase: WorkflowPhase): string {
  switch (phase) {
    case "telegram_inspect":
      return "یافتن گفتگوی تلگرام";
    case "telegram_download":
      return "دانلود یک فایل از تلگرام";
    case "print":
      return "چاپ یک فایل";
    case "browser_navigate":
      return "باز کردن صفحه در مرورگر";
  }
}

const DETAIL_LABELS: Record<WorkflowPhase, Array<[string, string]>> = {
  telegram_inspect: [["contact", "مخاطب (نام دقیق)"]],
  telegram_download: [
    ["contact", "مخاطب"],
    ["fileName", "نام فایل"],
    ["sizeText", "اندازه"],
    ["destination", "پوشهٔ ذخیره"],
  ],
  print: [
    ["file", "فایل"],
    ["printer", "چاپگر"],
    ["copies", "تعداد نسخه"],
    ["color", "رنگ"],
    ["pages", "صفحه‌ها"],
    ["handler", "برنامهٔ چاپ"],
    ["warnings", "هشدار"],
  ],
  browser_navigate: [
    ["url", "نشانی"],
    ["query", "عبارت جستجو"],
  ],
};

/** Only known fields, in a fixed order, with Persian labels. */
export function workflowDetailRows(prompt: WorkflowPrompt): Array<{ label: string; value: string }> {
  const rows: Array<{ label: string; value: string }> = [];
  for (const [key, label] of DETAIL_LABELS[prompt.phase]) {
    const value = prompt.details[key];
    if (value === undefined || value === "" || (Array.isArray(value) && value.length === 0)) continue;
    rows.push({ label, value: Array.isArray(value) ? value.join("، ") : String(value) });
  }
  return rows;
}

/** The runtime action for one answer. The secret travels only with an approved print step. */
export function decisionAction(
  prompt: WorkflowPrompt,
  approved: boolean,
  secret?: string,
): { actionId: string; data: { actionToken: string; approved: boolean; secret?: string } } {
  const data: { actionToken: string; approved: boolean; secret?: string } = {
    actionToken: prompt.actionToken,
    approved,
  };
  if (approved && needsSecret(prompt)) {
    data.secret = secret ?? "";
  }
  return { actionId: APP_WORKFLOW_DECISION_ACTION, data };
}

export type WorkflowApprovalView = {
  prompt: WorkflowPrompt | null;
  submitting: boolean;
  unconfirmed: boolean;
  secretRejected: boolean;
  result: WorkflowResult | null;
};

type Send = (prompt: WorkflowPrompt, approved: boolean, secret?: string) => Promise<void>;

export class WorkflowApprovalController {
  private queue: WorkflowPrompt[] = [];
  private revoked = new Set<string>();
  private submitting: string | null = null;
  private unconfirmed = false;
  private secretRejected = false;
  private result: WorkflowResult | null = null;
  private listeners = new Set<() => void>();
  private view: WorkflowApprovalView = {
    prompt: null, submitting: false, unconfirmed: false, secretRejected: false, result: null,
  };

  constructor(private readonly send: Send, private readonly onError: (error: unknown) => void) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): WorkflowApprovalView => this.view;

  private publish(): void {
    const prompt = this.queue[0] ?? null;
    this.view = {
      prompt,
      submitting: prompt !== null && this.submitting === prompt.actionToken,
      unconfirmed: prompt !== null && this.unconfirmed,
      secretRejected: prompt !== null && this.secretRejected,
      result: this.result,
    };
    for (const listener of this.listeners) listener();
  }

  private resetDisplayedState(): void {
    this.submitting = null;
    this.unconfirmed = false;
    this.secretRejected = false;
  }

  prompt(prompt: WorkflowPrompt): void {
    if (this.revoked.has(prompt.actionToken)) return;
    if (this.queue.some((item) => item.actionToken === prompt.actionToken)) return;
    this.queue.push(prompt);
    this.publish();
  }

  revoke(actionToken: string): void {
    this.revoked.add(actionToken);
    const wasDisplayed = this.queue[0]?.actionToken === actionToken;
    this.queue = this.queue.filter((item) => item.actionToken !== actionToken);
    if (wasDisplayed) this.resetDisplayedState();
    this.publish();
  }

  finished(result: WorkflowResult): void {
    // The request's step ended: its prompts are no longer answerable.
    const displayed = this.queue[0];
    this.queue = this.queue.filter((item) => item.requestId !== result.requestId);
    if (displayed && displayed.requestId === result.requestId) this.resetDisplayedState();
    this.result = result;
    this.publish();
  }

  secretWasRejected(actionToken: string): void {
    if (this.queue[0]?.actionToken !== actionToken) return;
    this.submitting = null;
    this.secretRejected = true;
    this.publish();
  }

  dismissResult = (): void => {
    this.result = null;
    this.publish();
  };

  clear(): void {
    this.queue = [];
    this.resetDisplayedState();
    this.result = null;
    this.publish();
  }

  async answer(actionToken: string, approved: boolean, secret?: string): Promise<void> {
    const prompt = this.queue[0];
    if (!prompt || prompt.actionToken !== actionToken || this.submitting !== null) return;
    if (approved && needsSecret(prompt) && !(secret ?? "").trim()) return;
    this.submitting = actionToken;
    this.unconfirmed = false;
    this.secretRejected = false;
    this.publish();
    try {
      await this.send(prompt, approved, approved && needsSecret(prompt) ? secret : undefined);
    } catch (error) {
      if (this.queue[0]?.actionToken === actionToken) {
        this.submitting = null;
        this.unconfirmed = true;
        this.publish();
      }
      this.onError(error);
      return;
    }
    if (this.queue[0]?.actionToken !== actionToken) return;
    if (approved && needsSecret(prompt)) {
      // Stay open until the runtime answers: a wrong secret keeps the prompt.
      return;
    }
    // The identity is consumed; the next step arrives as its own prompt.
    this.queue.shift();
    this.resetDisplayedState();
    this.publish();
  }
}

export function createWorkflowApprovalController(
  invoke: (command: string, args: Record<string, unknown>) => Promise<unknown>,
  onError: (error: unknown) => void,
): WorkflowApprovalController {
  return new WorkflowApprovalController(async (prompt, approved, secret) => {
    const action = decisionAction(prompt, approved, secret);
    await invoke("send_runtime_action", { actionId: action.actionId, data: action.data });
  }, onError);
}

/** Route one runtime event; true when it was a workflow event. */
export function applyWorkflowEvent(
  controller: WorkflowApprovalController,
  eventType: string,
  message: string,
): boolean {
  switch (eventType) {
    case "app_workflow_approval_required": {
      const prompt = parseWorkflowPrompt(message);
      if (prompt !== null) controller.prompt(prompt);
      return true;
    }
    case "app_workflow_stale": {
      const token = parseWorkflowStale(message);
      if (token !== null) controller.revoke(token);
      return true;
    }
    case "app_workflow_secret_rejected": {
      const token = parseWorkflowStale(message);
      if (token !== null) controller.secretWasRejected(token);
      return true;
    }
    case "app_workflow_finished": {
      const result = parseWorkflowFinished(message);
      if (result !== null) controller.finished(result);
      return true;
    }
    default:
      return false;
  }
}

export function outcomeTitle(outcome: string): string {
  switch (outcome) {
    case "succeeded":
      return "انجام شد";
    case "submitted":
      return "در صف چاپ";
    case "needs_attention":
      return "نیاز به رسیدگی";
    case "uncertain":
      return "نتیجه نامطمئن";
    case "stopped":
      return "متوقف شد";
    case "refused":
      return "انجام نشد";
    default:
      return "نتیجه";
  }
}
