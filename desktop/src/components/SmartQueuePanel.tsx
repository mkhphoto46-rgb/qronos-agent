/**
 * Work Qronos is holding, and why.
 *
 * Purely presentational: no `invoke`, no `listen`, no state of its own beyond
 * what it is handed. App.tsx owns the queue the way it owns everything else,
 * which is the pattern every other view here already follows.
 *
 * The one piece of judgement in it is which rows offer a "run anyway" button.
 * A task held because the machine is busy can be released by pressing it. A
 * task held because the model will not fit cannot be — that is a safety limit
 * — so the button is not shown at all rather than shown and then refused. The
 * Python side decides this and sends `overridable`; nothing here guesses.
 */

import "./SmartQueuePanel.css";

export type QueueTaskState =
  | "queued"
  | "paused"
  | "running"
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
};

export type QueueSnapshot = {
  revision: number;
  paused: boolean;
  level: "unknown" | "free" | "busy";
  holdingSince: number | null;
  tasks: QueueTask[];
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
  onOverride: (taskId: string) => void;
  onCancel: (taskId: string) => void;
  onTogglePaused: (paused: boolean) => void;
  onDismissRefusal: () => void;
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
};

const STATE_LABELS: Record<QueueTaskState, string> = {
  queued: "در انتظار",
  paused: "متوقف",
  running: "در حال اجرا",
  done: "انجام شد",
  failed: "ناموفق",
  cancelled: "لغو شد",
};

const BREACH_KICKERS: Record<string, string> = {
  vram_exhausted: "SAFETY FLOOR — VRAM EXHAUSTED",
  gpu_temperature: "SAFETY FLOOR — GPU TOO HOT",
  system_memory: "SAFETY FLOOR — SYSTEM MEMORY",
};

function toneFor(task: QueueTask): string {
  if (task.state === "running") return "running";
  if (task.state === "failed") return "failed";
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
  onOverride,
  onCancel,
  onTogglePaused,
  onDismissRefusal,
}: SmartQueuePanelProps) {
  const tasks = snapshot?.tasks ?? [];
  const waiting = tasks.filter(
    (task) => task.state === "queued" || task.state === "paused",
  );
  const machineIsBusy = snapshot?.level === "busy";

  return (
    <section className="smart-queue-panel">
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
          onClick={() => onTogglePaused(!snapshot?.paused)}
        >
          {snapshot?.paused ? "HELD" : "HOLD QUEUE"}
        </button>
      </header>

      {machineIsBusy && waiting.length > 0 && (
        <p className="smart-queue-hold-note">
          دستگاه شما شلوغ است؛ کار سنگین نگه داشته شد.
          <span className="smart-queue-kicker">
            SUSTAINED LOAD — WORK HELD
          </span>
        </p>
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

      {tasks.length === 0 ? (
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
                    <dd>{STATE_LABELS[task.state]}</dd>
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

                {(task.state === "queued" || task.state === "paused") && (
                  <button
                    type="button"
                    className="smart-queue-cancel"
                    onClick={() => onCancel(task.taskId)}
                  >
                    لغو
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      <p className="smart-queue-note">
        صف ذخیره نمی‌شود و با بستن برنامه پاک می‌شود.
        <span className="smart-queue-kicker">IN MEMORY ONLY</span>
      </p>
    </section>
  );
}

export default SmartQueuePanel;
