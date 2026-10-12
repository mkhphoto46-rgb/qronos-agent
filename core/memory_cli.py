from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.user_memory import MemoryStore


def _payload() -> dict[str, object]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def main() -> int:
    from core import startup_admission  # before the first store opens (core/startup_admission.py)

    refused = startup_admission.enter_cli("memory")
    if refused is not None:
        return refused
    if len(sys.argv) < 2:
        raise ValueError("Memory operation is required.")

    op = sys.argv[1].strip().lower()
    payload = _payload()
    store = MemoryStore()

    if op == "snapshot":
        result = {
            "ok": True,
            **store.status(),
            "memories": [
                item.public()
                for item in store.list(include_inactive=True)
            ],
        }
    elif op == "status":
        result = {"ok": True, **store.status()}
    elif op == "list":
        result = {
            "ok": True,
            "memories": [
                item.public()
                for item in store.list(
                    include_inactive=_bool(payload.get("includeInactive"), True)
                )
            ],
        }
    elif op == "set_enabled":
        enabled = _bool(payload.get("enabled"), True)
        store.set_memory_enabled(enabled)
        result = {"ok": True, **store.status()}
    elif op == "delete":
        memory_id = str(payload.get("id", "")).strip()
        if not memory_id:
            raise ValueError("Memory id is required.")
        store.delete(memory_id)
        result = {"ok": True, **store.status()}
    elif op == "approve":
        memory_id = str(payload.get("id", "")).strip()
        if not memory_id:
            raise ValueError("Memory id is required.")
        result = {"ok": True, "memory": store.approve_candidate(memory_id).public()}
    elif op == "retry_pending":
        result = {"ok": True, **store.retry_pending_embeddings(), **store.status()}
    elif op == "reindex":
        result = {"ok": True, "reindex": store.rebuild_index(), **store.status()}
    elif op == "create":
        result = {
            "ok": True,
            "memory": store.create_candidate(
                value=str(payload.get("value", "")),
                category=str(payload.get("category", "")),
                memory_key=str(payload.get("memoryKey", "")),
                scope_type=str(payload.get("scopeType", "global")),
                scope_key=str(payload.get("scopeKey", "")),
                confidence=float(payload.get("confidence", 1.0)),
                evidence_type=str(payload.get("evidenceType", "explicit")),
                sensitivity=str(payload.get("sensitivity", "normal")),
                source_kind=str(payload.get("sourceKind", "user")),
                source_conversation_id=str(payload.get("sourceConversationId", "")),
                source_message_id=str(payload.get("sourceMessageId", "")),
                source_request_id=str(payload.get("sourceRequestId", "")),
                user_confirmed_sensitive=_bool(
                    payload.get("userConfirmedSensitive"),
                    False,
                ),
            ).public(),
        }
    elif op == "supersede":
        memory_id = str(payload.get("id", "")).strip()
        if not memory_id:
            raise ValueError("Memory id is required.")
        result = {
            "ok": True,
            "memory": store.supersede(
                memory_id,
                value=str(payload.get("value", "")),
                confidence=float(payload.get("confidence", 1.0)),
                evidence_type=str(payload.get("evidenceType", "explicit")),
                sensitivity=(
                    str(payload.get("sensitivity"))
                    if payload.get("sensitivity") is not None
                    else None
                ),
                source_kind=str(payload.get("sourceKind", "user_correction")),
                source_conversation_id=str(payload.get("sourceConversationId", "")),
                source_message_id=str(payload.get("sourceMessageId", "")),
                source_request_id=str(payload.get("sourceRequestId", "")),
                user_confirmed_sensitive=_bool(
                    payload.get("userConfirmedSensitive"),
                    False,
                ),
            ).public(),
        }
    elif op == "retrieve":
        scopes = payload.get("scopes", {})
        result = {
            "ok": True,
            "results": store.retrieve(
                str(payload.get("query", "")),
                scopes=(
                    {str(k): str(v) for k, v in scopes.items()}
                    if isinstance(scopes, dict)
                    else {}
                ),
                limit=int(payload.get("limit", 5)),
                candidate_limit=int(payload.get("candidateLimit", 20)),
            ),
        }
    else:
        raise ValueError(f"Unsupported memory operation: {op}")

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "errorType": type(exc).__name__},
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)
