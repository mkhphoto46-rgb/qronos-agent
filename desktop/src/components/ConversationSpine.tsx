import {
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
  OracleMemoryNode,
} from "./OracleDnaSpine";

import "./ConversationSpine.css";

type ConversationRole =
  | "user"
  | "qronos";

type Conversation = {
  id: string;
  role: ConversationRole;
  time: string;
  text: string;
  /*
   * Where this memory sits along the DNA spine, 0..1. Not task progress.
   */
  spinePosition: number;
};

const conversations: Conversation[] = [
  {
    id: "conversation-3",
    role: "qronos",
    time: "19:34",
    text:
      "DNA بخش Context باید از جنس Particleهای Oracle باشد و Memory Knot بخشی از خود ساختار DNA احساس شود، نه یک عنصر جدا که روی آن قرار گرفته باشد.",
    spinePosition: 0.24,
  },

  {
    id: "conversation-2",
    role: "user",
    time: "19:21",
    text:
      "وقتی Memory انتخاب می‌شود، DNA باید در همان نقطه باز شود و متن از دل این شکاف بیرون بیاید؛ نه اینکه یک Card مستقل روی صفحه ظاهر شود.",
    spinePosition: 0.5,
  },

  {
    id: "conversation-1",
    role: "qronos",
    time: "19:06",
    text:
      "فوکوس اصلی رابط همچنان روی Oracle مرکزی باقی می‌ماند. Memory Pocket فقط حافظه انتخاب‌شده را از دل DNA آشکار می‌کند و تاریخچه کامل در بخش Conversations قرار می‌گیرد.",
    spinePosition: 0.76,
  },
];

function getPocketPosition(
  spinePosition: number,
) {
  const raw =
    18 + spinePosition * 61;

  return Math.min(
    66,
    Math.max(30, raw),
  );
}

function ConversationSpine() {
  const [
    selectedId,
    setSelectedId,
  ] =
    useState<string | null>(
      "conversation-1",
    );

  const previewRef =
    useRef<HTMLDivElement | null>(
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

  const selectedConversation =
    useMemo(
      () =>
        conversations.find(
          (conversation) =>
            conversation.id ===
            selectedId,
        ) ?? null,
      [selectedId],
    );

  const pocketStyle =
    useMemo<
      CSSProperties
    >(() => {
      if (
        !selectedConversation
      ) {
        return {};
      }

      return {
        "--memory-pocket-top":
          `${getPocketPosition(
            selectedConversation.spinePosition,
          )}%`,
      } as CSSProperties;
    }, [
      selectedConversation,
    ]);

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
      selection.addRange(range);
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
      setSelectedId(null);
    }
  };

  const handleMemorySelect = (
    id: string,
  ) => {
    setSelectedId(
      (current) =>
        current === id
          ? null
          : id,
    );
  };

  return (
    <aside
      className="conversation-spine"
      aria-label="Recent conversations"
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
            activeId={selectedId}
            onSelect={
              handleMemorySelect
            }
          />
        </div>

        {selectedConversation && (
          <article
            key={
              selectedConversation.id
            }
            className="memory-pocket"
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
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
            </div>

            <header className="memory-pocket-header">
              <div className="memory-pocket-identity">
                <span
                  className={`memory-pocket-role memory-pocket-role-${selectedConversation.role}`}
                >
                  {selectedConversation.role ===
                  "user"
                    ? "YOU"
                    : "QRONOS"}
                </span>

                <span className="memory-pocket-time">
                  {
                    selectedConversation.time
                  }
                </span>
              </div>

              <button
                type="button"
                className="memory-pocket-close"
                aria-label="بستن گفتگو"
                onClick={() =>
                  setSelectedId(
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
                  selectedConversation.text
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