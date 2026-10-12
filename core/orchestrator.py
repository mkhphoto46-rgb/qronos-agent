from __future__ import annotations

import os

from dataclasses import dataclass
from typing import Sequence

from core.activity_guard import (
    ActivityGuard,
    ActivityMode,
    ResourcePressure,
)
from core.brain_runtime import (
    BrainMessage,
    BrainMessageRole,
    BrainRuntime,
)
from core.compute_estimator import ComputeEstimator, ComputeLevel
from core.model_manager import (
    InstalledModelSizes,
    ModelManager,
    TaskClass,
)
from core.model_registry import get_model
from core.memory_runtime import MemoryRuntime
from core.ollama_controller import OllamaController
from core.resource_guard import (
    read_gpu_status,
    read_system_status,
    read_system_status_since_last_call,
)
from core.resource_policy import ResourceDecision
from core.resource_context import snapshot_for_external_pressure
from core.resource_ownership import (
    GLOBAL_RESOURCE_LEDGER,
    ResourceBudget,
    ResourceOwner,
    WorkloadPriority,
)
from core.telemetry_cache import TelemetryCache
from core.task_plan import PlanStep, TaskPlan
from core.workers import Unavailable, WorkerRegistry


QRONOS_SYSTEM_PROMPT = """
You are Qronos, the user's personal AI assistant.

Identity rules:
- Your name and product identity are Qronos.
- Keep that identity internally consistent in every Brain mode.
- Do not introduce yourself, state your name, or describe yourself in ordinary answers.
- Do not begin ordinary answers with a greeting, self-introduction, or identity statement.
- If the user explicitly greets you, a brief natural greeting is allowed, but do not reintroduce yourself unless they ask who or what you are.
- Mention that you are Qronos only when the user explicitly asks about your name, identity, or which assistant they are speaking with.
- If asked what AI you are, what model you are, who your underlying model provider is, what LLM powers you, or similar questions, identify yourself only as Qronos, the user's personal AI assistant.
- Do not claim to be Qwen, Gemma, or any other underlying model.
- Do not reveal, speculate about, or volunteer internal model names, provider names, runtime implementation details, model routing details, or hidden infrastructure.
- Fast Brain and Heavy Brain are internal implementation concepts and must not alter your identity.

Language rules:
- Answer in the same dominant language as the user's current message unless the user explicitly requests another language.
- When the user writes or speaks Persian, answer in Persian.
- Do not switch from Persian to Arabic merely because both languages use similar scripts or because the Persian input is informal, colloquial, noisy, or imperfect.
- Natural English technical terms inside a Persian response are allowed when they are clearer or conventionally used.
- If the language of the current message is genuinely ambiguous, prefer the established language of the current conversation.

Response rules:
- Answer the user's request directly.
- Do not repeat or paraphrase the user's question before answering unless clarification is genuinely necessary.
- For simple questions, prefer the shortest complete correct answer.
- If one short sentence fully answers the question, use one short sentence.
- Do not repeat the same answer in multiple forms.
- Be accurate and helpful.
- Do not invent facts when information is uncertain.
- Preserve conversational context from the supplied message history.
""".strip()


FORCE_BRAIN_ENV = "QRONOS_FORCE_BRAIN"
HEAVY_EFFORT_ENV = "QRONOS_HEAVY_EFFORT"

# Brain request deadlines are role-aware and intentionally bounded.
#
# The old single 90-second runtime timeout was shorter than the measured
# GPT-OSS 20B cold-load path on the 8 GB development GPU (~100 s total, with
# ~98 s spent loading). That caused a valid Heavy request to be killed before
# the model could answer. Warm Heavy requests are much faster, so they retain
# tighter effort-specific ceilings; cold requests receive a separate load
# allowance rather than turning the global runtime timeout into a large number.
FAST_CHAT_TIMEOUT_SECONDS = 90.0
HEAVY_COLD_LOAD_ALLOWANCE_SECONDS = 120.0
HEAVY_WARM_TIMEOUT_SECONDS = {
    "eco": 90.0,
    "normal": 120.0,
    "deep": 180.0,
}


def _keep_alive_enabled(value: str) -> bool:
    return value.strip().lower() not in {
        "",
        "0",
        "0s",
        "0m",
        "0h",
    }


@dataclass(frozen=True)
class StepResult:
    """Result of one executed task step."""

    order: int
    success: bool
    output: str
    error: str | None = None

    # Set when the step could not run because no worker answers for its
    # task type. Carries a reason code, so a caller can tell a missing
    # capability from a failed one without reading the error text.
    unavailable: Unavailable | None = None

    # Set when resource policy, not the brain, refused the step. A caller
    # that can wait — the queue — defers on this instead of failing, without
    # having to recognise the refusal by its wording.
    resource_blocked: bool = False


#: Upper bound on one installed-model listing read during admission.
INSTALLED_LISTING_TIMEOUT_SECONDS = 2.0


def _installed_size_reader(runtime) -> InstalledModelSizes | None:
    """
    Installed model sizes from the same local model runtime, for admission.

    For a runtime with a local model store (one that names a base URL). The
    raw /api/tags listing is read through the bounded loopback-only local
    model client and validated by InstalledModelSizes before any value is
    used. A base URL that is not a valid local address makes every read fail,
    so new GPU loads are deferred rather than admitted on the estimate.

    A runtime with no base URL at all - an injected or legacy backend - gets
    no reader, and admission keeps the configured estimate for it.
    """
    if not hasattr(runtime, "base_url"):
        return None

    base_url = getattr(runtime, "base_url", None)

    def read_listing():
        from core.local_ipc import local_model_base_url, requests_client

        url = local_model_base_url(str(base_url))
        response = requests_client.get(
            f"{url}/api/tags",
            timeout=INSTALLED_LISTING_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()

    return InstalledModelSizes(read_listing)


class Orchestrator:
    """
    Execute Qronos plans with resource and activity awareness.

    The Orchestrator also owns the product-level Qronos identity message.
    This keeps identity consistent regardless of which BrainRuntime or
    model is selected.
    """

    def __init__(
        self,
        runtime: BrainRuntime | None = None,
        model_manager: ModelManager | None = None,
        activity_guard: ActivityGuard | None = None,
        workers: WorkerRegistry | None = None,
        telemetry: TelemetryCache | None = None,
        memory_runtime: MemoryRuntime | None = None,
    ) -> None:
        self.runtime = (
            runtime
            if runtime is not None
            else OllamaController()
        )

        # Temporary compatibility alias for the existing tests and
        # development code. Higher-level Qronos code should use runtime.
        self.ollama = self.runtime

        self.model_manager = (
            model_manager
            if model_manager is not None
            else ModelManager(
                runtime=self.runtime,
                installed_size_bytes=_installed_size_reader(self.runtime),
            )
        )

        self.telemetry = telemetry or TelemetryCache(
            read_system=lambda: read_system_status_since_last_call(),
            read_gpu=lambda: read_gpu_status(),
            first_read_system=lambda: read_system_status(),
        )

        self.activity_guard = (
            activity_guard
            if activity_guard is not None
            else ActivityGuard(
                snapshot_reader=lambda: snapshot_for_external_pressure(
                    self.telemetry.current()
                )
            )
        )

        self.memory_runtime = memory_runtime
        if (
            self.memory_runtime is None
            and MemoryRuntime.enabled_by_environment()
        ):
            self.memory_runtime = MemoryRuntime(
                activity_guard=self.activity_guard,
            )

        # Empty unless something registers a worker, so an orchestrator
        # built the old way behaves exactly as it did before.
        self.workers = (
            workers
            if workers is not None
            else WorkerRegistry()
        )

        # ComputeEstimator is now authoritative only for Heavy effort
        # (eco/normal/deep). Intent Gate + Resolver authority remains a
        # separate routing stage, so this change does not silently replace
        # the current worker/router behavior.
        self.compute_estimator = ComputeEstimator()

        # Warm-model ownership must survive across turns.  The global resource
        # ledger is process-local, so a model preloaded by the PowerShell helper
        # (or kept alive by Ollama after a previous turn) has to be adopted by
        # the Python runtime before ActivityGuard classifies GPU pressure.
        self._resident_brain_reservations: dict[str, str] = {}

    def execute_plan(
        self,
        plan: TaskPlan,
        conversation_messages: (
            Sequence[BrainMessage] | None
        ) = None,
        priority: WorkloadPriority | None = None,
    ) -> list[StepResult]:
        """
        Execute plan steps in order.

        ``priority`` is how the brain reservation is attributed in the
        resource ledger. Queued background work passes BACKGROUND; a direct
        user turn leaves it unset. It changes the record, not the admission:
        every resource and activity check below applies the same either way.

        conversation_messages contains conversation turns that happened
        before the current plan request.

        The current PlanStep description is appended as the newest user
        message by the Orchestrator, preventing the current turn from being
        duplicated in the model context.
        """

        results: list[StepResult] = []

        for step in plan.steps:
            # Adopt any configured Fast/Heavy model that is already resident
            # before pressure classification.  This prevents a Qronos-owned
            # warm model from being mistaken for new external/user GPU load.
            self._sync_resident_brain_ownership()

            activity_state = (
                self.activity_guard.detect()
            )

            result = self._execute_step(
                step=step,
                previous_results=results,
                activity_mode=(
                    activity_state.mode
                ),
                resource_pressure=(
                    activity_state.resource_pressure
                ),
                conversation_messages=(
                    conversation_messages
                ),
                priority=priority,
            )

            results.append(
                result
            )

            if not result.success:
                break

        return results

    def _execute_step(
        self,
        step: PlanStep,
        previous_results: list[StepResult],
        activity_mode: ActivityMode,
        resource_pressure: ResourcePressure,
        conversation_messages: (
            Sequence[BrainMessage] | None
        ),
        priority: WorkloadPriority | None = None,
    ) -> StepResult:
        task_class = self._get_task_class(
            step
        )

        # Generic evaluation override. This is deliberately role-based rather
        # than model-name-based so Qronos remains modular when either brain is
        # replaced later. Worker routes remain untouched.
        forced_brain = os.environ.get(
            FORCE_BRAIN_ENV,
            "auto",
        ).strip().lower()

        if task_class is not None:
            if forced_brain == "fast":
                task_class = TaskClass.FAST
            elif forced_brain == "heavy":
                task_class = TaskClass.HEAVY

        if task_class is None:
            return self._execute_with_worker(
                step
            )

        try:
            selection = (
                self._prepare_resources(
                    task_class=task_class,
                    activity_mode=activity_mode,
                    resource_pressure=(
                        resource_pressure
                    ),
                )
            )

            if (
                selection.decision
                is not ResourceDecision.ALLOW
            ):
                return StepResult(
                    order=step.order,
                    success=False,
                    output="",
                    error=self._resource_error(
                        selection.decision,
                        resource_pressure,
                        selection,
                    ),
                    resource_blocked=True,
                )

            # The earlier resource snapshot may already be stale after
            # cleanup or plan preparation. Re-read immediately before
            # allowing the runtime to load or execute a brain.
            live_state = (
                self.activity_guard.detect()
            )

            selection = (
                self._prepare_resources(
                    task_class=task_class,
                    activity_mode=(
                        live_state.mode
                    ),
                    resource_pressure=(
                        live_state.resource_pressure
                    ),
                )
            )

            if (
                selection.decision
                is not ResourceDecision.ALLOW
            ):
                return StepResult(
                    order=step.order,
                    success=False,
                    output="",
                    error=self._resource_error(
                        selection.decision,
                        live_state.resource_pressure,
                        selection,
                    ),
                    resource_blocked=True,
                )

            memory_context = ""
            if self.memory_runtime is not None:
                try:
                    memory_result = self.memory_runtime.context_for_query(
                        step.description,
                        activity_mode=live_state.mode,
                        resource_pressure=live_state.resource_pressure,
                    )
                    memory_context = memory_result.context
                except Exception:
                    # User Memory is optional context. It must never turn a
                    # valid Brain request into a failed request.
                    memory_context = ""

            messages = (
                self._build_brain_messages(
                    description=(
                        step.description
                    ),
                    previous_results=(
                        previous_results
                    ),
                    conversation_messages=(
                        conversation_messages
                    ),
                    memory_context=memory_context,
                )
            )

            generation_effort = self._generation_effort(
                task_class=task_class,
                description=step.description,
            )
            generation = selection.model.generation_for(
                generation_effort
            )

            think_value = generation.think
            generation_budget = generation.num_predict
            keep_alive = self._effective_keep_alive(
                configured_keep_alive=generation.keep_alive,
                activity_mode=live_state.mode,
            )
            timeout_seconds = self._brain_timeout_seconds(
                task_class=task_class,
                generation_effort=generation_effort,
                already_resident=selection.already_resident,
            )

            background = priority is WorkloadPriority.BACKGROUND
            reservation_was_ours = (
                self._active_brain_reservation(selection.model.name) is None
            )
            reservation = self._reserve_brain_resources(
                selection.model.name,
                selection.model.estimated_vram_gb,
                priority=priority,
            )

            try:
                response = self.runtime.chat(
                    model_name=(
                        selection.model.name
                    ),
                    messages=messages,
                    think=think_value,
                    num_predict=generation_budget,
                    num_ctx=(
                        selection.model.context_tokens
                    ),
                    keep_alive=keep_alive,
                    timeout_seconds=timeout_seconds,
                )
            finally:
                # Model residency and resource ownership have the same lifetime.
                # If keep_alive is enabled, retain the reservation while Ollama
                # actually keeps this model resident.  A later role swap or
                # keep-alive expiry is reconciled by _sync_resident_brain_ownership.
                if background:
                    # Background work may share a model the user's own turn is
                    # using right now. It never unloads a model here and never
                    # ends a reservation it did not create; its caller decides,
                    # with foreground ownership in view, what it may unload.
                    # keep_alive still reaches the model runtime, which
                    # unloads only once every request using the model is done.
                    if reservation_was_ours:
                        self._release_brain_resources(
                            reservation.reservation_id
                        )

                    self._sync_resident_brain_ownership()
                elif not _keep_alive_enabled(keep_alive):
                    try:
                        self.runtime.stop_model(
                            selection.model.name
                        )
                    except Exception:
                        try:
                            self.model_manager.unload_qronos_models()
                        except Exception:
                            pass

                    self._release_brain_resources(
                        reservation.reservation_id
                    )
                else:
                    self._sync_resident_brain_ownership()

                    if not self.model_manager.is_model_resident(
                        selection.model.name
                    ):
                        self._release_brain_resources(
                            reservation.reservation_id
                        )

            return StepResult(
                order=step.order,
                success=True,
                output=response,
            )

        except Exception as exc:
            return StepResult(
                order=step.order,
                success=False,
                output="",
                error=str(exc),
            )

    def resident_brain_names(self) -> frozenset[str]:
        """
        Configured Qronos brains loaded right now. Raises when unknown.

        Unknown is not empty: a caller deciding what it may unload must not
        mistake "could not ask" for "nothing was loaded".
        """
        running = {
            item.name
            for item in self.runtime.list_running_models()
            if item.name
        }

        return frozenset(
            running.intersection(self._brain_profiles_by_name().keys())
        )

    def release_brains_loaded_since(self, before: frozenset[str]) -> bool:
        """
        Unload only the Qronos brains that were not resident in ``before``.

        For background work, which must give back what it loaded without
        evicting a brain that was already warm for the user's own turns.
        """
        changed = False

        for model_name in self.resident_brain_names() - set(before):
            try:
                self.runtime.stop_model(model_name)
                changed = True
            except Exception:
                pass

        self._sync_resident_brain_ownership()
        self.telemetry.invalidate()

        return changed

    def release_brains_for_voice_phase(self) -> bool:
        """
        Yield only configured Qronos Fast/Heavy models for the next GPU voice
        phase.

        This is intentionally narrower than runtime.unload_all(): unrelated
        Ollama models owned by the user are never touched. Warm ownership is
        reconciled immediately so ActivityGuard cannot keep stale Brain VRAM
        reservations after the physical model has yielded the slot.
        """
        changed = self.model_manager.unload_qronos_models()

        self._sync_resident_brain_ownership()
        self.telemetry.invalidate()

        return changed

    def answer_chat_only(self, messages: Sequence[BrainMessage]) -> str:
        """
        One tool-free answer from the Fast Brain, under its configured profile.

        For callers that must not plan, act or queue: the Device Link's remote
        questions. It is a Fast plan step without the plan. It gets the same
        admission: activity and resource checks, repeated immediately before
        the call. It uses the same model, context, think, ``num_predict`` and
        keep-alive (including the activity-mode keep-alive rule), and the same
        reservation and foreground cleanup. It adds nothing else: no planner,
        no workers, no user-memory context, no conversation history, no
        web-research overrides.

        ``messages`` are the caller's own system rules and the question. The
        Qronos identity always comes first; caller system messages follow it
        and cannot replace it.

        Raises ``RuntimeError`` when admission refuses, so the caller can say
        "busy" rather than wait or queue.
        """

        chat = [
            BrainMessage(
                role=BrainMessageRole.SYSTEM,
                content=QRONOS_SYSTEM_PROMPT,
            )
        ]
        chat.extend(message for message in messages if message.content.strip())

        task_class = TaskClass.FAST
        activity = self.activity_guard.detect()
        selection = self._prepare_resources(
            task_class=task_class,
            activity_mode=activity.mode,
            resource_pressure=activity.resource_pressure,
        )

        if selection.decision is not ResourceDecision.ALLOW:
            raise RuntimeError(
                self._resource_error(
                    selection.decision,
                    activity.resource_pressure,
                    selection,
                )
            )

        # Re-read immediately before loading or running the brain, exactly as
        # a plan step does: the first snapshot may be stale after cleanup.
        live_state = self.activity_guard.detect()
        selection = self._prepare_resources(
            task_class=task_class,
            activity_mode=live_state.mode,
            resource_pressure=live_state.resource_pressure,
        )

        if selection.decision is not ResourceDecision.ALLOW:
            raise RuntimeError(
                self._resource_error(
                    selection.decision,
                    live_state.resource_pressure,
                    selection,
                )
            )

        generation_effort = self._generation_effort(
            task_class=task_class,
            description="",
        )
        generation = selection.model.generation_for(generation_effort)
        keep_alive = self._effective_keep_alive(
            configured_keep_alive=generation.keep_alive,
            activity_mode=live_state.mode,
        )
        timeout_seconds = self._brain_timeout_seconds(
            task_class=task_class,
            generation_effort=generation_effort,
            already_resident=selection.already_resident,
        )
        reservation = self._reserve_brain_resources(
            selection.model.name,
            selection.model.estimated_vram_gb,
        )

        try:
            return self.runtime.chat(
                model_name=selection.model.name,
                messages=chat,
                think=generation.think,
                num_predict=generation.num_predict,
                num_ctx=selection.model.context_tokens,
                keep_alive=keep_alive,
                timeout_seconds=timeout_seconds,
            )
        finally:
            # The foreground cleanup of a Fast plan step, unchanged.
            if not _keep_alive_enabled(keep_alive):
                try:
                    self.runtime.stop_model(selection.model.name)
                except Exception:
                    try:
                        self.model_manager.unload_qronos_models()
                    except Exception:
                        pass

                self._release_brain_resources(reservation.reservation_id)
            else:
                self._sync_resident_brain_ownership()

                if not self.model_manager.is_model_resident(selection.model.name):
                    self._release_brain_resources(reservation.reservation_id)

    def answer_web_prompt(self, prompt: str) -> str:
        """Run Web Research's evidence prompt through the guarded Fast Brain."""
        activity = self.activity_guard.detect()
        selection = self._prepare_resources(
            TaskClass.FAST,
            activity.mode,
            activity.resource_pressure,
        )

        if selection.decision is not ResourceDecision.ALLOW:
            raise RuntimeError(
                self._resource_error(
                    selection.decision,
                    activity.resource_pressure,
                    selection,
                )
            )

        reservation = self._reserve_brain_resources(
            selection.model.name,
            selection.model.estimated_vram_gb,
        )

        try:
            return self.runtime.chat(
                model_name=selection.model.name,
                prompt=prompt,
                think=False,
                num_predict=768,
                # Web answers are occasional and can fill VRAM. Unload after
                # the request so the next voice turn is not blocked.
                keep_alive="0",
            )
        finally:
            try:
                self.runtime.stop_model(
                    selection.model.name
                )
            except Exception:
                try:
                    self.runtime.unload_all()
                except Exception:
                    pass

            self._release_brain_resources(
                reservation.reservation_id
            )

    def _prepare_resources(
        self,
        task_class: TaskClass,
        activity_mode: ActivityMode,
        resource_pressure: ResourcePressure,
    ):
        """
        Prepare the one available Qronos brain slot, then evaluate admission.

        On the 8 GB development GPU, Fast and Heavy are logically available
        together but are not allowed to accumulate in VRAM together. Only
        configured Qronos models are swapped; unrelated Ollama models are not
        touched.
        """

        slot_changed = self.model_manager.prepare_model_slot(
            task_class
        )

        if slot_changed:
            # Release ownership for the model that was just evicted before
            # reclassifying pressure for the target role.
            self._sync_resident_brain_ownership()
            self.telemetry.invalidate()
            refreshed = self.activity_guard.detect()
            activity_mode = refreshed.mode
            resource_pressure = refreshed.resource_pressure

        snapshot = self.telemetry.current()
        system = snapshot.system
        gpu = snapshot.gpu

        selection = self.model_manager.select_model(
            task_class=task_class,
            system=system,
            gpu=gpu,
            activity_mode=activity_mode,
            resource_pressure=resource_pressure,
        )

        if selection.decision is ResourceDecision.ALLOW:
            return selection

        # If the currently resident Qronos model still cannot be admitted,
        # unload Qronos-owned models only, refresh telemetry and retry once.
        # Never unload unrelated Ollama models owned by the user.
        changed = self.model_manager.unload_qronos_models()

        if not changed:
            return selection

        self.telemetry.invalidate()
        refreshed_activity = self.activity_guard.detect()
        snapshot = self.telemetry.current()

        return self.model_manager.select_model(
            task_class=task_class,
            system=snapshot.system,
            gpu=snapshot.gpu,
            activity_mode=refreshed_activity.mode,
            resource_pressure=refreshed_activity.resource_pressure,
        )

    @staticmethod
    def _brain_profiles_by_name():
        profiles = {}

        for role in ("fast", "heavy"):
            model = get_model(role)
            profiles[model.name] = model

        return profiles

    def _running_brain_names(self) -> set[str]:
        try:
            running = {
                item.name
                for item in self.runtime.list_running_models()
                if item.name
            }
        except Exception:
            return set()

        return running.intersection(
            self._brain_profiles_by_name().keys()
        )

    def _active_brain_reservation(self, model_name: str):
        workload = f"brain:{model_name}"

        for reservation in GLOBAL_RESOURCE_LEDGER.active(
            ResourceOwner.QRONOS
        ):
            if reservation.workload == workload:
                return reservation

        return None

    def _sync_resident_brain_ownership(self) -> None:
        """
        Reconcile actual Ollama Fast/Heavy residency with ResourceLedger.

        The PowerShell preloader runs in a different process, and Ollama can
        also keep a model warm after a completed turn.  In both cases the
        Python ledger must adopt the resident model before ActivityGuard reads
        pressure; otherwise Qronos can classify its own VRAM/GPU activity as
        external load and evict the Heavy Brain it is about to use.
        """

        running_names = self._running_brain_names()
        profiles = self._brain_profiles_by_name()
        changed = False

        for model_name, reservation_id in list(
            self._resident_brain_reservations.items()
        ):
            if model_name in running_names:
                continue

            self._release_brain_resources(
                reservation_id,
                forget_mapping=False,
            )
            self._resident_brain_reservations.pop(
                model_name,
                None,
            )
            changed = True

        for model_name in running_names:
            reservation_id = self._resident_brain_reservations.get(
                model_name
            )

            if reservation_id is not None:
                active = GLOBAL_RESOURCE_LEDGER.get(
                    reservation_id
                )

                if (
                    active is not None
                    and active in GLOBAL_RESOURCE_LEDGER.active(
                        ResourceOwner.QRONOS
                    )
                ):
                    continue

                self._resident_brain_reservations.pop(
                    model_name,
                    None,
                )

            existing = self._active_brain_reservation(
                model_name
            )

            if existing is not None:
                self._resident_brain_reservations[
                    model_name
                ] = existing.reservation_id
                continue

            profile = profiles[model_name]
            reservation = GLOBAL_RESOURCE_LEDGER.reserve(
                owner=ResourceOwner.QRONOS,
                workload=f"brain:{model_name}",
                priority=WorkloadPriority.INTERACTIVE_WARM,
                budget=ResourceBudget(
                    vram_mb=max(
                        0,
                        int(
                            round(
                                float(profile.estimated_vram_gb)
                                * 1024.0
                            )
                        ),
                    )
                ),
                allow_duplicate_workload=False,
            )
            self._resident_brain_reservations[
                model_name
            ] = reservation.reservation_id
            changed = True

        if changed:
            self.telemetry.invalidate()

    def _reserve_brain_resources(
        self,
        model_name: str,
        estimated_vram_gb: float,
        priority: WorkloadPriority | None = None,
    ):
        """Reserve or reuse ownership for one Fast/Heavy brain."""

        existing = self._active_brain_reservation(
            model_name
        )

        if existing is not None:
            self._resident_brain_reservations[
                model_name
            ] = existing.reservation_id
            return existing

        reservation = GLOBAL_RESOURCE_LEDGER.reserve(
            owner=ResourceOwner.QRONOS,
            workload=f"brain:{model_name}",
            priority=(
                priority
                if priority is not None
                else WorkloadPriority.ACTIVE_QRONOS_REQUEST
            ),
            budget=ResourceBudget(
                vram_mb=max(
                    0,
                    int(
                        round(
                            float(estimated_vram_gb)
                            * 1024.0
                        )
                    ),
                )
            ),
            allow_duplicate_workload=False,
        )
        self._resident_brain_reservations[
            model_name
        ] = reservation.reservation_id
        self.telemetry.invalidate()
        return reservation

    def _release_brain_resources(
        self,
        reservation_id: str,
        *,
        forget_mapping: bool = True,
    ) -> None:
        """Release Brain ownership and invalidate cached telemetry."""
        try:
            GLOBAL_RESOURCE_LEDGER.release(
                reservation_id
            )

        except Exception:
            # Ownership bookkeeping must never replace the user's real
            # Brain result with a cleanup error.
            pass

        finally:
            if forget_mapping:
                for model_name, tracked_id in list(
                    self._resident_brain_reservations.items()
                ):
                    if tracked_id == reservation_id:
                        self._resident_brain_reservations.pop(
                            model_name,
                            None,
                        )

            self.telemetry.invalidate()


    @staticmethod
    def _brain_timeout_seconds(
        task_class: TaskClass,
        generation_effort: str,
        already_resident: bool,
    ) -> float:
        """
        Return the bounded request deadline for one Brain execution.

        Fast keeps the existing conservative ceiling.

        Heavy separates two costs that the Ollama HTTP request combines:
        - model cold-load / placement;
        - answer generation.

        A resident Heavy model needs only the effort-specific generation
        ceiling. A cold Heavy request gets an additional bounded load allowance.
        This fixes cold activation without weakening the deadline for every
        model and without coupling policy to a concrete Heavy model name.
        """
        if task_class is TaskClass.FAST:
            return FAST_CHAT_TIMEOUT_SECONDS

        effort = (generation_effort or "eco").strip().lower()
        warm_timeout = HEAVY_WARM_TIMEOUT_SECONDS.get(
            effort,
            HEAVY_WARM_TIMEOUT_SECONDS["eco"],
        )

        if already_resident:
            return warm_timeout

        return (
            warm_timeout
            + HEAVY_COLD_LOAD_ALLOWANCE_SECONDS
        )


    @staticmethod
    def _effective_keep_alive(
        configured_keep_alive: str,
        activity_mode: ActivityMode,
    ) -> str:
        """
        Yield Brain residency immediately while the user is actively gaming
        or creating.

        Normal and idle conversational use keep the model profile's configured
        short warm-residency policy. User-active modes always win over model
        warmth so Qronos cannot hold VRAM after answering.
        """
        if activity_mode in {
            ActivityMode.GAMING_ASSIST,
            ActivityMode.GAMING_PERFORMANCE,
            ActivityMode.CREATOR_ASSIST,
            ActivityMode.CREATOR_PERFORMANCE,
        }:
            return "0"

        return configured_keep_alive


    def _generation_effort(
        self,
        task_class: TaskClass,
        description: str,
    ) -> str:
        if task_class is TaskClass.FAST:
            return "default"

        override = os.environ.get(
            HEAVY_EFFORT_ENV,
            "auto",
        ).strip().lower()

        if override in {"eco", "normal", "deep"}:
            return override

        estimate = self.compute_estimator.estimate(
            description
        )

        if estimate.level is ComputeLevel.HEAVY_DEEP:
            return "deep"

        if estimate.level is ComputeLevel.HEAVY_NORMAL:
            return "normal"

        # A capability-sensitive request can legitimately route to Heavy while
        # requiring little raw compute. In that case Heavy ECO is the correct
        # execution profile.
        return "eco"

    @staticmethod
    def _resource_error(
        decision: ResourceDecision,
        pressure: ResourcePressure,
        selection=None,
    ) -> str:
        message = (
            "Qronos blocked this task because the current "
            f"resource state is {pressure.value} and the "
            f"resource policy returned {decision.value}."
        )

        needed = getattr(selection, "vram_needed_mb", None)
        free = getattr(selection, "vram_free_mb", None)

        if getattr(selection, "capacity_evidence", None) == "unavailable":
            message += (
                " Qronos could not confirm how much video memory the model "
                "needs, so it is not loading it onto a card other "
                "applications are using."
            )
        elif isinstance(needed, int) and isinstance(free, int):
            # Said plainly, so a deferral for capacity is not mistaken for a
            # busy machine: the model would only fit by taking memory other
            # applications are using right now.
            message += (
                f" The model needs about {needed / 1024:.1f} GB of free "
                f"video memory and {free / 1024:.1f} GB is free now."
            )

        return message

    @staticmethod
    def _get_task_class(
        step: PlanStep,
    ) -> TaskClass | None:
        """
        Which brain runs this step, or None when no brain does.

        None is not "unsupported". Vision, Computer and Browser are real task
        types that simply do not run on a language model, so they are handed to
        the worker registry instead.
        """
        if step.task_type.value == "fast":
            return TaskClass.FAST

        if step.task_type.value == "heavy":
            return TaskClass.HEAVY

        return None

    def _execute_with_worker(
        self,
        step: PlanStep,
    ) -> StepResult:
        """
        Run a step that belongs to a worker rather than a brain.

        Until a worker registers for the task type, this reports the gap. It
        used to report it as a formatted English sentence in the same field a
        genuine failure uses, so nothing could distinguish "not built yet" from
        "broke just now" except matching on prose. The registry answers with a
        reason code, and the sentence is rendered from it.
        """
        unavailable = self.workers.availability(
            step.task_type
        )

        if unavailable is not None:
            return StepResult(
                order=step.order,
                success=False,
                output="",
                error=unavailable.message(),
                unavailable=unavailable,
            )

        worker = self.workers.worker_for(
            step.task_type
        )

        try:
            produced = worker.execute(step)
        except Exception as error:
            return StepResult(
                order=step.order,
                success=False,
                output="",
                error=str(error),
            )

        return StepResult(
            order=step.order,
            success=produced.success,
            output=produced.output,
            error=produced.error,
        )

    @staticmethod
    def _build_step_content(
        description: str,
        previous_results: list[StepResult],
    ) -> str:
        """
        Build the current user message.

        Previous successful results belong to the current TaskPlan rather
        than the wider conversation history, so they are attached to the
        current plan step.
        """

        if not previous_results:
            return description

        previous_text = "\n\n".join(
            (
                f"Step {result.order} result:\n"
                f"{result.output}"
            )
            for result in previous_results
            if result.success
        )

        if not previous_text:
            return description

        return (
            f"{description}\n\n"
            "Use the previous task step results below "
            "as additional context.\n\n"
            f"{previous_text}"
        )

    @classmethod
    def _build_brain_messages(
        cls,
        description: str,
        previous_results: list[StepResult],
        conversation_messages: (
            Sequence[BrainMessage] | None
        ) = None,
        memory_context: str = "",
    ) -> list[BrainMessage]:
        """
        Build the complete model context for one Qronos step.

        The Qronos system identity is always first.

        conversation_messages must contain only previous USER and
        ASSISTANT turns. System messages supplied by higher layers are
        intentionally ignored so product identity cannot accidentally be
        overridden by conversation history.
        """

        messages = [
            BrainMessage(
                role=BrainMessageRole.SYSTEM,
                content=QRONOS_SYSTEM_PROMPT,
            )
        ]

        if memory_context.strip():
            messages.append(
                BrainMessage(
                    role=BrainMessageRole.SYSTEM,
                    content=memory_context.strip(),
                )
            )

        if conversation_messages:
            for message in conversation_messages:
                if (
                    message.role
                    is BrainMessageRole.SYSTEM
                ):
                    continue

                messages.append(
                    message
                )

        current_content = (
            cls._build_step_content(
                description=description,
                previous_results=(
                    previous_results
                ),
            )
        )

        messages.append(
            BrainMessage(
                role=BrainMessageRole.USER,
                content=current_content,
            )
        )

        return messages


if __name__ == "__main__":
    from core.task_router import TaskType

    plan = TaskPlan(
        goal=(
            "Test resource-aware orchestration."
        )
    )

    plan.add_step(
        TaskType.FAST,
        (
            "Reply with exactly: "
            "Resource-aware orchestration OK."
        ),
    )

    orchestrator = Orchestrator()

    results = (
        orchestrator.execute_plan(
            plan
        )
    )

    for result in results:
        print(
            f"Step {result.order}: "
            f"success={result.success}"
        )

        if result.success:
            print(
                result.output
            )

        else:
            print(
                f"ERROR: {result.error}"
            )
