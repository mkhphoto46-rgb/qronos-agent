/**
 * Telegram's download folder setting (`workflows.telegram_download_folder`, core/settings_service.py).
 *
 * The person types the exact local folder Telegram Desktop saves downloads into; Qronos never guesses it. This
 * module only mirrors the backend's FORM rules so a mistake is explained before saving; the backend validates the
 * value again, and the runtime checks the folder itself (exists, local, not a link) before every Telegram approval.
 * No runtime imports, so it runs in tests as is.
 */

export const TELEGRAM_FOLDER_KEY = "workflows.telegram_download_folder";
export const MAX_FOLDER_CHARS = 1024;

const CONTROL = /[\u0000-\u001f\u007f]/;
const DRIVE_ABSOLUTE = /^[A-Za-z]:[\\/]/;

/** Why the typed text cannot name a local folder, in Persian, or null (an empty value clears the setting). */
export function telegramFolderProblem(value: string): string | null {
  const text = value.trim();
  if (!text) return null;
  if (text.length > MAX_FOLDER_CHARS) return "مسیر خیلی طولانی است.";
  if (CONTROL.test(text)) return "مسیر نویسهٔ نامعتبر دارد.";
  if (text.startsWith("\\\\") || text.startsWith("//")) {
    return "مسیر شبکه یا دستگاه پذیرفته نمی‌شود؛ فقط یک پوشهٔ محلی.";
  }
  if (!DRIVE_ABSOLUTE.test(text) || text.split(/[\\/]+/).includes("..")) {
    return "مسیر کامل یک پوشهٔ محلی را بنویسید، مثلاً D:\\Downloads\\Telegram Desktop";
  }
  return null;
}

/** The short Persian status shown under the field (never a claim the folder works until a download checks it). */
export function telegramFolderStatus(saved: string): string {
  return saved.trim()
    ? "ذخیره شد؛ پیش از هر دانلود دوباره بررسی می‌شود که پوشه وجود دارد، محلی است و پیوند نیست."
    : "تعیین نشده؛ تا وقتی تعیین نشود، دانلود از تلگرام انجام نمی‌شود.";
}
