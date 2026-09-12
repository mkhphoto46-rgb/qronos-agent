from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

APP = ROOT / "desktop" / "src" / "App.tsx"
VIEW = ROOT / "desktop" / "src" / "components" / "ConversationsView.tsx"
SPINE = ROOT / "desktop" / "src" / "components" / "ConversationSpine.tsx"
STORE = ROOT / "desktop" / "src" / "conversationStore.ts"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_home_and_full_conversation_share_one_store() -> None:
    app = read(APP)
    view = read(VIEW)

    assert 'from "./conversationStore"' in app
    assert "useConversationStore();" in app
    assert "sendConversationMessage(" in app
    assert "const sendHomeMessage =" in app

    assert 'from "../conversationStore"' in view
    assert "useConversationStore();" in view
    assert "sendConversationMessage(" in view
    assert "initialMessages" not in view


def test_memory_dna_reads_last_three_messages_from_same_store() -> None:
    text = read(SPINE)

    assert 'from "../conversationStore"' in text
    assert "useConversationStore();" in text
    assert ".slice(-3)" in text
    assert ".reverse()" in text
    assert "!message.pending" in text
    assert "!message.error" in text
    assert "const conversations: Conversation[] = [" not in text


def test_store_consumes_canonical_backend_timeline() -> None:
    text = read(STORE)

    assert '"qronos://runtime-event"' in text
    assert '"conversation_history_snapshot"' in text
    assert '"conversation_message_added"' in text
    assert "qronos.conversation_history_request" in text
    assert '"send_runtime_action"' in text
    assert '"send_text_message"' in text
    assert "mergeCanonicalMessage(" in text
    assert "applyHistorySnapshot(" in text


def test_text_response_is_only_a_correlated_fallback() -> None:
    text = read(STORE)

    assert '"text_response"' in text
    assert '"text_error"' in text
    assert "replacePendingFallback(" in text
    assert "current.pendingRequestId" in text
    assert "can never append a stale response" in text
