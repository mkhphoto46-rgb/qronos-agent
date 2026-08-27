import {
  useMemo,
  useState,
} from "react";

import "./SettingsView.css";

type SettingsPhase =
  | "home"
  | "entering-settings"
  | "settings"
  | "leaving-settings";

type SettingsViewProps = {
  phase: SettingsPhase;
  onClose: () => void;
  onOpenPermissions: () => void;
};

type SectionId =
  | "general"
  | "voice"
  | "behavior"
  | "memory"
  | "performance"
  | "web"
  | "notifications"
  | "data";

type SectionDefinition = {
  id: SectionId;
  group: "GENERAL" | "QRONOS" | "SYSTEM" | "DATA";
  label: string;
  english: string;
  icon: string;
  keywords: string[];
};

const sections: SectionDefinition[] = [
  { id: "general", group: "GENERAL", label: "عمومی", english: "General", icon: "01", keywords: ["theme", "language", "startup", "update", "ظاهر", "زبان", "شروع"] },
  { id: "voice", group: "QRONOS", label: "صدا و Wake Word", english: "Voice & Wake Word", icon: "02", keywords: ["voice", "wake", "microphone", "sensitivity", "صدا", "میکروفون", "حساسیت"] },
  { id: "behavior", group: "QRONOS", label: "رفتار و شخصی‌سازی", english: "Behavior", icon: "03", keywords: ["response", "thinking", "instructions", "پاسخ", "رفتار", "دستور"] },
  { id: "memory", group: "QRONOS", label: "حافظه", english: "Memory", icon: "04", keywords: ["memory", "remember", "حافظه", "یادآوری"] },
  { id: "performance", group: "SYSTEM", label: "عملکرد", english: "Performance", icon: "05", keywords: ["eco", "balanced", "performance", "priority", "عملکرد", "منابع"] },
  { id: "web", group: "SYSTEM", label: "وب و جست‌وجو", english: "Web & Search", icon: "06", keywords: ["web", "search", "sources", "citation", "وب", "جستجو", "منابع"] },
  { id: "notifications", group: "SYSTEM", label: "اعلان‌ها", english: "Notifications", icon: "07", keywords: ["notification", "warning", "sound", "اعلان", "هشدار", "صدا"] },
  { id: "data", group: "DATA", label: "داده و پشتیبان‌گیری", english: "Data & Backup", icon: "08", keywords: ["data", "backup", "export", "restore", "داده", "پشتیبان", "خروجی"] },
];

const groups = ["GENERAL", "QRONOS", "SYSTEM", "DATA"] as const;
const particles = Array.from({ length: 34 }, (_, index) => index);

function Toggle({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={`settings-toggle ${checked ? "settings-toggle-on" : ""}`}
      onClick={() => onChange(!checked)}
    >
      <span />
      <small>{checked ? "ON" : "OFF"}</small>
    </button>
  );
}

function SettingRow({
  title,
  description,
  children,
  accent = false,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
  accent?: boolean;
}) {
  return (
    <div className={`settings-row ${accent ? "settings-row-accent" : ""}`}>
      <div className="settings-row-copy">
        <strong>{title}</strong>
        <span>{description}</span>
      </div>
      <div className="settings-row-control">{children}</div>
    </div>
  );
}

function SettingsView({ phase, onClose, onOpenPermissions }: SettingsViewProps) {
  const [activeSection, setActiveSection] = useState<SectionId>("general");
  const [query, setQuery] = useState("");
  const [wakeWord, setWakeWord] = useState(true);
  const [memory, setMemory] = useState(true);
  const [webSources, setWebSources] = useState(true);
  const [notifications, setNotifications] = useState(true);
  const [notificationEvents, setNotificationEvents] = useState<Record<string, boolean>>({
    "Task completed": true,
    "Qronos needs attention": true,
    Errors: true,
    Warnings: true,
    Updates: true,
  });
  const [launchAtStartup, setLaunchAtStartup] = useState(true);
  const [tray, setTray] = useState(true);
  const [theme, setTheme] = useState("System");
  const [uiLanguage, setUiLanguage] = useState("فارسی");
  const [responseLanguage, setResponseLanguage] = useState("Auto");
  const [sensitivity, setSensitivity] = useState(56);
  const [responseLength, setResponseLength] = useState("Balanced");
  const [thinkingMode, setThinkingMode] = useState("Adaptive");
  const [performance, setPerformance] = useState("Balanced");
  const [searchDepth, setSearchDepth] = useState("Balanced");
  const [instructions, setInstructions] = useState("");
  const [toast, setToast] = useState("");

  const visibleSections = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return sections;
    return sections.filter((section) =>
      [section.label, section.english, ...section.keywords]
        .join(" ")
        .toLowerCase()
        .includes(normalized),
    );
  }, [query]);

  const showToast = (message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(""), 1900);
  };

  const chooseSection = (id: SectionId) => {
    setActiveSection(id);
    setQuery("");
  };

  const phaseClass =
    phase === "entering-settings"
      ? "settings-view-entering"
      : phase === "settings"
        ? "settings-view-visible"
        : phase === "leaving-settings"
          ? "settings-view-leaving"
          : "settings-view-hidden";

  const renderGeneral = () => (
    <>
      <SectionHeader kicker="APPLICATION" title="عمومی" description="ظاهر، زبان و رفتار شروع برنامه را تنظیم کنید." />
      <SettingGroup title="APPEARANCE" index="01">
        <SettingRow title="پوسته برنامه" description="ظاهر Qronos را با Windows هماهنگ کنید.">
          <Select value={theme} onChange={setTheme} options={["System", "Dark", "Light"]} />
        </SettingRow>
        <SettingRow title="مقیاس رابط" description="اندازه عناصر رابط کاربری.">
          <Select value="100%" onChange={() => undefined} options={["90%", "100%", "110%"]} />
        </SettingRow>
      </SettingGroup>
      <SettingGroup title="LANGUAGE" index="02">
        <SettingRow title="زبان رابط" description="زبان منوها و کنترل‌های Qronos.">
          <Select value={uiLanguage} onChange={setUiLanguage} options={["فارسی", "English"]} />
        </SettingRow>
        <SettingRow title="زبان پاسخ Qronos" description="مستقل از زبان رابط کاربری.">
          <Select value={responseLanguage} onChange={setResponseLanguage} options={["Auto", "فارسی", "English"]} />
        </SettingRow>
      </SettingGroup>
      <SettingGroup title="STARTUP" index="03">
        <SettingRow title="اجرا همراه Windows" description="Qronos پس از ورود شما آماده شود.">
          <Toggle checked={launchAtStartup} onChange={setLaunchAtStartup} label="Launch with Windows" />
        </SettingRow>
        <SettingRow title="حفظ در System Tray" description="با بستن پنجره، Qronos در پس‌زمینه فعال بماند.">
          <Toggle checked={tray} onChange={setTray} label="Keep in system tray" />
        </SettingRow>
      </SettingGroup>
      <SettingGroup title="ABOUT QRONOS" index="04">
        <div className="settings-about-card">
          <div><span>QRONOS</span><strong>STANDARD EDITION</strong><small>VERSION 0.1.0 • DEVELOPMENT BUILD</small></div>
          <button type="button" onClick={() => showToast("بررسی نسخه در Backend آینده فعال می‌شود.")}>CHECK FOR UPDATES</button>
        </div>
      </SettingGroup>
    </>
  );

  const renderVoice = () => (
    <>
      <SectionHeader kicker="VOICE INTERACTION" title="صدا و Wake Word" description="نحوه شنیدن و پاسخ صوتی Qronos را کنترل کنید." />
      <SettingGroup title="WAKE WORD" index="01">
        <SettingRow title="Wake Word" description="فعال‌سازی محلی Qronos با عبارت آموزش‌دیده." accent>
          <Toggle checked={wakeWord} onChange={setWakeWord} label="Wake Word" />
        </SettingRow>
        <SettingRow title="عبارت بیدارباش" description="این مدل برای عبارت ثابت Qronos آموزش دیده است.">
          <div className="settings-locked-value"><span>QRONOS</span><small>LOCKED</small></div>
        </SettingRow>
        <SettingRow title="حساسیت تشخیص" description="تعادل بین تشخیص بهتر و فعال‌شدن ناخواسته.">
          <div className="settings-slider-wrap">
            <input aria-label="Wake Word sensitivity" type="range" min="0" max="100" value={sensitivity} onChange={(event) => setSensitivity(Number(event.target.value))} />
            <div><span>LOW</span><strong>BALANCED</strong><span>HIGH</span></div>
          </div>
        </SettingRow>
        <div className="settings-action-row">
          <div><span className="settings-listening-dot" /><strong>WAKE DETECTION TEST</strong><small>تست کوتاه چندثانیه‌ای</small></div>
          <button type="button" onClick={() => showToast("Test Wake Word برای اتصال به Voice Runtime آماده است.")}>TEST WAKE WORD</button>
        </div>
      </SettingGroup>
      <SettingGroup title="INPUT & RECOGNITION" index="02">
        <SettingRow title="میکروفون" description="دستگاه ورودی پیش‌فرض Windows.">
          <Select value="Default Microphone" onChange={() => undefined} options={["Default Microphone"]} />
        </SettingRow>
        <div className="settings-level-meter"><span>INPUT LEVEL</span><div>{Array.from({ length: 18 }, (_, i) => <i key={i} className={i < 11 ? "active" : ""} />)}</div><small>LIVE PREVIEW</small></div>
        <SettingRow title="زبان تشخیص" description="تشخیص خودکار گفتار فارسی و انگلیسی.">
          <Select value="Persian + English" onChange={() => undefined} options={["Persian + English", "Persian", "English"]} />
        </SettingRow>
      </SettingGroup>
      <SettingGroup title="VOICE OUTPUT" index="03">
        <SettingRow title="صدای Qronos" description="صدای پیش‌فرض خروجی.">
          <Select value="Qronos Default" onChange={() => undefined} options={["Qronos Default"]} />
        </SettingRow>
        <SettingRow title="سرعت صدا" description="سرعت پخش پاسخ‌های صوتی.">
          <div className="settings-mini-slider"><input aria-label="Voice speed" type="range" min="70" max="130" defaultValue="100" /><strong>1.0×</strong></div>
        </SettingRow>
      </SettingGroup>
    </>
  );

  const renderBehavior = () => (
    <>
      <SectionHeader kicker="INTERACTION PROFILE" title="رفتار و شخصی‌سازی" description="سبک همکاری را شخصی‌سازی کنید؛ هسته تحلیلی Qronos ثابت می‌ماند." />
      <SettingGroup title="RESPONSE STYLE" index="01">
        <SettingRow title="طول پاسخ" description="میزان جزئیات پاسخ‌های معمول."><Segmented value={responseLength} onChange={setResponseLength} options={["Concise", "Balanced", "Detailed"]} /></SettingRow>
        <SettingRow title="Thinking Mode" description="میزان تحلیل براساس پیچیدگی درخواست."><Segmented value={thinkingMode} onChange={setThinkingMode} options={["Fast", "Adaptive", "Deep"]} /></SettingRow>
      </SettingGroup>
      <SettingGroup title="PERSONAL INSTRUCTIONS" index="02">
        <div className="settings-instructions">
          <div><strong>دستورهای شخصی</strong><span>به Qronos بگویید ترجیح می‌دهید چگونه با شما کار کند.</span></div>
          <textarea value={instructions} onChange={(event) => setInstructions(event.target.value)} placeholder="مثال: مفاهیم فنی را با نمونه‌های عملی توضیح بده..." />
          <footer><small>{instructions.length} / 1200</small><button type="button" onClick={() => showToast("تغییرات UI ثبت شد؛ ذخیره دائمی هنوز متصل نیست.")}>SAVE CHANGES</button></footer>
        </div>
      </SettingGroup>
      <div className="settings-core-note"><i /><div><strong>CORE BEHAVIOR PROTECTED</strong><span>Critical Thinking، Security و Evidence Checking از این صفحه غیرفعال نمی‌شوند.</span></div></div>
    </>
  );

  const renderMemory = () => (
    <>
      <SectionHeader kicker="PERSISTENT CONTEXT" title="حافظه" description="اطلاعات ماندگاری را که Qronos برای گفتگوهای بعدی نگه می‌دارد مدیریت کنید." />
      <SettingGroup title="MEMORY CONTROL" index="01">
        <SettingRow title="Qronos Memory" description="استفاده از اطلاعات ماندگار در گفتگوهای آینده." accent><Toggle checked={memory} onChange={setMemory} label="Qronos Memory" /></SettingRow>
      </SettingGroup>
      <div className="settings-empty-system">
        <div className="settings-dna-mark"><i /><i /><i /></div>
        <span>MEMORY MANAGER</span><strong>آماده اتصال به Memory Service</strong><p>تعداد و محتوای حافظه‌ها فقط پس از اتصال داده واقعی نمایش داده می‌شود.</p>
        <button type="button" disabled>MANAGE MEMORY</button>
      </div>
    </>
  );

  const renderPerformance = () => (
    <>
      <SectionHeader kicker="RESOURCE POLICY" title="عملکرد" description="Qronos چگونه زیر فشار سیستم منابع خود را مدیریت کند." />
      <SettingGroup title="PERFORMANCE PROFILE" index="01">
        <div className="settings-profile-grid">
          {[{id:"Eco",title:"ECO",copy:"کمترین مصرف منابع"},{id:"Balanced",title:"BALANCED",copy:"تعادل سرعت و مصرف"},{id:"Performance",title:"PERFORMANCE",copy:"بیشترین سرعت مجاز"}].map((item) => (
            <button key={item.id} type="button" className={performance === item.id ? "active" : ""} onClick={() => setPerformance(item.id)}><i /><strong>{item.title}</strong><span>{item.copy}</span>{item.id === "Balanced" && <small>RECOMMENDED</small>}</button>
          ))}
        </div>
      </SettingGroup>
      <SettingGroup title="SYSTEM PROTECTION" index="02">
        <div className="settings-protection-card"><div className="settings-shield"><span>✓</span></div><div><span>ALWAYS ACTIVE</span><strong>System Priority Protection</strong><p>Qronos هنگام نیاز برنامه فعال شما، مصرف منابع خودش را کاهش می‌دهد.</p></div><small>PROTECTED</small></div>
      </SettingGroup>
      <div className="settings-telemetry-note"><span>LIVE RESOURCE TELEMETRY</span><p>اطلاعات زنده CPU، RAM و GPU فقط در Home نمایش داده می‌شود.</p></div>
    </>
  );

  const renderWeb = () => (
    <>
      <SectionHeader kicker="RESEARCH POLICY" title="وب و جست‌وجو" description="عمق جست‌وجو و نحوه نمایش منابع را تعیین کنید." />
      <SettingGroup title="SEARCH BEHAVIOR" index="01">
        <SettingRow title="عمق جست‌وجو" description="سرعت و گستردگی بررسی منابع."><Segmented value={searchDepth} onChange={setSearchDepth} options={["Quick", "Balanced", "Deep"]} /></SettingRow>
      </SettingGroup>
      <SettingGroup title="SOURCES" index="02">
        <SettingRow title="نمایش منابع" description="منابع استفاده‌شده همراه پاسخ نمایش داده شوند."><Toggle checked={webSources} onChange={setWebSources} label="Show sources" /></SettingRow>
        <div className="settings-chip-list"><button className="active" type="button">GENERAL WEB</button><button className="active" type="button">DOCUMENTATION</button><button className="active" type="button">ACADEMIC</button><button type="button">NEWS</button></div>
      </SettingGroup>
      <PermissionDependency label="WEB ACCESS" onOpenPermissions={onOpenPermissions} />
    </>
  );

  const renderNotifications = () => (
    <>
      <SectionHeader kicker="ATTENTION CHANNELS" title="اعلان‌ها" description="مشخص کنید Qronos چه زمان و از چه روشی توجه شما را جلب کند." />
      <SettingGroup title="EVENTS" index="01">
        <SettingRow title="اعلان‌های اصلی" description="پایان کار، خطاها، هشدارها و نیاز به تأیید."><Toggle checked={notifications} onChange={setNotifications} label="Main notifications" /></SettingRow>
        {["Task completed", "Qronos needs attention", "Errors", "Warnings", "Updates"].map((item) => <div className="settings-check-row" key={item}><span>{item}</span><Toggle checked={notificationEvents[item]} onChange={(next) => setNotificationEvents((current) => ({ ...current, [item]: next }))} label={item} /></div>)}
      </SettingGroup>
      <SettingGroup title="DELIVERY" index="02">
        <div className="settings-chip-list"><button className="active" type="button">WINDOWS</button><button className="active" type="button">QRONOS CENTER</button><button type="button">SOUND</button></div>
      </SettingGroup>
    </>
  );

  const renderData = () => (
    <>
      <SectionHeader kicker="USER-OWNED DATA" title="داده و پشتیبان‌گیری" description="گفتگوها، فایل‌های پیوست و تنظیمات شخصی خود را مدیریت کنید." />
      <div className="settings-data-hero"><div className="settings-storage-orbit"><i /><i /><span /></div><div><span>STORAGE OVERVIEW</span><strong>در انتظار اتصال به User Data Manager</strong><p>تا زمان دریافت داده واقعی، حجم ساختگی نمایش داده نمی‌شود.</p></div><small>UI READY</small></div>
      <SettingGroup title="CONVERSATIONS" index="01">
        <div className="settings-action-grid"><ActionCard title="MANAGE HISTORY" copy="جست‌وجو، انتخاب و حذف گفتگوها" /><ActionCard title="EXPORT" copy="Markdown، JSON یا Text" /></div>
      </SettingGroup>
      <SettingGroup title="BACKUP & RESTORE" index="02">
        <div className="settings-action-grid"><ActionCard title="CREATE BACKUP" copy="آرشیو نسخه‌دار داده‌های کاربر" /><ActionCard title="RESTORE" copy="بازیابی انتخابی اطلاعات" /></div>
      </SettingGroup>
      <div className="settings-security-note"><span>ENCRYPTED BACKUP RECOMMENDED</span><p>Backup فقط داده‌های متعلق به کاربر را شامل می‌شود.</p></div>
    </>
  );

  const contentBySection: Record<SectionId, () => React.ReactNode> = {
    general: renderGeneral,
    voice: renderVoice,
    behavior: renderBehavior,
    memory: renderMemory,
    performance: renderPerformance,
    web: renderWeb,
    notifications: renderNotifications,
    data: renderData,
  };

  return (
    <section className={`settings-view ${phaseClass}`} aria-hidden={phase === "home"}>
      <div className="settings-atmosphere" aria-hidden="true">
        <div className="settings-field settings-field-a" />
        <div className="settings-field settings-field-b" />
        <div className="settings-orbit settings-orbit-a" />
        <div className="settings-orbit settings-orbit-b" />
        <div className="settings-particles">{particles.map((particle) => <i key={particle} style={{ "--particle-index": particle } as React.CSSProperties} />)}</div>
      </div>

      <div className="settings-shell">
        <header className="settings-header">
          <div className="settings-heading">
            <span>SYSTEM CONFIGURATION</span>
            <h1>تنظیمات Qronos</h1>
            <p>رفتار، صدا و ترجیحات سیستم را مدیریت کنید.</p>
          </div>
          <div className="settings-heading settings-heading-english" dir="ltr">
            <span>Q R O N O S</span>
            <h2>Qronos Settings</h2>
            <p>Manage system behavior, voice, and preferences.</p>
          </div>
          <button type="button" className="settings-close" onClick={onClose} aria-label="بازگشت به خانه"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 5-7 7 7 7" /></svg><span>بازگشت</span></button>
        </header>

        <div className="settings-layout">
          <aside className="settings-sidebar">
            <label className="settings-search">
              <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="5.5" /><path d="m15 15 4.2 4.2" /></svg>
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="جست‌وجو در تنظیمات..." />
              {query && <button type="button" onClick={() => setQuery("")}>×</button>}
            </label>
            <nav aria-label="بخش‌های تنظیمات">
              {groups.map((group) => {
                const groupSections = visibleSections.filter((section) => section.group === group);
                if (!groupSections.length) return null;
                return <div className="settings-nav-group" key={group}><header><span>{group}</span><i /></header>{groupSections.map((section) => <button key={section.id} type="button" className={activeSection === section.id ? "active" : ""} onClick={() => chooseSection(section.id)}><span className="settings-nav-index">{section.icon}</span><span><strong>{section.label}</strong><small>{section.english}</small></span><i /></button>)}</div>;
              })}
              {!visibleSections.length && <div className="settings-no-results"><strong>نتیجه‌ای پیدا نشد</strong><span>عبارت دیگری جست‌وجو کنید.</span></div>}
            </nav>
            <button type="button" className="settings-permissions-link" onClick={onOpenPermissions}><span className="settings-permissions-shield">◇</span><span><strong>مجوزها</strong><small>PERMISSIONS</small></span><i>↗</i></button>
          </aside>

          <main className="settings-content" key={activeSection}>{contentBySection[activeSection]()}</main>
        </div>
      </div>
      {toast && <div className="settings-toast"><i /><span>{toast}</span></div>}
    </section>
  );
}

function SectionHeader({ kicker, title, description }: { kicker: string; title: string; description: string }) {
  return <header className="settings-section-header"><div><span>{kicker}</span><h2>{title}</h2><p>{description}</p></div><i><span /></i></header>;
}

function SettingGroup({ title, index, children }: { title: string; index: string; children: React.ReactNode }) {
  return <section className="settings-group"><header><span>{title}</span><i /><small>{index}</small></header><div className="settings-group-body">{children}</div></section>;
}

function Select({ value, onChange, options }: { value: string; onChange: (value: string) => void; options: string[] }) {
  return <label className="settings-select"><select value={value} onChange={(event) => onChange(event.target.value)}>{options.map((option) => <option key={option}>{option}</option>)}</select><span>⌄</span></label>;
}

function Segmented({ value, onChange, options }: { value: string; onChange: (value: string) => void; options: string[] }) {
  return <div className="settings-segmented">{options.map((option) => <button key={option} type="button" className={value === option ? "active" : ""} onClick={() => onChange(option)}>{option}</button>)}</div>;
}

function PermissionDependency({ label, onOpenPermissions }: { label: string; onOpenPermissions: () => void }) {
  return <div className="settings-permission-dependency"><div><i /><span>PERMISSION DEPENDENCY</span><strong>{label}</strong><small>مجوز دسترسی فقط از بخش Permissions مدیریت می‌شود.</small></div><button type="button" onClick={onOpenPermissions}>OPEN PERMISSIONS ↗</button></div>;
}

function ActionCard({ title, copy }: { title: string; copy: string }) {
  return <button type="button" className="settings-action-card" disabled><span>{title}</span><strong>{copy}</strong><small>BACKEND REQUIRED</small><i>→</i></button>;
}

export default SettingsView;
