import {
  useEffect,
  useRef,
  useState,
} from "react";

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

type PermissionPolicy =
  | "DENY"
  | "SESSION"
  | "ALLOW"
  | "CUSTOM"
  | "ALWAYS ASK";

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

type PermissionItem = {
  id: string;
  group: PermissionGroup;
  title: string;
  subtitle: string;
  policy: PermissionPolicy;
  risk: RiskLevel;
  description: string;
  actions: string[];
  protectedNote?: string;
};

type SecurityActivityTone =
  | "allowed"
  | "session"
  | "ask"
  | "denied"
  | "policy";

type SecurityActivity = {
  id: string;
  time: string;
  event: string;
  target: string;
  policy: string;
  decision: string;
  source: string;
  tone: SecurityActivityTone;
};

type CustomRuleValue =
  | "ALLOW"
  | "SESSION"
  | "DENY"
  | "ALWAYS ASK";

type DeviceScopeMode =
  | "system-default"
  | "selected-device"
  | "any-device";

type PermissionsViewProps = {
  phase: ViewPhase;
  onClose: () => void;

  /**
   * Rendered above the security activity list.
   *
   * A slot rather than a set of queue props: this component is long enough
   * already, and the queue's state, its event subscription and its commands
   * all live in App.tsx with everything else of that kind.
   */
  smartQueue?: React.ReactNode;
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

const MAX_DPR = 1;
const TRANSITION_MS = 980;
const PARTICLE_COUNT = 3200;

const permissionItems: PermissionItem[] = [
  {
    id: "files",
    group: "files",
    title: "Files & Storage",
    subtitle: "فایل‌ها و فضای ذخیره‌سازی",
    policy: "CUSTOM",
    risk: "MEDIUM",
    description:
      "کنترل خواندن، ساخت، تغییر نام، جابه‌جایی، حذف و دسترسی Qronos به مسیرهای مجاز.",
    actions: [
      "Read — Allow",
      "Create — Allow",
      "Modify / Rename — Always Ask",
      "Delete to Recycle Bin — Always Ask",
      "Delete Forever — Always Ask",
    ],
    protectedNote:
      "Protected: Windows, System32, Credentials و مسیرهای امنیتی سیستم.",
  },
  {
    id: "apps",
    group: "apps",
    title: "Applications",
    subtitle: "کنترل اپلیکیشن‌ها",
    policy: "ALWAYS ASK",
    risk: "MEDIUM",
    description:
      "باز کردن، بستن و کنترل اپلیکیشن‌های دسکتاپ از طریق Actionهای مشخص و Typed.",
    actions: [
      "Open App — Always Ask",
      "Close App — Always Ask",
      "Window Control — Always Ask",
      "Sensitive Input — Always Ask",
    ],
  },
  {
    id: "browser",
    group: "web",
    title: "Browser & Web",
    subtitle: "مرورگر و وب",
    policy: "CUSTOM",
    risk: "HIGH",
    description:
      "مرور وب با تفکیک Navigate، Click، Type، Download، Upload و Submit.",
    actions: [
      "Navigate — Allow",
      "Click — Always Ask",
      "Type — Always Ask",
      "Download — Always Ask",
      "Upload — Always Ask",
      "Submit Forms — Always Ask",
    ],
    protectedNote:
      "Browser access به معنی اجازه‌ی ارسال Credential یا فایل حساس نیست.",
  },
  {
    id: "microphone",
    group: "sensors",
    title: "Microphone",
    subtitle: "میکروفون و ورودی صوتی",
    policy: "SESSION",
    risk: "LOW",
    description:
      "Wake Word و Voice Input فقط در زمان فعال بودن Qronos یا Session مجاز.",
    actions: [
      "Wake Word — Session",
      "Voice Input — Session",
      "Background Capture — Deny",
    ],
  },
  {
    id: "camera",
    group: "sensors",
    title: "Camera / Webcam",
    subtitle: "دوربین و وب‌کم",
    policy: "ALWAYS ASK",
    risk: "HIGH",
    description:
      "اجازه‌ی استفاده از Camera به Scope دستگاه وابسته است. Permission می‌تواند چند دوربین را پوشش دهد، اما هر Capture فقط یک Camera فعال دارد.",
    actions: [
      "Camera Access — Always Ask",
      "Switch Camera — Always Ask",
      "Background Capture — Deny",
    ],
  },
  {
    id: "screen",
    group: "sensors",
    title: "Screen Vision",
    subtitle: "دیدن صفحه و Screenshot",
    policy: "ALWAYS ASK",
    risk: "HIGH",
    description:
      "مشاهده یا Capture صفحه می‌تواند اطلاعات حساس را دربر بگیرد؛ بنابراین Session-scoped یا Always Ask.",
    actions: [
      "Single Screenshot — Always Ask",
      "Live Screen Context — Session",
      "Background Capture — Deny",
    ],
  },
  {
    id: "clipboard",
    group: "sensors",
    title: "Clipboard",
    subtitle: "کلیپ‌بورد",
    policy: "CUSTOM",
    risk: "HIGH",
    description:
      "خواندن Clipboard از نوشتن در Clipboard جدا است؛ چون ممکن است شامل Password یا Token باشد.",
    actions: [
      "Write Clipboard — Allow",
      "Read Clipboard — Always Ask",
      "Background Read — Deny",
    ],
  },
  {
    id: "automation",
    group: "automation",
    title: "Automation",
    subtitle: "اجرای خودکار Taskها",
    policy: "CUSTOM",
    risk: "HIGH",
    description:
      "Taskهای زمان‌بندی‌شده باید Scope و Permission مستقل داشته باشند و مجوز تعاملی را به ارث نبرند.",
    actions: [
      "Scheduled Task — Custom",
      "Background Actions — Always Ask",
      "Destructive Automation — Deny",
    ],
  },
  {
    id: "system",
    group: "system",
    title: "System Settings",
    subtitle: "تنظیمات سیستم",
    policy: "ALWAYS ASK",
    risk: "CRITICAL",
    description:
      "تغییر تنظیمات Windows، Startup و رفتارهای سطح سیستم باید همیشه با تأیید صریح انجام شوند.",
    actions: [
      "Windows Settings — Always Ask",
      "Startup / Persistence — Always Ask",
      "Security Controls — Deny",
      "Privilege Escalation — Deny",
    ],
    protectedNote:
      "Qronos نباید Permission Engine، Windows Security یا محدودیت‌های خودش را دور بزند.",
  },
];

// Empty until the action audit trail feeds it.
//
// This list used to hold five invented entries — a file deletion, a
// microphone session on a device this machine does not have, a denied
// upload — rendered under the heading "Permission & sensitive action
// history" with timestamps, policies and decisions. None of it had
// happened. On a Library shelf that is placeholder content; on the screen
// whose whole job is to answer "what has Qronos done on my computer, and
// what did I permit?", it is an answer that is confidently wrong.
//
// core/action_audit.py records every permission decision, allows and
// denials alike, and ActionAuditLog.for_action can rebuild a timeline.
// When a Tauri command exposes it, this becomes that list. Until then an
// empty state is the truthful thing to show.
const securityActivities: SecurityActivity[] = [];


const groupLabels: Array<{
  id: PermissionGroup;
  label: string;
}> = [
  {
    id: "files",
    label: "FILES & STORAGE",
  },
  {
    id: "apps",
    label: "APPLICATIONS",
  },
  {
    id: "web",
    label: "WEB",
  },
  {
    id: "sensors",
    label: "SENSORS",
  },
  {
    id: "automation",
    label: "AUTOMATION",
  },
  {
    id: "system",
    label: "SYSTEM",
  },
];

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
  smartQueue,
  phase,
  onClose,
}: PermissionsViewProps) {
  const [
    selectedId,
    setSelectedId,
  ] =
    useState(
      "files",
    );

  const [
    policies,
    setPolicies,
  ] =
    useState<
      Record<
        string,
        PermissionPolicy
      >
    >(
      () =>
        Object.fromEntries(
          permissionItems.map(
            (
              item,
            ) => [
              item.id,
              item.policy,
            ],
          ),
        ),
    );

  const [
    actionsPaused,
    setActionsPaused,
  ] =
    useState(false);

  const [
    customRules,
    setCustomRules,
  ] =
    useState<
      Record<
        string,
        Record<
          string,
          CustomRuleValue
        >
      >
    >(() => {
      const initial:
        Record<
          string,
          Record<
            string,
            CustomRuleValue
          >
        > = {};

      for (
        const item of
        permissionItems
      ) {
        initial[
          item.id
        ] = {};

        for (
          const action of
          item.actions
        ) {
          const [
            label,
            rawValue,
          ] =
            action.split(
              " — ",
            );

          const normalized =
            (
              rawValue ??
              "Always Ask"
            ).toUpperCase();

          let value:
            CustomRuleValue =
              "ALWAYS ASK";

          if (
            normalized ===
            "ALLOW"
          ) {
            value =
              "ALLOW";
          } else if (
            normalized ===
            "SESSION"
          ) {
            value =
              "SESSION";
          } else if (
            normalized ===
              "DENY" ||
            normalized ===
              "DENIED"
          ) {
            value =
              "DENY";
          } else {
            value =
              "ALWAYS ASK";
          }

          initial[
            item.id
          ][
            label
          ] =
            value;
        }
      }

      return initial;
    });

  const [
    deviceScope,
    setDeviceScope,
  ] =
    useState<
      Record<
        "microphone" | "camera",
        DeviceScopeMode
      >
    >({
      microphone:
        "system-default",
      camera:
        "system-default",
    });

  const selected =
    permissionItems.find(
      (
        item,
      ) =>
        item.id ===
        selectedId,
    ) ??
    permissionItems[0];

  const visible =
    phase ===
      "entering-permissions" ||
    phase ===
      "permissions" ||
    phase ===
      "leaving-permissions";

  const phaseClass =
    phase ===
    "entering-permissions"
      ? "permissions-view-entering"
      : phase ===
          "leaving-permissions"
        ? "permissions-view-leaving"
        : phase ===
            "permissions"
          ? "permissions-view-visible"
          : "";

  const isLockedCustomRule =
    (
      item:
        PermissionItem,
      actionLabel:
        string,
    ) => {
      const original =
        item.actions.find(
          (
            action,
          ) =>
            action.startsWith(
              `${actionLabel} —`,
            ),
        );

      if (!original) {
        return false;
      }

      return (
        original.endsWith(
          "Always Ask",
        ) ||
        original.endsWith(
          "Deny",
        )
      );
    };

  const changeCustomRule =
    (
      itemId:
        string,
      actionLabel:
        string,
      value:
        CustomRuleValue,
    ) => {
      setCustomRules(
        (
          current,
        ) => ({
          ...current,
          [itemId]: {
            ...current[
              itemId
            ],
            [actionLabel]:
              value,
          },
        }),
      );
    };

  const changePolicy =
    (
      policy:
        PermissionPolicy,
    ) => {
      if (
        selected.id ===
          "system" &&
        (
          policy ===
            "ALLOW" ||
          policy ===
            "SESSION"
        )
      ) {
        return;
      }

      setPolicies(
        (
          current,
        ) => ({
          ...current,
          [selected.id]:
            policy,
        }),
      );
    };

  return (
    <section
      className={`permissions-view ${phaseClass}`}
      aria-hidden={
        !visible
      }
      dir="rtl"
    >
      <PermissionParticleCanvas
        phase={
          phase
        }
      />

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
            <h1>
              مجوزها
            </h1>

            <p>
              کنترل دسترسی، ریسک و اجرای Actionهای Qronos
            </p>
          </div>

          <div className="permissions-heading-en">
            <h1>
              PERMISSIONS
            </h1>

            <p>
              Access control, risk, and Qronos action execution
            </p>
          </div>

          <button
            type="button"
            className="permissions-back"
            onClick={
              onClose
            }
          >
            <span>
              بازگشت
            </span>

            <svg
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <path d="M15 5 8 12l7 7" />
            </svg>
          </button>
        </header>

        <div className="permissions-emergency-bar">
          <div>
            <span>
              EMERGENCY CONTROL
            </span>

            <strong>
              Pause Actions
            </strong>

            <small>
              Chat و Voice فعال می‌مانند؛ اجرای Action متوقف می‌شود.
            </small>
          </div>

          <button
            type="button"
            className={
              actionsPaused
                ? "permissions-pause-toggle permissions-pause-toggle-active"
                : "permissions-pause-toggle"
            }
            onClick={() =>
              setActionsPaused(
                (
                  current,
                ) =>
                  !current,
              )
            }
          >
            <span />

            {
              actionsPaused
                ? "PAUSED"
                : "ACTIVE"
            }
          </button>
        </div>

        <div className="permissions-layout">
          <div className="permissions-catalog">
            {groupLabels.map(
              (
                group,
              ) => {
                const items =
                  permissionItems.filter(
                    (
                      item,
                    ) =>
                      item.group ===
                      group.id,
                  );

                return (
                  <section
                    key={
                      group.id
                    }
                    className="permissions-group"
                  >
                    <header>
                      <span>
                        {
                          group.label
                        }
                      </span>

                      <i />
                    </header>

                    <div className="permissions-group-items">
                      {items.map(
                        (
                          item,
                        ) => {
                          const policy =
                            policies[
                              item.id
                            ];

                          return (
                            <button
                              key={
                                item.id
                              }
                              type="button"
                              className={
                                selectedId ===
                                item.id
                                  ? "permissions-item permissions-item-selected"
                                  : "permissions-item"
                              }
                              onClick={() =>
                                setSelectedId(
                                  item.id,
                                )
                              }
                            >
                              <span className="permissions-item-icon">
                                <PermissionIcon
                                  group={
                                    item.group
                                  }
                                />
                              </span>

                              <span className="permissions-item-copy">
                                <strong>
                                  {
                                    item.title
                                  }
                                </strong>

                                <small>
                                  {
                                    item.subtitle
                                  }
                                </small>
                              </span>

                              <span
                                className={`permissions-policy-chip permissions-policy-${policy
                                  .toLowerCase()
                                  .split(
                                    " ",
                                  )
                                  .join(
                                    "-",
                                  )}`}
                              >
                                {
                                  policy
                                }
                              </span>

                              <span
                                className={`permissions-risk-dot permissions-risk-${item.risk.toLowerCase()}`}
                                title={`Risk: ${item.risk}`}
                              />
                            </button>
                          );
                        },
                      )}
                    </div>
                  </section>
                );
              },
            )}
          </div>

          <aside className="permissions-detail-panel">
            <div className="permissions-detail-head">
              <div className="permissions-detail-icon">
                <PermissionIcon
                  group={
                    selected.group
                  }
                />
              </div>

              <div>
                <span>
                  SELECTED POLICY
                </span>

                <h2>
                  {
                    selected.title
                  }
                </h2>

                <p>
                  {
                    selected.subtitle
                  }
                </p>
              </div>

              <span
                className={`permissions-risk-badge permissions-risk-${selected.risk.toLowerCase()}`}
              >
                {
                  selected.risk
                }
              </span>
            </div>

            <p className="permissions-detail-description">
              {
                selected.description
              }
            </p>

            <div className="permissions-policy-selector">
              {(
                [
                  "DENY",
                  "ALWAYS ASK",
                  "SESSION",
                  "ALLOW",
                  "CUSTOM",
                ] as PermissionPolicy[]
              ).map(
                (
                  policy,
                ) => {
                  const disabled =
                    selected.id ===
                      "system" &&
                    (
                      policy ===
                        "ALLOW" ||
                      policy ===
                        "SESSION"
                    );

                  return (
                    <button
                      key={
                        policy
                      }
                      type="button"
                      disabled={
                        disabled
                      }
                      className={`permissions-policy-option permissions-policy-option-${policy
                        .toLowerCase()
                        .split(
                          " ",
                        )
                        .join(
                          "-",
                        )} ${
                        policies[
                          selected.id
                        ] ===
                        policy
                          ? "permissions-policy-option-active"
                          : ""
                      }`}
                      onClick={() =>
                        changePolicy(
                          policy,
                        )
                      }
                    >
                      {
                        policy
                      }
                    </button>
                  );
                },
              )}
            </div>

            <div className="permissions-action-matrix">
              <header>
                <span>
                  ACTION MATRIX
                </span>

                <i />
              </header>

              {selected.actions.map(
                (
                  action,
                ) => {
                  const [
                    actionLabel,
                    fallbackValue,
                  ] =
                    action.split(
                      " — ",
                    );

                  const currentValue =
                    customRules[
                      selected.id
                    ]?.[
                      actionLabel
                    ] ??
                    fallbackValue ??
                    "ALWAYS ASK";

                  return (
                    <div
                      key={
                        action
                      }
                      className={`permissions-action-row permissions-action-row-${(
                        policies[
                          selected.id
                        ] ===
                        "CUSTOM"
                          ? currentValue
                          : fallbackValue
                      )
                        .toLowerCase()
                        .split(
                          " ",
                        )
                        .join(
                          "-",
                        )}`}
                    >
                      <span>
                        {
                          actionLabel
                        }
                      </span>

                      <strong>
                        {(
                          policies[
                            selected.id
                          ] ===
                          "CUSTOM"
                            ? currentValue
                            : fallbackValue
                        ).toUpperCase()}
                      </strong>

                      <i />
                    </div>
                  );
                },
              )}
            </div>

            {policies[
              selected.id
            ] ===
              "CUSTOM" && (
              <div className="permissions-custom-editor">
                <header>
                  <span>
                    CUSTOM RULES
                  </span>

                  <small>
                    هر Action را جداگانه تنظیم کن
                  </small>
                </header>

                <div className="permissions-custom-rule-list">
                  {selected.actions.map(
                    (
                      action,
                    ) => {
                      const [
                        actionLabel,
                      ] =
                        action.split(
                          " — ",
                        );

                      const locked =
                        isLockedCustomRule(
                          selected,
                          actionLabel,
                        );

                      const value =
                        customRules[
                          selected.id
                        ]?.[
                          actionLabel
                        ] ??
                        "ALWAYS ASK";

                      return (
                        <label
                          key={
                            actionLabel
                          }
                          className="permissions-custom-rule"
                        >
                          <span>
                            {
                              actionLabel
                            }
                          </span>

                          <select
                            value={
                              value
                            }
                            disabled={
                              locked
                            }
                            onChange={(
                              event,
                            ) =>
                              changeCustomRule(
                                selected.id,
                                actionLabel,
                                event.target.value as
                                  CustomRuleValue,
                              )
                            }
                          >
                            <option value="ALLOW">
                              ALLOW
                            </option>

                            <option value="SESSION">
                              SESSION
                            </option>

                            <option value="DENY">
                              DENY
                            </option>

                            <option value="ALWAYS ASK">
                              ALWAYS ASK
                            </option>
                          </select>

                          {locked && (
                            <strong>
                              LOCKED
                            </strong>
                          )}
                        </label>
                      );
                    },
                  )}
                </div>
              </div>
            )}

            {(selected.id ===
                "microphone" ||
              selected.id ===
                "camera") && (
              <div className="permissions-device-scope">
                <header>
                  <span>
                    DEVICE SCOPE
                  </span>

                  <small>
                    {
                      selected.id ===
                      "microphone"
                        ? "فقط یک ورودی صوتی هم‌زمان فعال می‌شود"
                        : "هر Capture فقط یک Camera فعال دارد"
                    }
                  </small>
                </header>

                <div className="permissions-device-scope-options">
                  <button
                    type="button"
                    className={
                      deviceScope[
                        selected.id
                      ] ===
                      "system-default"
                        ? "active"
                        : ""
                    }
                    onClick={() =>
                      setDeviceScope(
                        (
                          current,
                        ) => ({
                          ...current,
                          [selected.id]:
                            "system-default",
                        }),
                      )
                    }
                  >
                    SYSTEM DEFAULT
                  </button>

                  <button
                    type="button"
                    className={
                      deviceScope[
                        selected.id
                      ] ===
                      "selected-device"
                        ? "active"
                        : ""
                    }
                    onClick={() =>
                      setDeviceScope(
                        (
                          current,
                        ) => ({
                          ...current,
                          [selected.id]:
                            "selected-device",
                        }),
                      )
                    }
                  >
                    SELECTED DEVICE
                  </button>

                  <button
                    type="button"
                    disabled={
                      selected.id ===
                      "microphone"
                    }
                    className={
                      deviceScope[
                        selected.id
                      ] ===
                      "any-device"
                        ? "active"
                        : ""
                    }
                    onClick={() =>
                      setDeviceScope(
                        (
                          current,
                        ) => ({
                          ...current,
                          [selected.id]:
                            "any-device",
                        }),
                      )
                    }
                  >
                    ANY CONNECTED DEVICE
                  </button>
                </div>

                <div className="permissions-device-runtime-note">
                  <i />

                  <span>
                    {
                      deviceScope[
                        selected.id
                      ] ===
                      "selected-device"
                        ? "لیست واقعی Deviceها بعد از اتصال Windows Runtime اینجا نمایش داده می‌شود."
                        : selected.id ===
                            "microphone"
                          ? "Qronos به یک Microphone فعال متصل می‌شود؛ چند Microphone هم‌زمان مجاز نیست."
                          : "Permission می‌تواند Cameraهای متصل را پوشش دهد، ولی Capture هم‌زمان تک‌دستگاهی است."
                    }
                  </span>
                </div>
              </div>
            )}

            {selected.id ===
              "files" && (
              <div className="permissions-scope-panel">
                <span>
                  ALLOWED LOCATIONS
                </span>

                <strong>
                  E:\Project Qronos Agent
                </strong>

                <strong>
                  E:\Qronos Library
                </strong>

                <strong>
                  Downloads
                </strong>
              </div>
            )}

            {selected.protectedNote && (
              <div className="permissions-protected-note">
                <span>
                  PROTECTED
                </span>

                <p>
                  {
                    selected.protectedNote
                  }
                </p>
              </div>
            )}
          </aside>

          <aside className="permissions-activity-panel">
            {smartQueue}

            <header className="permissions-security-activity-head">
              <div>
                <span>
                  SECURITY ACTIVITY
                </span>

                <strong>
                  Permission & sensitive action history
                </strong>
              </div>

            </header>

            <div className="permissions-security-activity-list">
              {securityActivities.length === 0 && (
                <p className="permissions-security-activity-empty">
                  هنوز هیچ اقدامی ثبت نشده است.
                  <span>No actions have been recorded yet.</span>
                </p>
              )}

              {securityActivities.map(
                (
                  activity,
                ) => (
                  <article
                    key={
                      activity.id
                    }
                    className={`permissions-security-activity-row permissions-security-activity-${activity.tone}`}
                  >
                    <div className="permissions-security-activity-time">
                      <time>
                        {
                          activity.time
                        }
                      </time>

                      <i />
                    </div>

                    <div className="permissions-security-activity-main">
                      <strong>
                        {
                          activity.event
                        }
                      </strong>

                      <span
                        title={
                          activity.target
                        }
                      >
                        {
                          activity.target
                        }
                      </span>
                    </div>

                    <dl className="permissions-security-activity-meta">
                      <div>
                        <dt>
                          POLICY
                        </dt>

                        <dd>
                          {
                            activity.policy
                          }
                        </dd>
                      </div>

                      <div>
                        <dt>
                          DECISION
                        </dt>

                        <dd>
                          {
                            activity.decision
                          }
                        </dd>
                      </div>

                      <div className="wide">
                        <dt>
                          SOURCE
                        </dt>

                        <dd>
                          {
                            activity.source
                          }
                        </dd>
                      </div>
                    </dl>
                  </article>
                ),
              )}
            </div>

            <button
              type="button"
              className="permissions-security-activity-footer"
            >
              <span>
                VIEW FULL AUDIT
              </span>

              <svg
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <path d="M8 5l7 7-7 7" />
              </svg>
            </button>

            <div className="permissions-audit-integrity">
              <i />

              <span>
                Audit log is append-only in the final runtime architecture
              </span>
            </div>
          </aside>
        </div>
      </div>

      {actionsPaused && (
        <div className="permissions-paused-banner">
          <i />

          <span>
            ACTION EXECUTION PAUSED
          </span>
        </div>
      )}
    </section>
  );
}

export default PermissionsView;
