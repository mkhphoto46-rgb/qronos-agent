import { useEffect, useState } from "react";
import { refreshSetup, setupNeedsAttention, useSetupStore } from "../setupStore";
import "./SetupStatus.css";

const featureNames: Record<string, string> = {
  wakeWord: "واژهٔ بیدارباش",
  speechRecognition: "تبدیل گفتار به متن",
  persistentSpeechRecognition: "سرویس پیوستهٔ گفتار (اختیاری)",
  speechSynthesis: "پاسخ صوتی",
  imageSearch: "جست‌وجوی تصویری فریم‌ها (اختیاری)",
};
const roleNames: Record<string, string> = { fast: "پاسخ سریع", heavy: "پردازش سنگین", vision: "دیدن صفحه" };

export default function SetupStatus() {
  const { snapshot, loading, error } = useSetupStore();
  const [open, setOpen] = useState(false);
  useEffect(() => { void refreshSetup(); }, []);
  const needsAttention = error || (snapshot && setupNeedsAttention(snapshot));
  const label = loading ? "بررسی راه‌اندازی" : needsAttention ? "راه‌اندازی ناقص" : "بررسی راه‌اندازی";
  return <aside className="setup-status" dir="rtl">
    <button type="button" className={needsAttention ? "setup-toggle setup-attention" : "setup-toggle"}
      aria-expanded={open} aria-controls="qronos-setup-report" onClick={() => setOpen(value => !value)}>{label}</button>
    {open && <section id="qronos-setup-report" className="setup-report" aria-label="وضعیت راه‌اندازی محلی">
      <div className="setup-heading"><strong>راه‌اندازی محلی</strong><button type="button" onClick={() => setOpen(false)} aria-label="بستن">×</button></div>
      <p>این بررسی فقط فایل‌های محلی و فهرست مدل‌ها را می‌خواند. هیچ مدلی بارگیری یا اجرا نمی‌شود.</p>
      {error && <p role="alert">{error}</p>}
      {snapshot && <>
        <ul>{Object.entries(snapshot.features).map(([key, feature]) => <li key={key}>
          <strong>{featureNames[key] || key}</strong><span>{feature.assetsPresent ? "فایل‌ها موجودند" : "نیاز به فایل محلی"}</span>
          {feature.missing.length > 0 && <details><summary>فایل‌های موردنیاز</summary><code dir="ltr">{feature.missing.join("\n")}</code></details>}
        </li>)}</ul>
        <p>سرور مدل: {snapshot.modelServer.status === "available" ? "محلی و در دسترس" : "در دسترس نیست؛ Ollama محلی را بررسی کنید"}</p>
        <ul>{Object.entries(snapshot.modelServer.configured).map(([role, name]) => <li key={role}>
          <strong>{roleNames[role] || role}</strong><code dir="ltr">{name}</code>
          <span>{snapshot.modelServer.status !== "available" ? "بررسی نشد" : snapshot.modelServer.missingRoles?.includes(role) ? "مدل تنظیم‌شده نصب نیست" : "نام مدل موجود است"}</span>
        </li>)}</ul>
        <details><summary>پوشهٔ فایل‌های محلی</summary><code dir="ltr">{snapshot.assetRoot}</code>
          <p>فایل‌های تأییدشده را با ساختار پوشه‌های بالا در این مسیر قرار دهید. مدل‌ها را از مسیر مرورگر مجاز تهیه و به Ollama محلی وارد کنید. چیزی به‌صورت خودکار دانلود نمی‌شود.</p></details>
        <p className="setup-note">وجود فایل یا نام مدل، کیفیت پاسخ و سازگاری سخت‌افزار را تأیید نمی‌کند.</p>
      </>}
      <button type="button" disabled={loading} onClick={() => { void refreshSetup(); }}>{loading ? "در حال بررسی…" : "بررسی دوباره"}</button>
    </section>}
  </aside>;
}
