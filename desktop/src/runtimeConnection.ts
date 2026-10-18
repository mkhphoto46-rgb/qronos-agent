/**
 * The desktop's view of the local runtime connection: connecting, ready, failed or degraded.
 *
 * Ready means the bridge itself answered: `runtime_ready` (a new bridge reached its command loop) or `runtime_pong`
 * (an already running bridge answered a ping). A spawned process is never "ready". One attempt runs at a time: a
 * second start or a retry while busy joins nothing new, and a result of an older attempt or of a disposed controller
 * is ignored. The person only ever sees the fixed Persian texts below, never an error message, path or provider.
 * Retry only re-checks or starts the runtime; it never replays a user action or starts inference or a microphone.
 */

export type RuntimeConnectionPhase = "connecting" | "ready" | "failed" | "degraded";

export type RuntimeConnectionView = {
  phase: RuntimeConnectionPhase;
  label: string;
  detail: string;
  canRetry: boolean;
  busy: boolean;
};

export type RuntimeConnectionDeps = {
  /** Check the runtime and start it if needed: "spawned" (wait for runtime_ready) or "running" (ping it). */
  probe: (attempt: number) => Promise<"spawned" | "running">;
  /** Ask an already running bridge to answer with runtime_pong. */
  ping: () => Promise<void>;
  onChange: (view: RuntimeConnectionView) => void;
  setTimer?: (callback: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  readyTimeoutMs?: number;
};

export type RuntimeConnection = {
  start: () => Promise<void>;
  retry: () => boolean;
  handleEvent: (eventType: string) => void;
  dispose: () => void;
  view: () => RuntimeConnectionView;
};

export const RUNTIME_CONNECTION_TEXT = {
  connectingLabel: "در حال اتصال به Qronos…",
  connectingDetail: "سرویس محلی در حال آماده شدن است.",
  reconnectingLabel: "در حال اتصال دوباره به Qronos…",
  reconnectingDetail: "سرویس محلی دوباره راه‌اندازی می‌شود.",
  readyLabel: "Qronos آماده است",
  readyDetail: "",
  failedLabel: "راه‌اندازی Qronos انجام نشد",
  failedDetail: "سرویس محلی شروع نشد. دوباره تلاش کنید؛ اگر تکرار شد، «بررسی راه‌اندازی» را ببینید.",
  timeoutLabel: "Qronos پاسخ نداد",
  timeoutDetail: "سرویس محلی در زمان مقرر آماده نشد. دوباره تلاش کنید.",
  startupErrorLabel: "راه‌اندازی Qronos با خطا متوقف شد",
  startupErrorDetail: "سرویس محلی هنگام آماده شدن خطا داد. دوباره تلاش کنید.",
  degradedLabel: "Qronos خطایی گزارش کرد",
  degradedDetail: "اگر Qronos پاسخ نمی‌دهد، اتصال را دوباره بررسی کنید.",
} as const;

/** What the desktop shows before the first attempt reports anything. */
export const RUNTIME_CONNECTION_INITIAL: RuntimeConnectionView = {
  phase: "connecting", label: RUNTIME_CONNECTION_TEXT.connectingLabel, detail: RUNTIME_CONNECTION_TEXT.connectingDetail,
  canRetry: false, busy: true,
};

/** A first start can include loading local speech models before runtime_ready. */
export const DEFAULT_READY_TIMEOUT_MS = 90_000;

const READY_EVENTS = new Set(["runtime_ready", "runtime_pong"]);

export function createRuntimeConnection(deps: RuntimeConnectionDeps): RuntimeConnection {
  const setTimer = deps.setTimer ?? ((callback: () => void, ms: number) => setTimeout(callback, ms));
  const clearTimer = deps.clearTimer ?? ((handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  const timeoutMs = deps.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS;
  let attempt = 0;
  let pending: Promise<void> | null = null;
  let timer: unknown = null;
  let disposed = false;
  let current: RuntimeConnectionView = connecting(false);

  function connecting(again: boolean): RuntimeConnectionView {
    return { phase: "connecting", canRetry: false, busy: true,
             label: again ? RUNTIME_CONNECTION_TEXT.reconnectingLabel : RUNTIME_CONNECTION_TEXT.connectingLabel,
             detail: again ? RUNTIME_CONNECTION_TEXT.reconnectingDetail : RUNTIME_CONNECTION_TEXT.connectingDetail };
  }

  function show(view: RuntimeConnectionView): void {
    if (disposed) return;
    current = view;
    deps.onChange(view);
  }

  function stopTimer(): void {
    if (timer !== null) {
      clearTimer(timer);
      timer = null;
    }
  }

  function armTimer(owner: number): void {
    stopTimer();
    timer = setTimer(() => {
      timer = null;
      if (!disposed && owner === attempt && current.phase === "connecting") {
        fail(RUNTIME_CONNECTION_TEXT.timeoutLabel, RUNTIME_CONNECTION_TEXT.timeoutDetail);
      }
    }, timeoutMs);
  }

  function fail(label: string, detail: string): void {
    stopTimer();
    pending = null;
    show({ phase: "failed", label, detail, canRetry: true, busy: false });
  }

  function start(): Promise<void> {
    if (disposed) return Promise.resolve();
    if (current.phase === "connecting" && (pending !== null || timer !== null)) {
      return pending ?? Promise.resolve();  // one attempt at a time
    }
    attempt += 1;
    const owner = attempt;
    show(connecting(false));
    armTimer(owner);
    let run: Promise<void> | null = null;
    run = (async () => {
      try {
        const kind = await deps.probe(owner);
        if (disposed || owner !== attempt) return;
        if (kind === "running") {
          await deps.ping();
        }
      } catch {
        if (disposed || owner !== attempt || current.phase !== "connecting") return;
        fail(RUNTIME_CONNECTION_TEXT.failedLabel, RUNTIME_CONNECTION_TEXT.failedDetail);
      } finally {
        if (owner === attempt && pending === run) pending = null;
      }
    })();
    pending = run;
    return run;
  }

  function retry(): boolean {
    if (disposed || current.busy || (current.phase !== "failed" && current.phase !== "degraded")) return false;
    void start();
    return true;
  }

  function handleEvent(eventType: string): void {
    if (disposed) return;
    if (READY_EVENTS.has(eventType)) {
      stopTimer();
      pending = null;
      show({ phase: "ready", label: RUNTIME_CONNECTION_TEXT.readyLabel, detail: RUNTIME_CONNECTION_TEXT.readyDetail,
             canRetry: false, busy: false });
      return;
    }
    if (eventType === "runtime_error") {
      if (current.phase === "connecting") {
        fail(RUNTIME_CONNECTION_TEXT.startupErrorLabel, RUNTIME_CONNECTION_TEXT.startupErrorDetail);
      } else if (current.phase === "ready") {
        show({ phase: "degraded", label: RUNTIME_CONNECTION_TEXT.degradedLabel,
               detail: RUNTIME_CONNECTION_TEXT.degradedDetail, canRetry: true, busy: false });
      }
      return;
    }
    if (eventType === "runtime_stopping" && (current.phase === "ready" || current.phase === "degraded")) {
      attempt += 1;  // any older attempt's late result is now stale
      pending = null;
      show(connecting(true));
      armTimer(attempt);  // the supervisor restarts the bridge; nothing is spawned here
    }
  }

  function dispose(): void {
    disposed = true;
    stopTimer();
    pending = null;
  }

  return { start, retry, handleEvent, dispose, view: () => current };
}
