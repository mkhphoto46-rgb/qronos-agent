import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type {
  CSSProperties,
} from "react";

import "./App.css";

import ConversationSpine from "./components/ConversationSpine";
import ConversationsView from "./components/ConversationsView";
import LibraryView from "./components/LibraryView";
import CrossViewParticleTransition from "./components/CrossViewParticleTransition";
import RightTelemetryPanel from "./components/RightTelemetryPanel";
import QronosOrb from "./components/QronosOrb";
import OrbTaskRenderer from "./components/OrbTaskRenderer";
import type { OrbState } from "./components/OrbState";

import "./components/QronosResponsive.css";

type DebugScenario =
  | "idle"
  | "listening"
  | "thinking"
  | "responding"
  | "chat"
  | "userVoice"
  | "qronosVoice";

type DebugItem = {
  key: string;
  scenario: DebugScenario;
  label: string;
};

type ViewPhase =
  | "home"
  | "entering-conversations"
  | "conversations"
  | "leaving-conversations"
  | "entering-library"
  | "library"
  | "leaving-library";

const debugItems: DebugItem[] = [
  {
    key: "1",
    scenario: "idle",
    label: "IDLE",
  },
  {
    key: "2",
    scenario: "listening",
    label: "LISTENING",
  },
  {
    key: "3",
    scenario: "thinking",
    label: "THINKING",
  },
  {
    key: "4",
    scenario: "responding",
    label: "RESPONDING",
  },
  {
    key: "5",
    scenario: "chat",
    label: "CHAT",
  },
  {
    key: "6",
    scenario: "userVoice",
    label: "USER VOICE",
  },
  {
    key: "7",
    scenario: "qronosVoice",
    label: "QRONOS VOICE",
  },
];

const orbStateByScenario: Record<
  DebugScenario,
  OrbState
> = {
  idle: "idle",
  listening: "listening",
  thinking: "thinking",
  responding: "responding",
  chat: "thinking",
  userVoice: "listening",
  qronosVoice: "responding",
};

const statusByScenario: Record<
  DebugScenario,
  string
> = {
  idle: "آماده شنیدن",
  listening: "گوش می‌دهم",
  thinking: "در حال پردازش",
  responding: "پاسخ آماده است",
  chat: "در حال گفتگو",
  userVoice:
    "در حال شنیدن صدای شما",
  qronosVoice:
    "کرونوس در حال صحبت است",
};

const subtitleByScenario: Record<
  DebugScenario,
  string
> = {
  idle: "QRONOS CORE ONLINE",
  listening: "VOICE GATE OPEN",
  thinking: "COGNITIVE FLOW ACTIVE",
  responding:
    "RESPONSE CHANNEL READY",
  chat: "TEXT CHANNEL ACTIVE",
  userVoice:
    "USER VOICE VISUAL MODE",
  qronosVoice:
    "QRONOS VOICE VISUAL MODE",
};

function App() {
  const [
    scenario,
    setScenario,
  ] =
    useState<DebugScenario>(
      "idle",
    );

  const [
    uiScale,
    setUiScale,
  ] =
    useState(1);

  const [
    viewPhase,
    setViewPhase,
  ] =
    useState<ViewPhase>(
      "home",
    );

  const [
    crossViewTarget,
    setCrossViewTarget,
  ] =
    useState<
      | "conversations"
      | "library"
      | null
    >(null);

  const viewTransitionTimerRef =
    useRef<number | null>(
      null,
    );

  const clearViewTransitionTimer =
    () => {
      if (
        viewTransitionTimerRef.current ===
        null
      ) {
        return;
      }

      window.clearTimeout(
        viewTransitionTimerRef.current,
      );

      viewTransitionTimerRef.current =
        null;
    };

  const openConversations =
    () => {
      if (
        viewPhase ===
          "conversations" ||
        viewPhase ===
          "entering-conversations"
      ) {
        return;
      }

      clearViewTransitionTimer();

      if (
        viewPhase ===
          "library" ||
        viewPhase ===
          "entering-library"
      ) {
        setCrossViewTarget(
          "conversations",
        );

        /*
         * Direct cross-fade:
         * source Library stays visually alive through a parent CSS override
         * while Conversations enters underneath/over it.
         */
        setViewPhase(
          "entering-conversations",
        );

        viewTransitionTimerRef.current =
          window.setTimeout(
            () => {
              setViewPhase(
                "conversations",
              );

              setCrossViewTarget(
                null,
              );

              viewTransitionTimerRef.current =
                null;
            },
            420,
          );

        return;
      }

      setCrossViewTarget(
        null,
      );

      setViewPhase(
        "entering-conversations",
      );

      viewTransitionTimerRef.current =
        window.setTimeout(
          () => {
            setViewPhase(
              "conversations",
            );

            viewTransitionTimerRef.current =
              null;
          },
          1120,
        );
    };

  const openLibrary =
    () => {
      if (
        viewPhase ===
          "library" ||
        viewPhase ===
          "entering-library"
      ) {
        return;
      }

      clearViewTransitionTimer();

      if (
        viewPhase ===
          "conversations" ||
        viewPhase ===
          "entering-conversations"
      ) {
        setCrossViewTarget(
          "library",
        );

        /*
         * Direct cross-fade:
         * source Conversations stays visually alive through a parent CSS override
         * while Library enters underneath/over it.
         */
        setViewPhase(
          "entering-library",
        );

        viewTransitionTimerRef.current =
          window.setTimeout(
            () => {
              setViewPhase(
                "library",
              );

              setCrossViewTarget(
                null,
              );

              viewTransitionTimerRef.current =
                null;
            },
            420,
          );

        return;
      }

      setCrossViewTarget(
        null,
      );

      setViewPhase(
        "entering-library",
      );

      viewTransitionTimerRef.current =
        window.setTimeout(
          () => {
            setViewPhase(
              "library",
            );

            viewTransitionTimerRef.current =
              null;
          },
          1120,
        );
    };

  const returnHome =
    () => {
      if (
        viewPhase ===
          "home" ||
        viewPhase ===
          "leaving-conversations" ||
        viewPhase ===
          "leaving-library"
      ) {
        return;
      }

      clearViewTransitionTimer();

      setCrossViewTarget(
        null,
      );

      if (
        viewPhase ===
          "library" ||
        viewPhase ===
          "entering-library"
      ) {
        setViewPhase(
          "leaving-library",
        );
      } else {
        setViewPhase(
          "leaving-conversations",
        );
      }

      viewTransitionTimerRef.current =
        window.setTimeout(
          () => {
            setViewPhase(
              "home",
            );

            viewTransitionTimerRef.current =
              null;
          },
          1120,
        );
    };

  const orbState =
    useMemo(
      () =>
        orbStateByScenario[
          scenario
        ],
      [scenario],
    );

  const statusLabel =
    useMemo(
      () =>
        statusByScenario[
          scenario
        ],
      [scenario],
    );

  const statusSubtitle =
    useMemo(
      () =>
        subtitleByScenario[
          scenario
        ],
      [scenario],
    );

  useEffect(() => {
    const updateScale =
      () => {
        /*
         * Single shared desktop scale.
         * 1600 × 900 is the reference composition.
         * Every major UI region consumes this same value.
         */
        const widthScale =
          window.innerWidth /
          1600;

        const heightScale =
          window.innerHeight /
          900;

        const next =
          Math.max(
            0.62,
            Math.min(
              1,
              widthScale,
              heightScale,
            ),
          );

        setUiScale(
          Number(
            next.toFixed(4),
          ),
        );
      };

    updateScale();

    window.addEventListener(
      "resize",
      updateScale,
    );

    return () => {
      window.removeEventListener(
        "resize",
        updateScale,
      );
    };
  }, []);

  useEffect(() => {
    return () => {
      clearViewTransitionTimer();
    };
  }, []);

  useEffect(() => {
    const mapByKey: Record<
      string,
      DebugScenario
    > = {
      "1": "idle",
      "2": "listening",
      "3": "thinking",
      "4": "responding",
      "5": "chat",
      "6": "userVoice",
      "7": "qronosVoice",
    };

    const handleKeyDown = (
      event: KeyboardEvent,
    ) => {
      const target =
        event.target as
          | HTMLElement
          | null;

      if (
        target?.tagName ===
          "INPUT" ||
        target?.tagName ===
          "TEXTAREA"
      ) {
        return;
      }

      const nextScenario =
        mapByKey[event.key];

      if (nextScenario) {
        setScenario(
          nextScenario,
        );
      }

      if (
        event.key ===
        "Escape"
      ) {
        setScenario(
          "idle",
        );
      }
    };

    window.addEventListener(
      "keydown",
      handleKeyDown,
    );

    return () => {
      window.removeEventListener(
        "keydown",
        handleKeyDown,
      );
    };
  }, []);

  return (
    <main
      className={`app app-view-${viewPhase} ${
        crossViewTarget
          ? `app-cross-to-${crossViewTarget}`
          : ""
      }`}
      dir="rtl"
      style={
        {
          "--qronos-ui-scale":
            uiScale,
        } as CSSProperties
      }
    >
      <div className="ambient ambient-cyan" />
      <div className="ambient ambient-violet" />
      <div className="ui-grid" />

      <header className="brand">
        <div className="brand-name">
          Q R O N O S
        </div>

        <div className="brand-beam">
          <div className="brand-beam-base" />
          <div className="brand-beam-runner" />
        </div>
      </header>

      <section
        className="debug-panel"
        dir="ltr"
        aria-label="Qronos debug controls"
      >
        <div className="debug-panel-head">
          <span className="debug-live-dot" />

          <span className="debug-title">
            DEV MODE
          </span>

          <span className="debug-current">
            {scenario.toUpperCase()}
          </span>
        </div>

        <div className="debug-controls">
          {debugItems.map(
            (item) => (
              <button
                key={
                  item.scenario
                }
                type="button"
                className={
                  scenario ===
                  item.scenario
                    ? "debug-button debug-button-active"
                    : "debug-button"
                }
                onClick={() =>
                  setScenario(
                    item.scenario,
                  )
                }
              >
                <span className="debug-key">
                  {item.key}
                </span>

                <span>
                  {item.label}
                </span>
              </button>
            ),
          )}
        </div>

        <div className="debug-hint">
          ESC → IDLE
        </div>
      </section>

      <ConversationSpine />


      <RightTelemetryPanel />

      <section className="core-zone">
        <div className="orb-shell">
          <div className="orb-ring orb-ring-1" />
          <div className="orb-ring orb-ring-2" />

          <QronosOrb
            size={460}
            state={orbState}
          />

          <OrbTaskRenderer
            state={orbState}
          />
        </div>
      </section>

      <section className="orb-status">
        <div className="status-line status-line-left" />

        <div className="status-content">
          <span className="status-dot" />

          <span className="status-label">
            {statusLabel}
          </span>

          <span className="status-subtitle">
            {statusSubtitle}
          </span>
        </div>

        <div className="status-line status-line-right" />
      </section>

      <section className="command-dock">
        <div className="command-shell">
          <div className="command-glass-highlight" />

          <div className="command-stream">
            <span className="command-stream-dot dot-1" />
            <span className="command-stream-dot dot-2" />
            <span className="command-stream-dot dot-3" />
            <span className="command-stream-dot dot-4" />
            <span className="command-stream-dot dot-5" />
          </div>

          <button
            type="button"
            className="command-action command-action-mic"
            aria-label="میکروفون"
          >
            <svg
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <path
                d="M12 3.4a3 3 0 0 0-3 3v5.1a3 3 0 0 0 6 0V6.4a3 3 0 0 0-3-3Z"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.35"
              />

              <path
                d="M6.8 10.9v.7a5.2 5.2 0 0 0 10.4 0v-.7M12 16.8v3M9.3 19.8h5.4"
                fill="none"
                stroke="currentColor"
                strokeLinecap="round"
                strokeWidth="1.35"
              />
            </svg>
          </button>

          <textarea
            className="command-input"
            rows={1}
            placeholder="از کرونوس بپرس..."
            aria-label="دستور کرونوس"
            onInput={(
              event,
            ) => {
              const element =
                event.currentTarget;

              element.style.height =
                "0px";

              const nextHeight =
                Math.min(
                  element.scrollHeight,
                  116,
                );

              element.style.height =
                `${Math.max(
                  24,
                  nextHeight,
                )}px`;
            }}
          />

          <button
            type="button"
            className="command-action command-action-send"
            aria-label="ارسال"
          >
            <svg
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <path
                d="m5 12.1 13.2-6.3-4.8 12.7-2.2-4.4L5 12.1Z"
                fill="none"
                stroke="currentColor"
                strokeLinejoin="round"
                strokeWidth="1.35"
              />

              <path
                d="m11.2 14.1 7-8.3"
                fill="none"
                stroke="currentColor"
                strokeLinecap="round"
                strokeWidth="1.35"
              />
            </svg>
          </button>
        </div>

        <div className="command-hint">
          <span>
            صدا
          </span>

          <i />

          <span>
            متن
          </span>

          <i />

          <span>
            اجرای امن
          </span>
        </div>
      </section>

      <CrossViewParticleTransition
        target={
          crossViewTarget
        }
      />

      <ConversationsView
        phase={
          viewPhase ===
            "entering-library" ||
          viewPhase ===
            "library" ||
          viewPhase ===
            "leaving-library"
            ? "home"
            : viewPhase
        }
        onClose={
          returnHome
        }
      />

      <LibraryView
        phase={
          viewPhase
        }
        onClose={
          returnHome
        }
      />

      <nav
        className="bottom-nav"
        aria-label="ناوبری کرونوس"
      >
        <button
          type="button"
          className={
            viewPhase ===
              "home" ||
            viewPhase ===
              "leaving-conversations" ||
            viewPhase ===
              "leaving-library"
              ? "nav-item nav-item-active"
              : "nav-item"
          }
          aria-label="خانه"
          onClick={
            returnHome
          }
        >
          <span className="nav-icon">
            <svg
              viewBox="0 0 28 28"
              aria-hidden="true"
            >
              <circle
                cx="14"
                cy="14"
                r="8.2"
              />

              <path
                d="M5.2 14c2.4-5.8 15.2-8.4 18.1-1.7 2.2 5-4.4 9.4-9.9 9.1"
              />

              <circle
                className="nav-icon-core"
                cx="14"
                cy="14"
                r="2"
              />
            </svg>
          </span>

          <span>
            خانه
          </span>
        </button>

        <button
          type="button"
          className={
            viewPhase ===
              "conversations" ||
            viewPhase ===
              "entering-conversations"
              ? "nav-item nav-item-active"
              : "nav-item"
          }
          aria-label="گفتگوها"
          onClick={
            openConversations
          }
        >
          <span className="nav-icon">
            <svg
              viewBox="0 0 28 28"
              aria-hidden="true"
            >
              <path
                d="M5 7.5h13.5a4 4 0 0 1 4 4v3.3a4 4 0 0 1-4 4H12l-4.5 3v-3.4A4 4 0 0 1 5 14.8V7.5Z"
              />

              <path
                d="M9 13h1.4M13.3 13h1.4M17.6 13H19"
              />
            </svg>
          </span>

          <span
            className="nav-notification-badge"
            aria-label="3 اعلان جدید"
          >
            3
          </span>

          <span>
            گفتگوها
          </span>
        </button>

        <button
          type="button"
          className={
            viewPhase ===
              "library" ||
            viewPhase ===
              "entering-library"
              ? "nav-item nav-item-active"
              : "nav-item"
          }
          aria-label="کتابخانه"
          onClick={
            openLibrary
          }
        >
          <span className="nav-icon">
            <svg
              viewBox="0 0 28 28"
              aria-hidden="true"
            >
              <path
                d="M5.5 8.2 14 4l8.5 4.2L14 12.4 5.5 8.2Z"
              />

              <path
                d="M5.5 13.1 14 17.3l8.5-4.2M5.5 18 14 22.2l8.5-4.2"
              />

              <circle
                className="nav-icon-core"
                cx="14"
                cy="12.4"
                r="1.5"
              />
            </svg>
          </span>

          <span>
            کتابخانه
          </span>
        </button>

        <button
          type="button"
          className="nav-item"
          aria-label="مجوزها"
        >
          <span className="nav-icon">
            <svg
              viewBox="0 0 28 28"
              aria-hidden="true"
            >
              <path
                d="M14 4.5 22 7v6.3c0 5.2-3.2 8.4-8 10.2-4.8-1.8-8-5-8-10.2V7l8-2.5Z"
              />

              <path
                d="m10.4 14 2.3 2.4 5-5.2"
              />
            </svg>
          </span>

          <span>
            مجوزها
          </span>
        </button>

        <button
          type="button"
          className="nav-item"
          aria-label="تنظیمات"
        >
          <span className="nav-icon">
            <svg
              viewBox="0 0 28 28"
              aria-hidden="true"
            >
              <path
                d="M6 8h16M6 14h16M6 20h16"
              />

              <circle
                cx="11"
                cy="8"
                r="2"
              />

              <circle
                cx="18"
                cy="14"
                r="2"
              />

              <circle
                cx="9"
                cy="20"
                r="2"
              />
            </svg>
          </span>

          <span>
            تنظیمات
          </span>
        </button>
      </nav>
    </main>
  );
}

export default App;
