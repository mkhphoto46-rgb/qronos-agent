import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type {
  CSSProperties,
  KeyboardEvent,
} from "react";

import OracleDnaSpine from "./OracleDnaSpine";

import type {
  OracleMemoryAnchor,
  OracleMemoryNode,
  OracleMemoryPhase,
} from "./OracleDnaSpine";

import "./ConversationSpine.css";
import "./ConversationMemoryMotion.css";

type ConversationRole =
  | "user"
  | "qronos";

type Conversation = {
  id: string;
  role: ConversationRole;
  time: string;
  text: string;
  spinePosition: number;
};

type MemoryPocketParticle = {
  id: number;
  targetX: number;
  targetY: number;
  size: number;
  alpha: number;
  delay: number;
  returnDelay: number;
  duration: number;
  floatDelay: number;
  driftX: number;
  driftY: number;
};

type ParticleOrigin = {
  x: number;
  y: number;
};

const OPEN_DURATION = 760;
const CLOSE_DURATION = 620;

const conversations: Conversation[] = [
  {
    id: "conversation-3",
    role: "qronos",
    time: "19:34",
    text:
      "ساختار Context به شکل DNA طراحی شده تا هر گفت‌وگو به صورت یک Memory Node زنده داخل جریان حافظه دیده شود و بدون شلوغ کردن رابط کاربری، آخرین گفتگوها همیشه در دسترس باشند.",
    spinePosition: 0.24,
  },
  {
    id: "conversation-2",
    role: "user",
    time: "19:21",
    text:
      "وقتی Memory انتخاب می‌شود، می‌خواهم نود مربوط به آن باز شود و متن گفتگو از همان نقطه ظاهر شود؛ نه این‌که یک کارت مستقل و جدا از ساختار DNA روی صفحه قرار بگیرد.",
    spinePosition: 0.5,
  },
  {
    id: "conversation-1",
    role: "qronos",
    time: "19:06",
    text:
      "ساختار اصلی رابط همان Oracle مرکزی باقی می‌ماند. Memory Pocket فقط حافظه انتخاب‌شده را از دل DNA آشکار می‌کند و تاریخچه کامل همچنان در بخش Conversations قرار می‌گیرد.",
    spinePosition: 0.76,
  },
];

function buildPocketParticles(): MemoryPocketParticle[] {
  return Array.from(
    {
      length: 88,
    },
    (_, index) => {
      const column =
        index % 11;

      const row =
        Math.floor(
          index / 11,
        );

      const waveA =
        Math.sin(
          index * 1.47,
        );

      const waveB =
        Math.cos(
          index * 2.07,
        );

      const waveC =
        Math.sin(
          index * 0.83,
        );

      return {
        id: index,

        targetX:
          14 +
          column * 22 +
          waveA * 13,

        targetY:
          18 +
          row * 23 +
          waveB * 15,

        size:
          1.9 +
          (index % 6) *
            0.46,

        alpha:
          0.32 +
          (index % 7) *
            0.055,

        delay:
          (index % 17) *
          7,

        returnDelay:
          (index % 13) *
          5,

        duration:
          5200 +
          (index % 10) *
            360,

        floatDelay:
          -(
            (index % 16) *
            290
          ),

        driftX:
          2.5 +
          (index % 6) *
            1.05,

        driftY:
          -3.5 +
          (index % 9) *
            0.86 +
          waveC * 0.7,
      };
    },
  );
}

const pocketParticles =
  buildPocketParticles();

function getPocketPosition(
  spinePosition: number,
) {
  const raw =
    18 + spinePosition * 61;

  return Math.min(
    66,
    Math.max(
      30,
      raw,
    ),
  );
}

function ConversationSpine() {
  const [
    displayedId,
    setDisplayedId,
  ] =
    useState<string | null>(
      null,
    );

  const [
    memoryPhase,
    setMemoryPhase,
  ] =
    useState<OracleMemoryPhase>(
      "closed",
    );

  const [
    memoryAnchor,
    setMemoryAnchor,
  ] =
    useState<OracleMemoryAnchor | null>(
      null,
    );

  const [
    particleOrigin,
    setParticleOrigin,
  ] =
    useState<ParticleOrigin>({
      x: 0,
      y: 0,
    });

  const [
    anchorReady,
    setAnchorReady,
  ] =
    useState(false);

  const previewRef =
    useRef<HTMLDivElement | null>(
      null,
    );

  const pocketRef =
    useRef<HTMLElement | null>(
      null,
    );

  const transitionTimerRef =
    useRef<number | null>(
      null,
    );

  const pendingIdRef =
    useRef<string | null>(
      null,
    );

  const memoryNodes =
    useMemo<
      OracleMemoryNode[]
    >(
      () =>
        conversations.map(
          (conversation) => ({
            id:
              conversation.id,

            spinePosition:
              conversation.spinePosition,

            role:
              conversation.role,
          }),
        ),
      [],
    );

  const displayedConversation =
    useMemo(
      () =>
        conversations.find(
          (conversation) =>
            conversation.id ===
            displayedId,
        ) ?? null,
      [displayedId],
    );

  const pocketStyle =
    useMemo<
      CSSProperties
    >(() => {
      if (
        !displayedConversation
      ) {
        return {};
      }

      return {
        "--memory-pocket-top":
          `${getPocketPosition(
            displayedConversation.spinePosition,
          )}%`,

        "--memory-node-source-x":
          `${particleOrigin.x}px`,

        "--memory-node-source-y":
          `${particleOrigin.y}px`,
      } as CSSProperties;
    }, [
      displayedConversation,
      particleOrigin,
    ]);

  const clearTransitionTimer =
    () => {
      if (
        transitionTimerRef.current ===
        null
      ) {
        return;
      }

      window.clearTimeout(
        transitionTimerRef.current,
      );

      transitionTimerRef.current =
        null;
    };

  const resetAnchor =
    () => {
      setMemoryAnchor(
        null,
      );

      setParticleOrigin({
        x: 0,
        y: 0,
      });

      setAnchorReady(
        false,
      );
    };

  const beginOpening = (
    id: string,
  ) => {
    clearTransitionTimer();

    pendingIdRef.current =
      null;

    resetAnchor();

    setDisplayedId(
      id,
    );

    setMemoryPhase(
      "opening",
    );
  };

  const beginClosing = (
    nextId: string | null,
  ) => {
    clearTransitionTimer();

    pendingIdRef.current =
      nextId;

    setMemoryPhase(
      "closing",
    );

    transitionTimerRef.current =
      window.setTimeout(
        () => {
          const next =
            pendingIdRef.current;

          pendingIdRef.current =
            null;

          if (next) {
            resetAnchor();

            setDisplayedId(
              next,
            );

            setMemoryPhase(
              "opening",
            );

            return;
          }

          setDisplayedId(
            null,
          );

          setMemoryPhase(
            "closed",
          );

          resetAnchor();

          transitionTimerRef.current =
            null;
        },
        CLOSE_DURATION,
      );
  };

  useLayoutEffect(() => {
    if (
      !memoryAnchor ||
      !displayedId ||
      memoryAnchor.id !==
        displayedId
    ) {
      return;
    }

    const pocket =
      pocketRef.current;

    if (!pocket) {
      return;
    }

    const pocketRect =
      pocket.getBoundingClientRect();

    const pocketScaleX =
      pocket.offsetWidth > 0
        ? pocketRect.width /
          pocket.offsetWidth
        : 1;

    const pocketScaleY =
      pocket.offsetHeight > 0
        ? pocketRect.height /
          pocket.offsetHeight
        : 1;

    setParticleOrigin({
      x:
        (
          memoryAnchor.clientX -
          pocketRect.left
        ) /
        Math.max(
          0.001,
          pocketScaleX,
        ),

      y:
        (
          memoryAnchor.clientY -
          pocketRect.top
        ) /
        Math.max(
          0.001,
          pocketScaleY,
        ),
    });

    setAnchorReady(
      true,
    );

    if (
      memoryPhase ===
      "opening"
    ) {
      clearTransitionTimer();

      transitionTimerRef.current =
        window.setTimeout(
          () => {
            setMemoryPhase(
              "open",
            );

            transitionTimerRef.current =
              null;
          },
          OPEN_DURATION,
        );
    }
  }, [
    memoryAnchor,
    displayedId,
    memoryPhase,
  ]);

  useEffect(() => {
    const handleResize =
      () => {
        if (
          !memoryAnchor ||
          !displayedId ||
          memoryAnchor.id !==
            displayedId
        ) {
          return;
        }

        const pocket =
          pocketRef.current;

        if (!pocket) {
          return;
        }

        const pocketRect =
          pocket.getBoundingClientRect();

        setParticleOrigin({
          x:
            memoryAnchor.clientX -
            pocketRect.left,

          y:
            memoryAnchor.clientY -
            pocketRect.top,
        });
      };

    window.addEventListener(
      "resize",
      handleResize,
    );

    return () => {
      window.removeEventListener(
        "resize",
        handleResize,
      );
    };
  }, [
    memoryAnchor,
    displayedId,
  ]);

  useEffect(() => {
    return () => {
      clearTransitionTimer();
    };
  }, []);

  const selectPreviewText =
    () => {
      const element =
        previewRef.current;

      if (!element) {
        return;
      }

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

  const handlePreviewKeyDown = (
    event: KeyboardEvent<HTMLDivElement>,
  ) => {
    if (
      (event.ctrlKey ||
        event.metaKey) &&
      event.key.toLowerCase() ===
        "a"
    ) {
      event.preventDefault();

      selectPreviewText();
    }

    if (
      event.key === "Escape"
    ) {
      beginClosing(
        null,
      );
    }
  };

  const handleMemorySelect = (
    id: string,
  ) => {
    if (
      memoryPhase ===
      "closing"
    ) {
      pendingIdRef.current =
        id;

      return;
    }

    if (
      displayedId === id
    ) {
      beginClosing(
        null,
      );

      return;
    }

    if (displayedId) {
      beginClosing(
        id,
      );

      return;
    }

    beginOpening(
      id,
    );
  };

  return (
    <aside
      className="conversation-spine"
      aria-label="گفتگوهای اخیر"
      dir="ltr"
    >
      <header className="conversation-spine-header">
        <span className="conversation-spine-title">
          CONTEXT
        </span>
      </header>

      <div className="conversation-spine-stage">
        <div className="conversation-dna-zone">
          <OracleDnaSpine
            memories={memoryNodes}
            activeId={
              displayedId
            }
            phase={
              memoryPhase
            }
            onSelect={
              handleMemorySelect
            }
            onAnchorChange={
              setMemoryAnchor
            }
          />
        </div>

        {displayedConversation && (
          <article
            ref={pocketRef}
            key={
              displayedConversation.id
            }
            className={`memory-pocket memory-pocket-${memoryPhase} ${
              anchorReady
                ? "memory-pocket-anchor-ready"
                : "memory-pocket-anchor-pending"
            }`}
            style={pocketStyle}
          >
            <div
              className="memory-pocket-fog"
              aria-hidden="true"
            />

            <div
              className="memory-pocket-slit"
              aria-hidden="true"
            >
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
            </div>

            <div
              className="memory-pocket-bloom"
              aria-hidden="true"
            >
              {pocketParticles.map(
                (
                  particle,
                ) => (
                  <i
                    key={
                      particle.id
                    }
                    style={
                      {
                        "--memory-particle-target-x":
                          `${particle.targetX}px`,

                        "--memory-particle-target-y":
                          `${particle.targetY}px`,

                        "--memory-particle-size":
                          `${particle.size}px`,

                        "--memory-particle-alpha":
                          particle.alpha,

                        "--memory-particle-delay":
                          `${particle.delay}ms`,

                        "--memory-particle-return-delay":
                          `${particle.returnDelay}ms`,

                        "--memory-particle-duration":
                          `${particle.duration}ms`,

                        "--memory-particle-float-delay":
                          `${particle.floatDelay}ms`,

                        "--memory-particle-drift-x":
                          `${particle.driftX}px`,

                        "--memory-particle-drift-y":
                          `${particle.driftY}px`,
                      } as CSSProperties
                    }
                  />
                ),
              )}
            </div>

            <header className="memory-pocket-header">
              <div className="memory-pocket-identity">
                <span
                  className={`memory-pocket-role memory-pocket-role-${displayedConversation.role}`}
                >
                  {displayedConversation.role ===
                  "user"
                    ? "YOU"
                    : "QRONOS"}
                </span>

                <span className="memory-pocket-time">
                  {
                    displayedConversation.time
                  }
                </span>
              </div>

              <button
                type="button"
                className="memory-pocket-close"
                aria-label="بستن گفتگو"
                onClick={() =>
                  beginClosing(
                    null,
                  )
                }
              >
                ×
              </button>
            </header>

            <div
              ref={previewRef}
              className="memory-pocket-copy-zone"
              tabIndex={0}
              onKeyDown={
                handlePreviewKeyDown
              }
            >
              <p>
                {
                  displayedConversation.text
                }
              </p>
            </div>

            <footer className="memory-pocket-footer">
              <span>
                RECENT MEMORY
              </span>

              <span
                className="memory-pocket-typing"
                aria-hidden="true"
              >
                <i />
                <i />
                <i />
              </span>
            </footer>
          </article>
        )}
      </div>

      <footer className="conversation-spine-footer">
        <span className="conversation-memory-core">
          <i />
        </span>

        <span>
          RECENT MEMORY
        </span>
      </footer>
    </aside>
  );
}

export default ConversationSpine;
