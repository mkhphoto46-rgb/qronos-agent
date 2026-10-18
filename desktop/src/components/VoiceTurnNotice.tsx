import "./RuntimeConnectionStatus.css";

/**
 * failed: a voice turn ended in an error; retrying: one explicit retry is pending; retryFailed: that retry could not
 * be sent; retryRefused: the runtime refused it (busy with another request, or paused).
 */
export type VoiceNoticeState = "failed" | "retrying" | "retryFailed" | "retryRefused";

/** Fixed Persian text for a voice request that ended in an error (runtime event voice_turn_failed). */
export const VOICE_TURN_FAILED_TEXT = {
  label: "درخواست صوتی انجام نشد",
  detail: "«بررسی راه‌اندازی» را باز کنید تا ببینید فایل‌های گفتار موجودند، سپس دوباره امتحان کنید.",
  retryingLabel: "در حال آغاز دوبارهٔ گفتار…",
  retryingDetail: "پس از شروع شنیدن، این پیام بسته می‌شود.",
  retryFailedLabel: "شروع دوبارهٔ گفتار انجام نشد",
  retryFailedDetail: "اتصال Qronos و «بررسی راه‌اندازی» را ببینید، سپس دوباره امتحان کنید.",
  retryRefusedLabel: "Qronos الان نمی‌تواند گوش دهد",
  retryRefusedDetail: "کار قبلی هنوز در جریان است یا Qronos مکث شده است؛ پس از آن دوباره امتحان کنید.",
  retry: "دوباره صحبت کنید",
  dismiss: "بستن",
};

/**
 * Shown after a failed voice turn (for example a missing or failed speech service). Retry is an explicit click that
 * starts one new push-to-talk turn; it is disabled while that attempt is pending, and nothing listens or retries on
 * its own.
 */
export default function VoiceTurnNotice({ state, onRetry, onDismiss }: {
  state: VoiceNoticeState;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  const text = VOICE_TURN_FAILED_TEXT;
  const [label, detail] = state === "retrying" ? [text.retryingLabel, text.retryingDetail]
    : state === "retryFailed" ? [text.retryFailedLabel, text.retryFailedDetail]
    : state === "retryRefused" ? [text.retryRefusedLabel, text.retryRefusedDetail] : [text.label, text.detail];
  const pending = state === "retrying";
  return <aside className="runtime-connection runtime-connection-degraded" dir="rtl">
    <div role={pending ? "status" : "alert"} aria-live={pending ? "polite" : "assertive"}
      className="runtime-connection-text">
      <span className="runtime-connection-dot" aria-hidden="true" />
      <strong>{label}</strong>
      <span className="runtime-connection-detail">{detail}</span>
    </div>
    <button type="button" disabled={pending} onClick={onRetry}>{text.retry}</button>
    <button type="button" onClick={onDismiss}>{text.dismiss}</button>
  </aside>;
}
