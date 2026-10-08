import { useEffect, useMemo, useState } from "react";

import { remoteText } from "../deviceLink";
import type { DeviceLinkView, LinkDevice, LinkStatus } from "../deviceLink";
import { encodeQr, qrSvgPath } from "../qrCode";

import "./DeviceLinkPanel.css";

type Props = {
  view: DeviceLinkView;
  enabled: boolean;
  onSetEnabled: (enabled: boolean) => void;
  onRefresh: () => void;
  onOpenPairing: (name: string) => void;
  onCancelPairing: () => void;
  onRevoke: (deviceId: string) => void;
  onRemoteOff: (deviceId: string) => void;
  onResetSecurity: () => void;
  onTick: () => void;
  onDismissError: () => void;
  onClose: () => void;
};

const STATUS_TEXT: Record<LinkDevice["status"], string> = {
  pending: "در انتظار جفت شدن",
  active: "فعال",
  revoked: "دسترسی لغو شده",
};

function statusLine(enabled: boolean, status: LinkStatus | null): string {
  if (!enabled) return "خاموش است؛ هیچ گوشی‌ای نمی‌تواند به Qronos وصل شود.";
  if (status === null) return "در حال دریافت وضعیت...";
  if (status.security.quarantined) {
    return "پایشگر امنیتی به‌خاطر نشانه‌های حمله اتصال را قرنطینه کرده است؛ حتی در شبکهٔ خانگی خاموش است.";
  }
  if (status.running) return `روشن است؛ فقط گوشی‌های جفت‌شده در همین شبکهٔ خانگی (درگاه ${status.port ?? "?"}).`;
  return "روشن شده ولی اجرا نشد؛ وضعیت را تازه کنید یا Qronos را دوباره باز کنید.";
}

function when(seconds: number | null): string {
  return seconds === null ? "هرگز" : new Date(seconds * 1000).toLocaleString("fa-IR");
}

/**
 * The Device Link page in Persian: switch, status, pairing code and devices.
 *
 * The pairing code holds the new phone's key. It is drawn here from memory,
 * never as text, and disappears on cancel, expiry or leaving the page.
 * Revoking asks once more before it is sent. Access from outside the home
 * network is not offered in this version.
 */
export default function DeviceLinkPanel(props: Props) {
  const { view, enabled } = props;
  const [name, setName] = useState("");
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const pairing = view.pairing;

  useEffect(() => {
    props.onRefresh();
    return () => props.onClose();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (pairing === null) return;
    // Expire the code locally, and notice when the phone has paired.
    const tick = window.setInterval(props.onTick, 1000);
    const poll = window.setInterval(props.onRefresh, 3000);
    return () => {
      window.clearInterval(tick);
      window.clearInterval(poll);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pairing]);

  const qrPath = useMemo(() => {
    if (pairing === null) return null;
    const matrix = encodeQr(pairing.uri);
    return { path: qrSvgPath(matrix), size: matrix.size + 8 };
  }, [pairing]);

  const running = enabled && view.status?.running === true;

  return (
    <section className="device-link" dir="rtl" aria-labelledby="device-link-title">
      <h2 id="device-link-title">اتصال گوشی</h2>
      <p className="device-link-copy">
        گوشی‌هایی که با کد QR جفت شده‌اند می‌توانند از همین شبکهٔ خانگی سؤال بپرسند. پاسخ‌ها فقط متن هستند:
        از راه گوشی هیچ فایلی باز، حذف یا چاپ نمی‌شود و هیچ کاری روی رایانه انجام نمی‌شود.
      </p>

      <label className="device-link-switch">
        <input
          type="checkbox"
          checked={enabled}
          disabled={view.savingEnabled}
          onChange={(event) => props.onSetEnabled(event.target.checked)}
        />
        <span>اجازهٔ اتصال گوشی‌های جفت‌شده در این شبکه</span>
      </label>
      <p className="device-link-status" role="status">{statusLine(enabled, view.status)}</p>
      <p className="device-link-note">{remoteText(view.status)}</p>

      {view.status !== null && (view.status.security.quarantined || view.status.security.contained) && (
        <div className="device-link-security" role="alert">
          <p>
            پیش از بازنشانی، دستگاه‌های جفت‌شده را بررسی کنید و هر گوشی ناآشنا یا گم‌شده را لغو کنید.
            بازنشانی فقط از همین رایانه ممکن است.
          </p>
          {confirmReset ? (
            <span className="device-link-confirm">
              <button
                type="button"
                className="file-approval-button file-approval-deny"
                onClick={() => {
                  setConfirmReset(false);
                  props.onResetSecurity();
                }}
              >
                بله، بازنشانی شود
              </button>
              <button type="button" className="file-approval-button" onClick={() => setConfirmReset(false)}>نه</button>
            </span>
          ) : (
            <button type="button" className="file-approval-button" onClick={() => setConfirmReset(true)}>
              بازنشانی پایشگر امنیتی
            </button>
          )}
        </div>
      )}

      {view.error && (
        <div className="device-link-error" role="alert">
          <span>{view.error}</span>
          <button type="button" className="file-approval-button" onClick={props.onDismissError}>بستن</button>
        </div>
      )}

      {pairing === null ? (
        <form
          className="device-link-pair"
          onSubmit={(event) => {
            event.preventDefault();
            props.onOpenPairing(name);
          }}
        >
          <label>
            نام گوشی جدید
            <input
              type="text"
              value={name}
              maxLength={40}
              autoComplete="off"
              disabled={!running || view.waitingForPairing}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <button type="submit" className="file-approval-button" disabled={!running || view.waitingForPairing}>
            {view.waitingForPairing ? "در حال آماده‌سازی..." : "جفت کردن گوشی جدید"}
          </button>
        </form>
      ) : (
        <div className="device-link-qr" aria-live="polite">
          <p>
            این کد را با برنامهٔ Qronos گوشی «{pairing.name || "گوشی"}» اسکن کنید. کد کلید همین گوشی را دارد؛
            آن را به کسی نشان ندهید و از آن عکس نگیرید. تا دو دقیقه معتبر است.
          </p>
          {qrPath && (
            <svg
              className="device-link-qr-code"
              viewBox={`0 0 ${qrPath.size} ${qrPath.size}`}
              role="img"
              aria-label="کد QR جفت کردن گوشی"
              shapeRendering="crispEdges"
            >
              <rect width={qrPath.size} height={qrPath.size} fill="#fff" />
              <path d={qrPath.path} fill="#000" />
            </svg>
          )}
          <button type="button" className="file-approval-button" onClick={props.onCancelPairing}>لغو</button>
        </div>
      )}

      <h3>دستگاه‌های جفت‌شده</h3>
      {view.devices.length === 0 ? (
        <p>هنوز دستگاهی جفت نشده است.</p>
      ) : (
        <ul className="device-link-devices">
          {view.devices.map((device) => (
            <li key={device.deviceId} data-device-id={device.deviceId} data-status={device.status}>
              <div>
                <strong dir="auto">{device.name || "بدون نام"}</strong>
                <span>{STATUS_TEXT[device.status]}</span>
                <small>آخرین اتصال: {when(device.lastSeenAt)}</small>
                {device.remoteEnabled && <small>دسترسی از بیرون خانه: روشن</small>}
              </div>
              {device.remoteEnabled && device.status !== "revoked" && (
                <button type="button" className="file-approval-button" onClick={() => props.onRemoteOff(device.deviceId)}>
                  قطع دسترسی از بیرون
                </button>
              )}
              {device.status !== "revoked" && (confirmRevoke === device.deviceId ? (
                <span className="device-link-confirm">
                  <button
                    type="button"
                    className="file-approval-button file-approval-deny"
                    onClick={() => {
                      setConfirmRevoke(null);
                      props.onRevoke(device.deviceId);
                    }}
                  >
                    بله، لغو شود
                  </button>
                  <button type="button" className="file-approval-button" onClick={() => setConfirmRevoke(null)}>
                    نه
                  </button>
                </span>
              ) : (
                <button type="button" className="file-approval-button" onClick={() => setConfirmRevoke(device.deviceId)}>
                  لغو دسترسی
                </button>
              ))}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
