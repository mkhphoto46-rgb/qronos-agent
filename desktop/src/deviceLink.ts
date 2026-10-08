/**
 * The Device Link page: status, pairing, devices and revocation.
 *
 * The runtime holds the one LinkRuntime; this controller only sends the
 * validated qronos.link_* actions and shows what the runtime reports. A
 * pairing code holds the new phone's key: it is accepted only for this
 * window's own outstanding request nonce, kept in memory while it is shown,
 * and dropped on cancel, close, expiry or a new status saying the window
 * closed. It is never logged or stored. Remote (outside the home network)
 * access stays off until the link's WAN gate is accepted; the page can only
 * switch a device's remote access off.
 */

export const LINK_ACTIONS = {
  status: "qronos.link_status",
  devices: "qronos.link_devices",
  pairOpen: "qronos.link_pair_open",
  pairCancel: "qronos.link_pair_cancel",
  revoke: "qronos.link_revoke",
  remote: "qronos.link_remote",
} as const;

export const LINK_EVENTS = new Set(["link_status", "link_devices", "link_pairing", "link_error"]);

const DEVICE_ID = /^[0-9a-f]{16}$/;
const NONCE = /^[A-Za-z0-9_-]{16,64}$/;
const MAX_NAME = 40;
/** The runtime's pairing window; the URI's own expiry wins when sooner. */
export const PAIRING_SECONDS = 120;

export type LinkStatus = {
  enabled: boolean;
  running: boolean;
  port: number | null;
  remoteActive: boolean;
  remoteUnavailable: string | null;
  pairingOpen: boolean;
};

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
  if (
    r === null ||
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
  };
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
export function parseLinkPairing(message: string): { requestNonce: string; uri: string; expiresAt: number | null } | null {
  const r = parseObject(message);
  if (r === null || typeof r.requestNonce !== "string" || !NONCE.test(r.requestNonce)) return null;
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
  /** Save link.enabled and apply it in the runtime. */
  saveEnabled: (enabled: boolean) => Promise<void>;
  nonce: () => string;
  now: () => number; // seconds
  onError: (error: unknown) => void;
};

export class DeviceLinkController {
  private status: LinkStatus | null = null;
  private devices: LinkDevice[] = [];
  private pairing: ShownPairing | null = null;
  private pendingNonce: string | null = null;
  private pendingName = "";
  private savingEnabled = false;
  private error: string | null = null;
  private listeners = new Set<() => void>();
  private view: DeviceLinkView = {
    status: null, devices: [], pairing: null, waitingForPairing: false, savingEnabled: false, error: null,
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
    if (!(await this.send(LINK_ACTIONS.pairOpen, { requestNonce: nonce, name }))) {
      this.pendingNonce = null;
      this.publish();
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
): DeviceLinkController {
  return new DeviceLinkController({
    send: async (actionId, data) => {
      await invoke("send_runtime_action", { actionId, data });
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
    case "link_pairing": {
      const pairing = parseLinkPairing(message);
      if (pairing !== null) controller.pairingArrived(pairing);
      return true;
    }
    case "link_error": {
      const error = parseLinkError(message);
      if (error !== null) controller.errorArrived(error);
      return true;
    }
    default:
      return false;
  }
}
