import { useEffect, useState } from "react";

import { telegramFolderProblem, telegramFolderStatus, MAX_FOLDER_CHARS } from "../telegramFolder";

type TelegramFolderSettingProps = {
  /** The saved value (settings store). */
  saved: string;
  /** Validate in the backend, persist, and apply in the runtime; rejects with the reason. */
  onSave: (value: string) => Promise<void>;
  disabled?: boolean;
};

/** Telegram's own download folder: typed by the person, never guessed or browsed for. */
export default function TelegramFolderSetting({ saved, onSave, disabled = false }: TelegramFolderSettingProps) {
  const [draft, setDraft] = useState(saved);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => setDraft(saved), [saved]);

  const save = async (value: string) => {
    const problem = telegramFolderProblem(value);
    setError(problem);
    if (problem) return;
    setSaving(true);
    try {
      await onSave(value.trim());
    } catch {
      setError("ذخیره نشد؛ مسیر را بررسی کنید.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="settings-row">
      <div className="settings-row-copy">
        <strong>پوشهٔ دانلود تلگرام</strong>
        <span>
          همان پوشه‌ای که برنامهٔ تلگرام فایل‌ها را در آن ذخیره می‌کند (در تلگرام: تنظیمات ← پیشرفته ← مسیر
          دانلود). کرونوس آن را حدس نمی‌زند و فقط همین پوشه را بررسی می‌کند؛ فقط پوشهٔ محلی، بدون پیوند.
        </span>
        <small role="status">{error ?? telegramFolderStatus(saved)}</small>
      </div>
      <div className="settings-row-control">
        <input
          type="text"
          dir="ltr"
          value={draft}
          maxLength={MAX_FOLDER_CHARS}
          placeholder="D:\Downloads\Telegram"
          aria-label="پوشهٔ دانلود تلگرام"
          disabled={disabled || saving}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button type="button" disabled={disabled || saving || draft.trim() === saved.trim()} onClick={() => void save(draft)}>
          ذخیره
        </button>
        <button type="button" disabled={disabled || saving || !saved} onClick={() => void save("")}>
          پاک کردن
        </button>
      </div>
    </div>
  );
}
