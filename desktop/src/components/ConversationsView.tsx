import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import "./ConversationsView.css";

type ConversationAttachment =
  | {
      id: string;
      type: "file";
      title: string;
      meta: string;
      unread?: boolean;
    }
  | {
      id: string;
      type: "image";
      title: string;
      meta: string;
      unread?: boolean;
    }
  | {
      id: string;
      type: "link";
      title: string;
      meta: string;
      href: string;
      unread?: boolean;
    };

type ConversationMessage = {
  id: string;
  role:
    | "user"
    | "qronos";
  time: string;
  text: string;
  attachments?: ConversationAttachment[];
};

type ViewPhase =
  | "home"
  | "entering-conversations"
  | "conversations"
  | "leaving-conversations";

type ConversationsViewProps = {
  phase: ViewPhase;
  onClose: () => void;
};

type FlowParticle = {
  seed: number;
  originX: number;
  originY: number;
  controlX: number;
  controlY: number;
  finalX: number;
  finalY: number;
  size: number;
  alpha: number;
  phase: number;
  speed: number;
  tone:
    | "cyan"
    | "violet"
    | "white";
};

const MAX_DPR = 1;
const TRANSITION_MS = 1120;
const PARTICLE_COUNT = 520;

const initialMessages: ConversationMessage[] = [
  {
    id: "m1",
    role: "qronos",
    time: "19:08",
    text:
      "نسخه‌ی جدید رابط ذخیره شد. گزارش وضعیت فعلی پروژه و تغییرات UI آماده است.",
    attachments: [
      {
        id: "a1",
        type: "file",
        title: "Qronos_UI_Checkpoint.pdf",
        meta: "PDF • 2.4 MB",
        unread: true,
      },
    ],
  },
  {
    id: "m2",
    role: "user",
    time: "19:10",
    text:
      "خوبه. نسخه‌ی تصویری وضعیت فعلی هم بساز و لینک ریپازیتوری رو هم بفرست.",
  },
  {
    id: "m3",
    role: "qronos",
    time: "19:12",
    text:
      "این هم پیش‌نمایش تصویری رابط فعلی. فایل فقط به عنوان نمونه‌ی نمایشی این صفحه قرار گرفته.",
    attachments: [
      {
        id: "a2",
        type: "image",
        title: "Qronos UI Preview",
        meta: "PNG • GENERATED",
        unread: true,
      },
      {
        id: "a3",
        type: "link",
        title: "qronos-agent",
        meta: "GITHUB REPOSITORY",
        href:
          "https://github.com/mkhphoto46-rgb/qronos-agent",
        unread: true,
      },
    ],
  },
  {
    id: "m4",
    role: "user",
    time: "19:14",
    text:
      "می‌خوام تاریخچه‌ی گفتگوها همین‌جا قابل مرور باشه و صفحه حس یک اپ چت معمولی نده.",
  },
  {
    id: "m5",
    role: "qronos",
    time: "19:15",
    text:
      "نمای گفتگوها به صورت یک جریان زنده طراحی شده؛ پیام‌های شما سمت راست و پاسخ‌های Qronos سمت چپ قرار می‌گیرند و DNA فقط به عنوان یک حضور محو و پویا در پس‌زمینه باقی می‌ماند.",
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

function buildFlowParticles() {
  const random =
    seededRandom(
      918274,
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
      const edge =
        index % 4;

      const originX =
        0.075 +
        random() *
          0.105;

      const originY =
        0.06 +
        random() *
          0.88;

      let finalX = 0.5;
      let finalY = 0.5;

      if (edge === 0) {
        finalX =
          0.06 +
          random() *
            0.88;

        finalY =
          0.045 +
          random() *
            0.085;
      } else if (
        edge === 1
      ) {
        finalX =
          0.86 +
          random() *
            0.105;

        finalY =
          0.1 +
          random() *
            0.8;
      } else if (
        edge === 2
      ) {
        finalX =
          0.06 +
          random() *
            0.88;

        finalY =
          0.84 +
          random() *
            0.105;
      } else {
        finalX =
          0.035 +
          random() *
            0.105;

        finalY =
          0.1 +
          random() *
            0.8;
      }

      const toneRoll =
        random();

      return {
        seed:
          index,

        originX,
        originY,

        controlX:
          0.27 +
          random() *
            0.48,

        controlY:
          0.08 +
          random() *
            0.84,

        finalX,
        finalY,

        size:
          index % 5 ===
          0
            ? 0.34 +
              Math.pow(
                random(),
                0.82,
              ) *
                0.96
            : 0.2 +
              Math.pow(
                random(),
                0.9,
              ) *
                0.68,

        alpha:
          0.14 +
          random() *
            0.42,

        phase:
          random() *
          Math.PI *
          2,

        speed:
          0.42 +
          random() *
            0.7,

        tone:
          toneRoll >
          0.91
            ? "white"
            : toneRoll >
                0.76
              ? "violet"
              : "cyan",
      } satisfies FlowParticle;
    },
  );
}

const flowParticles =
  buildFlowParticles();

function createSprite(
  tone:
    | "cyan"
    | "violet"
    | "white",
) {
  const canvas =
    document.createElement(
      "canvas",
    );

  canvas.width = 32;
  canvas.height = 32;

  const context =
    canvas.getContext(
      "2d",
    );

  if (!context) {
    return canvas;
  }

  const gradient =
    context.createRadialGradient(
      16,
      16,
      0,
      16,
      16,
      16,
    );

  if (tone === "cyan") {
    gradient.addColorStop(
      0,
      "rgba(244,254,255,1)",
    );

    gradient.addColorStop(
      0.12,
      "rgba(105,232,255,0.98)",
    );

    gradient.addColorStop(
      0.38,
      "rgba(45,184,236,0.42)",
    );
  } else if (
    tone === "violet"
  ) {
    gradient.addColorStop(
      0,
      "rgba(249,246,255,1)",
    );

    gradient.addColorStop(
      0.13,
      "rgba(183,166,255,0.96)",
    );

    gradient.addColorStop(
      0.4,
      "rgba(111,82,230,0.38)",
    );
  } else {
    gradient.addColorStop(
      0,
      "rgba(255,255,255,1)",
    );

    gradient.addColorStop(
      0.14,
      "rgba(225,251,255,0.98)",
    );

    gradient.addColorStop(
      0.42,
      "rgba(101,220,250,0.32)",
    );
  }

  gradient.addColorStop(
    1,
    "rgba(0,0,0,0)",
  );

  context.fillStyle =
    gradient;

  context.fillRect(
    0,
    0,
    32,
    32,
  );

  return canvas;
}

function ParticleFlowCanvas({
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

  const phaseStartedAtRef =
    useRef(
      performance.now(),
    );

  useEffect(() => {
    phaseRef.current =
      phase;

    phaseStartedAtRef.current =
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

    const cyanSprite =
      createSprite(
        "cyan",
      );

    const violetSprite =
      createSprite(
        "violet",
      );

    const whiteSprite =
      createSprite(
        "white",
      );

    let width = 1;
    let height = 1;
    let dpr = 1;
    let animationFrame = 0;
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
            width * dpr,
          );

        canvas.height =
          Math.round(
            height * dpr,
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
        animationFrame =
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
          currentPhase ===
          "home"
        ) {
          return;
        }

        const elapsed =
          timestamp -
          phaseStartedAtRef.current;

        let transition = 1;

        if (
          currentPhase ===
          "entering-conversations"
        ) {
          transition =
            smootherStep(
              elapsed /
                TRANSITION_MS,
            );
        } else if (
          currentPhase ===
          "leaving-conversations"
        ) {
          transition =
            1 -
            smootherStep(
              elapsed /
                TRANSITION_MS,
            );
        }

        const time =
          timestamp *
          0.001;

        const settled =
          currentPhase ===
          "conversations";

        for (
          const particle of
          flowParticles
        ) {
          const originX =
            particle.originX *
            width;

          const originY =
            particle.originY *
            height;

          const controlX =
            particle.controlX *
            width;

          const controlY =
            particle.controlY *
            height;

          const finalX =
            particle.finalX *
            width;

          const finalY =
            particle.finalY *
            height;

          const inv =
            1 -
            transition;

          let x =
            inv *
              inv *
              originX +
            2 *
              inv *
              transition *
              controlX +
            transition *
              transition *
              finalX;

          let y =
            inv *
              inv *
              originY +
            2 *
              inv *
              transition *
              controlY +
            transition *
              transition *
              finalY;

          /*
           * Travel turbulence:
           * visible enough to avoid straight paths,
           * but cheap enough to keep frame time low.
           */
          const travelEnvelope =
            Math.sin(
              transition *
              Math.PI,
            );

          x +=
            Math.sin(
              particle.phase +
                transition *
                  (
                    4.2 +
                    particle.speed *
                      2.1
                  ),
            ) *
            4.2 *
            travelEnvelope;

          y +=
            Math.cos(
              particle.phase *
                1.37 +
                transition *
                  (
                    3.8 +
                    particle.speed *
                      1.9
                  ),
            ) *
            3.3 *
            travelEnvelope;

          /*
           * Perimeter motion after the page opens.
           * Larger travel radius than v3 so the field
           * visibly stays alive.
           */
          if (settled) {
            const orbit =
              4.5 +
              particle.size *
                4.8;

            x +=
              Math.sin(
                time *
                  particle.speed +
                  particle.phase,
              ) *
              orbit;

            y +=
              Math.cos(
                time *
                  (
                    particle.speed *
                    0.79
                  ) +
                  particle.phase *
                    1.21,
              ) *
              orbit *
              0.68;
          }

          const birth =
            smootherStep(
              transition /
                0.14,
            );

          const travelGlow =
            0.58 +
            Math.sin(
              transition *
                Math.PI,
            ) *
              0.42;

          const settlePulse =
            settled
              ? 0.44 +
                (
                  (
                    Math.sin(
                      time *
                        (
                          0.55 +
                          particle.speed *
                            0.42
                        ) +
                        particle.phase,
                    ) +
                    1
                  ) /
                  2
                ) *
                  0.28
              : travelGlow;

          const alpha =
            particle.alpha *
            birth *
            settlePulse;

          if (
            alpha <
            0.01
          ) {
            continue;
          }

          let fill =
            "rgba(103,229,255,1)";

          if (
            particle.tone ===
            "violet"
          ) {
            fill =
              "rgba(181,164,255,1)";
          } else if (
            particle.tone ===
            "white"
          ) {
            fill =
              "rgba(244,255,255,1)";
          }

          /*
           * Fast path:
           * most particles are tiny direct canvas dots.
           * No per-particle blur / shadow / DOM node.
           */
          context.globalAlpha =
            Math.min(
              1,
              alpha,
            );

          context.fillStyle =
            fill;

          const radius =
            Math.max(
              0.42,
              particle.size,
            );

          context.beginPath();

          context.arc(
            x,
            y,
            radius,
            0,
            Math.PI *
              2,
          );

          context.fill();

          /*
           * Only rare particles get a soft sprite halo.
           * This keeps the field luminous without the
           * "cloud" look or the old GPU cost.
           */
          if (
            particle.seed %
              19 ===
            0
          ) {
            const sprite =
              particle.tone ===
              "violet"
                ? violetSprite
                : particle.tone ===
                    "white"
                  ? whiteSprite
                  : cyanSprite;

            const glowSize =
              5 +
              particle.size *
                4;

            context.globalAlpha =
              Math.min(
                1,
                alpha *
                  0.24,
              );

            context.drawImage(
              sprite,
              x -
                glowSize /
                  2,
              y -
                glowSize /
                  2,
              glowSize,
              glowSize,
            );
          }
        }

        context.globalAlpha =
          1;
      };

    animationFrame =
      window.requestAnimationFrame(
        render,
      );

    return () => {
      window.cancelAnimationFrame(
        animationFrame,
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
      ref={canvasRef}
      className="conversations-particle-canvas"
      aria-hidden="true"
    />
  );
}

function AttachmentIcon({
  type,
}: {
  type:
    ConversationAttachment["type"];
}) {
  if (type === "file") {
    return (
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <path d="M7 3h7l4 4v14H7Z" />
        <path d="M14 3v5h5" />
        <path d="M9.5 13h5M9.5 16h5" />
      </svg>
    );
  }

  if (type === "image") {
    return (
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <rect
          x="4"
          y="5"
          width="16"
          height="14"
          rx="2"
        />
        <circle
          cx="9"
          cy="10"
          r="1.5"
        />
        <path d="m6 17 4-4 3 3 2-2 3 3" />
      </svg>
    );
  }

  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <path d="M9.5 14.5 14.5 9.5" />
      <path d="M7.2 16.8 5.7 18.3a3.3 3.3 0 0 1-4.7-4.7l3.5-3.5a3.3 3.3 0 0 1 4.7 0" />
      <path d="m16.8 7.2 1.5-1.5A3.3 3.3 0 1 1 23 10.4l-3.5 3.5a3.3 3.3 0 0 1-4.7 0" />
    </svg>
  );
}

function ConversationsView({
  phase,
  onClose,
}: ConversationsViewProps) {
  const [
    messages,
    setMessages,
  ] =
    useState<ConversationMessage[]>(
      initialMessages,
    );

  const [
    draft,
    setDraft,
  ] =
    useState("");

  const streamRef =
    useRef<HTMLDivElement | null>(
      null,
    );

  const composerRef =
    useRef<HTMLTextAreaElement | null>(
      null,
    );

  const sendMessage =
    () => {
      const text =
        draft.trim();

      if (!text) {
        return;
      }

      const now =
        new Date();

      const time =
        new Intl.DateTimeFormat(
          "en-GB",
          {
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
          },
        ).format(
          now,
        );

      setMessages(
        (
          current,
        ) => [
          ...current,
          {
            id:
              `local-${now.getTime()}`,
            role: "user",
            time,
            text,
          },
        ],
      );

      setDraft("");

      if (
        composerRef.current
      ) {
        composerRef.current.style.height =
          "24px";
      }
    };

  const selectBubbleContent =
    (
      element:
        HTMLElement,
    ) => {
      const selection =
        window.getSelection();

      if (!selection) {
        return;
      }

      const range =
        document.createRange();

      range.selectNodeContents(
        element,
      );

      selection.removeAllRanges();

      selection.addRange(
        range,
      );
    };

  const visible =
    phase !==
    "home";

  const phaseClass =
    phase ===
    "entering-conversations"
      ? "conversations-view-entering"
      : phase ===
          "leaving-conversations"
        ? "conversations-view-leaving"
        : phase ===
            "conversations"
          ? "conversations-view-visible"
          : "";

  const unreadArtifacts =
    useMemo(
      () =>
        messages.reduce(
          (
            total,
            message,
          ) =>
            total +
            (
              message.attachments?.filter(
                (
                  attachment,
                ) =>
                  attachment.unread,
              ).length ??
              0
            ),
          0,
        ),
      [messages],
    );

  useEffect(() => {
    if (
      phase !==
      "conversations"
    ) {
      return;
    }

    const stream =
      streamRef.current;

    if (!stream) {
      return;
    }

    window.requestAnimationFrame(
      () => {
        stream.scrollTo({
          top:
            stream.scrollHeight,
          behavior: "smooth",
        });
      },
    );
  }, [
    messages.length,
    phase,
  ]);

  return (
    <section
      className={`conversations-view ${phaseClass}`}
      aria-hidden={
        !visible
      }
      dir="rtl"
    >
      <ParticleFlowCanvas
        phase={
          phase
        }
      />

      <div
        className="conversations-transition-field"
        aria-hidden="true"
      />

      <div
        className="conversations-oracle-presence"
        aria-hidden="true"
      >
        <span className="conversations-oracle-core" />
        <span className="conversations-oracle-ring conversations-oracle-ring-a" />
        <span className="conversations-oracle-ring conversations-oracle-ring-b" />
      </div>

      <header className="conversations-header">
        <div className="conversations-heading-copy">
          <span className="conversations-kicker">
            MEMORY STREAM
          </span>

          <h1>
            گفتگوها
          </h1>

          <p>
            تاریخچه‌ی پیام‌های شما و پاسخ‌های Qronos
          </p>
        </div>

        <div className="conversations-header-meta">
          <span>
            TODAY
          </span>

          <i />

          <span>
            {messages.length} MESSAGES
          </span>

          <i />

          <span>
            {unreadArtifacts} NEW
          </span>
        </div>

        <button
          type="button"
          className="conversations-close"
          onClick={
            onClose
          }
          aria-label="بازگشت به خانه"
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

      <div className="conversations-day-divider">
        <span />

        <strong>
          امروز
        </strong>

        <span />
      </div>

      <div
        ref={streamRef}
        className="conversations-stream"
      >
        {messages.map(
          (
            message,
            index,
          ) => {
            const previous =
              index >
              0
                ? messages[
                    index -
                    1
                  ]
                : null;

            const roleShift =
              previous &&
              previous.role !==
                message.role;

            return (
              <article
                key={
                  message.id
                }
                className={`conversation-message-bubble conversation-message-${message.role} ${
                  roleShift
                    ? "conversation-message-role-shift"
                    : ""
                }`}
                tabIndex={0}
                onMouseDown={(
                  event,
                ) => {
                  if (
                    event.button ===
                    0
                  ) {
                    event.currentTarget.focus();
                  }
                }}
                onKeyDown={(
                  event,
                ) => {
                  if (
                    (
                      event.ctrlKey ||
                      event.metaKey
                    ) &&
                    event.key.toLowerCase() ===
                      "a"
                  ) {
                    event.preventDefault();
                    event.stopPropagation();

                    selectBubbleContent(
                      event.currentTarget,
                    );
                  }
                }}
              >
                <header className="conversation-message-head">
                  <span className="conversation-message-role">
                    {message.role ===
                    "user"
                      ? "YOU"
                      : "QRONOS"}
                  </span>

                  <time>
                    {message.time}
                  </time>
                </header>

                <p className="conversation-message-text">
                  {
                    message.text
                  }
                </p>

                {message.attachments &&
                  message.attachments.length >
                    0 && (
                    <div className="conversation-attachments">
                      {message.attachments.map(
                        (
                          attachment,
                        ) =>
                          attachment.type ===
                            "link" ? (
                            <a
                              key={
                                attachment.id
                              }
                              className={`conversation-attachment conversation-attachment-${attachment.type}`}
                              href={
                                attachment.href
                              }
                              target="_blank"
                              rel="noreferrer"
                            >
                              <span className="conversation-attachment-icon">
                                <AttachmentIcon
                                  type={
                                    attachment.type
                                  }
                                />
                              </span>

                              <span className="conversation-attachment-copy">
                                <strong>
                                  {
                                    attachment.title
                                  }
                                </strong>

                                <small>
                                  {
                                    attachment.meta
                                  }
                                </small>
                              </span>

                              {attachment.unread && (
                                <span
                                  className="conversation-attachment-unread"
                                  aria-label="جدید"
                                />
                              )}
                            </a>
                          ) : (
                            <button
                              key={
                                attachment.id
                              }
                              type="button"
                              className={`conversation-attachment conversation-attachment-${attachment.type}`}
                            >
                              {attachment.type ===
                                "image" && (
                                <span className="conversation-image-preview">
                                  <i />
                                  <i />
                                  <i />
                                </span>
                              )}

                              <span className="conversation-attachment-icon">
                                <AttachmentIcon
                                  type={
                                    attachment.type
                                  }
                                />
                              </span>

                              <span className="conversation-attachment-copy">
                                <strong>
                                  {
                                    attachment.title
                                  }
                                </strong>

                                <small>
                                  {
                                    attachment.meta
                                  }
                                </small>
                              </span>

                              {attachment.unread && (
                                <span
                                  className="conversation-attachment-unread"
                                  aria-label="جدید"
                                />
                              )}
                            </button>
                          ),
                      )}
                    </div>
                  )}
              </article>
            );
          },
        )}
      </div>

      <form
        className="conversations-composer"
        onSubmit={(
          event,
        ) => {
          event.preventDefault();

          sendMessage();
        }}
      >
        <div className="conversations-composer-shell">
          <span
            className="conversations-composer-state"
            aria-hidden="true"
          >
            <i />
            CHAT
          </span>

          <textarea
            ref={composerRef}
            value={draft}
            rows={1}
            className="conversations-composer-input"
            placeholder="پیام به Qronos..."
            aria-label="پیام جدید"
            onChange={(
              event,
            ) => {
              setDraft(
                event.target.value,
              );

              const element =
                event.currentTarget;

              element.style.height =
                "24px";

              const nextHeight =
                Math.min(
                  element.scrollHeight,
                  88,
                );

              element.style.height =
                `${Math.max(
                  24,
                  nextHeight,
                )}px`;
            }}
            onKeyDown={(
              event,
            ) => {
              if (
                event.key ===
                  "Enter" &&
                !event.shiftKey
              ) {
                event.preventDefault();

                sendMessage();
              }
            }}
          />

          <button
            type="submit"
            className="conversations-composer-send"
            aria-label="ارسال پیام"
            disabled={
              !draft.trim()
            }
          >
            <svg
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <path d="m5 12.1 13.2-6.3-4.8 12.7-2.2-4.4L5 12.1Z" />
              <path d="m11.2 14.1 7-8.3" />
            </svg>
          </button>
        </div>

        <div className="conversations-composer-hint">
          <span>
            ENTER ارسال
          </span>

          <i />

          <span>
            SHIFT + ENTER خط جدید
          </span>

          <i />

          <span>
            UI LOCAL PROTOTYPE
          </span>
        </div>
      </form>
    </section>
  );
}

export default ConversationsView;
