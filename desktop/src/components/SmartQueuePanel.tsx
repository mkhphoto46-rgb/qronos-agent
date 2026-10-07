/**
 * Work Qronos is holding, and why.
 *
 * Purely presentational: no `invoke`, no `listen`, no state of its own beyond
 * what it is handed. `queueStore.ts` owns the queue and `QueueDrawer.tsx`
 * wires the two together, the pattern every other view here already follows.
 *
 * The judgement in it is which controls each row offers, and none of it is
 * guessed here: the Python side sends `overridable`, `cancellable` and
 * `resumable`. A task held because the model will not fit cannot be run
 * anyway — that is a safety limit — so the button is not shown at all rather
 * than shown and then refused. An interrupted request offers Resume and
 * nothing that would run it by itself.
 */

import "./SmartQueuePanel.css";

export type QueueTaskState =
  | "queued"
  | "paused"
  | "running"
  | "interrupted"
  | "done"
  | "failed"
  | "cancelled";

export type QueueTask = {
  taskId: string;
  summary: string;
  weight: "light" | "heavy";
  state: QueueTaskState;
  attempts: number;
  queuedAt: number;
  heldReason: string | null;
  detail: string;
  overridable: boolean;
  override: boolean;
  startedAt?: number | null;
  // Cancelled, but its local model call has not returned yet.
  inFlight?: boolean;
  cancellable?: boolean;
  resumable?: boolean;
  // Queued conversation requests carry the request they answer; the answer
  // itself is shown in that conversation, not here.
  kind?: "conversation";
  requestId?: string;
  route?: "fast" | "heavy";
};

export type QueueOutcome = {
  taskId: string;
  requestId: string;
  summary: string;
  route: string;
  state: "done" | "failed" | "cancelled";
  detail: string;
  finishedAt: number;
};

export type QueueStorage = {
  durable: boolean;
  problem: string | null;
  detail: string;
};

export type QueueSnapshot = {
  revision: number;
  paused: boolean;
  runtimePaused?: boolean;
  level: "unknown" | "free" | "busy";
  holdingSince: number | null;
  tasks: QueueTask[];
  storage?: QueueStorage | null;
  recent?: QueueOutcome[];
};

export type QueueRefusal = {
  taskId?: string;
  breach?: string | null;
  detail?: string;
  requiredVramMb?: number;
  freeVramMb?: number | null;
};

type SmartQueuePanelProps = {
  snapshot: QueueSnapshot | null;
  refusal: QueueRefusal | null;
  notice?: string | null;
  onOverride: (taskId: string) => void;
  onCancel: (taskId: string) => void;
  onPause?: (taskId: string) => void;
  onResume?: (taskId: string) => void;
  onTogglePaused: (paused: boolean) => void;
  onDismissRefusal: () => void;
  onDismissNotice?: () => void;
  onShowRequest?: (requestId: string) => void;
};

/**
 * Why a task is waiting, in Persian.
 *
 * The reason arrives from `core/` as a code rather than a sentence, so the
 * wording lives here and can change without anything in Python caring.
 */
const REASON_LABELS: Record<string, string> = {
  sustained_load: "دستگاه شما شلوغ است",
  warming_up: "در حال سنجش وضعیت دستگاه",
  heavy_task_in_progress: "یک کار سنگین در حال اجراست",
  paused: "صف متوقف شده است",
  safety_floor: "حد ایمنی اجازه نمی‌دهد",
  foreground_active: "Qronos در حال پاسخ به شماست",
  storage_unavailable: "صف ذخیره نمی‌شود؛ اجرا تا رفع مشکل صبر می‌کند",
  activity_mode: "حالت بازی یا تولید محتوا فعال است",
};

const STATE_LABELS: Record<QueueTaskState, string> = {
  queued: "در انتظار",
  paused: "متوقف",
  running: "در حال اجرا",
  interrupted: "قطع شد",
  done: "انجام شد",
  failed: "ناموفق",
  cancelled: "لغو شد",
};

const STORAGE_PROBLEMS: Record<string, string> = {
  corrupt: "فایل ذخیره‌ی صف خراب است و دست‌نخورده نگه داشته شد.",
  unreadable: "فایل ذخیره‌ی صف خوانده نشد و دست‌نخورده نگه داشته شد.",
  too_new: "فایل صف با نسخه‌ی جدیدتری ساخته شده و دست‌نخورده ماند.",
  no_migration: "فایل صف از نسخه‌ای است که خوانده نمی‌شود.",
  unwritable: "صف روی دیسک ذخیره نشد.",
  unavailable: "جایی برای ذخیره‌ی صف در دسترس نیست.",
};

const BREACH_KICKERS: Record<string, string> = {
  vram_exhausted: "SAFETY FLOOR — VRAM EXHAUSTED",
  gpu_temperature: "SAFETY FLOOR — GPU TOO HOT",
  system_memory: "SAFETY FLOOR — SYSTEM MEMORY",
};

function toneFor(task: QueueTask): string {
  if (task.state === "running") return "running";
  if (task.state === "failed" || task.state === "interrupted") return "failed";
  if (task.heldReason === "safety_floor") return "held";
  return "waiting";
}

function waitedFor(queuedAt: number): string {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - queuedAt));

  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;

  return `${Math.floor(seconds / 3600)}h`;
}

export function SmartQueuePanel({
  snapshot,
  refusal,
  notice = null,
  onOverride,
  onCancel,
  onPause,
  onResume,
  onTogglePaused,
  onDismissRefusal,
  onDismissNotice,
  onShowRequest,
}: SmartQueuePanelProps) {
  const tasks = snapshot?.tasks ?? [];
  const recent = snapshot?.recent ?? [];
  const storage = snapshot?.storage ?? null;
  const waiting = tasks.filter(
    (task) => task.state === "queued" || task.state === "paused",
  );
  const interrupted = tasks.filter((task) => task.state === "interrupted");
  const machineIsBusy = snapshot?.level === "busy";

  return (
    <section className="smart-queue-panel" aria-label="Smart Queue">
      <header className="smart-queue-head">
        <div>
          <h3>کارهای در انتظار دستگاه آزاد</h3>
          <span className="smart-queue-kicker">SMART QUEUE</span>
        </div>

        <button
          type="button"
          className={
            "smart-queue-hold-toggle" +
            (snapshot?.paused ? " smart-queue-hold-toggle-active" : "")
          }
          aria-pressed={Boolean(snapshot?.paused)}
          disabled={!snapshot}
          onClick={() => onTogglePaused(!snapshot?.paused)}
        >
          {snapshot?.paused ? "RESUME QUEUE" : "HOLD QUEUE"}
        </button>
      </header>

      {!snapshot && (
        <p className="smart-queue-empty">
          در انتظار پاسخ Runtime…
          <span className="smart-queue-kicker">CONNECTING</span>
        </p>
      )}

      {snapshot?.runtimePaused && (
        <p className="smart-queue-hold-note">
          Qronos متوقف است؛ تا ادامه، هیچ کاری از صف اجرا نمی‌شود.
          <span className="smart-queue-kicker">QRONOS PAUSED</span>
        </p>
      )}

      {machineIsBusy && waiting.length > 0 && (
        <p className="smart-queue-hold-note">
          دستگاه شما شلوغ است؛ کار سنگین نگه داشته شد.
          <span className="smart-queue-kicker">
            SUSTAINED LOAD — WORK HELD
          </span>
        </p>
      )}

      {interrupted.length > 0 && (
        <p className="smart-queue-hold-note">
          {interrupted.length} درخواست هنگام بسته شدن Qronos نیمه‌کاره ماند.
          تا شما «ادامه» را نزنید دوباره اجرا نمی‌شود.
          <span className="smart-queue-kicker">
            INTERRUPTED — WAITING FOR YOU
          </span>
        </p>
      )}

      {storage && !storage.durable && (
        <div className="smart-queue-refusal" role="alert">
          <p>
            {STORAGE_PROBLEMS[storage.problem ?? ""] ??
              "صف ذخیره نمی‌شود."}{" "}
            اگر Qronos بسته شود، کارهای صف از دست می‌روند.
          </p>
          <span className="smart-queue-kicker">
            NOT SAVED — {(storage.problem ?? "unknown").toUpperCase()}
          </span>
          {storage.detail && (
            <p className="smart-queue-refusal-detail">{storage.detail}</p>
          )}
        </div>
      )}

      {notice && (
        <div className="smart-queue-refusal" role="status">
          <p className="smart-queue-refusal-detail">{notice}</p>
          {onDismissNotice && (
            <button
              type="button"
              className="smart-queue-refusal-dismiss"
              onClick={onDismissNotice}
            >
              باشه
            </button>
          )}
        </div>
      )}

      {refusal && (
        <div className="smart-queue-refusal" role="status">
          <p>اجرای اجباری اعمال نشد.</p>
          <span className="smart-queue-kicker">
            {BREACH_KICKERS[refusal.breach ?? ""] ?? "SAFETY FLOOR"}
          </span>
          {/* The numbers matter. A refusal that says only "not enough
              memory" invites the user to press the button again. */}
          <p className="smart-queue-refusal-detail">{refusal.detail}</p>
          <button
            type="button"
            className="smart-queue-refusal-dismiss"
            onClick={onDismissRefusal}
          >
            باشه
          </button>
        </div>
      )}

      {snapshot && tasks.length === 0 ? (
        <p className="smart-queue-empty">
          چیزی در صف نیست.
          <span className="smart-queue-kicker">Nothing is queued.</span>
        </p>
      ) : (
        <ul className="smart-queue-list">
          {tasks.map((task) => (
            <li
              key={task.taskId}
              className={`smart-queue-row smart-queue-row-${toneFor(task)}`}
            >
              <span className="smart-queue-row-time">
                {waitedFor(task.queuedAt)}
              </span>

              <div className="smart-queue-row-main">
                <p className="smart-queue-row-summary">{task.summary}</p>

                <dl className="smart-queue-row-meta">
                  <div>
                    <dt>STATE</dt>
                    <dd>
                      {task.inFlight
                        ? "در حال توقف"
                        : STATE_LABELS[task.state]}
                    </dd>
                  </div>
                  <div>
                    <dt>WEIGHT</dt>
                    <dd>{task.weight === "heavy" ? "سنگین" : "سبک"}</dd>
                  </div>
                  {task.heldReason && (
                    <div>
                      <dt>HELD</dt>
                      <dd>
                        {REASON_LABELS[task.heldReason] ?? task.heldReason}
                      </dd>
                    </div>
                  )}
                </dl>

                {task.detail && (
                  <p className="smart-queue-row-detail">{task.detail}</p>
                )}
              </div>

              <div className="smart-queue-actions">
                {task.overridable && (
                  <button
                    type="button"
                    className="smart-queue-override"
                    onClick={() => onOverride(task.taskId)}
                  >
                    اجرا به‌هرحال
                  </button>
                )}

                {task.resumable && onResume && (
                  <button
                    type="button"
                    className="smart-queue-override"
                    onClick={() => onResume(task.taskId)}
                  >
                    ادامه
                  </button>
                )}

                {task.state === "queued" && onPause && (
                  <button
                    type="button"
                    className="smart-queue-cancel"
                    onClick={() => onPause(task.taskId)}
                  >
                    توقف
                  </button>
                )}

                {(task.cancellable ??
                  (task.state === "queued" || task.state === "paused")) &&
                  !task.inFlight && (
                    <button
                      type="button"
                      className="smart-queue-cancel"
                      onClick={() => onCancel(task.taskId)}
                    >
                      لغو
                    </button>
                  )}

                {task.requestId && onShowRequest && (
                  <button
                    type="button"
                    className="smart-queue-cancel"
                    onClick={() => onShowRequest(task.requestId ?? "")}
                  >
                    پیام
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      {recent.length > 0 && (
        <>
          <span className="smart-queue-kicker">EARLIER OUTCOMES</span>
          <ul className="smart-queue-list">
            {recent.map((outcome) => (
              <li
                key={outcome.taskId}
                className={
                  "smart-queue-row " +
                  (outcome.state === "failed"
                    ? "smart-queue-row-failed"
                    : "smart-queue-row-waiting")
                }
              >
                <span className="smart-queue-row-time">
                  {waitedFor(outcome.finishedAt)}
                </span>

                <div className="smart-queue-row-main">
                  <p className="smart-queue-row-summary">{outcome.summary}</p>
                  <dl className="smart-queue-row-meta">
                    <div>
                      <dt>OUTCOME</dt>
                      <dd>{STATE_LABELS[outcome.state]}</dd>
                    </div>
                  </dl>
                  {outcome.detail && (
                    <p className="smart-queue-row-detail">{outcome.detail}</p>
                  )}
                </div>

                <div className="smart-queue-actions">
                  {onShowRequest && (
                    <button
                      type="button"
                      className="smart-queue-cancel"
                      onClick={() => onShowRequest(outcome.requestId)}
                    >
                      پیام
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      <p className="smart-queue-note">
        {storage?.durable
          ? "صف روی همین دستگاه ذخیره می‌شود. کاری که هنگام بسته شدن در حال اجرا باشد، تا شما «ادامه» را نزنید دوباره اجرا نمی‌شود."
          : "صف فعلاً فقط در حافظه است و با بستن برنامه پاک می‌شود."}
        <span className="smart-queue-kicker">
          {storage?.durable ? "SAVED ON THIS DEVICE" : "IN MEMORY ONLY"}
        </span>
      </p>
    </section>
  );
}

export default SmartQueuePanel;
