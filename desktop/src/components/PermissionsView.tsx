import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  refreshPermissions,
  resetPermission,
  setPermissionCustomRule,
  setPermissionPolicy,
  usePermissionStore,
} from "../permissionStore";
import type {
  PermissionAuditRecord,
  PermissionPolicyRecord,
  UserPermissionPolicy,
  UserPermissionRecord,
} from "../permissionStore";

import ApprovalSecretPanel from "./ApprovalSecretPanel";
import "./PermissionsView.css";

type ViewPhase =
  | "home"
  | "entering-conversations"
  | "conversations"
  | "leaving-conversations"
  | "entering-library"
  | "library"
  | "leaving-library"
  | "entering-permissions"
  | "permissions"
  | "leaving-permissions";

type RiskLevel =
  | "LOW"
  | "MEDIUM"
  | "HIGH"
  | "CRITICAL";

type PermissionGroup =
  | "files"
  | "apps"
  | "web"
  | "sensors"
  | "automation"
  | "system";

type PermissionsViewProps = {
  phase: ViewPhase;
  onClose: () => void;
  runtimePaused: boolean;
  onToggleRuntimePaused: (paused: boolean) => void;
};

type SecurityParticle = {
  id: number;
  startOffsetX: number;
  startOffsetY: number;
  targetX: number;
  targetY: number;
  controlBias: number;
  phase: number;
  speed: number;
  size: number;
  alpha: number;
  tone:
    | "cyan"
    | "indigo"
    | "white";
};

type CategoryMeta = {
  group: PermissionGroup;
  title: string;
  subtitle: string;
};


type UserPermissionMeta = {
  group: PermissionGroup;
  subtitle: string;
  description: string;
  risk: RiskLevel;
  availability: "READY" | "PARTIAL" | "UNAVAILABLE";
  availabilityText: string;
};

const USER_PERMISSION_META: Record<string, UserPermissionMeta> = {
  files: {
    group: "files",
    subtitle: "فایل‌ها و فضای ذخیره‌سازی",
    description:
      "کنترل دسترسی Qronos به خواندن، ساخت، ویرایش و حذف قابل‌بازیابی فایل‌ها.",
    risk: "HIGH",
    availability: "PARTIAL",
    availabilityText:
      "Policy backend فعال است؛ Agent file executors در Stage بعد کامل می‌شوند.",
  },
  apps: {
    group: "apps",
    subtitle: "برنامه‌ها",
    description:
      "کنترل اجرای برنامه، نصب، حذف و Device Control از مسیر Permission Gate.",
    risk: "HIGH",
    availability: "PARTIAL",
    availabilityText:
      "Policy backend فعال است؛ Application executors هنوز کامل نشده‌اند.",
  },
  browser: {
    group: "web",
    subtitle: "مرورگر و وب",
    description:
      "کنترل Navigation، ارسال داده و Upload از طریق Policyهای مستقل و قابل Audit.",
    risk: "HIGH",
    availability: "PARTIAL",
    availabilityText:
      "Policy backend فعال است؛ Interactive Browser executor هنوز کامل نشده است.",
  },
  microphone: {
    group: "sensors",
    subtitle: "میکروفون و ورودی صوتی",
    description:
      "Policy کاربر برای Microphone؛ اتصال مستقیم آن به Wake/Voice در Stage بعد انجام می‌شود.",
    risk: "HIGH",
    availability: "PARTIAL",
    availabilityText:
      "Policy ذخیره می‌شود؛ Wake/Voice enforcement هنوز به این Permission متصل نشده است.",
  },
  camera: {
    group: "sensors",
    subtitle: "دوربین و وب‌کم",
    description:
      "Policy دوربین با Security Hard Floor؛ Device source فعلی هنوز ساخته نشده است.",
    risk: "HIGH",
    availability: "UNAVAILABLE",
    availabilityText:
      "Camera device backend فعلاً موجود نیست؛ UI آن را به‌عنوان فعال وانمود نمی‌کند.",
  },
  screen: {
    group: "sensors",
    subtitle: "نمایش صفحه و اسکرین‌شات",
    description:
      "کنترل واقعی READ_SCREEN که مستقیماً توسط Security Gate و ScreenCapture enforce می‌شود.",
    risk: "HIGH",
    availability: "READY",
    availabilityText:
      "End-to-end فعال: User Policy → Effective Resolver → Gate → ScreenCapture.",
  },
  clipboard: {
    group: "sensors",
    subtitle: "کلیپ‌بورد",
    description:
      "Policy کاربر برای Clipboard؛ Agent Clipboard executor هنوز ساخته نشده است.",
    risk: "HIGH",
    availability: "UNAVAILABLE",
    availabilityText:
      "Clipboard Agent executor فعلاً موجود نیست.",
  },
  automation: {
    group: "automation",
    subtitle: "اجرای خودکار وظایف",
    description:
      "کنترل اجرای خودکار وظایف و توقف موقت Runtime از مسیر Runtime Control.",
    risk: "HIGH",
    availability: "PARTIAL",
    availabilityText:
      "Runtime-wide Pause فعال است؛ Scheduled Automation کامل در Stage بعد اضافه می‌شود.",
  },
  system: {
    group: "system",
    subtitle: "تنظیمات سیستم",
    description:
      "کنترل System Configuration با حفظ Hard Floor برای Registry، Security و Privilege Escalation.",
    risk: "CRITICAL",
    availability: "PARTIAL",
    availabilityText:
      "Policy backend فعال است؛ فقط executorهای whitelisted آینده اجازه اجرا خواهند داشت.",
  },
};

const USER_GROUP_LABELS: Array<{
  id: PermissionGroup;
  label: string;
}> = [
  { id: "files", label: "FILES & STORAGE" },
  { id: "apps", label: "APPLICATIONS" },
  { id: "web", label: "WEB" },
  { id: "sensors", label: "SENSORS" },
  { id: "automation", label: "AUTOMATION" },
  { id: "system", label: "SYSTEM" },
];

const USER_POLICY_OPTIONS: UserPermissionPolicy[] = [
  "DENY",
  "ALWAYS_ASK",
  "SESSION",
  "ALLOW",
  "CUSTOM",
];

const CUSTOM_RULE_OPTIONS: Array<Exclude<UserPermissionPolicy, "CUSTOM">> = [
  "DENY",
  "ALWAYS_ASK",
  "SESSION",
  "ALLOW",
];

function userMeta(permissionId: string): UserPermissionMeta {
  return (
    USER_PERMISSION_META[permissionId] ?? {
      group: "system",
      subtitle: "مجوز کاربر",
      description: "User permission.",
      risk: "HIGH",
      availability: "UNAVAILABLE",
      availabilityText: "Capability metadata is unavailable.",
    }
  );
}

function userPolicyClass(policy: UserPermissionPolicy) {
  return policy.toLowerCase().replaceAll("_", "-");
}

function effectiveRulePolicy(
  category: string,
  policies: PermissionPolicyRecord[],
): Exclude<UserPermissionPolicy, "CUSTOM"> {
  const record = policies.find((item) => item.category === category);

  if (!record) return "ALWAYS_ASK";
  if (record.level === "FORBIDDEN") return "DENY";
  if (record.level === "AUTO_ALLOW") return "ALLOW";

  return "ALWAYS_ASK";
}

const MAX_DPR = 1;
const TRANSITION_MS = 980;
const PARTICLE_COUNT = 1600;

const CATEGORY_META: Record<string, CategoryMeta> = {
  "conversation": {
    group: "files",
    title: "Conversation",
    subtitle: "گفتگوی محلی",
  },
  "system_status": {
    group: "files",
    title: "System Status",
    subtitle: "خواندن وضعیت منابع سیستم",
  },
  "read_non_sensitive": {
    group: "files",
    title: "Read Non-Sensitive",
    subtitle: "خواندن دادهٔ غیرحساس انتخاب‌شده",
  },
  "create_or_edit_file": {
    group: "files",
    title: "Create / Edit File",
    subtitle: "ساخت یا ویرایش فایل",
  },
  "delete_recoverable": {
    group: "files",
    title: "Recoverable Delete",
    subtitle: "حذف قابل‌بازیابی",
  },
  "open_application": {
    group: "apps",
    title: "Open Application",
    subtitle: "باز کردن اپلیکیشن",
  },
  "install_software": {
    group: "apps",
    title: "Install Software",
    subtitle: "نصب نرم‌افزار",
  },
  "uninstall_software": {
    group: "apps",
    title: "Uninstall Software",
    subtitle: "حذف نرم‌افزار",
  },
  "remote_control": {
    group: "apps",
    title: "Remote Control",
    subtitle: "کنترل از راه دور",
  },
  "device_control": {
    group: "apps",
    title: "Device Control",
    subtitle: "کنترل دستگاه جانبی",
  },
  "browser_navigation": {
    group: "web",
    title: "Browser Navigation",
    subtitle: "ناوبری مرورگر",
  },
  "external_communication": {
    group: "web",
    title: "External Communication",
    subtitle: "ارسال پیام یا داده به بیرون",
  },
  "upload_data": {
    group: "web",
    title: "Upload Data",
    subtitle: "آپلود دادهٔ محلی",
  },
  "read_screen": {
    group: "sensors",
    title: "Read Screen",
    subtitle: "مشاهدهٔ صفحه",
  },
  "watch_camera": {
    group: "sensors",
    title: "Watch Camera",
    subtitle: "شروع Session دوربین",
  },
  "hidden_surveillance": {
    group: "sensors",
    title: "Hidden Surveillance",
    subtitle: "Capture پنهانی صدا، تصویر یا صفحه",
  },
  "credential_access": {
    group: "sensors",
    title: "Credential Access",
    subtitle: "دسترسی به رمز، Token یا کلید خصوصی",
  },
  "code_generation": {
    group: "automation",
    title: "Code Generation",
    subtitle: "تولید کد یا Script اجرایی",
  },
  "code_analysis": {
    group: "automation",
    title: "Code Analysis",
    subtitle: "تحلیل کد بدون تغییر یا اجرا",
  },
  "code_modification": {
    group: "automation",
    title: "Code Modification",
    subtitle: "تغییر کد یا Script",
  },
  "script_execution": {
    group: "automation",
    title: "Script Execution",
    subtitle: "اجرای Shell / PowerShell / Python",
  },
  "system_configuration": {
    group: "system",
    title: "System Configuration",
    subtitle: "تغییر تنظیمات سیستم",
  },
  "registry_modification": {
    group: "system",
    title: "Registry Modification",
    subtitle: "تغییر Windows Registry",
  },
  "boot_configuration": {
    group: "system",
    title: "Boot Configuration",
    subtitle: "تغییر Boot و Recovery",
  },
  "raw_disk_access": {
    group: "system",
    title: "Raw Disk Access",
    subtitle: "دسترسی مستقیم به Disk / Partition",
  },
  "security_configuration": {
    group: "system",
    title: "Security Configuration",
    subtitle: "تغییر UAC / Firewall / Security",
  },
  "backup_destruction": {
    group: "system",
    title: "Backup Destruction",
    subtitle: "حذف یا تخریب Backup",
  },
  "privilege_escalation": {
    group: "system",
    title: "Privilege Escalation",
    subtitle: "افزایش سطح دسترسی",
  },
  "cyber_attack": {
    group: "system",
    title: "Cyber Attack",
    subtitle: "اقدام تهاجمی یا نفوذ",
  },
  "unauthorized_persistence": {
    group: "system",
    title: "Unauthorized Persistence",
    subtitle: "Persistence پنهانی یا بدون مجوز",
  },
  "security_bypass": {
    group: "system",
    title: "Security Bypass",
    subtitle: "دور زدن کنترل‌های امنیتی",
  },
  "irreversible_destruction": {
    group: "system",
    title: "Irreversible Destruction",
    subtitle: "تخریب غیرقابل‌بازیابی",
  },
  "modify_security_policy": {
    group: "system",
    title: "Modify Security Policy",
    subtitle: "تضعیف یا بازنویسی Policy امنیتی",
  },
};

function metaFor(category: string): CategoryMeta {
  return (
    CATEGORY_META[category] ?? {
      group: "system",
      title: category
        .split("_")
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(" "),
      subtitle: "Policy category",
    }
  );
}

function auditTone(record: PermissionAuditRecord) {
  if (record.decision === "deny" || record.outcome === "refused") return "denied";
  if (record.decision?.startsWith("require_")) return "ask";
  if (record.outcome === "succeeded" || record.decision === "allow") return "allowed";
  if (record.outcome === "awaiting_approval") return "ask";
  return "policy";
}

function auditTime(at: number) {
  if (!Number.isFinite(at) || at <= 0) return "--:--";
  return new Date(at * 1000).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function clamp01(
  value: number,
) {
  return Math.max(
    0,
    Math.min(
      1,
      value,
    ),
  );
}

function smootherStep(
  value: number,
) {
  const t =
    clamp01(
      value,
    );

  return (
    t *
    t *
    t *
    (
      t *
        (
          t * 6 -
          15
        ) +
      10
    )
  );
}

function seededRandom(
  seed: number,
) {
  let value =
    seed >>> 0;

  return () => {
    value =
      (
        value *
          1664525 +
        1013904223
      ) >>>
      0;

    return (
      value /
      4294967296
    );
  };
}

function buildSecurityParticles() {
  const random =
    seededRandom(
      762144,
    );

  return Array.from(
    {
      length:
        PARTICLE_COUNT,
    },
    (
      _,
      index,
    ) => {
      const zone =
        index % 7;

      let targetX =
        0.5;

      let targetY =
        0.5;

      if (
        zone ===
        0
      ) {
        targetX =
          0.08 +
          random() *
            0.84;

        targetY =
          0.1 +
          random() *
            0.11;
      } else if (
        zone ===
        1
      ) {
        targetX =
          0.12 +
          random() *
            0.26;

        targetY =
          0.24 +
          random() *
            0.56;
      } else if (
        zone ===
        2
      ) {
        targetX =
          0.39 +
          random() *
            0.28;

        targetY =
          0.25 +
          random() *
            0.52;
      } else if (
        zone ===
        3
      ) {
        targetX =
          0.69 +
          random() *
            0.2;

        targetY =
          0.24 +
          random() *
            0.56;
      } else if (
        zone ===
        4
      ) {
        targetX =
          0.06 +
          random() *
            0.12;

        targetY =
          0.25 +
          random() *
            0.58;
      } else if (
        zone ===
        5
      ) {
        targetX =
          0.83 +
          random() *
            0.1;

        targetY =
          0.25 +
          random() *
            0.58;
      } else {
        targetX =
          0.18 +
          random() *
            0.64;

        targetY =
          0.8 +
          random() *
            0.09;
      }

      const toneRoll =
        random();

      return {
        id:
          index,

        startOffsetX:
          (
            random() -
            0.5
          ) *
          72,

        startOffsetY:
          (
            random() -
            0.5
          ) *
          24,

        targetX,
        targetY,

        controlBias:
          (
            random() -
            0.5
          ) *
          0.34,

        phase:
          random() *
          Math.PI *
          2,

        speed:
          0.55 +
          random() *
            1.15,

        size:
          index % 11 ===
          0
            ? 0.62 +
              random() *
                0.55
            : 0.22 +
              random() *
                0.42,

        alpha:
          0.16 +
          random() *
            0.44,

        tone:
          toneRoll >
          0.93
            ? "white"
            : toneRoll >
                0.73
              ? "indigo"
              : "cyan",
      } satisfies SecurityParticle;
    },
  );
}

const securityParticles =
  buildSecurityParticles();

function PermissionParticleCanvas({
  phase,
}: {
  phase: ViewPhase;
}) {
  const canvasRef =
    useRef<HTMLCanvasElement | null>(
      null,
    );

  const phaseRef =
    useRef<ViewPhase>(
      phase,
    );

  const phaseStartedRef =
    useRef(
      performance.now(),
    );

  useEffect(() => {
    phaseRef.current =
      phase;

    phaseStartedRef.current =
      performance.now();
  }, [phase]);

  useEffect(() => {
    const canvas =
      canvasRef.current;

    if (!canvas) {
      return;
    }

    const context =
      canvas.getContext(
        "2d",
        {
          alpha: true,
        },
      );

    if (!context) {
      return;
    }

    let width = 1;
    let height = 1;
    let dpr = 1;
    let frame = 0;
    let visible =
      !document.hidden;

    const resize =
      () => {
        width =
          Math.max(
            1,
            window.innerWidth,
          );

        height =
          Math.max(
            1,
            window.innerHeight,
          );

        dpr =
          Math.min(
            window.devicePixelRatio ||
              1,
            MAX_DPR,
          );

        canvas.width =
          Math.round(
            width *
            dpr,
          );

        canvas.height =
          Math.round(
            height *
            dpr,
          );

        canvas.style.width =
          `${width}px`;

        canvas.style.height =
          `${height}px`;

        context.setTransform(
          dpr,
          0,
          0,
          dpr,
          0,
          0,
        );
      };

    resize();

    const handleResize =
      () => {
        resize();
      };

    const handleVisibility =
      () => {
        visible =
          !document.hidden;
      };

    window.addEventListener(
      "resize",
      handleResize,
    );

    document.addEventListener(
      "visibilitychange",
      handleVisibility,
    );

    const render =
      (
        timestamp: number,
      ) => {
        frame =
          window.requestAnimationFrame(
            render,
          );

        if (!visible) {
          return;
        }

        context.clearRect(
          0,
          0,
          width,
          height,
        );

        const currentPhase =
          phaseRef.current;

        if (
          currentPhase !==
            "entering-permissions" &&
          currentPhase !==
            "permissions" &&
          currentPhase !==
            "leaving-permissions"
        ) {
          return;
        }

        const navButton =
          document.querySelector<HTMLElement>(
            '[data-qronos-nav="permissions"]',
          );

        const rect =
          navButton?.getBoundingClientRect();

        const startX =
          rect
            ? rect.left +
              rect.width /
                2
            : width *
              0.67;

        const startY =
          rect
            ? rect.top +
              rect.height /
                2
            : height -
              48;

        const elapsed =
          timestamp -
          phaseStartedRef.current;

        let transition =
          1;

        if (
          currentPhase ===
          "entering-permissions"
        ) {
          transition =
            smootherStep(
              elapsed /
                TRANSITION_MS,
            );
        } else if (
          currentPhase ===
          "leaving-permissions"
        ) {
          transition =
            1 -
            smootherStep(
              elapsed /
                TRANSITION_MS,
            );
        }

        const settled =
          currentPhase ===
          "permissions";

        const time =
          timestamp *
          0.001;

        for (
          const particle of
          securityParticles
        ) {
          const delayed =
            clamp01(
              (
                transition -
                (
                  particle.id %
                  19
                ) *
                  0.008
              ) /
                0.88,
            );

          const t =
            smootherStep(
              delayed,
            );

          const sourceX =
            startX +
            particle.startOffsetX;

          const sourceY =
            startY +
            particle.startOffsetY;

          const targetX =
            particle.targetX *
            width;

          const targetY =
            particle.targetY *
            height;

          const controlX =
            sourceX +
            (
              targetX -
              sourceX
            ) *
              0.44 +
            particle.controlBias *
              width;

          const controlY =
            startY -
            (
              150 +
              Math.abs(
                particle.controlBias,
              ) *
                180
            );

          const inv =
            1 -
            t;

          let x =
            inv *
              inv *
              sourceX +
            2 *
              inv *
              t *
              controlX +
            t *
              t *
              targetX;

          let y =
            inv *
              inv *
              sourceY +
            2 *
              inv *
              t *
              controlY +
            t *
              t *
              targetY;

          const envelope =
            Math.sin(
              t *
              Math.PI,
            );

          x +=
            Math.sin(
              particle.phase +
              t *
                5.4,
            ) *
            7 *
            envelope;

          y +=
            Math.cos(
              particle.phase *
                1.21 +
              t *
                4.8,
            ) *
            5 *
            envelope;

          if (
            settled
          ) {
            x +=
              Math.sin(
                time *
                  particle.speed +
                particle.phase,
              ) *
              5.5;

            y +=
              Math.cos(
                time *
                  particle.speed *
                  0.82 +
                particle.phase *
                  1.3,
              ) *
              3.8;
          }

          const birth =
            smootherStep(
              t /
                0.12,
            );

          const settledPulse =
            settled
              ? 0.24 +
                (
                  (
                    Math.sin(
                      time *
                        (
                          0.7 +
                          particle.speed *
                            0.35
                        ) +
                      particle.phase,
                    ) +
                    1
                  ) /
                  2
                ) *
                  0.32
              : 0.62 +
                envelope *
                  0.32;

          const alpha =
            particle.alpha *
            birth *
            settledPulse;

          if (
            alpha <
            0.008
          ) {
            continue;
          }

          context.globalAlpha =
            Math.min(
              1,
              alpha,
            );

          context.fillStyle =
            particle.tone ===
            "indigo"
              ? "rgba(112,129,255,1)"
              : particle.tone ===
                  "white"
                ? "rgba(244,254,255,1)"
                : "rgba(73,224,255,1)";

          context.beginPath();

          context.arc(
            x,
            y,
            particle.size,
            0,
            Math.PI *
              2,
          );

          context.fill();
        }

        context.globalAlpha =
          1;
      };

    frame =
      window.requestAnimationFrame(
        render,
      );

    return () => {
      window.cancelAnimationFrame(
        frame,
      );

      window.removeEventListener(
        "resize",
        handleResize,
      );

      document.removeEventListener(
        "visibilitychange",
        handleVisibility,
      );
    };
  }, []);

  return (
    <canvas
      ref={
        canvasRef
      }
      className="permissions-particle-canvas"
      aria-hidden="true"
    />
  );
}

function PermissionIcon({
  group,
}: {
  group: PermissionGroup;
}) {
  if (
    group ===
    "files"
  ) {
    return (
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <path d="M4 7h6l2 2h8v10H4Z" />
        <path d="M4 7V5h6l2 2" />
      </svg>
    );
  }

  if (
    group ===
    "apps"
  ) {
    return (
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <rect x="4" y="4" width="16" height="16" rx="2" />
        <path d="M4 9h16M9 4v16" />
      </svg>
    );
  }

  if (
    group ===
    "web"
  ) {
    return (
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="8" />
        <path d="M4 12h16M12 4c2 2.2 3 4.8 3 8s-1 5.8-3 8M12 4c-2 2.2-3 4.8-3 8s1 5.8 3 8" />
      </svg>
    );
  }

  if (
    group ===
    "sensors"
  ) {
    return (
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <path d="M12 4v6M8 7a4 4 0 1 0 8 0M6 11a6 6 0 0 0 12 0M12 17v3M9 20h6" />
      </svg>
    );
  }

  if (
    group ===
    "automation"
  ) {
    return (
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <path d="M7 7h10v10H7Z" />
        <path d="M12 3v4M12 17v4M3 12h4M17 12h4" />
        <circle cx="12" cy="12" r="2.2" />
      </svg>
    );
  }

  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <path d="M12 3 20 6v6c0 5-3 7.8-8 9-5-1.2-8-4-8-9V6Z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  );
}

function PermissionsView({
  runtimePaused,
  onToggleRuntimePaused,
  phase,
  onClose,
}: PermissionsViewProps) {
  const {
    approvalSecret,
    policies,
    userPermissions,
    audit,
    mutable,
    policySource,
    policyStoreSource,
    auditSource,
    loading,
    error,
  } = usePermissionStore();

  const [selectedPermissionId, setSelectedPermissionId] = useState("files");
  const [auditExpanded, setAuditExpanded] = useState(false);
  const [mutationBusy, setMutationBusy] = useState<string | null>(null);

  const visible =
    phase === "entering-permissions" ||
    phase === "permissions" ||
    phase === "leaving-permissions";

  const phaseClass =
    phase === "entering-permissions"
      ? "permissions-view-entering"
      : phase === "leaving-permissions"
        ? "permissions-view-leaving"
        : phase === "permissions"
          ? "permissions-view-visible"
          : "";

  useEffect(() => {
    if (phase === "permissions") {
      void refreshPermissions();
    }
  }, [phase]);

  useEffect(() => {
    if (phase !== "permissions") {
      return;
    }

    const auditSyncTimer = window.setInterval(
      () => {
        void refreshPermissions();
      },
      6000,
    );

    return () => {
      window.clearInterval(
        auditSyncTimer,
      );
    };
  }, [phase]);

  useEffect(() => {
    if (!userPermissions.length) return;

    if (!userPermissions.some((item) => item.id === selectedPermissionId)) {
      setSelectedPermissionId(userPermissions[0].id);
    }
  }, [selectedPermissionId, userPermissions]);

  const selected: UserPermissionRecord | null =
    userPermissions.find((item) => item.id === selectedPermissionId) ??
    userPermissions[0] ??
    null;

  const grouped = useMemo(
    () =>
      USER_GROUP_LABELS.map((group) => ({
        ...group,
        items: userPermissions.filter(
          (item) => userMeta(item.id).group === group.id,
        ),
      })),
    [userPermissions],
  );

  const visibleAudit = auditExpanded ? audit : audit.slice(0, 8);

  const runMutation = async (
    key: string,
    mutation: () => Promise<void>,
  ) => {
    if (mutationBusy) return;

    setMutationBusy(key);

    try {
      await mutation();
    } finally {
      setMutationBusy(null);
    }
  };

  const changePolicy = (policy: UserPermissionPolicy) => {
    if (!selected || !mutable) return;

    void runMutation(
      `policy:${selected.id}:${policy}`,
      () => setPermissionPolicy(selected.id, policy),
    );
  };

  const changeCustomRule = (
    category: string,
    policy: Exclude<UserPermissionPolicy, "CUSTOM">,
  ) => {
    if (!selected || !mutable) return;

    void runMutation(
      `rule:${selected.id}:${category}:${policy}`,
      () => setPermissionCustomRule(selected.id, category, policy),
    );
  };

  return (
    <section
      className={`permissions-view ${phaseClass}`}
      aria-hidden={!visible}
      dir="rtl"
    >
      <PermissionParticleCanvas phase={phase} />

      <div
        className="permissions-security-field"
        aria-hidden="true"
      >
        <span className="permissions-security-ring permissions-security-ring-a" />
        <span className="permissions-security-ring permissions-security-ring-b" />
        <span className="permissions-security-core" />
      </div>

      <div className="permissions-responsive-shell">
        <header className="permissions-header">
          <div className="permissions-heading">
            <h1>مجوزها</h1>
            <p>کنترل واقعی دسترسی و اجرای Actionهای Qronos</p>
          </div>

          <div className="permissions-heading-en permissions-ltr">
            <h1>PERMISSIONS</h1>
            <p>User-controlled policy with enforced security floor</p>
          </div>

          <button
            type="button"
            className="permissions-back"
            onClick={onClose}
          >
            <span>بازگشت</span>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M15 5 8 12l7 7" />
            </svg>
          </button>
        </header>

        <div className="permissions-emergency-bar">
          <div>
            <span>RUNTIME CONTROL</span>
            <strong>Pause Actions</strong>
            <small>
              Voice، Chat، Screen actions و Queue execution موقتاً متوقف می‌شوند.
              این حالت ذخیره نمی‌شود و با restart شدن Qronos خودکار به Active برمی‌گردد.
            </small>
          </div>

          <button
            type="button"
            className={
              runtimePaused
                ? "permissions-pause-toggle permissions-pause-toggle-active"
                : "permissions-pause-toggle"
            }
            onClick={() => onToggleRuntimePaused(!runtimePaused)}
            aria-pressed={runtimePaused}
            aria-label={
              runtimePaused
                ? "Resume Qronos runtime"
                : "Pause Qronos runtime"
            }
            title={
              runtimePaused
                ? "Click to resume all Qronos runtime channels"
                : "Click to temporarily pause Qronos runtime channels"
            }
          >
            <span />
            {runtimePaused ? "PAUSED" : "ACTIVE"}
          </button>
        </div>

        <div className="permissions-layout">
          <div className="permissions-catalog">
            {loading && userPermissions.length === 0 && (
              <div className="permissions-truth-state">
                <strong>در حال بارگذاری User Permissions...</strong>
                <span>{policyStoreSource || policySource}</span>
              </div>
            )}

            {error && userPermissions.length === 0 && (
              <div className="permissions-truth-state permissions-truth-state-error">
                <strong>Permission backend در دسترس نیست</strong>
                <span>{error}</span>
                <button
                  type="button"
                  onClick={() => void refreshPermissions()}
                >
                  RETRY
                </button>
              </div>
            )}

            {grouped.map((group) => {
              if (!group.items.length) return null;

              return (
                <section
                  key={group.id}
                  className="permissions-group"
                >
                  <header className="permissions-ltr">
                    <span>{group.label}</span>
                    <i />
                  </header>

                  <div className="permissions-group-items">
                    {group.items.map((item) => {
                      const meta = userMeta(item.id);

                      return (
                        <button
                          key={item.id}
                          type="button"
                          className={
                            selectedPermissionId === item.id
                              ? "permissions-item permissions-item-selected"
                              : "permissions-item"
                          }
                          onClick={() => setSelectedPermissionId(item.id)}
                        >
                          <span className="permissions-item-icon">
                            <PermissionIcon group={meta.group} />
                          </span>

                          <span className="permissions-item-copy">
                            <strong className="permissions-ltr">
                              {item.title}
                            </strong>
                            <small>{meta.subtitle}</small>
                          </span>

                          <span
                            className={`permissions-policy-chip permissions-user-policy-chip permissions-user-policy-${userPolicyClass(item.policy)}`}
                          >
                            {item.policy.replaceAll("_", " ")}
                          </span>

                          <span
                            className={`permissions-risk-dot permissions-risk-${meta.risk.toLowerCase()}`}
                            title={`Risk: ${meta.risk}`}
                          />
                        </button>
                      );
                    })}
                  </div>
                </section>
              );
            })}
          </div>

          <aside className="permissions-detail-panel">
            {!selected ? (
              <div className="permissions-truth-state">
                <strong>Permission انتخاب‌شده‌ای وجود ندارد</strong>
                <span>{error ?? "User permission snapshot is empty."}</span>
              </div>
            ) : (
              <>
                <div className="permissions-detail-head">
                  <div className="permissions-detail-icon">
                    <PermissionIcon
                      group={userMeta(selected.id).group}
                    />
                  </div>

                  <div className="permissions-detail-copy">
                    <span className="permissions-ltr">SELECTED PERMISSION</span>
                    <h2 className="permissions-ltr">{selected.title}</h2>
                    <p className="permissions-detail-subtitle-fa">
                      {userMeta(selected.id).subtitle}
                    </p>
                  </div>

                  <span
                    className={`permissions-risk-badge permissions-risk-${userMeta(selected.id).risk.toLowerCase()}`}
                  >
                    {userMeta(selected.id).risk}
                  </span>
                </div>

                <p className="permissions-detail-description">
                  {userMeta(selected.id).description}
                </p>

                <div
                  className={`permissions-capability-state permissions-capability-${userMeta(selected.id).availability.toLowerCase()}`}
                >
                  <span className="permissions-ltr">
                    {userMeta(selected.id).availability}
                  </span>
                  <p>{userMeta(selected.id).availabilityText}</p>
                </div>

                <div className="permissions-policy-selector">
                  {USER_POLICY_OPTIONS.map((policy) => (
                    <button
                      key={policy}
                      type="button"
                      disabled={!mutable || Boolean(mutationBusy)}
                      className={`permissions-policy-option permissions-user-policy-option permissions-user-policy-${userPolicyClass(policy)} ${
                        selected.policy === policy
                          ? "permissions-policy-option-active"
                          : ""
                      }`}
                      onClick={() => changePolicy(policy)}
                    >
                      {mutationBusy === `policy:${selected.id}:${policy}`
                        ? "SAVING"
                        : policy.replaceAll("_", " ")}
                    </button>
                  ))}
                </div>

                <div className="permissions-action-matrix permissions-policy-truth">
                  <header className="permissions-ltr">
                    <span>POLICY TRUTH</span>
                    <i />
                  </header>

                  <div className="permissions-action-row permissions-ltr">
                    <span>USER PERMISSION</span>
                    <strong>{selected.id.toUpperCase()}</strong>
                    <i />
                  </div>

                  <div className="permissions-action-row permissions-ltr">
                    <span>USER POLICY</span>
                    <strong>{selected.policy.replaceAll("_", " ")}</strong>
                    <i />
                  </div>

                  <div className="permissions-action-row permissions-ltr">
                    <span>PERSISTED</span>
                    <strong>{selected.configured ? "YES" : "DEFAULT"}</strong>
                    <i />
                  </div>

                  <div className="permissions-action-row permissions-ltr">
                    <span>ACTION CATEGORIES</span>
                    <strong>{selected.categories.length}</strong>
                    <i />
                  </div>
                </div>

                {selected.policy === "CUSTOM" && (
                  <div className="permissions-custom-editor permissions-custom-editor-v2">
                    <header>
                      <span className="permissions-ltr">CUSTOM RULES</span>
                      <small>هر Action Category را جداگانه تنظیم کن</small>
                    </header>

                    {selected.categories.length === 0 ? (
                      <div className="permissions-custom-empty">
                        <strong>NO CONNECTED ACTION CATEGORY</strong>
                        <span>
                          این Permission هنوز executor/category متصل ندارد.
                        </span>
                      </div>
                    ) : (
                      <div className="permissions-custom-rule-list">
                        {selected.categories.map((category) => {
                          const locked =
                            selected.lockedCategories.includes(category);

                          const current =
                            selected.customRules[category] ??
                            effectiveRulePolicy(category, policies);

                          return (
                            <div
                              key={category}
                              className="permissions-custom-rule-row"
                            >
                              <div className="permissions-custom-rule-copy">
                                <strong className="permissions-ltr">
                                  {metaFor(category).title}
                                </strong>
                                <span className="permissions-ltr">
                                  {category}
                                </span>
                              </div>

                              <div className="permissions-custom-rule-options">
                                {CUSTOM_RULE_OPTIONS.map((policy) => {
                                  const blocked =
                                    locked && policy !== "DENY";

                                  return (
                                    <button
                                      key={policy}
                                      type="button"
                                      disabled={
                                        !mutable ||
                                        Boolean(mutationBusy) ||
                                        blocked
                                      }
                                      className={`permissions-custom-rule-option permissions-user-policy-${userPolicyClass(policy)} ${
                                        current === policy
                                          ? "permissions-custom-rule-option-active"
                                          : ""
                                      }`}
                                      onClick={() =>
                                        changeCustomRule(category, policy)
                                      }
                                    >
                                      {mutationBusy ===
                                      `rule:${selected.id}:${category}:${policy}`
                                        ? "..."
                                        : policy.replaceAll("_", " ")}
                                    </button>
                                  );
                                })}
                              </div>

                              {locked && (
                                <span className="permissions-hard-floor-label permissions-ltr">
                                  HARD FLOOR · DENY ONLY
                                </span>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                )}

                <div className="permissions-backend-note">
                  <span className="permissions-ltr">POLICY STORE</span>
                  <strong className="permissions-ltr">
                    {policyStoreSource || "permissions_v2.json"}
                  </strong>
                  <p>
                    تغییر Policy از UI به backend نوشته می‌شود، snapshot دوباره
                    خوانده می‌شود و Security Gate Effective Policy را enforce می‌کند.
                  </p>
                </div>

                <button
                  type="button"
                  className="permissions-reset-button"
                  disabled={!mutable || Boolean(mutationBusy)}
                  onClick={() =>
                    void runMutation(
                      `reset:${selected.id}`,
                      () => resetPermission(selected.id),
                    )
                  }
                >
                  {mutationBusy === `reset:${selected.id}`
                    ? "RESETTING..."
                    : "RESET TO DEFAULT"}
                </button>

                <ApprovalSecretPanel
                  status={approvalSecret}
                  visible={phase === "permissions"}
                  disabled={!mutable}
                />
              </>
            )}
          </aside>

          <aside className="permissions-activity-panel">
            <header className="permissions-security-activity-head">
              <div className="permissions-ltr">
                <span>SECURITY ACTIVITY</span>
                <strong>Permission & sensitive action history</strong>
              </div>

            </header>

            {error && audit.length > 0 && (
              <p className="permissions-security-activity-empty permissions-security-activity-warning">
                آخرین Audit نمایش داده می‌شود؛ همگام‌سازی خودکار جدید ناموفق بود.
                <span className="permissions-ltr">{error}</span>
              </p>
            )}

            <div className="permissions-security-activity-list">
              {!loading && audit.length === 0 && !error && (
                <p className="permissions-security-activity-empty">
                  هنوز هیچ Action ثبت‌شده‌ای در Audit وجود ندارد.
                  <span className="permissions-ltr">
                    No recorded permission decisions yet.
                  </span>
                </p>
              )}

              {loading && audit.length === 0 && (
                <p className="permissions-security-activity-empty">
                  در حال خواندن Audit واقعی...
                  <span className="permissions-ltr">{auditSource}</span>
                </p>
              )}

              {error && audit.length === 0 && (
                <p className="permissions-security-activity-empty">
                  Audit در دسترس نیست.
                  <span className="permissions-ltr">{error}</span>
                </p>
              )}

              {visibleAudit.map((activity) => (
                <article
                  key={`${activity.actionId}-${activity.at}-${activity.event}`}
                  className={`permissions-security-activity-row permissions-security-activity-${auditTone(activity)}`}
                >
                  <div className="permissions-security-activity-time permissions-ltr">
                    <time>{auditTime(activity.at)}</time>
                    <i />
                  </div>

                  <div className="permissions-security-activity-main permissions-ltr">
                    <strong>
                      {(activity.event || "event").toUpperCase()}
                      {activity.summary ? ` · ${activity.summary}` : ""}
                    </strong>
                    <span title={activity.target || activity.category || ""}>
                      {activity.target || activity.category || "Qronos action"}
                    </span>
                  </div>

                  <dl className="permissions-security-activity-meta permissions-ltr">
                    <div>
                      <dt>CATEGORY</dt>
                      <dd>{(activity.category || "unknown").toUpperCase()}</dd>
                    </div>

                    <div>
                      <dt>DECISION</dt>
                      <dd>
                        {(activity.decision || activity.outcome || "—").toUpperCase()}
                      </dd>
                    </div>

                    <div className="wide">
                      <dt>SOURCE</dt>
                      <dd>{auditSource}</dd>
                    </div>
                  </dl>
                </article>
              ))}
            </div>

            {audit.length > 8 && (
              <button
                type="button"
                className="permissions-security-activity-footer"
                onClick={() =>
                  setAuditExpanded(
                    (current) => !current,
                  )
                }
              >
                <span>
                  {auditExpanded
                    ? "COLLAPSE AUDIT"
                    : `VIEW ALL ${audit.length} EVENTS`}
                </span>

                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d={auditExpanded ? "M8 15l7-7" : "M8 5l7 7-7 7"} />
                </svg>
              </button>
            )}

            <div className="permissions-audit-integrity permissions-ltr">
              <i />
              <span>
                {auditSource} · persisted policy events · no fabricated activity
              </span>
            </div>
          </aside>
        </div>
      </div>

      {runtimePaused && (
        <div className="permissions-paused-banner">
          <i />
          <span>QRONOS RUNTIME PAUSED · TEMPORARY</span>
        </div>
      )}
    </section>
  );
}

export default PermissionsView;
