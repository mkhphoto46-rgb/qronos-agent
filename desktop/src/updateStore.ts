/**
 * Offline update lifecycle, desktop side (backend: core/update_cli.py).
 *
 * The desktop only relays what the person chose and what the backend decided:
 * - paths come from native file/folder dialogs, never typed URLs;
 * - an approval needs the token the backend issued for exactly that package
 *   and the person's approval secret, which is sent once and never kept;
 * - a launch needs the backend's single-use launch token, and happens only
 *   after the desktop has really stopped the runtime (the backend proves that
 *   itself through the runtime's mutexes); the app exits after a launch.
 *
 * Tauri command (proposed to Control): update_command(operation, payload),
 * the same shape as permissions_command, running the "update" backend service.
 */

export type UpdateInvoke = (
  command: string,
  args: { operation: string; payload: Record<string, unknown> },
) => Promise<unknown>;

export type UpdateKind = "upgrade" | "repair" | "rollback";

export type UpdateOffer = {
  version: string;
  fromVersion: string;
  kind: UpdateKind;
  keyId: string;
  owner: string;
  packageSha256: string;
  packageBytes: number;
  packageName: string;
  notes: string;
  installer: string;
  manifest: string;
  changedStores: string[];
  snapshotStores: string[];
  restoreSnapshot: string | null;
};

export type UpdateStatus = {
  runningVersion: string;
  trust: { configured: boolean; activeKeys: number; error: string | null };
  phase: "idle" | "staged" | "handed_off";
  staged: { version: string; kind: UpdateKind; packageSha256: string } | null;
  launching: boolean;
  outcome: { result: "applied" | "not_applied"; version: string; dataMismatch: string[] } | null;
  events: { at: number; event: string; version: string | null; detail: string }[];
};

export type UpdateRefusal = { ok: false; code: string; message: string };

export class UpdateRefused extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

/** Persian text for each backend refusal code; unknown codes fall back to a generic line. */
export const REFUSAL_TEXT: Record<string, string> = {
  no_trusted_key: "هیچ کلید امضای مالک تنظیم نشده است؛ هیچ بستهٔ به‌روزرسانی پذیرفته نمی‌شود.",
  untrusted_signature: "این بسته را کلید مورد اعتماد مالک امضا نکرده است.",
  revoked_key: "این بسته فقط با کلیدی امضا شده که باطل شده است.",
  weak_key: "کلید امضای این بسته پذیرفتنی نیست.",
  package_mismatch: "فایل نصب‌کننده با مانیفست امضاشده یکی نیست یا تغییر کرده است.",
  malformed: "مانیفست به‌روزرسانی معتبر نیست.",
  not_a_file: "فایل انتخاب‌شده وجود ندارد یا فایل معمولی نیست.",
  not_a_folder: "پوشهٔ انتخاب‌شده وجود ندارد یا پیوند است.",
  wrong_product: "این بسته برای Qronos نیست.",
  wrong_platform: "این بسته برای این رایانه نیست.",
  unsigned_rollback: "نسخهٔ قدیمی‌تر فقط با بستهٔ بازگشت امضاشده نصب می‌شود.",
  below_security_floor: "این نسخه پایین‌تر از کف امنیتی است و نصب نمی‌شود.",
  upgrade_path: "ابتدا باید نسخهٔ میانی نصب شود.",
  data_schema: "این نسخه نمی‌تواند داده‌های فعلی شما را بخواند.",
  data_epoch: "این نسخه به دورهٔ سازگاری دیگری از داده‌ها تعلق دارد.",
  unsupported_schema: "این بسته داده‌های Qronos را کامل توصیف نمی‌کند.",
  unsupported_migration: "این به‌روزرسانی قالب داده‌ای را تغییر می‌دهد که نمی‌توان از آن نسخهٔ پشتیبان نگه داشت؛ پذیرفته نمی‌شود.",
  rollback_data: "برای این بازگشت، نسخهٔ پشتیبان مناسبی از داده‌ها وجود ندارد؛ بازگشت انجام نمی‌شود.",
  permission_denied: "سیاست مجوزها نصب نرم‌افزار را ممنوع کرده است.",
  approval_secret_missing: "ابتدا در تنظیمات رمز تأیید را تعیین کنید.",
  approval_secret_rejected: "رمز تأیید نادرست است. چیزی تغییر نکرد.",
  approval_secret_unavailable: "رمز تأیید اکنون قابل بررسی نیست.",
  not_approved: "این تأیید ناشناخته، منقضی یا قبلاً استفاده شده است. چیزی تغییر نکرد.",
  approval_expired: "مهلت تأیید تمام شده است؛ دوباره بررسی کنید.",
  approval_mismatch: "فایل‌ها پس از نمایش تغییر کرده‌اند؛ چیزی تأیید نشد.",
  not_staged: "به‌روزرسانی تأییدشده آماده نیست.",
  runtime_running: "Qronos هنوز در حال اجراست؛ ابتدا باید متوقف شود.",
  launch_failed: "ویندوز نتوانست نصب‌کننده را اجرا کند.",
  busy: "بخش دیگری از Qronos در حال تغییر به‌روزرسانی‌هاست؛ دوباره تلاش کنید.",
  store: "پوشهٔ به‌روزرسانی‌ها قابل اعتماد نیست؛ چیزی تغییر نکرد.",
  key_file: "فایل کلیدهای مورد اعتماد معتبر نیست.",
  bad_request: "درخواست نامعتبر است.",
  version_unknown: "نسخهٔ در حال اجرای Qronos معلوم نیست.",
};

export function refusalText(code: string): string {
  return REFUSAL_TEXT[code] ?? "به‌روزرسانی انجام نشد.";
}

const TOKEN = /^[A-Za-z0-9_-]{43}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const KINDS: ReadonlySet<string> = new Set(["upgrade", "repair", "rollback"]);

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new UpdateRefused("bad_response", "The update backend returned an invalid answer.");
  }
  return value as Record<string, unknown>;
}

function parseOffer(value: unknown): UpdateOffer {
  const offer = record(value);
  if (typeof offer.version !== "string" || typeof offer.kind !== "string" || !KINDS.has(offer.kind)
      || typeof offer.packageSha256 !== "string" || !SHA256.test(offer.packageSha256)
      || typeof offer.owner !== "string" || typeof offer.keyId !== "string") {
    throw new UpdateRefused("bad_response", "The update backend returned an invalid offer.");
  }
  return offer as unknown as UpdateOffer;
}

function token(value: unknown): string {
  if (typeof value !== "string" || !TOKEN.test(value)) {
    throw new UpdateRefused("bad_response", "The update backend returned an invalid token.");
  }
  return value;
}

/** The first 16 hex digits, grouped for reading aloud: "a1b2 c3d4 e5f6 0718". */
export function shortDigest(sha256: string): string {
  return sha256.slice(0, 16).replace(/(.{4})(?!$)/g, "$1 ");
}

export type UpdateClient = ReturnType<typeof createUpdateClient>;

// The running version is not sent: the backend takes it from the desktop
// binary's own package information (QRONOS_APP_VERSION), never from here.
export function createUpdateClient(invoke: UpdateInvoke) {
  async function call(operation: string, payload: Record<string, unknown> = {}) {
    const answer = record(await invoke("update_command", { operation, payload }));
    if (answer.ok !== true) {
      const code = typeof answer.code === "string" ? answer.code : "unknown";
      throw new UpdateRefused(code, refusalText(code));
    }
    return answer;
  }

  return {
    async status(): Promise<UpdateStatus> {
      return record((await call("status")).status) as unknown as UpdateStatus;
    },
    async discover(folder: string): Promise<{ installer: string; manifest: string }[]> {
      const packages = (await call("discover", { folder })).packages;
      return Array.isArray(packages) ? packages as { installer: string; manifest: string }[] : [];
    },
    /** Verifies and asks the permission gate; the token is kept only by the caller, in memory. */
    async request(installer: string, manifest: string): Promise<{ offer: UpdateOffer; approvalToken: string }> {
      const answer = await call("request", { installer, manifest });
      return { offer: parseOffer(answer.offer), approvalToken: token(answer.approvalToken) };
    },
    /** Sends the secret once; nothing here keeps it. */
    async approve(approvalToken: string, secret: string): Promise<{ launchToken: string }> {
      const answer = await call("approve", { approvalToken, secret });
      return { launchToken: token(answer.launchToken) };
    },
    async launch(launchToken: string): Promise<void> {
      await call("launch", { launchToken });
    },
    async discard(): Promise<void> {
      await call("discard");
    },
  };
}

/** Stops the runtime, launches, then exits; any refusal brings the runtime back. */
export async function launchUpdate(
  client: UpdateClient,
  launchToken: string,
  hooks: {
    stopRuntime: () => Promise<boolean>;
    restartRuntime: () => Promise<void>;
    exitApp: () => Promise<void>;
  },
): Promise<void> {
  const stopped = await hooks.stopRuntime();
  if (!stopped) {
    throw new UpdateRefused("runtime_running", refusalText("runtime_running"));
  }
  try {
    await client.launch(launchToken);
  } catch (error) {
    await hooks.restartRuntime();
    throw error;
  }
  await hooks.exitApp();
}
