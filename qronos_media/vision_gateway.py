"""The media Vision gateway: this runtime's CONFIGURED Vision profile on its own brain runtime, and nothing else.

Media never talks to a model server of its own and never chooses a model, tag, quantization, context,
``num_predict``, think setting or keep-alive. A media Vision request is the call core/vision_worker.py already makes
(``brain_describe_fn``): ``BrainRuntime.chat`` of THIS runtime's orchestrator with the profile from
``core.model_registry.get_model("vision")`` - its name, ``context_tokens`` and ``generation_for("default")`` (think,
num_predict, keep_alive) - one user message with one prepared picture, then ``stop_model`` for that name. Media adds
only its own fixed instruction, a JSON response schema and the picture.

The resource side is NOT here: the stage runs under the runtime-issued HostedModelAuthority's lease for the declared
:data:`qronos_media.resources.QWEN3_VL_4B` (ledger budget + managed governor slot). This gateway marks the dispatch on
that lease before each request, marks a failed request unresolved, and gives the lease a capability
(:class:`RuntimeResidency`) whose only proof is the bound runtime's own list of running models.

Fail closed:

* no runtime source (the standalone tool, a runtime still starting) -> ``model_unavailable``;
* the configured Vision profile is not the declared one (another name, a larger VRAM estimate, another runtime) ->
  ``vision_profile_mismatch`` (the declaration, not the request, decides what may be reserved);
* the person's own turn is in progress, or Qronos is paused -> ``model_busy`` before any call;
* the orchestrator bound when the stage started must still be the runtime's current one -> ``generation_changed``;
* any failure of the call -> ``vision_request_failed`` (the lease is marked unresolved).

Nothing runs at import.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable

from qronos_media.resources import QWEN3_VL_4B, ResourceRefused

ROLE = "vision"
MAX_PROMPT_CHARS = 4_000
MAX_ANSWER_CHARS = 6_000


def configured_vision_profile() -> Any:
    from core.model_registry import get_model

    return get_model(ROLE)


class RuntimeResidency:
    """The lease capability for the declared Vision model: proof comes only from the bound runtime's model list."""

    def __init__(self, runtime: Any, model: str) -> None:
        self._runtime = runtime
        self._model = model

    def state(self) -> str:
        return "unknown"  # a brain runtime has no whole-tree lifecycle proof here

    def residency(self) -> str:
        """"unloaded" when the runtime does not list the model as running, "loaded" when it does, else "unknown"."""
        try:
            running = self._runtime.list_running_models()
        except Exception:  # noqa: BLE001 - an unreadable runtime is not proof
            return "unknown"
        names = {str(getattr(item, "name", "")) for item in running or ()}
        return "loaded" if self._model in names else "unloaded"


@dataclass(frozen=True)
class VisionBinding:
    """The orchestrator captured at the stage start, its runtime, and the configured profile's exact settings."""

    orchestrator: object
    runtime: object
    model: str
    profile: str
    context_tokens: int
    think: object
    num_predict: int | None
    keep_alive: str
    capability: RuntimeResidency

    def settings(self) -> dict[str, object]:
        return {"think": self.think, "numPredict": self.num_predict, "keepAlive": self.keep_alive,
                "contextTokens": self.context_tokens}


class VisionGateway:
    """``source()`` returns (orchestrator, busy) of THIS runtime, or None (the same source as the Fast-brain gateway);
    built by the bridge; tests pass fakes."""

    def __init__(self, source: Callable[[], Any], *, profile: Callable[[], Any] = configured_vision_profile) -> None:
        self._source = source
        self._profile = profile

    def _current(self) -> tuple[object, Callable[[], bool]] | None:
        try:
            pair = self._source()
        except Exception:  # noqa: BLE001 - an unreadable runtime is no runtime
            return None
        if not isinstance(pair, tuple) or len(pair) != 2:
            return None
        orchestrator, busy = pair
        runtime = getattr(orchestrator, "runtime", None) if orchestrator is not None else None
        if runtime is None or not callable(getattr(runtime, "chat", None)) or not callable(busy):
            return None
        return orchestrator, busy

    def _checked_profile(self) -> Any:
        try:
            profile = self._profile()
        except Exception:  # noqa: BLE001
            raise ResourceRefused("vision_profile_mismatch", "the configured Vision profile is unreadable") from None
        name = str(getattr(profile, "name", ""))
        vram = math.ceil(float(getattr(profile, "estimated_vram_gb", 0.0)) * 1024)
        if (str(getattr(profile, "role", "")) != ROLE or name != QWEN3_VL_4B.name or vram > QWEN3_VL_4B.vram_mb
                or str(getattr(profile, "runtime", "")) != "ollama"):
            raise ResourceRefused("vision_profile_mismatch", f"configured {name!r} is not the declared Vision model")
        return profile

    def unavailable_reason(self) -> str | None:
        if self._current() is None:
            return "model_unavailable"
        try:
            self._checked_profile()
        except ResourceRefused as refused:
            return refused.reason
        return None

    def bind(self) -> VisionBinding:
        current = self._current()
        if current is None:
            raise ResourceRefused("model_unavailable", "no runtime Vision in this process")
        profile = self._checked_profile()
        generation = profile.generation_for("default")
        runtime = getattr(current[0], "runtime")
        return VisionBinding(current[0], runtime, profile.name, str(getattr(profile, "profile_id", ROLE)),
                             int(profile.context_tokens), generation.think, generation.num_predict,
                             generation.keep_alive, RuntimeResidency(runtime, profile.name))

    def look(self, binding: VisionBinding, lease: Any, image: Any, prompt: str, schema: dict) -> str:
        """One Vision answer about one prepared picture, under ``lease`` (the declared Vision ModelLease)."""
        from core.brain_runtime import BrainMessage, BrainMessageRole

        if not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError("a media Vision prompt must be non-empty and bounded")
        current = self._current()
        if current is None:
            raise ResourceRefused("model_unavailable", "the runtime's Vision is gone")
        orchestrator, busy = current
        if orchestrator is not binding.orchestrator or getattr(orchestrator, "runtime", None) is not binding.runtime:
            raise ResourceRefused("generation_changed", "the runtime is not the one this stage bound")
        if busy():
            raise ResourceRefused("model_busy", "the person's own turn or Pause comes first")
        lease.mark_dispatched()  # from here the runtime may load the model under this lease
        try:
            answer = binding.runtime.chat(
                model_name=binding.model,
                messages=[BrainMessage(role=BrainMessageRole.USER, content=prompt, images=(image,))],
                think=binding.think,
                num_predict=binding.num_predict,
                num_ctx=binding.context_tokens,
                keep_alive=binding.keep_alive,
                response_format=schema,
            )
        except Exception as exc:  # noqa: BLE001 - typed, never a crash of the media worker
            lease.mark_unresolved()
            raise ResourceRefused("vision_request_failed", type(exc).__name__) from None
        finally:
            try:
                binding.runtime.stop_model(binding.model)  # nothing stays on the card (as core/vision_worker does)
            except Exception:  # noqa: BLE001 - the residency proof, not this call, decides when it is freed
                pass
        return str(answer or "")[:MAX_ANSWER_CHARS]
