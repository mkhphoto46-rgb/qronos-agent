/**
 * The Device Hub as the desktop sees it (H1, printer chunk).
 *
 * Every operation is one typed `qronos.device_hub` runtime action `{op, requestId, ...}` sent through the registered
 * `send_runtime_action` command; the bridge answers with a `device_hub_result` runtime event carrying the same
 * requestId. Nothing here decides permission or touches a device: core/device_hub.py does, through the Windows
 * spooler and drivers and the Qronos gate.
 *
 * - Only allow-listed operations and bounded fields are sent.
 * - An answer is matched by requestId and operation; a late, stale or foreign answer is ignored. No answer in time
 *   resolves as `timeout`.
 * - A typed approval secret goes straight to the one `decide` send and is never stored here.
 * - Printing itself is approved in the existing print approval dialog (app workflow), not here.
 * - A 3D package (.3mf) goes only to a device whose driver proved 3D capability; it is approved here with the typed
 *   secret, and a success means only "handed to the Windows spooler", never a printed part.
 *
 * No runtime imports: the transport is injected, so this module runs in tests as is.
 */

export type DeviceHubOp =
  | "devices.list" | "device.jobs" | "job.control" | "print.request" | "print3d.request" | "decide" | "decline";

export const DEVICE_HUB_OPS: ReadonlySet<string> = new Set<DeviceHubOp>([
  "devices.list", "device.jobs", "job.control", "print.request", "print3d.request", "decide", "decline",
]);

export const DEVICE_HUB_ACTION_ID = "qronos.device_hub";
export const DEVICE_HUB_RESULT_EVENT = "device_hub_result";
export const MAX_EVENT_CHARS = 1_000_000;
export const DEFAULT_TIMEOUT_MS = 60_000;
export const JOB_COMMANDS = ["pause", "resume", "cancel"] as const;
export type JobCommand = (typeof JOB_COMMANDS)[number];
export const COLORS = ["default", "color", "monochrome"] as const;
export type PrintColor = (typeof COLORS)[number];

export type DeviceHubResult = { requestId: string; op: DeviceHubOp; ok: boolean; code?: string; [field: string]: unknown };

export type DeviceCapabilities = {
  copiesMax?: number | null;
  color?: boolean;
  duplex?: boolean;
  collate?: boolean;
  paperSizes?: string[];
  /** 3D printers only, as the driver declares them (microns). */
  buildVolumeMicrons?: { width: number; depth: number; height: number } | null;
  threeMfVersion?: string;
  threeMfExtensions?: string[];
};

export type Device = {
  id: string;
  name: string;
  kind: "printer" | "3d_printer";
  connection: "local" | "network";
  shared: boolean;
  isDefault: boolean;
  driver: string;
  status: { problems: string[]; warnings: string[]; ready: boolean };
  queuedJobs: number;
  capabilities: DeviceCapabilities;
  workflows: string[];
  unsupported: string;
};

export type DeviceJob = { jobId: number; document: string; own: boolean; status: string[]; pagesPrinted: number; totalPages: number };
export type UnsupportedClass = { kind: string; reason: string; detail: string };
export type DeviceApproval = { approvalId: string; needsSecret: boolean; preview: Record<string, unknown> };
export type RuntimeEvent = { eventType: string; status: string; message: string };

export type DeviceHubTransport = {
  send(actionId: string, data: Record<string, unknown>): Promise<void>;
  listen(handler: (event: RuntimeEvent) => void): Promise<() => void>;
  setTimer(callback: () => void, ms: number): unknown;
  clearTimer(handle: unknown): void;
};

const REQUEST_ID = /^[A-Za-z0-9_-]{1,64}$/;
const DEVICE_ID = /^prn-[0-9a-f]{20}$/;
const APPROVAL_ID = /^[A-Za-z0-9_-]{8,128}$/;
const PAGES = /^(all|[0-9]{1,5}(-[0-9]{1,5})?(,[0-9]{1,5}(-[0-9]{1,5})?){0,31})$/;
const MAX_PATH_CHARS = 4096;
const MAX_SECRET_CHARS = 256;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown, limit: number): string {
  return typeof value === "string" ? value.slice(0, limit) : "";
}

function strings(value: unknown, limit: number): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").slice(0, limit) : [];
}

export function isDeviceId(value: unknown): value is string {
  return typeof value === "string" && DEVICE_ID.test(value);
}

/** A runtime event's Device Hub answer, or null when it is not a well-formed one. */
export function parseDeviceHubEvent(event: unknown): DeviceHubResult | null {
  if (!isRecord(event) || event.eventType !== DEVICE_HUB_RESULT_EVENT || typeof event.message !== "string") return null;
  if (event.message.length > MAX_EVENT_CHARS) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(event.message);
  } catch {
    return null;
  }
  if (!isRecord(payload) || typeof payload.requestId !== "string" || !REQUEST_ID.test(payload.requestId)) return null;
  if (typeof payload.op !== "string" || !DEVICE_HUB_OPS.has(payload.op) || typeof payload.ok !== "boolean") return null;
  return payload as DeviceHubResult;
}

/** The devices of a `devices.list` answer, validated field by field (anything malformed is dropped). */
export function devicesOf(result: DeviceHubResult): Device[] {
  if (!result.ok || !Array.isArray(result.devices)) return [];
  const out: Device[] = [];
  for (const item of result.devices.slice(0, 64)) {
    if (!isRecord(item) || !isDeviceId(item.id) || typeof item.name !== "string") continue;
    const status = isRecord(item.status) ? item.status : {};
    const caps = isRecord(item.capabilities) ? item.capabilities : {};
    const volume = isRecord(caps.buildVolumeMicrons) ? caps.buildVolumeMicrons : null;
    const dimensions = volume && [volume.width, volume.depth, volume.height].every(
      (value) => typeof value === "number" && Number.isInteger(value) && value > 0);
    out.push({
      id: item.id,
      name: text(item.name, 300),
      kind: item.kind === "3d_printer" ? "3d_printer" : "printer",
      connection: item.connection === "network" ? "network" : "local",
      shared: item.shared === true,
      isDefault: item.isDefault === true,
      driver: text(item.driver, 200),
      status: { problems: strings(status.problems, 16), warnings: strings(status.warnings, 16), ready: status.ready === true },
      queuedJobs: typeof item.queuedJobs === "number" ? item.queuedJobs : 0,
      capabilities: {
        copiesMax: typeof caps.copiesMax === "number" ? caps.copiesMax : null,
        color: caps.color === true,
        duplex: caps.duplex === true,
        collate: caps.collate === true,
        paperSizes: strings(caps.paperSizes, 32),
        buildVolumeMicrons: volume && dimensions
          ? { width: volume.width as number, depth: volume.depth as number, height: volume.height as number } : null,
        threeMfVersion: text(caps.threeMfVersion, 200),
        threeMfExtensions: strings(caps.threeMfExtensions, 16),
      },
      workflows: strings(item.workflows, 8),
      unsupported: text(item.unsupported, 64),
    });
  }
  return out;
}

export function jobsOf(result: DeviceHubResult): DeviceJob[] {
  if (!result.ok || !Array.isArray(result.jobs)) return [];
  return result.jobs.slice(0, 100).filter(isRecord).filter((job) => typeof job.jobId === "number").map((job) => ({
    jobId: job.jobId as number,
    document: text(job.document, 200),
    own: job.own === true,
    status: strings(job.status, 12),
    pagesPrinted: typeof job.pagesPrinted === "number" ? job.pagesPrinted : 0,
    totalPages: typeof job.totalPages === "number" ? job.totalPages : 0,
  }));
}

export function unsupportedClassesOf(result: DeviceHubResult): UnsupportedClass[] {
  if (!result.ok || !Array.isArray(result.unsupportedClasses)) return [];
  return result.unsupportedClasses.slice(0, 16).filter(isRecord).map((item) => ({
    kind: text(item.kind, 64), reason: text(item.reason, 64), detail: text(item.detail, 300),
  }));
}

/** The approval a result asks for, validated; null when it asks for none. */
export function approvalOf(result: DeviceHubResult): DeviceApproval | null {
  if (result.status !== "approval_required" || typeof result.approvalId !== "string") return null;
  if (!APPROVAL_ID.test(result.approvalId)) return null;
  return { approvalId: result.approvalId, needsSecret: result.needsSecret === true,
           preview: isRecord(result.preview) ? result.preview : {} };
}

/** Persian words for the stable refusal codes (anything else is shown with its code). */
export const DEVICE_REFUSAL_TEXT: Record<string, string> = {
  paused: "Qronos متوقف است؛ بعد از ادامه دوباره امتحان کنید.",
  stale_approval: "این تأیید دیگر معتبر نیست؛ درخواست را دوباره بفرستید.",
  secret_rejected: "رمز تأیید درست نبود؛ درخواست را دوباره بفرستید.",
  denied: "سیاست دسترسی دستگاه‌ها این کار را رد کرد.",
  not_own_job: "فقط کارهای چاپ خودتان را می‌توانید تغییر دهید.",
  job_gone: "این کار دیگر در صف چاپ نیست.",
  job_changed: "این کار با کاری که تأیید شد یکی نیست؛ چیزی تغییر نکرد.",
  device_gone: "این دستگاه دیگر در ویندوز نصب نیست.",
  unsupported: "این دستگاه این کار را پشتیبانی نمی‌کند.",
  over_capability: "این درخواست از توان این دستگاه بیشتر است.",
  spooler_unavailable: "سرویس چاپ ویندوز در دسترس نیست.",
  spooler_refused: "سرویس چاپ ویندوز این تغییر را نپذیرفت.",
  timeout: "پاسخی به‌موقع نرسید؛ فهرست را دوباره بخوانید.",
  stale: "این پاسخ مربوط به نمای قبلی بود.",
  closed: "ارتباط با Qronos بسته شد.",
  bad_request: "درخواست نامعتبر بود.",
  internal: "خطای داخلی؛ جزئیات در گزارش‌های Qronos است.",
  secret_unavailable: "برای چاپ سه‌بعدی، رمز تأیید Qronos باید تنظیم شده باشد.",
  file_missing: "این فایل وجود ندارد.",
  file_empty: "این فایل خالی است.",
  file_unreadable: "این فایل خوانده نشد.",
  link_refused: "فایل یا یکی از پوشه‌هایش پیوند (link) است؛ پذیرفته نشد.",
  too_large: "بسته یا بخشی از آن بیش از حد بزرگ است.",
  bad_package: "این فایل یک بستهٔ 3MF سالم و خودبسنده نیست؛ چیزی فرستاده نشد.",
  incompatible_3mf_version: "نسخهٔ 3MF این مدل با نسخه‌ای که درایور چاپگر اعلام کرده یکی نیست.",
  unsupported_3mf_extension: "این مدل افزونه‌ای از 3MF لازم دارد که درایور چاپگر اعلام نکرده است.",
  capability_gone: "درایور دیگر توان چاپ سه‌بعدی را اعلام نمی‌کند؛ چیزی فرستاده نشد.",
  file_changed: "فایل بعد از تأیید تغییر کرده است؛ چیزی فرستاده نشد.",
  not_3d_target: "صف چاپ ویندوز بستهٔ سه‌بعدی را نپذیرفت؛ چیزی فرستاده نشد.",
  native_refused: "ویندوز تحویل بستهٔ سه‌بعدی را رد کرد؛ کار نیمه‌کاره لغو شد.",
  unavailable: "چاپ سه‌بعدی فقط از راه ویندوز در دسترس است.",
};

/** Persian words for a device's stated limit (Device.unsupported). */
export const DEVICE_LIMIT_TEXT: Record<string, string> = {
  "3d_capability_unproven":
    "نام یا درایور این صف به چاپگر سه‌بعدی اشاره می‌کند، اما درایورش توان چاپ سه‌بعدی (کلیدواژه‌های 3D در PrintCapabilities) را اعلام نمی‌کند؛ نه چاپ سه‌بعدی و نه چاپ سند پیشنهاد نمی‌شود.",
  driver_capabilities_unreadable: "توانایی‌های این چاپگر از درایور خوانده نشد؛ چاپ پیشنهاد نمی‌شود.",
};

export function limitText(code: string): string {
  return DEVICE_LIMIT_TEXT[code] ?? code;
}

export function refusalText(code: unknown): string {
  return typeof code === "string" && code in DEVICE_REFUSAL_TEXT ? DEVICE_REFUSAL_TEXT[code] : `رد شد (${String(code)})`;
}

export type PrintFields = { deviceId: string; path: string; copies: number; color: PrintColor; pages: string };

/** Why a print form cannot be sent (Persian), or null when it is well-formed for this device. */
export function printProblem(device: Device | undefined, fields: PrintFields): string | null {
  if (!device || device.id !== fields.deviceId) return "یک چاپگر انتخاب کنید.";
  if (!device.workflows.includes("print_document")) return DEVICE_REFUSAL_TEXT.unsupported;
  const path = fields.path.trim();
  if (!path || path.length > MAX_PATH_CHARS || !/^([A-Za-z]:[\\/]|\\\\)/.test(path)) {
    return "مسیر کامل یک فایل را وارد کنید (مثلاً C:\\Users\\...\\file.pdf).";
  }
  if (!Number.isInteger(fields.copies) || fields.copies < 1 || fields.copies > 99) return "تعداد نسخه باید ۱ تا ۹۹ باشد.";
  const max = device.capabilities.copiesMax;
  if (typeof max === "number" && fields.copies > max) return `این چاپگر حداکثر ${max} نسخه چاپ می‌کند.`;
  if (fields.color === "color" && device.capabilities.color === false) return "این چاپگر رنگی چاپ نمی‌کند.";
  if (!PAGES.test(fields.pages.trim())) return "صفحه‌ها: all یا محدوده‌هایی مثل 1-3,5";
  return null;
}

export type Print3DFields = { deviceId: string; path: string };

/** Why a 3D form cannot be sent (Persian), or null when it is well-formed for this device. */
export function print3dProblem(device: Device | undefined, fields: Print3DFields): string | null {
  if (!device || device.id !== fields.deviceId) return "یک چاپگر سه‌بعدی انتخاب کنید.";
  if (device.kind !== "3d_printer" || !device.workflows.includes("print_3d")) return DEVICE_REFUSAL_TEXT.unsupported;
  const path = fields.path.trim();
  if (!path || path.length > MAX_PATH_CHARS || !/^([A-Za-z]:[\\/]|\\\\)/.test(path) || !/\.3mf$/i.test(path)) {
    return "مسیر کامل یک فایل ‎.3mf‎ را وارد کنید.";
  }
  return null;
}

/** The truthful words for a 3D handoff: Windows accepted the package; nothing is said about a printed part. */
export function handoffText(result: DeviceHubResult): string {
  const job = typeof result.jobId === "number" ? ` (کار ${result.jobId} در صف)` : "";
  return `بستهٔ 3MF به صف چاپ ویندوز سپرده شد${job}. این به معنای چاپ شدن قطعه نیست؛ وضعیت را روی خود دستگاه ببینید.`;
}

export type DeviceHubClient = {
  list(): Promise<DeviceHubResult>;
  jobs(deviceId: string): Promise<DeviceHubResult>;
  controlJob(deviceId: string, jobId: number, command: JobCommand): Promise<DeviceHubResult>;
  print(fields: PrintFields): Promise<DeviceHubResult>;
  /** Ask to hand one exact .3mf to a proven 3D printer; the answer asks for an approval with the typed secret. */
  print3d(fields: Print3DFields): Promise<DeviceHubResult>;
  /** The person's yes for one approval this client was shown; the secret goes to this one send only. */
  decide(approval: DeviceApproval, secret?: string): Promise<DeviceHubResult>;
  decline(approval: DeviceApproval): Promise<DeviceHubResult>;
  /** Pending answers resolve as `stale`; late answers are ignored. */
  reset(): void;
  pending(): number;
  dispose(): void;
};

type Waiter = { resolve: (result: DeviceHubResult) => void; timer: unknown; generation: number; op: DeviceHubOp };

export function createDeviceHubClient(transport: DeviceHubTransport, options: { timeoutMs?: number } = {}): DeviceHubClient {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const waiters = new Map<string, Waiter>();
  let generation = 0;
  let counter = 0;
  let unlisten: (() => void) | null = null;
  let listening: Promise<void> | null = null;

  function handle(event: RuntimeEvent): void {
    const result = parseDeviceHubEvent(event);
    if (result === null) return;
    const waiter = waiters.get(result.requestId);
    if (!waiter || waiter.generation !== generation || waiter.op !== result.op) return; // late, stale or foreign
    waiters.delete(result.requestId);
    transport.clearTimer(waiter.timer);
    waiter.resolve(result);
  }

  function ensureListening(): Promise<void> {
    if (unlisten) return Promise.resolve();
    if (!listening) {
      listening = transport.listen(handle).then((cleanup) => {
        unlisten = cleanup;
      });
    }
    return listening;
  }

  async function send(op: DeviceHubOp, fields: Record<string, unknown>): Promise<DeviceHubResult> {
    await ensureListening();
    counter = (counter + 1) % 1_000_000_000;
    const requestId = `d${generation.toString(36)}x${counter.toString(36)}`;
    const mine = generation;
    const answer = new Promise<DeviceHubResult>((resolve) => {
      const timer = transport.setTimer(() => {
        if (waiters.get(requestId)) {
          waiters.delete(requestId);
          resolve({ requestId, op, ok: false, code: "timeout" });
        }
      }, timeoutMs);
      waiters.set(requestId, { resolve, timer, generation: mine, op });
    });
    try {
      await transport.send(DEVICE_HUB_ACTION_ID, { ...fields, op, requestId });
    } catch {
      const waiter = waiters.get(requestId);
      if (waiter) {
        waiters.delete(requestId);
        transport.clearTimer(waiter.timer);
        waiter.resolve({ requestId, op, ok: false, code: "closed" });
      }
    }
    return answer;
  }

  function refuse(op: DeviceHubOp, code: string): Promise<DeviceHubResult> {
    return Promise.resolve({ requestId: "local", op, ok: false, code });
  }

  return {
    list: () => send("devices.list", {}),
    jobs: (deviceId) => (isDeviceId(deviceId) ? send("device.jobs", { deviceId }) : refuse("device.jobs", "bad_request")),
    controlJob(deviceId, jobId, command) {
      if (!isDeviceId(deviceId) || !Number.isInteger(jobId) || jobId < 1 || !JOB_COMMANDS.includes(command)) {
        return refuse("job.control", "bad_request");
      }
      return send("job.control", { deviceId, jobId, command });
    },
    print(fields) {
      if (!isDeviceId(fields.deviceId) || !COLORS.includes(fields.color) || !PAGES.test(fields.pages.trim())) {
        return refuse("print.request", "bad_request");
      }
      return send("print.request", { deviceId: fields.deviceId, path: fields.path.trim(), copies: fields.copies,
                                     color: fields.color, pages: fields.pages.trim() });
    },
    print3d(fields) {
      const path = fields.path.trim();
      if (!isDeviceId(fields.deviceId) || !path || path.length > MAX_PATH_CHARS || !/\.3mf$/i.test(path)) {
        return refuse("print3d.request", "bad_request");
      }
      return send("print3d.request", { deviceId: fields.deviceId, path });
    },
    decide(approval, secret) {
      const fields: Record<string, unknown> = { approvalId: approval.approvalId, approved: true };
      if (approval.needsSecret && typeof secret === "string" && secret.length > 0 && secret.length <= MAX_SECRET_CHARS) {
        fields.secret = secret;
      }
      return send("decide", fields);
    },
    decline: (approval) => send("decline", { approvalId: approval.approvalId }),
    reset() {
      generation += 1;
      for (const [requestId, waiter] of waiters) {
        transport.clearTimer(waiter.timer);
        waiter.resolve({ requestId, op: waiter.op, ok: false, code: "stale" });
      }
      waiters.clear();
    },
    pending: () => waiters.size,
    dispose() {
      this.reset();
      if (unlisten) unlisten();
      unlisten = null;
    },
  };
}
