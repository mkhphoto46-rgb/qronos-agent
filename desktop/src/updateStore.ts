/**
 * Offline update lifecycle, desktop side (backend: core/update_cli.py).
 *
 * The desktop only relays what the person chose and what the backend decided:
 * - paths come from native file/folder dialogs, never typed URLs;
 * - an approval needs the token the backend issued for exactly that package
 *   and the person's approval secret, which is sent once and never kept;
 * - a launch needs the backend's single-use launch token, and happens only
 *   after the desktop has really stopped the runtime (the backend proves that
 *   itself); the app exits after a launch. The backend refuses launching
 *   (launch_unavailable) until its runtime/desktop dependencies are accepted.
 * - Only a typed refusal that proves no installer started may bring the
 *   runtime back. Any other failure (a lost transport, an unknown answer) is
 *   "outcome unknown": the installer may be running, so no competing writer
 *   is started and the person is told so.
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

export type UpdatePhase = "idle" | "staged" | "handed_off";

/** applied: a version change proves the new build runs. unconfirmed: a same-version repair
 *  whose installer may not have run (kept staged). not_applied: cancelled, failed or crashed. */
export type UpdateOutcomeResult = "applied" | "not_applied" | "unconfirmed";

export type UpdateOutcome = {
  result: UpdateOutcomeResult;
  kind: UpdateKind;
  version: string;
  dataMismatch: string[];
  dataRestored: boolean;
};

/** A repair the start-up barrier resolved as unconfirmed (no valid installation receipt). A restart
 *  cannot change it; the person discards it, or retries: discard, then check the package again
 *  (a new request, approval and launch). The old launch is never replayed. */
export type UpdateUnconfirmed = {
  version: string;
  kind: UpdateKind;
  receipt: "absent" | "invalid";
  actions: Array<"discard" | "retry">;
};

export type UpdateStatus = {
  runningVersion: string;
  trust: { configured: boolean; activeKeys: number; error: string | null };
  launchAvailable: boolean;
  phase: UpdatePhase;
  staged: { version: string; kind: UpdateKind; packageSha256: string } | null;
  launchReady: boolean;
  updateInProgress: boolean;
  launching: boolean;
  recoveryPending: boolean;
  outcome: UpdateOutcome | null;
  unconfirmed: UpdateUnconfirmed | null;
};

export type UpdateCandidate = { installer: string; manifest: string };

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
  launch_outcome_unknown: "معلوم نیست نصب‌کننده اجرا شده باشد؛ نتیجه هنگام شروع دوبارهٔ Qronos مشخص می‌شود.",
  launch_unavailable: "اجرای نصب‌کننده هنوز فعال نشده است؛ بستهٔ تأییدشده آماده باقی می‌ماند.",
  update_in_progress: "یک به‌روزرسانی هم‌اکنون در حال اجراست؛ پس از پایان آن دوباره تلاش کنید.",
  writers_active: "بخش دیگری از Qronos هنوز داده‌ها را باز نگه داشته است؛ ابتدا باید بسته شود.",
  recovery_pending: "یک به‌روزرسانی نیمه‌کاره باید هنگام شروع دوبارهٔ Qronos بازیابی شود.",
  recovery_failed: "بازگرداندن داده‌های ذخیره‌شده ناموفق بود؛ Qronos تا رفع این مشکل داده‌ها را باز نمی‌کند.",
  bad_response: "پاسخ بخش به‌روزرسانی معتبر نبود.",
  busy: "بخش دیگری از Qronos در حال تغییر به‌روزرسانی‌هاست؛ دوباره تلاش کنید.",
  store: "پوشهٔ به‌روزرسانی‌ها قابل اعتماد نیست؛ چیزی تغییر نکرد.",
  key_file: "فایل کلیدهای مورد اعتماد معتبر نیست.",
  bad_request: "درخواست نامعتبر است.",
  version_unknown: "نسخهٔ در حال اجرای Qronos معلوم نیست.",
};

export function refusalText(code: string): string {
  return REFUSAL_TEXT[code] ?? "به‌روزرسانی انجام نشد.";
}

/** Typed refusals that prove the backend started no installer and changed no data: only these may
 *  bring the runtime back after it was stopped for a launch. */
export const LAUNCH_NOT_STARTED: ReadonlySet<string> = new Set([
  "launch_unavailable", "runtime_running", "update_in_progress", "writers_active", "recovery_pending",
  "not_approved", "approval_expired", "permission_denied", "not_staged",
  // The backend raises launch_failed ONLY when Windows created no installer process (NotStarted);
  // every ambiguous failure is launch_outcome_unknown, which is not in this set.
  "launch_failed",
]);

/** Of those, the ones raised before the single-use launch token was spent: keep the token. */
export const LAUNCH_TOKEN_KEPT: ReadonlySet<string> = new Set([
  "launch_unavailable", "runtime_running", "update_in_progress", "writers_active",
]);

/** A launch whose result is not known (transport lost, unknown answer): the installer may be running. */
export class UpdateOutcomeUnknown extends Error {
  readonly code = "outcome_unknown";
  constructor() {
    super("نتیجهٔ اجرای نصب‌کننده معلوم نیست. Qronos دوباره راه‌اندازی نمی‌شود؛ اگر نصب‌کننده باز است آن را "
      + "تمام کنید و سپس Qronos را باز کنید.");
  }
}

const TOKEN = /^[A-Za-z0-9_-]{43}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const KEY_ID = /^[0-9a-f]{32}$/;
const KINDS: ReadonlySet<string> = new Set(["upgrade", "repair", "rollback"]);
// Semantic Versioning 2.0 (as core.local_update.parse_version), at most 128 characters.
const VERSION = /^\d{1,9}\.\d{1,9}\.\d{1,9}(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/;
const STORE_ID = /^[a-z][a-z0-9_]{0,47}$/;
const SNAPSHOT_ID = /^\d{10}-[0-9a-f]{12}$/;
const PACKAGE_NAME = /^qronos-desktop_[0-9A-Za-z.+-]{1,128}_x64-setup\.exe$/;
const OWNER = /^[^\u0000-\u001f\u007f]{1,128}$/;
// Tab, LF and CR are allowed in release notes; no other control character.
const NOTES = /^[^\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]{0,2000}$/;
const MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024;

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new UpdateRefused("bad_response", "The update backend returned an invalid answer.");
  }
  return value as Record<string, unknown>;
}

function isVersion(value: unknown): value is string {
  return typeof value === "string" && value.length <= 128 && VERSION.test(value);
}

function matches(value: unknown, pattern: RegExp): value is string {
  return typeof value === "string" && pattern.test(value);
}

function storeList(value: unknown, what: string): string[] {
  if (!Array.isArray(value) || value.length > 64 || !value.every((item) => matches(item, STORE_ID))) bad(what);
  return [...(value as string[])];
}

/** Every field the approval card renders or the backend binds, checked exactly and bounded; a fresh object
 *  with only those fields is returned, so nothing unvalidated reaches the panel. */
export function parseOffer(value: unknown): UpdateOffer {
  const offer = record(value);
  if (!isVersion(offer.version) || !isVersion(offer.fromVersion)
      || typeof offer.kind !== "string" || !KINDS.has(offer.kind)
      || !matches(offer.keyId, KEY_ID) || !matches(offer.owner, OWNER)
      || !matches(offer.packageSha256, SHA256)
      || typeof offer.packageBytes !== "number" || !Number.isSafeInteger(offer.packageBytes)
      || offer.packageBytes < 0 || offer.packageBytes > MAX_PACKAGE_BYTES
      || !matches(offer.packageName, PACKAGE_NAME)
      || !matches(offer.notes, NOTES)
      || !matches(offer.installer, ABSOLUTE) || (offer.installer as string).length > 4096
      || offer.manifest !== `${offer.installer}.qronos-update.json`
      || !(offer.restoreSnapshot === null || matches(offer.restoreSnapshot, SNAPSHOT_ID))) {
    bad("offer");
  }
  return {
    version: offer.version as string, fromVersion: offer.fromVersion as string, kind: offer.kind as UpdateKind,
    keyId: offer.keyId as string, owner: offer.owner as string, packageSha256: offer.packageSha256 as string,
    packageBytes: offer.packageBytes as number, packageName: offer.packageName as string,
    notes: offer.notes as string, installer: offer.installer as string, manifest: offer.manifest as string,
    changedStores: storeList(offer.changedStores, "offer"), snapshotStores: storeList(offer.snapshotStores, "offer"),
    restoreSnapshot: offer.restoreSnapshot as string | null,
  };
}

function token(value: unknown): string {
  if (typeof value !== "string" || !TOKEN.test(value)) {
    throw new UpdateRefused("bad_response", "The update backend returned an invalid token.");
  }
  return value;
}

function bad(what: string): never {
  throw new UpdateRefused("bad_response", `The update backend returned an invalid ${what}.`);
}

const PHASES: ReadonlySet<string> = new Set(["idle", "staged", "handed_off"]);
const RESULTS: ReadonlySet<string> = new Set(["applied", "not_applied", "unconfirmed"]);
const UNCONFIRMED_RECEIPTS: ReadonlySet<string> = new Set(["absent", "invalid"]);

function bool(value: unknown, what: string): boolean {
  return typeof value === "boolean" ? value : bad(what);
}

function parseOutcome(value: unknown): UpdateOutcome | null {
  if (value === null || value === undefined) return null;
  const outcome = record(value);
  if (typeof outcome.result !== "string" || !RESULTS.has(outcome.result) || typeof outcome.kind !== "string"
      || !KINDS.has(outcome.kind) || !isVersion(outcome.version)) {
    bad("outcome");
  }
  return {
    result: outcome.result as UpdateOutcomeResult, kind: outcome.kind as UpdateKind, version: outcome.version as string,
    dataMismatch: storeList(outcome.dataMismatch, "outcome"), dataRestored: bool(outcome.dataRestored, "outcome"),
  };
}

function parseUnconfirmed(value: unknown): UpdateUnconfirmed | null {
  if (value === null || value === undefined) return null;
  const item = record(value);
  const actions = item.actions;
  if (!isVersion(item.version) || item.kind !== "repair"
      || typeof item.receipt !== "string" || !UNCONFIRMED_RECEIPTS.has(item.receipt)
      || !Array.isArray(actions) || actions.length !== 2 || actions[0] !== "discard" || actions[1] !== "retry") {
    bad("unconfirmed update");
  }
  return { version: item.version as string, kind: "repair", receipt: item.receipt as "absent" | "invalid",
           actions: ["discard", "retry"] };
}

/** Only the fields the panel shows, each checked. */
export function parseStatus(value: unknown): UpdateStatus {
  const status = record(value);
  const trust = record(status.trust);
  if (!isVersion(status.runningVersion) || typeof status.phase !== "string" || !PHASES.has(status.phase)
      || typeof trust.configured !== "boolean" || typeof trust.activeKeys !== "number"
      || !Number.isSafeInteger(trust.activeKeys) || trust.activeKeys < 0 || trust.activeKeys > 1024
      || !(trust.error === null || trust.error === undefined || matches(trust.error, STORE_ID))) {
    bad("status");
  }
  let staged: UpdateStatus["staged"] = null;
  if (status.staged !== null && status.staged !== undefined) {
    const item = record(status.staged);
    if (!isVersion(item.version) || typeof item.kind !== "string" || !KINDS.has(item.kind)
        || !matches(item.packageSha256, SHA256)) {
      bad("staged update");
    }
    staged = { version: item.version as string, kind: item.kind as UpdateKind,
               packageSha256: item.packageSha256 as string };
  }
  return {
    runningVersion: status.runningVersion as string,
    trust: { configured: trust.configured as boolean, activeKeys: trust.activeKeys as number,
             error: typeof trust.error === "string" ? trust.error : null },
    launchAvailable: bool(status.launchAvailable, "status"),
    phase: status.phase as UpdatePhase,
    staged,
    launchReady: status.launchReady !== null && status.launchReady !== undefined,
    updateInProgress: bool(status.updateInProgress, "status"),
    launching: bool(status.launching, "status"),
    recoveryPending: bool(status.recoveryPending, "status"),
    outcome: parseOutcome(status.outcome),
    unconfirmed: parseUnconfirmed(status.unconfirmed),
  };
}

const ABSOLUTE = /^[A-Za-z]:\\[^\0]*$/;

/** At most 16 installer/manifest pairs, each an absolute path, the manifest beside its installer. */
export function parseCandidates(value: unknown): UpdateCandidate[] {
  if (!Array.isArray(value) || value.length > 16) bad("package list");
  return (value as unknown[]).map((entry) => {
    const item = record(entry);
    if (typeof item.installer !== "string" || typeof item.manifest !== "string"
        || !ABSOLUTE.test(item.installer) || item.manifest !== `${item.installer}.qronos-update.json`) {
      bad("package list");
    }
    return { installer: item.installer as string, manifest: item.manifest as string };
  });
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
      return parseStatus((await call("status")).status);
    },
    async discover(folder: string): Promise<UpdateCandidate[]> {
      return parseCandidates((await call("discover", { folder })).packages);
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
    /** Retry an unconfirmed repair: discard it; the caller then checks the package again, which is a new
     *  request, approval and launch (a new nonce). Nothing of the old launch is replayed. */
    async retry(): Promise<void> {
      await call("discard");
    },
  };
}

/** Stops the runtime, launches, then exits.
 *
 * - A typed refusal that proves nothing started (LAUNCH_NOT_STARTED) brings the runtime
 *   back and is re-thrown; the caller keeps the launch token only for LAUNCH_TOKEN_KEPT.
 * - Anything else is UpdateOutcomeUnknown: the runtime is NOT restarted (the installer may
 *   be running and replacing files) and the app does not exit on its own.
 */
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
    if (error instanceof UpdateRefused && LAUNCH_NOT_STARTED.has(error.code)) {
      await hooks.restartRuntime();
      throw error;
    }
    throw new UpdateOutcomeUnknown();
  }
  await hooks.exitApp();
}
