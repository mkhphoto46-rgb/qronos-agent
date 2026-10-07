/**
 * Screen/Paint approval prompts from the runtime.
 *
 * Every prompt carries a single-use backend approval identity. The desktop
 * answers with that identity only; it never echoes question text as the key,
 * so an answer to an old prompt can never approve a newer prompt with the
 * same wording, and an unanswered prompt revoked by Stop/Pause/shutdown can
 * be dropped before it is ever shown.
 */

export type ScreenPromptKind =
  | "paint_draw"
  | "paint_undo"
  | "screen_read";

export type ScreenPrompt = {
  approvalId: string;
  kind: ScreenPromptKind;
  question: string;
};

const APPROVAL_ID = /^[A-Za-z0-9_-]{22,64}$/;

export function isApprovalId(value: unknown): value is string {
  return typeof value === "string" && APPROVAL_ID.test(value);
}

/** Parse a `voice_needs_screen` event message; null when it is unusable. */
export function parseScreenPrompt(message: string): ScreenPrompt | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(message);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) {
    return null;
  }
  const record = parsed as Record<string, unknown>;
  const kind = record.kind;
  if (
    !isApprovalId(record.approvalId) ||
    (kind !== "paint_draw" && kind !== "paint_undo" && kind !== "screen_read")
  ) {
    return null;
  }
  return {
    approvalId: record.approvalId,
    kind,
    question: typeof record.question === "string" ? record.question : "",
  };
}

/** Approval ids the runtime revoked (`computer_approval_discarded`). */
export function parseDiscardedApprovalIds(message: string): string[] {
  try {
    const parsed = JSON.parse(message) as { approvalIds?: unknown };
    return Array.isArray(parsed.approvalIds)
      ? parsed.approvalIds.filter(isApprovalId)
      : [];
  } catch {
    return [];
  }
}

export function screenPromptText(prompt: ScreenPrompt): string {
  const asked = prompt.question.trim();
  switch (prompt.kind) {
    case "paint_draw":
      return (
        "Qronos برای کشیدن این نقاشی در Paint باید پنجرهٔ Paint را پیش و " +
        `پس از کشیدن ببیند.\n«${asked}»\nاجازه می‌دهی؟`
      );
    case "paint_undo":
      return (
        "Qronos برای برگرداندن نقاشی خودش در Paint باید بوم را پیش و پس " +
        `از بازگردانی ببیند.\n«${asked}»\nاجازه می‌دهی؟`
      );
    case "screen_read":
      return (
        "Qronos برای پاسخ به این سؤال باید پنجره‌ای را که جلو بود ببیند." +
        `\n«${asked}»\nاجازه می‌دهی؟`
      );
  }
}

/**
 * Prompts waiting to be shown, in arrival order. A prompt is shown at most
 * once, and one revoked before it is shown is never shown.
 */
export class ScreenPromptQueue {
  private readonly waiting: ScreenPrompt[] = [];
  private readonly seen = new Set<string>();

  enqueue(prompt: ScreenPrompt): boolean {
    if (this.seen.has(prompt.approvalId)) {
      return false;
    }
    this.seen.add(prompt.approvalId);
    this.waiting.push(prompt);
    return true;
  }

  discard(approvalIds: readonly string[]): void {
    const revoked = new Set(approvalIds);
    // A revoked identity can never be shown, even if its prompt event is
    // processed after the revocation.
    for (const approvalId of revoked) {
      this.seen.add(approvalId);
    }
    for (let index = this.waiting.length - 1; index >= 0; index -= 1) {
      if (revoked.has(this.waiting[index].approvalId)) {
        this.waiting.splice(index, 1);
      }
    }
  }

  next(): ScreenPrompt | undefined {
    return this.waiting.shift();
  }

  /** Forget everything waiting (runtime stopped or replaced). */
  clear(): void {
    this.waiting.length = 0;
  }

  get size(): number {
    return this.waiting.length;
  }
}

/** What the approval dialog shows; null when no prompt is displayed. */
export type ScreenApprovalView = {
  prompt: ScreenPrompt;
  text: string;
  submitting: boolean;
  failed: boolean;
};

/** Sends one decision for one identity (the typed look_at_screen command). */
export type ScreenDecisionSender = (
  prompt: ScreenPrompt,
  approved: boolean,
) => Promise<unknown>;

/**
 * The one screen/Paint approval the desktop shows, plus those waiting.
 *
 * Nonblocking replacement for window.confirm: events keep flowing while a
 * prompt is displayed, so a revocation (Stop/Pause/shutdown) dismisses the
 * matching visible prompt at once without sending any decision.
 *
 * Every displayed prompt gets a new generation. A decision callback that
 * settles after its prompt was revoked or replaced belongs to an older
 * generation and can neither close nor alter the prompt now displayed, even
 * when the newer prompt has identical wording. Decisions are sent only for
 * the displayed identity, once (duplicates are ignored while one is in
 * flight), and never automatically retried.
 */
export class ScreenApprovalController {
  private readonly queue = new ScreenPromptQueue();
  private readonly listeners = new Set<() => void>();
  private active: ScreenPrompt | null = null;
  private generation = 0;
  private submitting = false;
  private failed = false;
  private view: ScreenApprovalView | null = null;
  private readonly send: ScreenDecisionSender;
  private readonly onSendError: (error: unknown) => void;

  constructor(
    send: ScreenDecisionSender,
    onSendError: (error: unknown) => void = () => {},
  ) {
    this.send = send;
    this.onSendError = onSendError;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  /** Stable snapshot (same object until something changes). */
  getSnapshot = (): ScreenApprovalView | null => this.view;

  get waiting(): number {
    return this.queue.size;
  }

  /** A new prompt from the runtime; shown now if nothing is displayed. */
  receive(prompt: ScreenPrompt): void {
    if (this.active?.approvalId === prompt.approvalId) {
      return;
    }
    if (this.queue.enqueue(prompt)) {
      this.advance();
    }
  }

  /**
   * The runtime revoked these identities. Waiting ones are dropped unseen;
   * a displayed one is dismissed immediately with no decision. Identities
   * that are neither waiting nor displayed change nothing.
   */
  revoke(approvalIds: readonly string[]): void {
    if (approvalIds.length === 0) {
      return;
    }
    this.queue.discard(approvalIds);
    if (this.active !== null && approvalIds.includes(this.active.approvalId)) {
      this.close();
      this.advance();
    }
  }

  /**
   * Decide the displayed prompt. Ignored unless ``approvalId`` is the one
   * displayed and no decision for it is already in flight.
   */
  async answer(approvalId: string, approved: boolean): Promise<void> {
    const prompt = this.active;
    if (prompt === null || prompt.approvalId !== approvalId || this.submitting) {
      return;
    }
    const generation = this.generation;
    this.submitting = true;
    this.failed = false;
    this.publish();
    try {
      await this.send(prompt, approved);
    } catch (error) {
      this.onSendError(error);
      if (this.generation === generation) {
        // Not delivered: keep the same prompt up so the person can decide
        // again or decline. Nothing is retried on their behalf.
        this.submitting = false;
        this.failed = true;
        this.publish();
      }
      return;
    }
    if (this.generation === generation) {
      this.close();
      this.advance();
    }
  }

  /** Runtime stopped, restarted or the app unmounted: show nothing, send nothing. */
  clear(): void {
    this.queue.clear();
    this.close();
    this.publish();
  }

  private close(): void {
    this.active = null;
    this.submitting = false;
    this.failed = false;
    this.generation += 1;
  }

  private advance(): void {
    if (this.active === null) {
      const next = this.queue.next();
      if (next !== undefined) {
        this.active = next;
        this.generation += 1;
        this.submitting = false;
        this.failed = false;
      }
    }
    this.publish();
  }

  private publish(): void {
    const prompt = this.active;
    this.view =
      prompt === null
        ? null
        : {
            prompt,
            text: screenPromptText(prompt),
            submitting: this.submitting,
            failed: this.failed,
          };
    for (const listener of [...this.listeners]) {
      listener();
    }
  }
}

/** Runtime events that end every prompt of the current runtime. */
const RUNTIME_ENDED_EVENTS = new Set(["runtime_stopping", "runtime_ready"]);

/**
 * Route one runtime event to the approval controller. Returns true when the
 * event belonged to the screen-approval flow.
 */
export function applyScreenApprovalEvent(
  controller: ScreenApprovalController,
  eventType: string,
  message: string,
): boolean {
  switch (eventType) {
    case "voice_needs_screen": {
      const prompt = parseScreenPrompt(message);
      if (prompt === null) {
        // Never show (or answer) a prompt without its backend identity.
        console.error(
          "[Qronos Screen Approval] prompt without a valid approvalId ignored.",
        );
        return true;
      }
      controller.receive(prompt);
      return true;
    }
    case "computer_approval_discarded":
      controller.revoke(parseDiscardedApprovalIds(message));
      return true;
    default:
      if (RUNTIME_ENDED_EVENTS.has(eventType)) {
        // A prompt never outlives the runtime that asked it.
        controller.clear();
        return true;
      }
      return false;
  }
}

/** Keyboard intent inside the dialog: Escape declines (unless sending). */
export function screenApprovalKeyIntent(
  key: string,
  submitting: boolean,
): "decline" | null {
  return key === "Escape" && !submitting ? "decline" : null;
}

export function screenPromptTitle(kind: ScreenPromptKind): string {
  switch (kind) {
    case "paint_draw":
      return "اجازهٔ دیدن Paint برای کشیدن";
    case "paint_undo":
      return "اجازهٔ دیدن Paint برای بازگردانی";
    case "screen_read":
      return "اجازهٔ دیدن پنجره";
  }
}
