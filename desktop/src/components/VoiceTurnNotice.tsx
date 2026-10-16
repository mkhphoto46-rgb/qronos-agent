import "./RuntimeConnectionStatus.css";

/** Fixed Persian text for a voice request that ended in an error (runtime event voice_turn_failed). */
export const VOICE_TURN_FAILED_TEXT = {
  label: "درخواست صوتی انجام نشد",
  detail: "«بررسی راه‌اندازی» را باز کنید تا ببینید فایل‌های گفتار موجودند، سپس دوباره امتحان کنید.",
  retry: "دوباره صحبت کنید",
  dismiss: "بستن",
};

/**
 * Shown after a failed voice turn (for example a missing or failed speech service). Retry is an explicit click that
 * starts one new push-to-talk turn; nothing listens or retries on its own.
 */
export default function VoiceTurnNotice({ onRetry, onDismiss }: { onRetry: () => void; onDismiss: () => void }) {
  return <aside className="runtime-connection runtime-connection-degraded" style={{ top: 112 }} dir="rtl">
    <div role="alert" aria-live="assertive" className="runtime-connection-text">
      <span className="runtime-connection-dot" aria-hidden="true" />
      <strong>{VOICE_TURN_FAILED_TEXT.label}</strong>
      <span className="runtime-connection-detail">{VOICE_TURN_FAILED_TEXT.detail}</span>
    </div>
    <button type="button" onClick={onRetry}>{VOICE_TURN_FAILED_TEXT.retry}</button>
    <button type="button" onClick={onDismiss}>{VOICE_TURN_FAILED_TEXT.dismiss}</button>
  </aside>;
}
