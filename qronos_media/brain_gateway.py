"""The media Fast-brain gateway: this runtime's existing governed Fast brain, and nothing else.

Media never talks to a model server and never chooses a model, tag, quantization, context, ``num_predict``, think
setting or keep-alive. A media generation request is ONE call to ``Orchestrator.answer_chat_only(messages)`` of the
running Qronos (the same call the paired Device Link uses for remote questions): the Fast brain under its configured
profile, with that call's activity and resource admission (read twice), its shared-ledger brain reservation and its
foreground cleanup. Media adds only its own system rules and a bounded evidence prompt.

Fail closed:

* no runtime source (the standalone tool, a runtime that is still starting) -> ``model_unavailable``;
* the person's own turn is in progress, or Qronos is paused -> ``model_busy`` before any call;
* the call's admission refuses (``RuntimeError``) -> ``model_busy``; any other failure -> ``brain_request_failed``;
* the orchestrator captured when the request was admitted must still be the runtime's current one at every call
  (generation binding), else ``generation_changed``.

Nothing here runs at import. The caller (one media worker, inside a media lease) makes one call at a time and checks
Stop/Pause/foreground between calls.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Callable

from qronos_media.resources import ResourceRefused

ROLE = "fast"
#: Bounds on what media sends and what it accepts back (characters).
MAX_PROMPT_CHARS = 12_000
MAX_ANSWER_CHARS = 8_000


@dataclass(frozen=True)
class BrainBinding:
    """The runtime orchestrator captured at admission, and the Fast profile's identity for provenance."""

    orchestrator: object
    model: str
    profile: str


def fast_profile_identity() -> tuple[str, str]:
    """(model tag, profile name) of the CONFIGURED Fast role, read for provenance only (never chosen here)."""
    try:
        from core.model_registry import get_model

        profile = get_model(ROLE)
        return str(getattr(profile, "name", "")), str(getattr(profile, "profile_id", ROLE))
    except Exception:  # noqa: BLE001 - unreadable config: provenance says so, nothing else changes
        return "unknown", ROLE


class FastBrainGateway:
    """``source()`` returns (orchestrator, busy) of THIS runtime, or None; ``busy()`` is True while the person's turn
    runs or Qronos is paused. Built by the bridge; tests pass a fake source."""

    def __init__(self, source: Callable[[], Any], *, identity: Callable[[], tuple[str, str]] = fast_profile_identity
                 ) -> None:
        self._source = source
        self._identity = identity

    def _current(self) -> tuple[object, Callable[[], bool]] | None:
        try:
            pair = self._source()
        except Exception:  # noqa: BLE001 - an unreadable runtime is no runtime
            return None
        if not isinstance(pair, tuple) or len(pair) != 2:
            return None
        orchestrator, busy = pair
        if orchestrator is None or not callable(getattr(orchestrator, "answer_chat_only", None)) or not callable(busy):
            return None
        return orchestrator, busy

    def unavailable_reason(self) -> str | None:
        return None if self._current() is not None else "model_unavailable"

    def bind(self) -> BrainBinding:
        current = self._current()
        if current is None:
            raise ResourceRefused("model_unavailable", "no governed Fast brain in this process")
        model, profile = self._identity()
        return BrainBinding(current[0], model, profile)

    def ask(self, binding: BrainBinding, system: str, user: str) -> str:
        """One governed Fast answer for the bound runtime, bounded both ways."""
        from core.brain_runtime import BrainMessage, BrainMessageRole

        if not system.strip() or not user.strip() or len(system) + len(user) > MAX_PROMPT_CHARS:
            raise ValueError("a media brain prompt must be non-empty and bounded")
        current = self._current()
        if current is None:
            raise ResourceRefused("model_unavailable", "the governed Fast brain is gone")
        orchestrator, busy = current
        if orchestrator is not binding.orchestrator:
            raise ResourceRefused("generation_changed", "the runtime's brain is not the one this request bound")
        if busy():
            raise ResourceRefused("model_busy", "the person's own turn or Pause comes first")
        messages = [BrainMessage(role=BrainMessageRole.SYSTEM, content=system),
                    BrainMessage(role=BrainMessageRole.USER, content=user)]
        try:
            answer = orchestrator.answer_chat_only(messages)
        except RuntimeError:
            raise ResourceRefused("model_busy", "the Fast brain's admission refused") from None
        except Exception as exc:  # noqa: BLE001 - typed, never a crash of the media worker
            raise ResourceRefused("brain_request_failed", type(exc).__name__) from None
        text = str(answer or "")
        return text[:MAX_ANSWER_CHARS]


def prompt_hash(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
