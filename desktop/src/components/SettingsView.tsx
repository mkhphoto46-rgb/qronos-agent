import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

import {
  approveMemory,
  deleteMemory,
  rebuildMemoryIndex,
  refreshMemory,
  retryPendingMemory,
  setMemoryEnabled,
  supersedeMemory,
  useMemoryStore,
} from "../memoryStore";

import {
  refreshSettings,
  setSetting,
  useSettingsStore,
  type SettingValue,
} from "../settingsStore";

import {
  type AudioOutputDevice,
  type AudioOutputDevicesResponse,
} from "../audio/audioOutput";

import type { DeviceLinkController } from "../deviceLink";
import DeviceLinkPanel from "./DeviceLinkPanel";

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
  deviceLink: DeviceLinkController;
};

type SectionId =
  | "general"
  | "hotkeys"
  | "voice"
  | "behavior"
  | "memory"
  | "performance"
  | "web"
  | "notifications"
  | "link"
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
  { id: "hotkeys", group: "GENERAL", label: "کلیدهای میانبر", english: "Hotkeys & Shortcuts", icon: "02", keywords: ["hotkey", "shortcut", "keyboard", "کلید", "میانبر", "کیبورد"] },
  { id: "voice", group: "QRONOS", label: "صدا و Wake Word", english: "Voice & Wake Word", icon: "03", keywords: ["voice", "wake", "microphone", "speaker", "output", "sensitivity", "صدا", "میکروفون", "خروجی", "حساسیت"] },
  { id: "behavior", group: "QRONOS", label: "رفتار و شخصی‌سازی", english: "Behavior", icon: "04", keywords: ["response", "thinking", "instructions", "پاسخ", "رفتار", "دستور"] },
  { id: "memory", group: "QRONOS", label: "حافظه", english: "Memory", icon: "05", keywords: ["memory", "remember", "حافظه", "یادآوری"] },
  { id: "performance", group: "SYSTEM", label: "عملکرد", english: "Performance", icon: "06", keywords: ["eco", "balanced", "performance", "priority", "عملکرد", "منابع"] },
  { id: "web", group: "SYSTEM", label: "وب و جست‌وجو", english: "Web & Search", icon: "07", keywords: ["web", "search", "sources", "citation", "وب", "جستجو", "منابع"] },
  { id: "notifications", group: "SYSTEM", label: "اعلان‌ها", english: "Notifications", icon: "08", keywords: ["notification", "warning", "sound", "اعلان", "هشدار", "صدا"] },
  { id: "link", group: "SYSTEM", label: "اتصال گوشی", english: "Device Link", icon: "09", keywords: ["phone", "link", "pair", "device", "revoke", "گوشی", "اتصال", "جفت", "دستگاه"] },
  { id: "data", group: "DATA", label: "داده و پشتیبان‌گیری", english: "Data & Backup", icon: "10", keywords: ["data", "backup", "export", "restore", "داده", "پشتیبان", "خروجی"] },
];

type HotkeyBinding = {
  actionId: string;
  title: string;
  english: string;
  description: string;
  accelerator: string | null;
  defaultAccelerator: string | null;
  scope: "global" | "inApp";
  enabled: boolean;
  status: string;
};

type HotkeySettings = { schemaVersion: number; bindings: HotkeyBinding[] };

type MicrophoneDevice = {
  id: string;
  name: string;
  hostApi: string;
  deviceIndex: number | null;
  isDefault: boolean;
  maxInputChannels: number;
};

type AudioDevicesResponse = {
  ok: boolean;
  devices: MicrophoneDevice[];
  selectedDeviceId?: string;
  selectionRemapped?: boolean;
};

type SystemIntegrationSnapshot = {
  launchAtStartup: boolean;
  keepInTray: boolean;
};

const fallbackHotkeys: HotkeySettings = { schemaVersion: 1, bindings: [] };

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

function SettingsView({ phase, onClose, onOpenPermissions, deviceLink }: SettingsViewProps) {
  const deviceLinkView = useSyncExternalStore(deviceLink.subscribe, deviceLink.getSnapshot);
  const [activeSection, setActiveSection] = useState<SectionId>("general");
  const [query, setQuery] = useState("");
  const [sensitivityDraft, setSensitivityDraft] = useState(56);
  const voiceMutationRef = useRef(false);
  const [webSources, setWebSources] = useState(true);
  const [notifications, setNotifications] = useState(true);
  const [notificationEvents, setNotificationEvents] = useState<Record<string, boolean>>({
    "Task completed": true,
    "Qronos needs attention": true,
    Errors: true,
    Warnings: true,
    Updates: true,
  });
  const [launchAtStartup, setLaunchAtStartup] = useState(false);
  const [tray, setTray] = useState(false);
  const [systemIntegrationLoading, setSystemIntegrationLoading] = useState(true);
  const [microphones, setMicrophones] = useState<MicrophoneDevice[]>([
    {
      id: "",
      name: "Default Microphone",
      hostApi: "Windows Default",
      deviceIndex: null,
      isDefault: true,
      maxInputChannels: 1,
    },
  ]);
  const [microphonesLoading, setMicrophonesLoading] = useState(false);
  const [canonicalInputDeviceId, setCanonicalInputDeviceId] = useState("");
  const [inputLevel, setInputLevel] = useState(0);
  const [outputDevices, setOutputDevices] = useState<AudioOutputDevice[]>([
    {
      id: "",
      name: "System Default Output",
      hostApi: "Windows / system default",
      deviceIndex: null,
      isDefault: true,
      maxOutputChannels: 2,
      defaultSampleRate: 0,
    },
  ]);
  const [outputDevicesLoading, setOutputDevicesLoading] = useState(false);
  const [canonicalOutputDeviceId, setCanonicalOutputDeviceId] = useState("");
  const [theme, setTheme] = useState("System");
  const [uiLanguage, setUiLanguage] = useState("فارسی");
  const [responseLanguage, setResponseLanguage] = useState("Auto");
  const [responseLength, setResponseLength] = useState("Balanced");
  const [thinkingMode, setThinkingMode] = useState("Adaptive");
  const [performance, setPerformance] = useState("Balanced");
  const [searchDepth, setSearchDepth] = useState("Balanced");
  const [instructions, setInstructions] = useState("");
  const [toast, setToast] = useState("");
  const [hotkeys, setHotkeys] = useState<HotkeySettings>(fallbackHotkeys);
  const [hotkeysLoading, setHotkeysLoading] = useState(true);
  const [capturingAction, setCapturingAction] = useState<string | null>(null);
  const [capturedShortcut, setCapturedShortcut] = useState("");
  const captureRef = useRef<HTMLDivElement | null>(null);

  const memoryState = useMemoryStore();
  const settingsState = useSettingsStore();

  const wakeWord =
    typeof settingsState.values["voice.wake_word_enabled"] === "boolean"
      ? (settingsState.values["voice.wake_word_enabled"] as boolean)
      : true;

  const wakeSensitivity =
    typeof settingsState.values["voice.wake_sensitivity"] === "number"
      ? Math.max(
          0,
          Math.min(
            100,
            Math.round(
              settingsState.values["voice.wake_sensitivity"] as number,
            ),
          ),
        )
      : 56;

  const voiceLanguageCode =
    typeof settingsState.values["voice.language"] === "string"
      ? (settingsState.values["voice.language"] as string)
      : "fa";

  const voiceLanguageLabel =
    voiceLanguageCode === "auto"
      ? "Persian + English"
      : voiceLanguageCode === "en"
        ? "English"
        : "Persian";

  const inputDeviceId =
    typeof settingsState.values["voice.input_device_id"] === "string"
      ? (settingsState.values["voice.input_device_id"] as string)
      : "";

  const microphoneSelectValue =
    microphones.some((device) => device.id === inputDeviceId)
      ? inputDeviceId
      : microphones.some((device) => device.id === canonicalInputDeviceId)
        ? canonicalInputDeviceId
        : inputDeviceId;

  const outputDeviceId =
    typeof settingsState.values["voice.output_device_id"] === "string"
      ? (settingsState.values["voice.output_device_id"] as string)
      : "";

  const outputSelectValue =
    outputDevices.some((device) => device.id === outputDeviceId)
      ? outputDeviceId
      : outputDevices.some((device) => device.id === canonicalOutputDeviceId)
        ? canonicalOutputDeviceId
        : outputDeviceId;

  const uiScalePercent =
    typeof settingsState.values["appearance.ui_scale_percent"] === "number"
      ? Math.max(
          90,
          Math.min(
            110,
            Math.round(
              settingsState.values["appearance.ui_scale_percent"] as number,
            ),
          ),
        )
      : 100;

  const persistedTray =
    settingsState.values["system.keep_in_tray"] === true;

  const refreshSystemIntegration = async () => {
    setSystemIntegrationLoading(true);

    try {
      const state = await invoke<SystemIntegrationSnapshot>(
        "get_system_integration_state",
      );
      setLaunchAtStartup(state.launchAtStartup);
      setTray(state.keepInTray);
    } catch (error) {
      console.error("[Qronos System Integration] refresh failed:", error);
    } finally {
      setSystemIntegrationLoading(false);
    }
  };

  const refreshMicrophones = async () => {
    setMicrophonesLoading(true);

    try {
      const result = await invoke<AudioDevicesResponse>(
        "settings_command",
        {
          operation: "audio_devices",
          payload: {},
        },
      );

      if (!result.ok || !Array.isArray(result.devices)) {
        throw new Error("Microphone backend returned an invalid device list.");
      }

      setMicrophones(result.devices);
      setCanonicalInputDeviceId(
        typeof result.selectedDeviceId === "string"
          ? result.selectedDeviceId
          : "",
      );
    } catch (error) {
      console.error("[Qronos Microphone] enumeration failed:", error);
      showToast(String(error));
    } finally {
      setMicrophonesLoading(false);
    }
  };

  const refreshOutputDevices = async () => {
    setOutputDevicesLoading(true);

    try {
      const result = await invoke<AudioOutputDevicesResponse>(
        "settings_command",
        {
          operation: "audio_output_devices",
          payload: {},
        },
      );

      if (!result.ok || !Array.isArray(result.devices)) {
        throw new Error(
          "Audio output backend returned an invalid device list.",
        );
      }

      setOutputDevices(result.devices);
      setCanonicalOutputDeviceId(
        typeof result.selectedDeviceId === "string"
          ? result.selectedDeviceId
          : "",
      );
    } catch (error) {
      console.error(
        "[Qronos Voice Output] native enumeration failed:",
        error,
      );

      setOutputDevices([
        {
          id: "",
          name: "System Default Output",
          hostApi: "Windows / system default",
          deviceIndex: null,
          isDefault: true,
          maxOutputChannels: 2,
          defaultSampleRate: 0,
        },
      ]);
      setCanonicalOutputDeviceId("");
      showToast(String(error));
    } finally {
      setOutputDevicesLoading(false);
    }
  };

  useEffect(() => {
    if (phase === "settings" || phase === "entering-settings") {
      void refreshMemory();
      void refreshSettings().catch(() => undefined);
      void refreshSystemIntegration();
      void refreshMicrophones();
      void refreshOutputDevices();
    }
  }, [phase]);

  useEffect(() => {
    if (
      activeSection !== "voice" ||
      (phase !== "settings" && phase !== "entering-settings")
    ) {
      return;
    }

    const refreshNativeAudioDevices = () => {
      void refreshMicrophones();
      void refreshOutputDevices();
    };

    const handleWindowFocus = () => {
      refreshNativeAudioDevices();
    };

    const handleDeviceChange = () => {
      refreshNativeAudioDevices();
    };

    window.addEventListener(
      "focus",
      handleWindowFocus,
    );

    navigator.mediaDevices?.addEventListener(
      "devicechange",
      handleDeviceChange,
    );

    return () => {
      window.removeEventListener(
        "focus",
        handleWindowFocus,
      );

      navigator.mediaDevices?.removeEventListener(
        "devicechange",
        handleDeviceChange,
      );
    };
  }, [activeSection, phase]);

  useEffect(() => {
    setSensitivityDraft(wakeSensitivity);
  }, [wakeSensitivity, settingsState.revision]);

  useEffect(() => {
    if (
      activeSection !== "voice" ||
      (phase !== "settings" && phase !== "entering-settings")
    ) {
      setInputLevel(0);
      return;
    }

    let disposed = false;
    let unlisten: (() => void) | undefined;
    let meterStarted = false;

    const connect = async () => {
      unlisten = await listen<{
        eventType: string;
        status: string;
        message: string;
      }>(
        "qronos://runtime-event",
        (event) => {
          if (event.payload.eventType !== "microphone_input_level") {
            return;
          }

          try {
            const payload = JSON.parse(event.payload.message) as {
              level?: unknown;
              active?: unknown;
            };

            const level =
              typeof payload.level === "number"
                ? Math.max(0, Math.min(1, payload.level))
                : 0;

            if (!disposed) {
              setInputLevel(level);
            }
          } catch {
            if (!disposed) {
              setInputLevel(0);
            }
          }
        },
      );

      const status = await invoke<{ running: boolean }>(
        "get_runtime_status",
      );

      if (!status.running || disposed) {
        return;
      }

      await invoke(
        "send_runtime_action",
        {
          actionId: "qronos.input_meter_start",
        },
      );

      meterStarted = true;
    };

    void connect().catch((error) => {
      console.error("[Qronos Microphone Meter]", error);
    });

    return () => {
      disposed = true;
      unlisten?.();
      setInputLevel(0);

      if (meterStarted) {
        void invoke(
          "send_runtime_action",
          {
            actionId: "qronos.input_meter_stop",
          },
        ).catch(() => undefined);
      }
    };
  }, [activeSection, phase]);

  const [memoryManagerOpen, setMemoryManagerOpen] = useState(false);
  const [editingMemoryId, setEditingMemoryId] = useState<string | null>(null);
  const [editingMemoryValue, setEditingMemoryValue] = useState("");
  const [memoryBusy, setMemoryBusy] = useState(false);

  useEffect(() => {
    invoke<HotkeySettings>("get_hotkey_settings")
      .then(setHotkeys)
      .catch(() => setHotkeys(fallbackHotkeys))
      .finally(() => setHotkeysLoading(false));
  }, []);

  useEffect(() => {
    if (capturingAction) captureRef.current?.focus();
  }, [capturingAction]);

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

  const reloadVoiceSettingsInRuntime = async () => {
    const status = await invoke<{ running: boolean }>(
      "get_runtime_status",
    );

    if (!status.running) {
      return false;
    }

    await invoke(
      "send_runtime_action",
      {
        actionId: "qronos.settings_reload",
      },
    );

    return true;
  };

  const applyVoiceSetting = async (
    key: string,
    value: SettingValue,
    successMessage: string,
  ) => {
    if (voiceMutationRef.current) {
      return;
    }

    voiceMutationRef.current = true;

    try {
      await setSetting(
        key,
        value,
      );

      try {
        const appliedNow =
          await reloadVoiceSettingsInRuntime();

        showToast(
          appliedNow
            ? successMessage
            : `${successMessage} در اجرای بعدی Runtime اعمال می‌شود.`,
        );
      } catch (runtimeError) {
        console.error(
          "[Qronos Settings] runtime apply failed:",
          runtimeError,
        );

        showToast(
          "تنظیم ذخیره شد؛ Runtime فعلی نتوانست آن را اعمال کند و با اجرای بعدی اعمال می‌شود.",
        );
      }
    } catch (error) {
      console.error(
        "[Qronos Settings] save failed:",
        error,
      );

      void refreshSettings().catch(
        () => undefined,
      );

      showToast(
        String(error),
      );
    } finally {
      voiceMutationRef.current = false;
    }
  };

  const applyOutputSetting = async (
    deviceId: string,
    successMessage: string,
  ) => {
    try {
      await setSetting(
        "voice.output_device_id",
        deviceId,
      );
      await refreshOutputDevices();
      showToast(successMessage);
    } catch (error) {
      console.error(
        "[Qronos Voice Output] selection failed:",
        error,
      );
      showToast(String(error));
    }
  };

  const commitWakeSensitivity = (
    next: number,
  ) => {
    const cleaned = Math.max(
      0,
      Math.min(
        100,
        Math.round(next),
      ),
    );

    setSensitivityDraft(cleaned);

    if (cleaned === wakeSensitivity) {
      return;
    }

    void applyVoiceSetting(
      "voice.wake_sensitivity",
      cleaned,
      "حساسیت Wake Word ذخیره و اعمال شد.",
    );
  };

  const setLaunchWithWindows = async (
    enabled: boolean,
  ) => {
    try {
      const state = await invoke<SystemIntegrationSnapshot>(
        "set_launch_at_startup",
        { enabled },
      );

      setLaunchAtStartup(state.launchAtStartup);
      setTray(state.keepInTray);
      showToast(
        state.launchAtStartup
          ? "اجرای Qronos همراه Windows فعال شد."
          : "اجرای Qronos همراه Windows غیرفعال شد.",
      );
    } catch (error) {
      showToast(String(error));
      void refreshSystemIntegration();
    }
  };

  const setKeepInTray = async (
    enabled: boolean,
  ) => {
    const previous = persistedTray;

    try {
      const state = await invoke<SystemIntegrationSnapshot>(
        "set_keep_in_tray",
        { enabled },
      );

      try {
        await setSetting(
          "system.keep_in_tray",
          state.keepInTray,
        );
      } catch (persistError) {
        await invoke<SystemIntegrationSnapshot>(
          "set_keep_in_tray",
          { enabled: previous },
        ).catch(() => undefined);

        throw persistError;
      }

      setTray(state.keepInTray);
      showToast(
        state.keepInTray
          ? "System Tray فعال شد."
          : "System Tray غیرفعال شد.",
      );
    } catch (error) {
      showToast(String(error));
      void refreshSettings().catch(() => undefined);
      void refreshSystemIntegration();
    }
  };

  const setInterfaceScale = async (
    label: string,
  ) => {
    const parsed = Number(
      label.replace("%", ""),
    );

    if (!Number.isFinite(parsed)) {
      return;
    }

    try {
      await setSetting(
        "appearance.ui_scale_percent",
        Math.round(parsed),
      );
      showToast("مقیاس رابط ذخیره شد.");
    } catch (error) {
      showToast(String(error));
      void refreshSettings().catch(() => undefined);
    }
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
          <Select
            value={`${uiScalePercent}%`}
            onChange={(next) => {
              void setInterfaceScale(next);
            }}
            options={["90%", "100%", "110%"]}
          />
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
        <SettingRow title="اجرا همراه Windows" description="پس از ورود به Windows، Qronos به‌صورت خودکار اجرا شود.">
          <Toggle
            checked={launchAtStartup}
            onChange={(next) => {
              void setLaunchWithWindows(next);
            }}
            label="Launch with Windows"
            disabled={systemIntegrationLoading}
          />
        </SettingRow>
        <SettingRow title="حفظ در System Tray" description="با بستن پنجره، Qronos در پس‌زمینه فعال بماند و از Tray باز شود.">
          <Toggle
            checked={tray}
            onChange={(next) => {
              void setKeepInTray(next);
            }}
            label="Keep in system tray"
            disabled={systemIntegrationLoading}
          />
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
          <Toggle
            checked={wakeWord}
            onChange={(next) => {
              void applyVoiceSetting(
                "voice.wake_word_enabled",
                next,
                next
                  ? "Wake Word فعال شد."
                  : "Wake Word غیرفعال شد.",
              );
            }}
            label="Wake Word"
          />
        </SettingRow>
        <SettingRow title="عبارت بیدارباش" description="این مدل برای عبارت ثابت Qronos آموزش دیده است.">
          <div className="settings-locked-value"><span>QRONOS</span><small>LOCKED</small></div>
        </SettingRow>
        <SettingRow title="حساسیت تشخیص" description="تعادل بین تشخیص بهتر و فعال‌شدن ناخواسته.">
          <div className="settings-slider-wrap">
            <input
              aria-label="Wake Word sensitivity"
              type="range"
              min="0"
              max="100"
              value={sensitivityDraft}
              onChange={(event) => {
                setSensitivityDraft(
                  Number(event.target.value),
                );
              }}
              onPointerUp={(event) => {
                commitWakeSensitivity(
                  Number(event.currentTarget.value),
                );
              }}
              onKeyUp={(event) => {
                commitWakeSensitivity(
                  Number(event.currentTarget.value),
                );
              }}
              onBlur={(event) => {
                commitWakeSensitivity(
                  Number(event.currentTarget.value),
                );
              }}
            />
            <div><span>LOW</span><strong>BALANCED</strong><span>HIGH</span></div>
          </div>
        </SettingRow>
        <div className="settings-action-row">
          <div><span className="settings-listening-dot" /><strong>WAKE DETECTION TEST</strong><small>تست کوتاه چندثانیه‌ای</small></div>
          <button type="button" onClick={() => showToast("Test Wake Word برای اتصال به Voice Runtime آماده است.")}>TEST WAKE WORD</button>
        </div>
      </SettingGroup>
      <SettingGroup title="INPUT & RECOGNITION" index="02">
        <SettingRow title="میکروفون" description="دستگاه ورودی صوتی Windows برای Qronos.">
          <label className="settings-select">
            <select
              value={microphoneSelectValue}
              disabled={microphonesLoading}
              onChange={(event) => {
                setCanonicalInputDeviceId(event.target.value);
                void applyVoiceSetting(
                  "voice.input_device_id",
                  event.target.value,
                  "میکروفون ورودی ذخیره و اعمال شد.",
                );
              }}
            >
              {inputDeviceId &&
                !microphones.some(
                  (device) => device.id === microphoneSelectValue,
                ) && (
                  <option value={inputDeviceId}>
                    Unavailable microphone — using system default
                  </option>
                )}
              {microphones.map((device) => (
                <option
                  key={device.id || "__default__"}
                  value={device.id}
                >
                  {device.id
                    ? `${device.name}${device.isDefault ? " • SYSTEM DEFAULT" : ""}`
                    : "Default Microphone"}
                </option>
              ))}
            </select>
            <span>⌄</span>
          </label>
        </SettingRow>
        <div className="settings-level-meter">
          <span>INPUT LEVEL</span>
          <div>
            {Array.from({ length: 18 }, (_, i) => (
              <i
                key={i}
                className={
                  i < Math.round(inputLevel * 18)
                    ? "active"
                    : ""
                }
              />
            ))}
          </div>
          <small>LIVE PREVIEW</small>
        </div>
        <SettingRow title="زبان تشخیص" description="تشخیص خودکار گفتار فارسی و انگلیسی.">
          <Select
            value={voiceLanguageLabel}
            onChange={(next) => {
              const language =
                next === "Persian + English"
                  ? "auto"
                  : next === "English"
                    ? "en"
                    : "fa";

              void applyVoiceSetting(
                "voice.language",
                language,
                "زبان تشخیص گفتار ذخیره و اعمال شد.",
              );
            }}
            options={["Persian + English", "Persian", "English"]}
          />
        </SettingRow>
      </SettingGroup>
      <SettingGroup title="VOICE OUTPUT" index="03">
        <SettingRow title="دستگاه خروجی" description="اسپیکر یا هدفونی که پاسخ صوتی Qronos از آن پخش می‌شود.">
          <label className="settings-select">
            <select
              value={outputSelectValue}
              disabled={outputDevicesLoading}
              onChange={(event) => {
                const next = event.target.value;

                void applyOutputSetting(
                  next,
                  next
                    ? "دستگاه خروجی صدای Qronos ذخیره و اعمال شد."
                    : "خروجی صدای Qronos روی System Default قرار گرفت.",
                );
              }}
            >
              {outputDeviceId &&
                !outputDevices.some(
                  (device) => device.id === outputDeviceId,
                ) && (
                  <option value={outputDeviceId}>
                    Unavailable output — using system default
                  </option>
                )}
              {outputDevices.map((device) => (
                <option
                  key={device.id || "__system_output__"}
                  value={device.id}
                >
                  {device.id
                    ? `${device.name} • ${device.hostApi}`
                    : device.name}
                </option>
              ))}
            </select>
            <span>⌄</span>
          </label>
        </SettingRow>
        <SettingRow title="سرعت صدا" description="سرعت پخش پاسخ‌های صوتی.">
          <div className="settings-mini-slider"><input aria-label="Voice speed" type="range" min="70" max="130" defaultValue="100" /><strong>1.0×</strong></div>
        </SettingRow>
      </SettingGroup>
    </>
  );

  const shortcutFromEvent = (event: React.KeyboardEvent) => {
    const ignored = ["Control", "Shift", "Alt", "Meta"];
    if (ignored.includes(event.key)) return "";
    const parts: string[] = [];
    if (event.ctrlKey) parts.push("Ctrl");
    if (event.altKey) parts.push("Alt");
    if (event.shiftKey) parts.push("Shift");
    if (event.metaKey) parts.push("Super");
    const key = event.key === " " ? "Space" : event.key.length === 1 ? event.key.toUpperCase() : event.key;
    parts.push(key);
    return parts.join("+");
  };

  const saveCapturedHotkey = async () => {
    if (!capturingAction || !capturedShortcut) return;
    try {
      await invoke("validate_hotkey", { actionId: capturingAction, accelerator: capturedShortcut });
      const next = await invoke<HotkeySettings>("update_hotkey", { actionId: capturingAction, accelerator: capturedShortcut });
      setHotkeys(next);
      setCapturingAction(null);
      setCapturedShortcut("");
      showToast("میانبر جدید ثبت شد.");
    } catch (error) {
      showToast(String(error));
    }
  };

  const clearHotkey = async () => {
    if (!capturingAction) return;
    try {
      const next = await invoke<HotkeySettings>("update_hotkey", { actionId: capturingAction, accelerator: null });
      setHotkeys(next);
      setCapturingAction(null);
      setCapturedShortcut("");
      showToast("میانبر پاک شد.");
    } catch (error) { showToast(String(error)); }
  };

  const toggleHotkey = async (actionId: string, enabled: boolean) => {
    try {
      setHotkeys(await invoke<HotkeySettings>("set_hotkey_enabled", { actionId, enabled }));
    } catch (error) { showToast(String(error)); }
  };

  const resetAllHotkeys = async () => {
    try {
      setHotkeys(await invoke<HotkeySettings>("reset_hotkeys"));
      showToast("همه میانبرها به حالت پیش‌فرض برگشتند.");
    } catch (error) { showToast(String(error)); }
  };

  const renderHotkeyRows = (scope: "global" | "inApp") => {
    const items = hotkeys.bindings.filter((item) => item.scope === scope);
    if (hotkeysLoading) return <div className="settings-hotkey-loading">LOADING HOTKEY REGISTRY...</div>;
    if (!items.length) return <div className="settings-hotkey-loading">HOTKEY BACKEND IS NOT AVAILABLE</div>;
    return <div className="settings-hotkey-list">{items.map((item) => (
      <div className="settings-hotkey-row" key={item.actionId}>
        <div className="settings-hotkey-copy"><strong>{item.title}</strong><span>{item.english}</span><small>{item.description}</small></div>
        <div className="settings-hotkey-controls">
          <span className={`settings-hotkey-status status-${item.status.toLowerCase()}`}>{item.status}</span>
          <kbd>{item.accelerator || "UNASSIGNED"}</kbd>
          <button type="button" onClick={() => { setCapturingAction(item.actionId); setCapturedShortcut(item.accelerator || ""); }}>CHANGE</button>
          {scope === "global" && <Toggle checked={item.enabled} onChange={(next) => toggleHotkey(item.actionId, next)} label={item.english} />}
        </div>
      </div>
    ))}</div>;
  };

  const renderHotkeys = () => (
    <>
      <SectionHeader kicker="INPUT CONTROL" title="کلیدهای میانبر" description="دسترسی سریع به Qronos و بخش‌های اصلی را مدیریت کنید." />
      <SettingGroup title="GLOBAL HOTKEYS" index="01">{renderHotkeyRows("global")}</SettingGroup>
      <SettingGroup title="IN-APP SHORTCUTS" index="02">{renderHotkeyRows("inApp")}</SettingGroup>
      <div className="settings-hotkey-footer"><div><span>SAFE SHORTCUT REGISTRY</span><strong>میانبرها فقط Actionهای تأییدشده Qronos را اجرا می‌کنند.</strong></div><button type="button" onClick={resetAllHotkeys}>RESET TO DEFAULTS</button></div>
      {capturingAction && <div className="settings-hotkey-capture-backdrop">
        <div className="settings-hotkey-capture" ref={captureRef} tabIndex={0} onKeyDown={(event) => { event.preventDefault(); event.stopPropagation(); if (event.key === "Escape") { setCapturingAction(null); setCapturedShortcut(""); return; } const value = shortcutFromEvent(event); if (value) setCapturedShortcut(value); }}>
          <span>PRESS NEW SHORTCUT</span><h3>میانبر جدید را فشار دهید</h3><kbd>{capturedShortcut || "WAITING FOR INPUT..."}</kbd><p>برای لغو، کلید Escape را فشار دهید.</p>
          <footer><button type="button" onClick={() => { setCapturingAction(null); setCapturedShortcut(""); }}>CANCEL</button><button type="button" onClick={clearHotkey}>CLEAR</button><button type="button" className="primary" disabled={!capturedShortcut} onClick={saveCapturedHotkey}>SAVE</button></footer>
        </div>
      </div>}
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

  const runMemoryAction = async (
    action: () => Promise<void>,
    successMessage?: string,
  ) => {
    try {
      setMemoryBusy(true);
      await action();
      if (successMessage) showToast(successMessage);
    } catch (error) {
      showToast(
        `Memory: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      setMemoryBusy(false);
    }
  };

  const beginMemoryEdit = (id: string, value: string) => {
    setEditingMemoryId(id);
    setEditingMemoryValue(value);
  };

  const saveMemoryEdit = async () => {
    const id = editingMemoryId;
    const value = editingMemoryValue.trim();
    if (!id || !value) return;

    await runMemoryAction(
      async () => {
        await supersedeMemory(id, value);
        setEditingMemoryId(null);
        setEditingMemoryValue("");
      },
      "اصلاح حافظه ثبت شد",
    );
  };

  const renderMemory = () => {
    const {
      enabled,
      counts,
      provider,
      index,
      memories,
      loading,
      error,
      databasePath,
      vectorPath,
    } = memoryState;

    const providerLabel = provider.ready
      ? "LOCAL EMBEDDING READY"
      : provider.reason === "embedding_model_not_installed"
        ? "EMBEDDING MODEL NOT INSTALLED"
        : provider.reason === "loading"
          ? "CHECKING EMBEDDING"
          : "EMBEDDING OFFLINE";

    const activeMemories = memories.filter(
      (item) => item.status !== "SUPERSEDED",
    );

    return (
      <>
        <SectionHeader kicker="PERSISTENT CONTEXT" title="حافظه" description="اطلاعات ماندگاری را که Qronos برای گفتگوهای بعدی نگه می‌دارد مدیریت کنید." />

        <SettingGroup title="MEMORY CONTROL" index="01">
          <SettingRow
            title="Qronos Memory"
            description="استفاده از حافظه‌های فعال در گفتگوهای آینده. خاموش کردن این گزینه داده‌ها را حذف نمی‌کند."
            accent
          >
            <Toggle
              checked={enabled}
              onChange={(next) =>
                void runMemoryAction(
                  () => setMemoryEnabled(next),
                  next ? "Memory فعال شد" : "Memory غیرفعال شد",
                )
              }
              label="Qronos Memory"
              disabled={loading || memoryBusy}
            />
          </SettingRow>

          <SettingRow
            title="Vector Index"
            description="هر Memory فعال باید embedding محلی معتبر داشته باشد؛ Vector Index منبع حقیقت نیست و قابل بازسازی است."
          >
            <div className="settings-memory-index-state">
              <strong className={index.healthy ? "ready" : "attention"}>
                {index.healthy ? "HEALTHY" : "ATTENTION"}
              </strong>
              <span>
                {index.count} VECTOR
                {index.dimensions > 0 ? ` • ${index.dimensions}D` : ""}
              </span>
            </div>
          </SettingRow>
        </SettingGroup>

        <div className="settings-empty-system settings-memory-system">
          <div className="settings-dna-mark"><i /><i /><i /></div>
          <span>MEMORY MANAGER</span>
          <strong>
            {loading
              ? "در حال خواندن Memory Service..."
              : error
                ? "Memory Service در دسترس نیست"
                : `${counts.active} حافظه فعال`}
          </strong>
          <p>
            {error
              ? error
              : `${providerLabel} • ${counts.pendingEmbedding} منتظر Vector • ${counts.pendingReview} منتظر بررسی`}
          </p>

          <div className="settings-memory-summary">
            <div>
              <span>ACTIVE</span>
              <strong>{counts.active}</strong>
            </div>
            <div>
              <span>PENDING</span>
              <strong>{counts.pendingEmbedding + counts.pendingReview}</strong>
            </div>
            <div>
              <span>INDEX</span>
              <strong>{index.count}</strong>
            </div>
          </div>

          <div className="settings-memory-manager-actions">
            <button
              type="button"
              className="settings-memory-manage-button"
              disabled={loading || Boolean(error)}
              onClick={() => setMemoryManagerOpen((current) => !current)}
            >
              {memoryManagerOpen ? "CLOSE MANAGER" : "MANAGE MEMORY"}
            </button>

            <button
              type="button"
              className="settings-memory-secondary-button"
              disabled={memoryBusy}
              onClick={() =>
                void runMemoryAction(
                  () => refreshMemory(),
                  "Memory refresh شد",
                )
              }
            >
              REFRESH
            </button>
          </div>

          {memoryManagerOpen && (
            <div className="settings-memory-manager">
              <header>
                <div>
                  <span>CANONICAL MEMORY</span>
                  <strong>{provider.model || "NO EMBEDDING MODEL"}</strong>
                </div>
                <small>
                  {provider.localOnly ? "LOCAL ONLY" : "REMOTE BLOCKED"}
                </small>
              </header>

              <div className="settings-memory-paths">
                <span title={databasePath}>DB • {databasePath || "—"}</span>
                <span title={vectorPath}>VECTOR • {vectorPath || "—"}</span>
              </div>

              <div className="settings-memory-list">
                {activeMemories.length === 0 ? (
                  <div className="settings-memory-empty">
                    هنوز حافظه‌ای ثبت نشده است.
                  </div>
                ) : (
                  activeMemories.map((item) => (
                    <article
                      key={item.id}
                      className={`settings-memory-item settings-memory-${item.status.toLowerCase()}`}
                    >
                      {editingMemoryId === item.id ? (
                        <div className="settings-memory-editor">
                          <textarea
                            value={editingMemoryValue}
                            onChange={(event) =>
                              setEditingMemoryValue(event.target.value)
                            }
                            maxLength={8000}
                          />
                          <div>
                            <button
                              type="button"
                              disabled={memoryBusy || !editingMemoryValue.trim()}
                              onClick={() => void saveMemoryEdit()}
                            >
                              SAVE REVISION
                            </button>
                            <button
                              type="button"
                              disabled={memoryBusy}
                              onClick={() => {
                                setEditingMemoryId(null);
                                setEditingMemoryValue("");
                              }}
                            >
                              CANCEL
                            </button>
                          </div>
                        </div>
                      ) : (
                        <>
                          <div className="settings-memory-item-copy">
                            <strong>{item.value}</strong>
                            <span>
                              {item.category.toUpperCase()}
                              {" • "}
                              {item.scopeType.toUpperCase()}
                              {item.scopeKey ? `:${item.scopeKey}` : ""}
                              {" • "}
                              {Math.round(item.confidence * 100)}%
                              {" • "}
                              {item.evidenceType.toUpperCase()}
                            </span>
                          </div>

                          <div className="settings-memory-item-state">
                            <small>{item.status.replace(/_/g, " ")}</small>
                            {item.status === "PENDING_REVIEW" && (
                              <button
                                type="button"
                                disabled={memoryBusy}
                                onClick={() =>
                                  void runMemoryAction(
                                    () => approveMemory(item.id),
                                    "Memory برای Vectorization تأیید شد",
                                  )
                                }
                              >
                                APPROVE
                              </button>
                            )}
                            {item.status === "ACTIVE" && (
                              <button
                                type="button"
                                disabled={memoryBusy}
                                onClick={() =>
                                  beginMemoryEdit(item.id, item.value)
                                }
                              >
                                EDIT
                              </button>
                            )}
                            <button
                              type="button"
                              className="danger"
                              disabled={memoryBusy}
                              onClick={() =>
                                void runMemoryAction(
                                  () => deleteMemory(item.id),
                                  "Memory حذف شد",
                                )
                              }
                            >
                              DELETE
                            </button>
                          </div>
                        </>
                      )}
                    </article>
                  ))
                )}
              </div>

              <footer>
                <button
                  type="button"
                  disabled={memoryBusy || !provider.ready || counts.pendingEmbedding === 0}
                  onClick={() =>
                    void runMemoryAction(
                      () => retryPendingMemory(),
                      "Pending Memory بررسی شد",
                    )
                  }
                >
                  RETRY PENDING
                </button>

                <button
                  type="button"
                  disabled={memoryBusy || !provider.ready}
                  onClick={() =>
                    void runMemoryAction(
                      () => rebuildMemoryIndex(),
                      "Vector Index بازسازی شد",
                    )
                  }
                >
                  REBUILD INDEX
                </button>
              </footer>
            </div>
          )}
        </div>
      </>
    );
  };

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

  const renderDeviceLink = () => (
    <DeviceLinkPanel
      view={deviceLinkView}
      enabled={settingsState.values["link.enabled"] === true}
      onSetEnabled={(enabled) => void deviceLink.setEnabled(enabled)}
      onRefresh={() => void deviceLink.refresh()}
      onOpenPairing={(name) => void deviceLink.openPairing(name)}
      onCancelPairing={() => void deviceLink.cancelPairing()}
      onRevoke={(deviceId) => void deviceLink.revoke(deviceId)}
      onRemoteOff={(deviceId) => void deviceLink.remoteOff(deviceId)}
      onTick={deviceLink.tick}
      onDismissError={deviceLink.dismissError}
      onClose={deviceLink.close}
    />
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
    hotkeys: renderHotkeys,
    voice: renderVoice,
    behavior: renderBehavior,
    memory: renderMemory,
    performance: renderPerformance,
    web: renderWeb,
    notifications: renderNotifications,
    link: renderDeviceLink,
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
