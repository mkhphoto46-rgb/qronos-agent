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
    for (let index = this.waiting.length - 1; index >= 0; index -= 1) {
      if (revoked.has(this.waiting[index].approvalId)) {
        this.waiting.splice(index, 1);
      }
    }
  }

  next(): ScreenPrompt | undefined {
    return this.waiting.shift();
  }

  get size(): number {
    return this.waiting.length;
  }
}
