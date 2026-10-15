/**
 * The Device Link page: status, pairing, devices and revocation.
 *
 * The runtime holds the one LinkRuntime; this controller only sends the
 * validated qronos.link_* actions and shows what the runtime reports. A
 * pairing code holds the new phone's key: it is accepted only for this
 * window's own outstanding request nonce, kept in memory while it is shown,
 * and dropped on cancel, close, expiry or a new status saying the window
 * closed. It is never logged or stored. The request goes through the typed
 * link_pair_open command, which binds it to this window; the reply arrives
 * only on this window's own qronos://link-pairing event, never on the
 * broadcast runtime-event channel.
 *
 * Remote (outside the home network) access is the PC owner's explicit choice: a global switch saved only at this
 * PC, a relay the owner configures (no default endpoint), and a finite per-phone grant. Each WAN change carries a
 * tracking nonce (not authority); only a link_operation_result with the same nonce AND action finishes it. Without
 * an answer in 10 s the outcome is shown as unknown, the status is read ONCE, and the change is never resent.
 * Switching a phone's remote access off and revoking are always available.
 */

export const LINK_ACTIONS = {
  status: "qronos.link_status",
  devices: "qronos.link_devices",
  pairOpen: "qronos.link_pair_open",
  pairCancel: "qronos.link_pair_cancel",
  revoke: "qronos.link_revoke",
  remote: "qronos.link_remote",
  relaySet: "qronos.link_relay_set",
  securityReset: "qronos.link_security_reset",
} as const;

export const LINK_EVENTS = new Set(["link_status", "link_devices", "link_error", "link_operation_result"]);
/** How long a WAN change waits for its own result before its outcome is unknown. */
export const OPERATION_TIMEOUT_MS = 10_000;
const MAX_CA_PEM = 16 * 1024;
const MAX_REMOTE_DAYS = 365;
/** The desktop host delivers a pairing reply only to the window that asked, here. */
export const LINK_PAIRING_EVENT = "qronos://link-pairing";

const DEVICE_ID = /^[0-9a-f]{16}$/;
const NONCE = /^[A-Za-z0-9_-]{16,64}$/;
const MAX_NAME = 40;
/** The runtime's pairing window; the URI's own expiry wins when sooner. */
export const PAIRING_SECONDS = 120;

/** The link's Security Monitor (Link D9). Missing means not ready. */
export type LinkSecurity = {
  ready: boolean;
  contained: boolean;
  quarantined: boolean;
};

/** Non-secret facts about the configured relay (never the CA text or a path). */
export type LinkRelay = {
  configured: boolean;
  host: string | null;
  port: number | null;
  tlsName: string | null;
  pinnedCa: boolean;
};

export type LinkStatus = {
  enabled: boolean;
  running: boolean;
  port: number | null;
  remoteActive: boolean;
  remoteUnavailable: string | null;
  pairingOpen: boolean;
  security: LinkSecurity;
  /** null when the runtime does not report a relay (older status). */
  relay: LinkRelay | null;
};

function parseRelay(value: unknown): LinkRelay | null | undefined {
  if (value === undefined) return null;
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const r = value as Record<string, unknown>;
  const text = (v: unknown) => v === null || (typeof v === "string" && v.length <= 253);
  if (
    typeof r.configured !== "boolean" || typeof r.pinnedCa !== "boolean" || !text(r.host) || !text(r.tlsName) ||
    !(r.port === null || (typeof r.port === "number" && Number.isInteger(r.port) && r.port > 0 && r.port < 65536))
  ) {
    return undefined;
  }
  return { configured: r.configured, host: r.host as string | null, port: r.port as number | null,
           tlsName: r.tlsName as string | null, pinnedCa: r.pinnedCa };
}

function parseSecurity(value: unknown): LinkSecurity | null {
  if (value === undefined) return { ready: false, contained: false, quarantined: false };
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const r = value as Record<string, unknown>;
  if (typeof r.ready !== "boolean" || typeof r.contained !== "boolean" || typeof r.quarantined !== "boolean") {
    return null;
  }
  return { ready: r.ready, contained: r.contained, quarantined: r.quarantined };
}

export type LinkDevice = {
  deviceId: string;
  name: string;
  status: "pending" | "active" | "revoked";
  createdAt: number;
  lastSeenAt: number | null;
  remoteEnabled: boolean;
  remoteExpiresAt: number | null;
};

export type ShownPairing = { uri: string; name: string; expiresAt: number };

export type DeviceLinkView = {
  status: LinkStatus | null;
  devices: LinkDevice[];
  pairing: ShownPairing | null;
  waitingForPairing: boolean;
  savingEnabled: boolean;
  error: string | null;
  /** The WAN change waiting for its result, if any. */
  operation?: LinkOperation | null;
  savingRemote?: boolean;
};

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

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

export function parseLinkStatus(message: string): LinkStatus | null {
  const r = parseObject(message);
  const security = r === null ? null : parseSecurity(r.security);
  const relay = r === null ? undefined : parseRelay(r.relay);
  if (
    r === null ||
    security === null ||
    relay === undefined ||
    typeof r.enabled !== "boolean" ||
    typeof r.running !== "boolean" ||
    typeof r.remote_active !== "boolean" ||
    typeof r.pairing_open !== "boolean" ||
    !(r.port === null || (finite(r.port) && Number.isInteger(r.port) && r.port > 0 && r.port < 65536)) ||
    !(r.remote_unavailable === null || (typeof r.remote_unavailable === "string" && r.remote_unavailable.length <= 64))
  ) {
    return null;
  }
  return {
    enabled: r.enabled,
    running: r.running,
    port: r.port as number | null,
    remoteActive: r.remote_active,
    remoteUnavailable: r.remote_unavailable as string | null,
    pairingOpen: r.pairing_open,
    security,
    relay,
  };
}

const REMOTE_TEXT: Record<string, string> = {
  remote_access_off: "دسترسی از بیرون خانه خاموش است؛ فقط از همین رایانه روشن می‌شود.",
  no_relay_configured: "دسترسی از بیرون خانه روشن است اما هنوز رله‌ای تنظیم نشده است.",
  relay_settings_unreadable: "تنظیم رله خوانده نشد؛ دسترسی از بیرون خانه بسته می‌ماند.",
  security_quarantined: "پایشگر امنیتی اتصال را قرنطینه کرده است.",
  security_contained: "پایشگر امنیتی دسترسی از بیرون خانه را بسته است.",
  security_monitor_not_ready: "پایشگر امنیتی آماده نیست؛ دسترسی از بیرون خانه بسته می‌ماند.",
};

/** The state of access from outside the home network, in Persian; never a claim that a phone is reachable. */
export function remoteText(status: LinkStatus | null): string {
  if (status?.remoteActive) return "اتصال به رله برای دسترسی از بیرون خانه فعال شده است؛ رسیدن گوشی هنوز آزموده نشده است.";
  const reason = status?.remoteUnavailable ?? null;
  return (reason !== null && REMOTE_TEXT[reason]) || "دسترسی از بیرون خانه در دسترس نیست.";
}

/** One WAN change's acknowledgment: the bridge's actual commit or refusal for that nonce. */
export function parseLinkOperationResult(message: string):
  { requestNonce: string; action: string; status: "applied" | "refused"; code: string | null } | null {
  const r = parseObject(message);
  if (r === null || typeof r.requestNonce !== "string" || !NONCE.test(r.requestNonce)) return null;
  if (typeof r.action !== "string" || r.action.length > 64) return null;
  if (r.status !== "applied" && r.status !== "refused") return null;
  if (!(r.code === undefined || r.code === null || (typeof r.code === "string" && r.code.length <= 32))) return null;
  return { requestNonce: r.requestNonce, action: r.action, status: r.status, code: (r.code as string | undefined) ?? null };
}

export function parseLinkDevices(message: string): LinkDevice[] | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(message);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed) || parsed.length > 200) return null;
  const devices: LinkDevice[] = [];
  for (const item of parsed) {
    if (typeof item !== "object" || item === null || Array.isArray(item)) return null;
    const r = item as Record<string, unknown>;
    if (
      typeof r.device_id !== "string" || !DEVICE_ID.test(r.device_id) ||
      typeof r.name !== "string" || r.name.length > 64 ||
      (r.status !== "pending" && r.status !== "active" && r.status !== "revoked") ||
      !finite(r.created_at) ||
      !(r.last_seen_at === null || finite(r.last_seen_at)) ||
      typeof r.remote_enabled !== "boolean" ||
      !(r.remote_expires_at === null || finite(r.remote_expires_at)) ||
      "secret" in r
    ) {
      return null;
    }
    devices.push({
      deviceId: r.device_id,
      name: r.name,
      status: r.status,
      createdAt: r.created_at,
      lastSeenAt: r.last_seen_at as number | null,
      remoteEnabled: r.remote_enabled,
      remoteExpiresAt: r.remote_expires_at as number | null,
    });
  }
  return devices;
}

/** The pairing answer, or null. The URI must be the link's own pairing scheme. */
export function parseLinkPairing(payload: unknown): { requestNonce: string; uri: string; expiresAt: number | null } | null {
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return null;
  const r = payload as Record<string, unknown>;
  if (typeof r.requestNonce !== "string" || !NONCE.test(r.requestNonce)) return null;
  if (typeof r.uri !== "string" || r.uri.length > 400 || !r.uri.startsWith("qronos://pair?")) return null;
  const expiry = Number(new URLSearchParams(r.uri.slice("qronos://pair?".length)).get("e"));
  return { requestNonce: r.requestNonce, uri: r.uri, expiresAt: Number.isFinite(expiry) && expiry > 0 ? expiry : null };
}

export function parseLinkError(message: string): { action: string; code: string } | null {
  const r = parseObject(message);
  if (r === null || typeof r.action !== "string" || typeof r.code !== "string") return null;
  return { action: r.action.slice(0, 64), code: r.code.slice(0, 32) };
}

const ERROR_TEXT: Record<string, string> = {
  invalid_request: "درخواست معتبر نبود؛ کاری انجام نشد.",
  not_running: "اتصال گوشی روشن نیست. ابتدا آن را روشن کنید.",
  unknown_device: "این دستگاه پیدا نشد؛ فهرست را تازه کنید.",
  remote_unavailable: "دسترسی از بیرون خانه هنوز در این نسخه فعال نیست.",
  remote_access_off: "ابتدا دسترسی از بیرون خانه را در همین رایانه روشن کنید.",
  relay_not_configured: "ابتدا رله را تنظیم کنید.",
  relay_settings_unreadable: "تنظیم رله خوانده نشد؛ آن را دوباره ذخیره کنید.",
  relay_invalid: "نشانی، درگاه یا گواهی رله معتبر نبود؛ چیزی ذخیره نشد.",
  device_revoked: "دسترسی این دستگاه لغو شده است.",
  security_quarantined: "پایشگر امنیتی اتصال را قرنطینه کرده است.",
  security_contained: "پایشگر امنیتی دسترسی از بیرون خانه را بسته است.",
  security_monitor_not_ready: "پایشگر امنیتی آماده نیست.",
  outcome_unknown: "معلوم نیست این تغییر انجام شد یا نه؛ وضعیت دوباره خوانده شد. پیش از تکرار، آن را ببینید.",
  failed: "این کار انجام نشد. دوباره امتحان کنید.",
};

export function linkErrorText(code: string): string {
  return ERROR_TEXT[code] ?? ERROR_TEXT.failed;
}

/** A device name the runtime will accept: trimmed, printable, at most 40 characters. */
export function cleanDeviceName(value: string): string | null {
  const name = value.trim();
  if (name.length > MAX_NAME) return null;
  // Control, line-break and bidi-override characters are refused, as the runtime does;
  // Persian zero-width joiners are allowed.
  for (const character of name) {
    const code = character.codePointAt(0) as number;
    if (
      code < 0x20 || (code >= 0x7f && code <= 0x9f) || code === 0x2028 || code === 0x2029 ||
      (code >= 0x202a && code <= 0x202e) || (code >= 0x2066 && code <= 0x2069)
    ) {
      return null;
    }
  }
  return name;
}

type Send = (actionId: string, data: Record<string, unknown>) => Promise<void>;

export type DeviceLinkDeps = {
  send: Send;
  /** The typed host command that binds the request to this window. */
  requestPairing: (requestNonce: string, name: string) => Promise<void>;
  /** Save link.enabled and apply it in the runtime. */
  saveEnabled: (enabled: boolean) => Promise<void>;
  nonce: () => string;
  now: () => number; // seconds
  onError: (error: unknown) => void;
  /** Save link.remote_access_enabled at this PC and apply it (A's settings path). Optional until wired. */
  saveRemoteEnabled?: (enabled: boolean) => Promise<void>;
  setTimer?: (callback: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
};

/** A WAN change waiting for its own acknowledgment. */
export type LinkOperation = { action: string; state: "pending" };

export type RelayRequest = { host: string; port: number; serverName?: string; caPem?: string };

const HOST = /^[A-Za-z0-9.:\-\[\]]{1,253}$/;

export class DeviceLinkController {
  private status: LinkStatus | null = null;
  private devices: LinkDevice[] = [];
  private pairing: ShownPairing | null = null;
  private pendingNonce: string | null = null;
  private pendingName = "";
  private savingEnabled = false;
  private savingRemote = false;
  private error: string | null = null;
  private operation: { action: string; nonce: string; timer: unknown } | null = null;
  private listeners = new Set<() => void>();
  private view: DeviceLinkView = {
    status: null, devices: [], pairing: null, waitingForPairing: false, savingEnabled: false, error: null,
    operation: null, savingRemote: false,
  };

  constructor(private readonly deps: DeviceLinkDeps) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): DeviceLinkView => this.view;

  private publish(): void {
    this.view = {
      status: this.status,
      devices: this.devices,
      pairing: this.pairing,
      waitingForPairing: this.pendingNonce !== null,
      savingEnabled: this.savingEnabled,
      error: this.error,
      operation: this.operation === null ? null : { action: this.operation.action, state: "pending" },
      savingRemote: this.savingRemote,
    };
    for (const listener of this.listeners) listener();
  }

  private async send(actionId: string, data: Record<string, unknown> = {}): Promise<boolean> {
    try {
      await this.deps.send(actionId, data);
      return true;
    } catch (error) {
      this.error = linkErrorText("failed");
      this.publish();
      this.deps.onError(error);
      return false;
    }
  }

  refresh = (): Promise<boolean> => this.send(LINK_ACTIONS.status);

  setEnabled = async (enabled: boolean): Promise<void> => {
    if (this.savingEnabled) return;
    this.savingEnabled = true;
    this.error = null;
    if (!enabled) this.dropPairing();
    this.publish();
    try {
      await this.deps.saveEnabled(enabled);
    } catch (error) {
      this.error = "تنظیم ذخیره نشد؛ وضعیت اتصال تغییر نکرد.";
      this.deps.onError(error);
    } finally {
      this.savingEnabled = false;
      this.publish();
    }
    await this.refresh();
  };

  openPairing = async (rawName: string): Promise<void> => {
    if (this.pendingNonce !== null || this.pairing !== null) return;
    const name = cleanDeviceName(rawName);
    if (name === null) {
      this.error = "نام دستگاه باید حداکثر ۴۰ نویسهٔ قابل نمایش باشد.";
      this.publish();
      return;
    }
    const nonce = this.deps.nonce();
    if (!NONCE.test(nonce)) throw new Error("bad nonce source");
    this.pendingNonce = nonce;
    this.pendingName = name;
    this.error = null;
    this.publish();
    try {
      await this.deps.requestPairing(nonce, name);
    } catch (error) {
      this.pendingNonce = null;
      this.error = linkErrorText("failed");
      this.publish();
      this.deps.onError(error);
    }
  };

  /** Only the answer to this window's own request is shown. */
  pairingArrived(answer: { requestNonce: string; uri: string; expiresAt: number | null }): void {
    if (this.pendingNonce === null || answer.requestNonce !== this.pendingNonce) return;
    const limit = this.deps.now() + PAIRING_SECONDS;
    this.pairing = {
      uri: answer.uri,
      name: this.pendingName,
      expiresAt: answer.expiresAt === null ? limit : Math.min(limit, answer.expiresAt),
    };
    this.pendingNonce = null;
    this.publish();
  }

  /** Called by the panel's timer: an expired code is no longer shown. */
  tick = (): void => {
    if (this.pairing !== null && this.deps.now() >= this.pairing.expiresAt) {
      this.dropPairing();
      this.publish();
      void this.refresh();
    }
  };

  cancelPairing = async (): Promise<void> => {
    const wasShown = this.pairing !== null || this.pendingNonce !== null;
    this.dropPairing();
    this.publish();
    if (wasShown) await this.send(LINK_ACTIONS.pairCancel);
  };

  private dropPairing(): void {
    this.pairing = null;
    this.pendingNonce = null;
    this.pendingName = "";
  }

  revoke = (deviceId: string): Promise<boolean> => {
    if (!this.devices.some((device) => device.deviceId === deviceId && device.status !== "revoked")) {
      return Promise.resolve(false);
    }
    return this.send(LINK_ACTIONS.revoke, { deviceId });
  };

  remoteOff = (deviceId: string): Promise<boolean> => {
    if (!this.devices.some((device) => device.deviceId === deviceId && device.remoteEnabled)) {
      return Promise.resolve(false);
    }
    return this.send(LINK_ACTIONS.remote, { deviceId, enabled: false });
  };

  // ---------------------------------------------------------------- WAN (owner opt-in, relay, per-phone grant)

  /** Save the global switch for access from outside the home network, at this PC only. */
  setRemoteEnabled = async (enabled: boolean): Promise<void> => {
    if (this.savingRemote) return;
    const save = this.deps.saveRemoteEnabled;
    if (save === undefined) {
      this.error = "این تنظیم هنوز در این صفحه در دسترس نیست.";
      this.publish();
      return;
    }
    this.savingRemote = true;
    this.error = null;
    this.publish();
    try {
      await save(enabled);
    } catch (error) {
      this.error = "تنظیم ذخیره نشد؛ دسترسی از بیرون خانه تغییر نکرد.";
      this.deps.onError(error);
    } finally {
      this.savingRemote = false;
      this.publish();
    }
    await this.refresh();
  };

  relaySet = (request: RelayRequest): Promise<boolean> => {
    const { host, port, serverName, caPem } = request;
    if (
      typeof host !== "string" || !HOST.test(host) ||
      typeof port !== "number" || !Number.isInteger(port) || port < 1 || port > 65535 ||
      !(serverName === undefined || (typeof serverName === "string" && HOST.test(serverName))) ||
      !(caPem === undefined || (typeof caPem === "string" && caPem.length > 0 && caPem.length <= MAX_CA_PEM))
    ) {
      this.error = linkErrorText("relay_invalid");
      this.publish();
      return Promise.resolve(false);
    }
    const data: Record<string, unknown> = { host, port };
    if (serverName !== undefined) data.serverName = serverName;
    if (caPem !== undefined) data.caPem = caPem;
    return this.startOperation(LINK_ACTIONS.relaySet, data);
  };

  relayClear = (): Promise<boolean> => this.startOperation(LINK_ACTIONS.relaySet, { clear: true });

  /** Remote access for one listed, unrevoked phone for a finite number of days (1..365). */
  remoteOn = (deviceId: string, days: number): Promise<boolean> => {
    const listed = this.devices.some((device) => device.deviceId === deviceId && device.status !== "revoked");
    if (!listed || !Number.isInteger(days) || days < 1 || days > MAX_REMOTE_DAYS) return Promise.resolve(false);
    return this.startOperation(LINK_ACTIONS.remote, { deviceId, enabled: true, days });
  };

  private async startOperation(action: string, data: Record<string, unknown>): Promise<boolean> {
    if (this.operation !== null) return false;  // one WAN change at a time
    const nonce = this.deps.nonce();
    if (!NONCE.test(nonce)) throw new Error("bad nonce source");
    const setTimer = this.deps.setTimer ?? ((callback: () => void, ms: number) => globalThis.setTimeout(callback, ms));
    const operation = { action, nonce, timer: null as unknown };
    this.operation = operation;
    this.error = null;
    operation.timer = setTimer(() => this.operationTimedOut(operation), OPERATION_TIMEOUT_MS);
    this.publish();
    const sent = await this.send(action, { ...data, requestNonce: nonce });
    if (!sent && this.operation === operation) this.finishOperation();
    return sent;
  }

  private finishOperation(): void {
    if (this.operation === null) return;
    const clearTimer = this.deps.clearTimer ?? ((handle: unknown) => globalThis.clearTimeout(handle as number));
    clearTimer(this.operation.timer);
    this.operation = null;
  }

  /** No answer in time: the outcome is unknown; read the state ONCE and never resend the change. */
  private operationTimedOut(operation: { action: string; nonce: string }): void {
    if (this.operation !== operation) return;
    this.operation = null;
    this.error = linkErrorText("outcome_unknown");
    this.publish();
    void this.send(LINK_ACTIONS.status);
  }

  /** Only the result for the pending change's own nonce AND action finishes it; anything else is ignored. */
  operationResultArrived(result: { requestNonce: string; action: string; status: "applied" | "refused"; code: string | null }): void {
    const pending = this.operation;
    if (pending === null || result.requestNonce !== pending.nonce || result.action !== pending.action) return;
    this.finishOperation();
    this.error = result.status === "refused" ? linkErrorText(result.code ?? "failed") : null;
    this.publish();
  }

  statusArrived(status: LinkStatus): void {
    this.status = status;
    if (!status.running || (!status.pairingOpen && this.pairing !== null)) {
      // The window closed: paired, cancelled, expired or the link stopped.
      this.dropPairing();
    }
    this.publish();
  }

  devicesArrived(devices: LinkDevice[]): void {
    this.devices = devices;
    this.publish();
  }

  errorArrived(error: { action: string; code: string }): void {
    if (error.action === LINK_ACTIONS.pairOpen) this.pendingNonce = null;
    this.error = linkErrorText(error.code);
    this.publish();
  }

  /** Clear the Security Monitor's latches; only from this PC's desktop. */
  resetSecurity = (): Promise<boolean> => {
    const security = this.status?.security;
    if (!security || (!security.quarantined && !security.contained)) return Promise.resolve(false);
    this.error = null;
    this.publish();
    return this.send(LINK_ACTIONS.securityReset);
  };

  dismissError = (): void => {
    this.error = null;
    this.publish();
  };

  /** Leaving the page: the code is not kept for later. */
  close = (): void => {
    void this.cancelPairing();
  };
}

export function createDeviceLinkController(
  invoke: (command: string, args: Record<string, unknown>) => Promise<unknown>,
  saveEnabled: (enabled: boolean) => Promise<void>,
  onError: (error: unknown) => void,
  random: (bytes: Uint8Array) => Uint8Array = (bytes) => crypto.getRandomValues(bytes),
  now: () => number = () => Date.now() / 1000,
  saveRemoteEnabled?: (enabled: boolean) => Promise<void>,
): DeviceLinkController {
  return new DeviceLinkController({
    ...(saveRemoteEnabled === undefined ? {} : { saveRemoteEnabled }),
    send: async (actionId, data) => {
      await invoke("send_runtime_action", { actionId, data });
    },
    requestPairing: async (requestNonce, name) => {
      await invoke("link_pair_open", { requestNonce, name });
    },
    saveEnabled,
    nonce: () => Array.from(random(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join(""),
    now,
    onError,
  });
}

/** Route one runtime event; true when it was a Device Link event. */
export function applyDeviceLinkEvent(controller: DeviceLinkController, eventType: string, message: string): boolean {
  switch (eventType) {
    case "link_status": {
      const status = parseLinkStatus(message);
      if (status !== null) controller.statusArrived(status);
      return true;
    }
    case "link_devices": {
      const devices = parseLinkDevices(message);
      if (devices !== null) controller.devicesArrived(devices);
      return true;
    }
    case "link_pairing":
      // Never accepted from the broadcast channel; see bindPairingDelivery.
      return true;
    case "link_error": {
      const error = parseLinkError(message);
      if (error !== null) controller.errorArrived(error);
      return true;
    }
    case "link_operation_result": {
      const result = parseLinkOperationResult(message);
      if (result !== null) controller.operationResultArrived(result);
      return true;
    }
    default:
      return false;
  }
}

type Listen = (
  event: string,
  handler: (event: { payload: unknown }) => void,
) => Promise<() => void>;

/** Receive pairing replies addressed to this window; returns the unlisten function. */
export function bindPairingDelivery(listen: Listen, controller: DeviceLinkController): Promise<() => void> {
  return listen(LINK_PAIRING_EVENT, (event) => {
    const pairing = parseLinkPairing(event.payload);
    if (pairing !== null) controller.pairingArrived(pairing);
  });
}
