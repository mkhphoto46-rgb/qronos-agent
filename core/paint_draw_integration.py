from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from core.action_audit import ActionAuditLog, AuditEvent
from core.actions import ActionOutcome, ActionRequest, ActionResult
from core.computer_executor import ComputerAction, ComputerExecutor, ComputerOperation
from core.input_capabilities import InputCapabilities
from core.model_manager import ModelManager
from core.paint_adapter import PaintAdapter, PaintCanvasRegion
from core.paint_state import PaintStateGrounder, PaintStateNormalizer
from core.paint_verification import PaintPostDrawVerification, PaintPostDrawVerifier
from core.reference_sketch_pipeline import ReferenceSketchPipeline
from core.screen_capture import ScreenCapture
from core.sketch_critic import SketchVisualCritic
from core.undo_journal import UndoJournal
from core.vision_image import PreparedImage
from core.window_binding import WindowBindingStale, WindowRect
from security.gate import Verdict, evaluate
from security.permissions import ActionCategory, PermissionDecision


MAX_DRAW_PROMPT_CHARS = 800
REFERENCE_PATH_ENV = "QRONOS_PAINT_REFERENCE_PATH"


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
class PaintDrawExecution:
    request: ActionRequest
    result: ActionResult
    run: PaintDrawRun | None = None


EvaluateFn = Callable[..., Verdict]
ModelManagerFactory = Callable[[], ModelManager]
ScreenCaptureFactory = Callable[[], ScreenCapture]
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
        sleep_fn: SleepFn = time.sleep,
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
        self._sleep = sleep_fn

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
            prepared.gui_request,
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

        run: PaintDrawRun | None = None
        try:
            run = self._perform(prepared.action)
            detail = (
                "Paint drawing completed and post-draw verification passed: "
                + run.verification.summary()
            )
            result = ActionResult(
                action_id=prepared.gui_request.action_id,
                outcome=ActionOutcome.SUCCEEDED,
                detail=detail,
            )
        except Exception as exc:
            result = ActionResult(
                action_id=prepared.gui_request.action_id,
                outcome=ActionOutcome.FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )

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

        return PaintDrawExecution(prepared.gui_request, result, run)

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

    def _perform(self, action: PaintDrawAction) -> PaintDrawRun:
        reference = self.reference_provider.reference_for(action.prompt)

        manager = self._model_manager_factory()
        if not manager.ensure_runtime_ready():
            raise PaintDrawError(
                "Local Qronos brain runtime/Ollama could not be reached or "
                "started automatically."
            )

        approved_sketch, _diagnostics = ReferenceSketchPipeline(
            manager.runtime
        ).plan(action.prompt, reference)

        # OPEN_APP is a substep of the already-authorized composite action, not
        # a second user request. Use the exact-PID launch/binding path that has
        # already passed live validation.
        _detail, binding = self.computer_executor._perform_with_context(
            ComputerAction(ComputerOperation.OPEN_APP, app_id="paint")
        )
        if binding is None:
            raise PaintDrawError("Paint launch returned no verified window binding.")

        self.computer_executor.acquire_foreground(binding)

        fresh = self.computer_executor.window_binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=False,
        )
        prime_x = (fresh.client_rect.left + fresh.client_rect.right) // 2
        prime_y = (fresh.client_rect.top + fresh.client_rect.bottom) // 2
        inputs = InputCapabilities.for_binder(self.computer_executor.window_binder)
        try:
            inputs.hover.hover(
                binding,
                prime_x,
                prime_y,
                approved=True,
            )
        except WindowBindingStale:
            # Moving into Paint can make its chrome settle. Coordinates are not
            # used after this point until a fresh exact-PID binding is created.
            pass

        self._sleep(0.8)
        binding = self.computer_executor.bind_app_window(
            "paint",
            require_foreground=True,
            expected_pid=binding.snapshot.pid,
        )

        # Paint persists its last drawing tool, colour, and width across runs.
        # Pointer geometry can therefore be perfect while a stale Brush/Marker
        # state produces a visibly scribbled result. Normalize toolbar state
        # *before* the before-capture and before any canvas side effect. Every
        # toolbar coordinate is vision-grounded to this exact HWND/layout.
        capture = self._screen_capture_factory()
        PaintStateNormalizer(
            self.computer_executor.window_binder,
            capture,
            PaintStateGrounder(manager.runtime),
            inputs=inputs,
            sleep_fn=self._sleep,
        ).normalize(
            binding,
            approved=True,
        )

        # A toolbar interaction must not silently change the window geometry.
        # Refresh the exact-PID binding after normalization so the canvas
        # capability and before/after captures are issued from settled state.
        binding = self.computer_executor.bind_app_window(
            "paint",
            require_foreground=True,
            expected_pid=binding.snapshot.pid,
        )
        canvas = PaintCanvasRegion.verified(
            binding,
            _safe_inner_square(binding.snapshot.client_rect),
        )

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

        draw = PaintAdapter(self.computer_executor.window_binder).execute_approved(
            binding,
            approved_sketch,
            canvas,
            approved=True,
            step_delay_seconds=0.006,
            inset_pixels=12,
        )

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


def _safe_inner_square(client: WindowRect) -> WindowRect:
    """Conservative, layout-relative Paint region used until UIA canvas lookup."""
    if not client.usable:
        raise PaintDrawError("Paint client rectangle is not usable.")

    left_inset = max(80, round(client.width * 0.165))
    right_inset = left_inset
    top_inset = max(120, round(client.height * 0.30))
    bottom_inset = max(90, round(client.height * 0.18))

    base = WindowRect(
        left=client.left + left_inset,
        top=client.top + top_inset,
        right=client.right - right_inset,
        bottom=client.bottom - bottom_inset,
    )
    if base.width < 300 or base.height < 200:
        raise PaintDrawError(
            "Paint client area is too small for the conservative drawing region."
        )

    side = min(base.width, base.height)
    center_x = (base.left + base.right) // 2
    center_y = (base.top + base.bottom) // 2
    half = side // 2
    square = WindowRect(
        left=center_x - half,
        top=center_y - half,
        right=center_x - half + side,
        bottom=center_y - half + side,
    )
    if not square.usable:
        raise PaintDrawError("Paint safe drawing region is not usable.")
    return square
