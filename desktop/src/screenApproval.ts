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
  /**
   * The last decision could not be confirmed (the command rejected). It may
   * or may not have reached the runtime, so state may already have changed.
   */
  unconfirmed: boolean;
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
 * flight), and never automatically replayed: a rejected send does not prove
 * the decision was undelivered, so the outcome is reported as unconfirmed.
 */
export class ScreenApprovalController {
  private readonly queue = new ScreenPromptQueue();
  private readonly listeners = new Set<() => void>();
  private active: ScreenPrompt | null = null;
  private generation = 0;
  private submitting = false;
  private unconfirmed = false;
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

  /**
   * Changes whenever the displayed prompt changes or everything is cleared
   * (runtime end, unmount). Async work captures it and drops its result if
   * it no longer matches.
   */
  get generationToken(): number {
    return this.generation;
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
    this.unconfirmed = false;
    this.publish();
    try {
      await this.send(prompt, approved);
    } catch (error) {
      this.onSendError(error);
      if (this.generation === generation) {
        // Unconfirmed, not "undelivered": a flush/connection failure can
        // follow delivery. Keep the same identity on screen with an honest
        // warning; nothing is replayed on the person's behalf. A deliberate
        // second answer reuses the single-use identity, which the runtime
        // refuses if the first one already arrived.
        this.submitting = false;
        this.unconfirmed = true;
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
    this.unconfirmed = false;
    this.generation += 1;
  }

  private advance(): void {
    if (this.active === null) {
      const next = this.queue.next();
      if (next !== undefined) {
        this.active = next;
        this.generation += 1;
        this.submitting = false;
        this.unconfirmed = false;
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
            unconfirmed: this.unconfirmed,
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

/** The Tauri invoke shape the approval flow needs (tests pass a fake). */
export type InvokeFn = (
  command: string,
  args?: Record<string, unknown>,
) => Promise<unknown>;

/**
 * The controller App uses: decisions go only through the typed
 * look_at_screen command, carrying only the prompt's approval identity.
 */
export function createScreenApprovalController(
  invoke: InvokeFn,
  onSendError: (error: unknown) => void = () => {},
): ScreenApprovalController {
  return new ScreenApprovalController(
    (prompt, approved) =>
      invoke("look_at_screen", {
        approvalId: prompt.approvalId,
        approved,
        windowOnly: true,
      }),
    onSendError,
  );
}

export type TimerApi = {
  setTimeout: (callback: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
};

const browserTimers: TimerApi = {
  setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
  clearTimeout: (handle) =>
    globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** Interval between read-only runtime status checks while a prompt is up. */
export const SCREEN_APPROVAL_STATUS_INTERVAL_MS = 2000;

/** Only an explicit `running: false` counts; errors and unknown shapes do not. */
export function isStoppedRuntimeStatus(status: unknown): boolean {
  return (
    typeof status === "object" &&
    status !== null &&
    (status as { running?: unknown }).running === false
  );
}

/**
 * Detects a runtime that ended without saying so (killed or crashed: the
 * desktop reader just sees EOF, so no runtime_stopping event arrives).
 *
 * Only while a prompt is visible, it reads the existing get_runtime_status
 * command (read-only; it never starts or restarts the runtime) at a fixed
 * interval, with at most one check in flight. Each check captures the
 * controller's generation; if the prompt or runtime changed meanwhile (a
 * newer prompt, runtime_ready, a clear), the result is dropped. A failed or
 * unrecognised status read is "unknown", never "stopped", and ordinary
 * runtime_error diagnostics are not consulted at all. dispose() cancels the
 * timer and suppresses any late result.
 */
export class ScreenApprovalRuntimeWatch {
  private readonly controller: ScreenApprovalController;
  private readonly readStatus: () => Promise<unknown>;
  private readonly timers: TimerApi;
  private readonly intervalMs: number;
  private timer: unknown = null;
  private inFlight = false;
  private disposed = false;
  private unsubscribe: (() => void) | null = null;

  constructor(
    controller: ScreenApprovalController,
    readStatus: () => Promise<unknown>,
    timers: TimerApi = browserTimers,
    intervalMs: number = SCREEN_APPROVAL_STATUS_INTERVAL_MS,
  ) {
    this.controller = controller;
    this.readStatus = readStatus;
    this.timers = timers;
    this.intervalMs = intervalMs;
  }

  start(): void {
    if (this.disposed || this.unsubscribe !== null) {
      return;
    }
    this.unsubscribe = this.controller.subscribe(() => this.sync());
    this.sync();
  }

  dispose(): void {
    this.disposed = true;
    this.unsubscribe?.();
    this.unsubscribe = null;
    this.cancel();
  }

  private sync(): void {
    if (this.disposed) {
      return;
    }
    if (this.controller.getSnapshot() === null) {
      this.cancel();
      return;
    }
    if (this.timer === null && !this.inFlight) {
      this.timer = this.timers.setTimeout(() => {
        void this.check();
      }, this.intervalMs);
    }
  }

  private cancel(): void {
    if (this.timer !== null) {
      this.timers.clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private async check(): Promise<void> {
    this.timer = null;
    if (this.disposed || this.controller.getSnapshot() === null) {
      return;
    }
    const generation = this.controller.generationToken;
    this.inFlight = true;
    let stopped = false;
    try {
      stopped = isStoppedRuntimeStatus(await this.readStatus());
    } catch {
      stopped = false;
    } finally {
      this.inFlight = false;
    }
    if (this.disposed) {
      return;
    }
    if (stopped && this.controller.generationToken === generation) {
      // The runtime that asked is gone: drop its prompts, answer nothing.
      this.controller.clear();
      return;
    }
    this.sync();
  }
}

/** App wiring: watch the runtime through the read-only status command. */
export function startScreenApprovalRuntimeWatch(
  controller: ScreenApprovalController,
  invoke: InvokeFn,
  timers?: TimerApi,
): ScreenApprovalRuntimeWatch {
  const watch = new ScreenApprovalRuntimeWatch(
    controller,
    () => invoke("get_runtime_status"),
    timers,
  );
  watch.start();
  return watch;
}
