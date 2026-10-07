from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from core.action_audit import ActionAuditLog, AuditEvent
from core.action_cancellation import ActionCancelled, CancellationToken, checkpoint
from core.actions import ActionOutcome, ActionRequest, ActionResult
from core.computer_executor import (
    ComputerAction,
    ComputerExecutor,
    ComputerOperation,
    undo_unavailable_restoration,
)
from core.input_capabilities import InputCapabilities
from core.input_safety import BoundInputAuthorization
from core.model_manager import ModelManager
from core.paint_adapter import PaintAdapter, PaintCanvasProgress, PaintCanvasRegion
from core.paint_canvas_grounding import ground_paint_canvas
from core.paint_state import PaintStateGrounder, PaintStateNormalizer
from core.paint_verification import (
    PaintPostDrawVerification,
    PaintPostDrawVerifier,
    _crop_capture,
)
from core.reference_sketch_pipeline import ReferenceSketchPipeline
from core.screen_capture import Capture, ScreenCapture
from core.sketch_critic import SketchVisualCritic
from core.undo_journal import EntryState, NotUndoable, Reversal, UndoEntry, UndoJournal
from core.vision_image import PreparedImage
from core.window_binding import WindowBinding, WindowBindingStale, WindowRect
from security.gate import ActionGrant, Verdict, evaluate, issue_grant
from security.permissions import ActionCategory, PermissionDecision


MAX_DRAW_PROMPT_CHARS = 800
REFERENCE_PATH_ENV = "QRONOS_PAINT_REFERENCE_PATH"

#: Upper bound on Ctrl+Z presses one undo may send. A ShapePlan holds at most
#: 64 strokes; a partial stroke adds at most one more edit.
MAX_PAINT_UNDO_STEPS = 65

#: How many recent drawings keep in-memory canvas evidence for undo. Older
#: evidence is dropped and its journal entry is expired, never left claiming
#: a reversal that can no longer be verified.
MAX_PAINT_UNDO_EVIDENCE = 4

#: Per-channel tolerance when deciding two canvas frames show the same
#: pixels. Deliberately strict: any visible change since Qronos drew means the
#: top of Paint's undo history may be someone else's edit.
CANVAS_MATCH_TOLERANCE = 8


class PaintDrawError(RuntimeError):
    """A production Paint drawing request could not be completed safely."""


class PaintReferenceUnavailable(PaintDrawError):
    """No trusted local reference-image provider is connected."""


class PaintReferenceProvider(Protocol):
    """Produce one local line-art reference for a natural-language request."""

    def reference_for(self, request: str) -> str | Path | PreparedImage: ...


class EnvironmentPaintReferenceProvider:
    """
    Transitional local provider used until Qronos's image generator is wired.

    The provider never guesses, downloads, or shells out. The user/developer may
    point ``QRONOS_PAINT_REFERENCE_PATH`` at one explicit local image. The normal
    pre-draw visual gate still compares that image with the actual request, so a
    stale/mismatched reference is rejected before Paint receives mouse input.
    """

    _ALLOWED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})

    def __init__(self, env_name: str = REFERENCE_PATH_ENV) -> None:
        self.env_name = str(env_name).strip() or REFERENCE_PATH_ENV

    def reference_for(self, request: str) -> Path:
        _validated_prompt(request)
        raw = os.environ.get(self.env_name, "").strip()
        if not raw:
            raise PaintReferenceUnavailable(
                "Qronos Paint drawing is wired, but no image-generator/reference "
                f"provider is connected. Set {self.env_name} to one trusted local "
                "line-art image for integration testing, or connect the real local "
                "image generator through PaintReferenceProvider."
            )

        path = Path(raw).expanduser().resolve(strict=False)
        if not path.is_file():
            raise PaintReferenceUnavailable(
                f"Configured Paint reference does not exist: {path}"
            )
        if path.suffix.lower() not in self._ALLOWED_SUFFIXES:
            raise PaintReferenceUnavailable(
                "Configured Paint reference must be PNG, JPEG, WEBP, or BMP."
            )
        return path


@dataclass(frozen=True)
class PaintDrawAction:
    prompt: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "prompt", _validated_prompt(self.prompt))

    @property
    def app_id(self) -> str:
        return "paint"

    def gui_request(self) -> ActionRequest:
        return ActionRequest(
            category=ActionCategory.GUI_AUTOMATION,
            target="app:paint",
            summary=(
                "Open Microsoft Paint and draw one bounded, visually approved "
                "reference sketch using verified mouse input."
            ),
            parameters={
                "operation": "draw_in_paint",
                "appId": "paint",
                "description": self.prompt,
            },
        )

    def screen_request(self) -> ActionRequest:
        return ActionRequest(
            category=ActionCategory.READ_SCREEN,
            target="app:paint",
            summary=(
                "Observe only the verified Paint window immediately before and "
                "after drawing so Qronos can verify its own GUI side effect."
            ),
            parameters={
                "operation": "verify_paint_drawing",
                "appId": "paint",
            },
        )


@dataclass(frozen=True)
class PreparedPaintDrawAction:
    action: PaintDrawAction
    gui_request: ActionRequest
    gui_verdict: Verdict
    screen_request: ActionRequest
    screen_verdict: Verdict


@dataclass(frozen=True)
class PaintDrawRun:
    verification: PaintPostDrawVerification
    stroke_count: int
    point_count: int
    pid: int
    hwnd: int


@dataclass(frozen=True)
class PaintDrawSideEffects:
    """What one drawing attempt actually changed, whatever its outcome."""

    paint_activation_requested: bool = False
    toolbar_clicks: int = 0
    strokes_planned: int = 0
    strokes_completed: int = 0
    canvas_marks_committed: int = 0
    canvas_marks_unproven: int = 0

    @property
    def touched_canvas(self) -> bool:
        return self.canvas_marks_committed > 0 or self.canvas_marks_unproven > 0

    @property
    def any(self) -> bool:
        return (
            self.paint_activation_requested
            or self.toolbar_clicks > 0
            or self.touched_canvas
        )


@dataclass(frozen=True)
class PaintDrawExecution:
    request: ActionRequest
    result: ActionResult
    run: PaintDrawRun | None = None
    side_effects: PaintDrawSideEffects = PaintDrawSideEffects()
    undo_entry: UndoEntry | None = None

    @property
    def undo_available(self) -> bool:
        """True only when an exact, evidence-checked reversal is on record."""
        return (
            self.undo_entry is not None
            and self.undo_entry.reversal is Reversal.APP_UNDO_STEPS
            and self.undo_entry.undoable
        )


@dataclass(frozen=True)
class PreparedPaintUndo:
    """Gate decisions for reversing one earlier Qronos drawing."""

    undoes_action_id: str
    gui_request: ActionRequest
    gui_verdict: Verdict
    screen_request: ActionRequest
    screen_verdict: Verdict


@dataclass(frozen=True)
class PaintUndoExecution:
    request: ActionRequest
    result: ActionResult
    undoes_action_id: str
    steps_sent: int = 0
    restored: bool = False


@dataclass(frozen=True)
class _PaintUndoEvidence:
    """
    Process-local proof needed to reverse one drawing without touching
    anything else. Pixels never leave memory; the journal holds only counts.
    """

    pid: int
    hwnd: int
    identity_token: str
    layout_token: str
    canvas_rect: WindowRect
    steps: int
    before_canvas: object  # numpy uint8 RGB array
    after_canvas: object


class _PaintRunState:
    """Mutable progress of one drawing attempt, filled in by ``_perform``."""

    def __init__(self) -> None:
        self.computer_effects: list[str] = []
        self.normalizer: PaintStateNormalizer | None = None
        self.canvas_progress = PaintCanvasProgress()
        self.binding: WindowBinding | None = None
        self.canvas: PaintCanvasRegion | None = None
        self.capture: ScreenCapture | None = None
        self.before: Capture | None = None
        self.after: Capture | None = None

    def side_effects(self) -> PaintDrawSideEffects:
        progress = self.canvas_progress
        return PaintDrawSideEffects(
            paint_activation_requested=bool(self.computer_effects),
            toolbar_clicks=(
                self.normalizer.toolbar_clicks_attempted
                if self.normalizer is not None
                else 0
            ),
            strokes_planned=progress.planned_strokes,
            strokes_completed=progress.completed_strokes,
            canvas_marks_committed=progress.committed_marks,
            canvas_marks_unproven=progress.unproven_marks,
        )


EvaluateFn = Callable[..., Verdict]
ModelManagerFactory = Callable[[], ModelManager]
ScreenCaptureFactory = Callable[[], ScreenCapture]
InputsFactory = Callable[..., InputCapabilities]
SleepFn = Callable[[float], None]


class PaintDrawExecutor:
    """
    Production composite for ``request -> reference -> Paint -> verify``.

    Two independent permission decisions are required:
    - GUI_AUTOMATION for the pointer side effect;
    - READ_SCREEN for the bounded before/after Paint observation.

    The caller must satisfy both exact decisions. This makes it impossible for a
    voice-confirmed GUI action to silently claim the stronger UI screen consent.
    """

    def __init__(
        self,
        computer_executor: ComputerExecutor,
        *,
        reference_provider: PaintReferenceProvider | None = None,
        audit: ActionAuditLog | None = None,
        journal: UndoJournal | None = None,
        evaluate_fn: EvaluateFn = evaluate,
        model_manager_factory: ModelManagerFactory = ModelManager,
        screen_capture_factory: ScreenCaptureFactory = ScreenCapture,
        inputs_factory: InputsFactory = InputCapabilities.for_binder,
        sleep_fn: SleepFn = time.sleep,
        undo_step_settle_seconds: float = 0.15,
    ) -> None:
        self.computer_executor = computer_executor
        self.reference_provider = (
            reference_provider
            if reference_provider is not None
            else EnvironmentPaintReferenceProvider()
        )
        self.audit = audit
        self.journal = journal
        self._evaluate = evaluate_fn
        self._model_manager_factory = model_manager_factory
        self._screen_capture_factory = screen_capture_factory
        self._inputs_factory = inputs_factory
        self._sleep = sleep_fn
        self._undo_step_settle_seconds = max(0.0, float(undo_step_settle_seconds))
        # action_id -> evidence, oldest first. Process-local by design.
        self._undo_evidence: dict[str, _PaintUndoEvidence] = {}

    def _pause(self, seconds: float, cancel_token: CancellationToken | None) -> None:
        checkpoint(cancel_token)
        if cancel_token is not None and self._sleep is time.sleep:
            cancel_token.sleep(seconds)
            return
        self._sleep(seconds)
        checkpoint(cancel_token)

    def prepare(self, action: PaintDrawAction) -> PreparedPaintDrawAction:
        if not isinstance(action, PaintDrawAction):
            raise TypeError("PaintDrawExecutor.prepare requires PaintDrawAction.")
        gui_request = action.gui_request()
        screen_request = action.screen_request()
        return PreparedPaintDrawAction(
            action=action,
            gui_request=gui_request,
            gui_verdict=self._evaluate_request(gui_request),
            screen_request=screen_request,
            screen_verdict=self._evaluate_request(screen_request),
        )

    def execute_prepared(
        self,
        prepared: PreparedPaintDrawAction,
        *,
        gui_approval: PermissionDecision | None = None,
        screen_approval: PermissionDecision | None = None,
        reevaluate: bool = True,
        cancel_token: CancellationToken | None = None,
    ) -> PaintDrawExecution:
        if not isinstance(prepared, PreparedPaintDrawAction):
            raise TypeError(
                "PaintDrawExecutor.execute_prepared requires PreparedPaintDrawAction."
            )

        gui_verdict = (
            self._evaluate_request(prepared.gui_request)
            if reevaluate
            else prepared.gui_verdict
        )
        screen_verdict = (
            self._evaluate_request(prepared.screen_request)
            if reevaluate
            else prepared.screen_verdict
        )

        blocked = self._blocked_result(
            prepared.gui_request,
            gui_verdict,
            gui_approval,
            label="GUI automation",
        )
        if blocked is not None:
            return PaintDrawExecution(prepared.gui_request, blocked)

        blocked = self._blocked_result(
            prepared.screen_request,
            screen_verdict,
            screen_approval,
            label="Paint screen verification",
        )
        if blocked is not None:
            return PaintDrawExecution(prepared.gui_request, blocked)

        self._record_approval(prepared.gui_request, gui_verdict, gui_approval)
        self._record_approval(
            prepared.screen_request,
            screen_verdict,
            screen_approval,
        )

        gui_grant = issue_grant(
            prepared.gui_request,
            gui_verdict,
            approval=gui_approval,
            ttl_seconds=180.0,
        )

        run: PaintDrawRun | None = None
        state = _PaintRunState()
        try:
            run = self._perform(
                prepared.action,
                gui_grant,
                state=state,
                cancel_token=cancel_token,
            )
            detail = (
                "Paint drawing completed and post-draw verification passed: "
                + run.verification.summary()
            )
            result = ActionResult(
                action_id=prepared.gui_request.action_id,
                outcome=ActionOutcome.SUCCEEDED,
                detail=detail,
            )
        except ActionCancelled as exc:
            result = ActionResult(
                action_id=prepared.gui_request.action_id,
                outcome=ActionOutcome.CANCELLED,
                detail=(
                    f"Paint drawing cancelled ({exc.reason}); "
                    + _describe_side_effects(state.side_effects())
                ),
            )
        except Exception as exc:
            result = ActionResult(
                action_id=prepared.gui_request.action_id,
                outcome=ActionOutcome.FAILED,
                detail=(
                    f"{type(exc).__name__}: {exc}; "
                    + _describe_side_effects(state.side_effects())
                ),
            )

        side_effects = state.side_effects()
        undo_entry = self._record_undo(prepared.gui_request, state, side_effects)

        if self.audit is not None:
            self.audit.record_result(prepared.gui_request, result)
            screen_result = ActionResult(
                action_id=prepared.screen_request.action_id,
                outcome=result.outcome,
                detail=(
                    "Bounded Paint observation completed."
                    if result.outcome is ActionOutcome.SUCCEEDED
                    else "Paint observation/drawing composite did not complete."
                ),
            )
            self.audit.record_result(prepared.screen_request, screen_result)

        return PaintDrawExecution(
            prepared.gui_request,
            result,
            run,
            side_effects=side_effects,
            undo_entry=undo_entry,
        )

    def decline_screen(self, prepared: PreparedPaintDrawAction) -> None:
        if self.audit is None:
            return
        self.audit.record(
            AuditEvent.DENIED_BY_USER,
            prepared.screen_request,
            decision=prepared.screen_verdict.decision,
            outcome=ActionOutcome.REFUSED,
        )
        self.audit.record(
            AuditEvent.COMPLETED,
            prepared.gui_request,
            outcome=ActionOutcome.REFUSED,
        )

    def discard_pending(self, prepared: PreparedPaintDrawAction, *, reason: str) -> None:
        """
        Audit a prepared drawing that Stop/Pause/shutdown discarded while it
        was still waiting for approval. Nothing ran; the approval it was
        waiting for can no longer be used.
        """
        if self.audit is None:
            return
        for request in (prepared.screen_request, prepared.gui_request):
            self.audit.record_result(
                request,
                ActionResult(
                    action_id=request.action_id,
                    outcome=ActionOutcome.CANCELLED,
                    detail=f"Discarded before approval ({reason}); nothing ran.",
                ),
            )

    def _evaluate_request(self, request: ActionRequest) -> Verdict:
        return self._evaluate(
            request,
            audit=(self.audit.record_verdict if self.audit is not None else None),
        )

    @staticmethod
    def _blocked_result(
        request: ActionRequest,
        verdict: Verdict,
        approval: PermissionDecision | None,
        *,
        label: str,
    ) -> ActionResult | None:
        if verdict.refused:
            return ActionResult(
                action_id=request.action_id,
                outcome=ActionOutcome.REFUSED,
                detail=verdict.reason,
            )
        if verdict.needs_approval and approval is not verdict.decision:
            return ActionResult(
                action_id=request.action_id,
                outcome=ActionOutcome.AWAITING_APPROVAL,
                detail=f"{label} requires {verdict.decision.value}.",
            )
        return None

    def _record_approval(
        self,
        request: ActionRequest,
        verdict: Verdict,
        approval: PermissionDecision | None,
    ) -> None:
        if (
            self.audit is not None
            and verdict.needs_approval
            and approval is verdict.decision
        ):
            self.audit.record(
                AuditEvent.APPROVED,
                request,
                decision=verdict.decision,
            )

    def _perform(
        self,
        action: PaintDrawAction,
        gui_grant: ActionGrant,
        *,
        state: _PaintRunState | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> PaintDrawRun:
        """
        Run the composite. Cancellation is checked between every native step;
        model calls cannot be interrupted, so their results are discarded at
        the next checkpoint. ``state`` records each side effect as it happens.
        """
        state = state if state is not None else _PaintRunState()
        checkpoint(cancel_token)
        reference = self.reference_provider.reference_for(action.prompt)

        manager = self._model_manager_factory()
        if not manager.ensure_runtime_ready():
            raise PaintDrawError(
                "Local Qronos brain runtime/Ollama could not be reached or "
                "started automatically."
            )
        checkpoint(cancel_token)

        approved_sketch, _diagnostics = ReferenceSketchPipeline(
            manager.runtime
        ).plan(action.prompt, reference)
        # Planning can take a long time; Stop during it must still prevent
        # every Windows side effect.
        checkpoint(cancel_token)

        # OPEN_APP is a substep of the already-authorized composite action, not
        # a second user request. Use the exact-PID launch/binding path that has
        # already passed live validation.
        _detail, binding = self.computer_executor._perform_with_context(
            ComputerAction(ComputerOperation.OPEN_APP, app_id="paint"),
            cancel_token=cancel_token,
            effects=state.computer_effects,
        )
        if binding is None:
            raise PaintDrawError("Paint launch returned no verified window binding.")

        self.computer_executor.acquire_foreground(binding, cancel_token=cancel_token)

        fresh = self.computer_executor.window_binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=False,
        )
        prime_x = (fresh.client_rect.left + fresh.client_rect.right) // 2
        prime_y = (fresh.client_rect.top + fresh.client_rect.bottom) // 2
        inputs = self._inputs_factory(self.computer_executor.window_binder)
        input_authorization = BoundInputAuthorization.bind(gui_grant, binding)
        checkpoint(cancel_token)
        try:
            inputs.hover.hover(
                binding,
                prime_x,
                prime_y,
                authorization=input_authorization,
            )
        except WindowBindingStale:
            # Moving into Paint can make its chrome settle. Coordinates are not
            # used after this point until a fresh exact-PID binding is created.
            pass

        self._pause(0.8, cancel_token)
        binding = self.computer_executor.bind_app_window(
            "paint",
            require_foreground=True,
            expected_pid=binding.snapshot.pid,
        )
        input_authorization = BoundInputAuthorization.bind(gui_grant, binding)

        # Paint persists its last drawing tool, colour, and width across runs.
        # Pointer geometry can therefore be perfect while a stale Brush/Marker
        # state produces a visibly scribbled result. Normalize toolbar state
        # *before* the before-capture and before any canvas side effect. Every
        # toolbar coordinate is vision-grounded to this exact HWND/layout.
        capture = self._screen_capture_factory()
        state.capture = capture
        ui_grounder = PaintStateGrounder(manager.runtime)
        try:
            normalizer = PaintStateNormalizer(
                self.computer_executor.window_binder,
                capture,
                ui_grounder,
                inputs=inputs,
                sleep_fn=self._sleep,
                close_grounder=False,
            )
            state.normalizer = normalizer
            normalizer.normalize(
                binding,
                authorization=input_authorization,
                screen_approved=True,
                cancel_token=cancel_token,
            )

            # A toolbar interaction must not silently change the window geometry.
            # Refresh the exact-PID binding after normalization so the canvas
            # capability and before/after captures are issued from settled state.
            binding = self.computer_executor.bind_app_window(
                "paint",
                require_foreground=True,
                expected_pid=binding.snapshot.pid,
            )
            input_authorization = BoundInputAuthorization.bind(gui_grant, binding)
            canvas = ground_paint_canvas(
                binding,
                self.computer_executor.window_binder,
                capture,
                ui_grounder,
                screen_approved=True,
            )
        finally:
            ui_grounder.close()

        checkpoint(cancel_token)
        before = capture.capture_bound(
            binding,
            self.computer_executor.window_binder,
            approved=True,
            require_foreground=True,
            reason=(
                "Observe only the verified Paint window immediately before "
                "the approved drawing."
            ),
        )
        state.binding = binding
        state.canvas = canvas
        state.before = before

        draw = PaintAdapter(
            self.computer_executor.window_binder,
            inputs=inputs,
        ).execute_approved(
            binding,
            approved_sketch,
            canvas,
            authorization=input_authorization.restrict(canvas.rect),
            step_delay_seconds=0.006,
            inset_pixels=12,
            cancel_token=cancel_token,
            progress=state.canvas_progress,
        )

        # The drawing is complete. From here on only observation remains, so
        # a late Stop no longer changes the outcome of the side effect.
        self._sleep(0.5)
        after = capture.capture_bound(
            binding,
            self.computer_executor.window_binder,
            approved=True,
            require_foreground=True,
            reason=(
                "Observe only the verified Paint window immediately after "
                "the approved drawing to verify Qronos's own side effect."
            ),
        )
        state.after = after

        verification = PaintPostDrawVerifier(
            SketchVisualCritic(manager.runtime)
        ).verify(
            binding,
            approved_sketch,
            canvas,
            before,
            after,
            inset_pixels=12,
        )
        verification.require_pass()

        return PaintDrawRun(
            verification=verification,
            stroke_count=len(draw.strokes),
            point_count=sum(len(stroke.points) for stroke in draw.strokes),
            pid=binding.snapshot.pid,
            hwnd=binding.hwnd,
        )

    # ------------------------------------------------------------------ undo

    def _record_undo(
        self,
        request: ActionRequest,
        state: _PaintRunState,
        side_effects: PaintDrawSideEffects,
    ) -> UndoEntry | None:
        """
        Journal what this attempt changed, promising only what can be proven.

        APP_UNDO_STEPS is recorded only when the exact number of Paint edits
        is certain and before/after canvas evidence exists to prove, at undo
        time, that nothing else was stacked on top. Everything else that
        changed is recorded as irreversible with the reason.
        """
        if self.journal is None or not side_effects.any:
            return None

        reason = ""
        evidence: _PaintUndoEvidence | None = None
        if not side_effects.touched_canvas:
            reason = (
                "paint_tool_state_not_restorable"
                if side_effects.toolbar_clicks
                else "closing_app_could_discard_user_state"
            )
        elif side_effects.canvas_marks_unproven:
            reason = "paint_edit_count_unproven"
        elif side_effects.canvas_marks_committed > MAX_PAINT_UNDO_STEPS:
            reason = "paint_edit_count_exceeds_undo_bound"
        else:
            evidence = self._build_undo_evidence(state, side_effects)
            if evidence is None:
                reason = "paint_post_action_evidence_unavailable"

        if evidence is None:
            return self.journal.record(
                request,
                Reversal.IRREVERSIBLE,
                undo_unavailable_restoration(reason),
            )

        entry = self.journal.record(
            request,
            Reversal.APP_UNDO_STEPS,
            {
                "app": "paint",
                "pid": evidence.pid,
                "hwnd": evidence.hwnd,
                "steps": evidence.steps,
                "evidence": "process_local_canvas_frames",
                # Undo replays only canvas edits; toolbar selection is not
                # part of Paint's edit history and is not restored.
                "toolStateRestored": False,
            },
        )
        self._remember_evidence(request.action_id, evidence)
        return entry

    def _build_undo_evidence(
        self,
        state: _PaintRunState,
        side_effects: PaintDrawSideEffects,
    ) -> _PaintUndoEvidence | None:
        binding = state.binding
        canvas = state.canvas
        if binding is None or canvas is None or state.before is None:
            return None
        after = state.after
        if after is None and state.capture is not None:
            # Interrupted after a clean release: take one passive frame of
            # the already-approved window so a later undo can prove nothing
            # else changed. No input is sent; failure just means no undo.
            try:
                after = state.capture.capture_bound(
                    binding,
                    self.computer_executor.window_binder,
                    approved=True,
                    require_foreground=True,
                    reason=(
                        "Record the verified Paint canvas after an interrupted "
                        "drawing so Qronos can later reverse only its own edits."
                    ),
                )
            except Exception:
                return None
        if after is None:
            return None
        try:
            before_pixels = _canvas_pixels(state.before, canvas.rect)
            after_pixels = _canvas_pixels(after, canvas.rect)
        except Exception:
            return None
        return _PaintUndoEvidence(
            pid=binding.snapshot.pid,
            hwnd=binding.hwnd,
            identity_token=binding.identity_token,
            layout_token=binding.layout_token,
            canvas_rect=canvas.rect,
            steps=side_effects.canvas_marks_committed,
            before_canvas=before_pixels,
            after_canvas=after_pixels,
        )

    def _remember_evidence(self, action_id: str, evidence: _PaintUndoEvidence) -> None:
        self._undo_evidence[action_id] = evidence
        while len(self._undo_evidence) > MAX_PAINT_UNDO_EVIDENCE:
            oldest = next(iter(self._undo_evidence))
            self._forget_evidence(oldest, expire=True)

    def _forget_evidence(self, action_id: str, *, expire: bool) -> None:
        self._undo_evidence.pop(action_id, None)
        if not expire or self.journal is None:
            return
        try:
            entry = self.journal.claim(action_id)
        except NotUndoable:
            return
        # Without evidence the entry can no longer be verified; it must not
        # keep advertising a reversal.
        self.journal.mark(entry.action_id, EntryState.EXPIRED)

    def prepare_undo(self, action_id: str) -> PreparedPaintUndo:
        """Evaluate the gate for reversing one earlier Qronos drawing."""
        original = str(action_id or "").strip()
        if not original:
            raise ValueError("prepare_undo requires the drawing's action id.")
        gui_request = ActionRequest(
            category=ActionCategory.GUI_AUTOMATION,
            target="app:paint",
            summary=(
                "Undo only the edits Qronos itself made in Microsoft Paint, "
                "after proving nothing else changed since."
            ),
            parameters={
                "operation": "undo_paint_drawing",
                "appId": "paint",
                "undoesActionId": original,
            },
        )
        screen_request = ActionRequest(
            category=ActionCategory.READ_SCREEN,
            target="app:paint",
            summary=(
                "Observe only the verified Paint canvas before and after undo "
                "to prove Qronos reverses its own edits and nothing else."
            ),
            parameters={
                "operation": "verify_paint_undo",
                "appId": "paint",
                "undoesActionId": original,
            },
        )
        return PreparedPaintUndo(
            undoes_action_id=original,
            gui_request=gui_request,
            gui_verdict=self._evaluate_request(gui_request),
            screen_request=screen_request,
            screen_verdict=self._evaluate_request(screen_request),
        )

    def execute_undo(
        self,
        prepared: PreparedPaintUndo,
        *,
        gui_approval: PermissionDecision | None = None,
        screen_approval: PermissionDecision | None = None,
        reevaluate: bool = True,
        cancel_token: CancellationToken | None = None,
    ) -> PaintUndoExecution:
        """
        Reverse exactly one earlier drawing, or refuse without input.

        Refusal (no keystroke sent) when: the gate does not allow it; the
        entry is not a pending APP_UNDO_STEPS entry; a later Qronos action
        touched Paint or holds a newer reversible change; the evidence is
        gone; the exact Paint process/window/layout is not the one drawn in;
        or the canvas differs from how Qronos left it (someone else's edit
        would be on top of Paint's undo history).

        Once the first Ctrl+Z is sent, any interruption marks the entry FAILED
        (terminal; never retried blind) and reports how many steps were sent.
        """
        if not isinstance(prepared, PreparedPaintUndo):
            raise TypeError("execute_undo requires PreparedPaintUndo.")
        original = prepared.undoes_action_id
        gui_verdict = (
            self._evaluate_request(prepared.gui_request)
            if reevaluate
            else prepared.gui_verdict
        )
        screen_verdict = (
            self._evaluate_request(prepared.screen_request)
            if reevaluate
            else prepared.screen_verdict
        )
        for request, verdict, approval, label in (
            (prepared.gui_request, gui_verdict, gui_approval, "Paint undo input"),
            (prepared.screen_request, screen_verdict, screen_approval, "Paint undo verification"),
        ):
            blocked = self._blocked_result(request, verdict, approval, label=label)
            if blocked is not None:
                return self._finish_undo(prepared, blocked)

        try:
            evidence = self._undo_preconditions(original)
        except NotUndoable as exc:
            return self._finish_undo(
                prepared,
                ActionResult(
                    action_id=prepared.gui_request.action_id,
                    outcome=ActionOutcome.REFUSED,
                    detail=f"Undo unavailable: {exc}",
                ),
            )

        self._record_approval(prepared.gui_request, gui_verdict, gui_approval)
        self._record_approval(prepared.screen_request, screen_verdict, screen_approval)
        gui_grant = issue_grant(
            prepared.gui_request,
            gui_verdict,
            approval=gui_approval,
            ttl_seconds=60.0,
        )

        binder = self.computer_executor.window_binder
        steps_sent = 0
        try:
            checkpoint(cancel_token)
            binding = self._rebind_exact_window(evidence)
            self.computer_executor.acquire_foreground(binding, cancel_token=cancel_token)
            binder.revalidate(binding, require_foreground=True, require_same_layout=True)
            capture = self._screen_capture_factory()
            current = self._canvas_now(capture, binding, evidence)
            if not _canvas_matches(current, evidence.after_canvas):
                raise NotUndoable(
                    "the Paint canvas changed since Qronos drew; the newest "
                    "Paint edit may not be Qronos's, so nothing was undone"
                )

            keyboard = self._inputs_factory(binder).keyboard
            authorization = BoundInputAuthorization.bind(gui_grant, binding)
            restored = _canvas_matches(current, evidence.before_canvas)
            while not restored and steps_sent < evidence.steps:
                checkpoint(cancel_token)
                keyboard.press_chord(
                    binding,
                    ("ctrl", "z"),
                    authorization=authorization,
                )
                steps_sent += 1
                if self._undo_step_settle_seconds:
                    self._pause(self._undo_step_settle_seconds, cancel_token)
                # Stop as soon as the canvas is back to the pre-draw state, so
                # a miscounted edit can never reach the user's older work.
                current = self._canvas_now(capture, binding, evidence)
                restored = _canvas_matches(current, evidence.before_canvas)
        except NotUndoable as exc:
            outcome = ActionOutcome.REFUSED if steps_sent == 0 else ActionOutcome.FAILED
            return self._conclude_undo(prepared, evidence, steps_sent, False, outcome, str(exc))
        except ActionCancelled as exc:
            return self._conclude_undo(
                prepared,
                evidence,
                steps_sent,
                False,
                ActionOutcome.CANCELLED,
                f"cancelled ({exc.reason})",
            )
        except Exception as exc:
            return self._conclude_undo(
                prepared,
                evidence,
                steps_sent,
                False,
                ActionOutcome.FAILED,
                f"{type(exc).__name__}: {exc}",
            )

        if restored:
            return self._conclude_undo(
                prepared,
                evidence,
                steps_sent,
                True,
                ActionOutcome.SUCCEEDED,
                "canvas verified identical to its state before the drawing",
            )
        return self._conclude_undo(
            prepared,
            evidence,
            steps_sent,
            False,
            ActionOutcome.FAILED,
            "canvas does not match its pre-drawing state after the recorded "
            "number of undo steps",
        )

    def _undo_preconditions(self, action_id: str) -> _PaintUndoEvidence:
        if self.journal is None:
            raise NotUndoable("no undo journal is connected")
        entry = self.journal.claim(action_id)
        if entry.reversal is not Reversal.APP_UNDO_STEPS:
            raise NotUndoable(f"{entry.summary} has no exact app-level reversal")
        for later in self.journal.entries_after(action_id):
            if later.target == entry.target or later.undoable:
                raise NotUndoable(
                    "a later Qronos action touched Paint or holds a newer "
                    "reversible change; undo that first"
                )
        evidence = self._undo_evidence.get(action_id)
        if evidence is None:
            raise NotUndoable("the canvas evidence for this drawing is no longer held")
        if not 0 < evidence.steps <= MAX_PAINT_UNDO_STEPS:
            raise NotUndoable("the recorded edit count is outside the safe bound")
        return evidence

    def _rebind_exact_window(self, evidence: _PaintUndoEvidence) -> WindowBinding:
        try:
            binding = self.computer_executor.bind_app_window(
                "paint",
                require_foreground=False,
                expected_pid=evidence.pid,
            )
        except Exception as exc:
            raise NotUndoable(
                f"the exact Paint window Qronos drew in is not available ({type(exc).__name__})"
            ) from exc
        if (
            binding.hwnd != evidence.hwnd
            or binding.identity_token != evidence.identity_token
        ):
            raise NotUndoable("the Paint window/process is not the one Qronos drew in")
        if binding.layout_token != evidence.layout_token:
            raise NotUndoable(
                "the Paint window layout changed, so the recorded canvas "
                "evidence no longer lines up"
            )
        return binding

    def _canvas_now(
        self,
        capture: ScreenCapture,
        binding: WindowBinding,
        evidence: _PaintUndoEvidence,
    ):
        frame = capture.capture_bound(
            binding,
            self.computer_executor.window_binder,
            approved=True,
            require_foreground=True,
            reason=(
                "Observe only the verified Paint canvas to prove an undo "
                "reverses Qronos's own edits and nothing else."
            ),
        )
        if (
            frame.window != evidence.hwnd
            or frame.binding_token != evidence.identity_token
            or frame.layout_token != evidence.layout_token
        ):
            raise NotUndoable("the observed frame does not belong to the exact Paint window")
        return _canvas_pixels(frame, evidence.canvas_rect)

    def _conclude_undo(
        self,
        prepared: PreparedPaintUndo,
        evidence: _PaintUndoEvidence,
        steps_sent: int,
        restored: bool,
        outcome: ActionOutcome,
        why: str,
    ) -> PaintUndoExecution:
        original = prepared.undoes_action_id
        if steps_sent > 0 or restored:
            # The document changed (or is verified back): the entry is settled
            # either way, and a half-done undo is never replayed blind.
            final_state = EntryState.UNDONE if restored else EntryState.FAILED
            if self.journal is not None:
                self.journal.mark(original, final_state)
            self._undo_evidence.pop(original, None)
            if restored and self.audit is not None:
                self.audit.record(
                    AuditEvent.UNDONE,
                    prepared.gui_request,
                    outcome=ActionOutcome.SUCCEEDED,
                )
        detail = (
            f"Paint undo {outcome.value}: {why}; "
            f"{steps_sent} of {evidence.steps} undo step(s) sent."
        )
        if steps_sent and not restored:
            detail += (
                " The document is partly reverted; Qronos will not retry. "
                "Paint's own Redo can re-apply the reverted steps."
            )
        return self._finish_undo(
            prepared,
            ActionResult(
                action_id=prepared.gui_request.action_id,
                outcome=outcome,
                detail=detail,
            ),
            steps_sent=steps_sent,
            restored=restored,
        )

    def _finish_undo(
        self,
        prepared: PreparedPaintUndo,
        result: ActionResult,
        *,
        steps_sent: int = 0,
        restored: bool = False,
    ) -> PaintUndoExecution:
        if self.audit is not None:
            self.audit.record_result(prepared.gui_request, result)
        return PaintUndoExecution(
            request=prepared.gui_request,
            result=result,
            undoes_action_id=prepared.undoes_action_id,
            steps_sent=steps_sent,
            restored=restored,
        )


def _describe_side_effects(effects: PaintDrawSideEffects) -> str:
    if not effects.any:
        return "no Windows side effect occurred."
    parts = []
    if effects.paint_activation_requested:
        parts.append("Paint was opened/activated")
    if effects.toolbar_clicks:
        parts.append(f"{effects.toolbar_clicks} toolbar click(s) changed tool state")
    if effects.touched_canvas:
        parts.append(
            f"{effects.strokes_completed} of {effects.strokes_planned} stroke(s) "
            f"completed, {effects.canvas_marks_committed} canvas edit(s) committed"
        )
        if effects.canvas_marks_unproven:
            parts.append(
                f"{effects.canvas_marks_unproven} interrupted stroke(s) released "
                "after the window changed"
            )
    return "; ".join(parts) + "."


def _canvas_pixels(capture, rect: WindowRect):
    import numpy as np

    image, _box = _crop_capture(capture, rect)
    return np.asarray(image.convert("RGB"), dtype=np.uint8)


def _canvas_matches(observed, expected) -> bool:
    import numpy as np

    if observed is None or expected is None:
        return False
    if getattr(observed, "shape", None) != getattr(expected, "shape", None):
        return False
    difference = np.abs(observed.astype(np.int16) - expected.astype(np.int16))
    return bool(difference.max(initial=0) <= CANVAS_MATCH_TOLERANCE)


def _validated_prompt(value: str) -> str:
    prompt = " ".join(str(value or "").split()).strip()
    if not prompt:
        raise ValueError("Paint drawing prompt cannot be empty.")
    if len(prompt) > MAX_DRAW_PROMPT_CHARS:
        raise ValueError(
            f"Paint drawing prompt exceeds {MAX_DRAW_PROMPT_CHARS} characters."
        )
    if any(ord(char) < 32 and char not in "\t\n\r" for char in prompt):
        raise ValueError("Paint drawing prompt contains control characters.")
    return prompt
