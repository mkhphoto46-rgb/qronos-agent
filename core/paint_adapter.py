from __future__ import annotations

from dataclasses import dataclass

from core.input_capabilities import InputCapabilities
from core.input_safety import BoundInputAuthorization
from core.pointer_stroke import PointerStrokeResult
from core.shape_planner import ShapePlan
from core.sketch_approval import VisualApprovedSketch
from core.sketch_preview import digest_shape_plan
from core.window_binding import WindowBinder, WindowBinding, WindowRect


class PaintAdapterError(RuntimeError):
    """Paint-specific execution preconditions were not satisfied."""


@dataclass(frozen=True)
class PaintCanvasRegion:
    """A short-lived canvas capability tied to one verified Paint layout."""

    rect: WindowRect
    hwnd: int
    identity_token: str
    layout_token: str

    @classmethod
    def verified(
        cls,
        binding: WindowBinding,
        rect: WindowRect,
    ) -> "PaintCanvasRegion":
        if binding.app_id != "paint":
            raise PaintAdapterError("A Paint canvas region requires a Paint binding.")
        if binding.snapshot.process_name != "mspaint.exe":
            raise PaintAdapterError("Paint binding does not belong to mspaint.exe.")
        client = binding.snapshot.client_rect
        if not rect.usable:
            raise PaintAdapterError("Paint canvas rectangle is not usable.")
        if not _rect_inside(client, rect):
            raise PaintAdapterError(
                "Paint canvas rectangle must be fully inside the verified client area."
            )
        return cls(
            rect=rect,
            hwnd=binding.hwnd,
            identity_token=binding.identity_token,
            layout_token=binding.layout_token,
        )

    def matches(self, binding: WindowBinding) -> bool:
        return (
            binding.hwnd == self.hwnd
            and binding.identity_token == self.identity_token
            and binding.layout_token == self.layout_token
        )


@dataclass(frozen=True)
class PaintDrawResult:
    plan_name: str
    canvas: PaintCanvasRegion
    strokes: tuple[PointerStrokeResult, ...]


class PaintAdapter:
    """
    Execute generic ShapePlans inside a caller-supplied, verified Paint canvas.

    Canvas discovery is deliberately NOT faked here. UI Automation or Vision may
    provide the real canvas rectangle later; this adapter only accepts a region
    tied to the same exact WindowBinding/layout and keeps all pointer input inside it.
    """

    def __init__(
        self,
        binder: WindowBinder,
        *,
        inputs: InputCapabilities | None = None,
    ) -> None:
        self._binder = binder
        self._inputs = inputs if inputs is not None else InputCapabilities.for_binder(binder)

    def execute_approved(
        self,
        binding: WindowBinding,
        approved_sketch: VisualApprovedSketch,
        canvas: PaintCanvasRegion,
        *,
        authorization: BoundInputAuthorization,
        step_delay_seconds: float = 0.006,
        inset_pixels: int = 0,
        max_step_pixels: float = 8.0,
        press_settle_seconds: float = 0.012,
        release_settle_seconds: float = 0.012,
    ) -> PaintDrawResult:
        """
        Production sketch path: only a visually approved frozen plan may reach Paint.

        The low-level ``execute`` method remains available for primitive tests and
        trusted callers, but agent sketch execution should use this gate.
        """
        if not isinstance(approved_sketch, VisualApprovedSketch):
            raise PaintAdapterError(
                "Paint sketch execution requires a VisualApprovedSketch capability."
            )
        if digest_shape_plan(approved_sketch.plan) != approved_sketch.plan_digest:
            raise PaintAdapterError(
                "VisualApprovedSketch plan digest does not match the frozen approved plan."
            )

        return self.execute(
            binding,
            approved_sketch.plan,
            canvas,
            authorization=authorization,
            step_delay_seconds=step_delay_seconds,
            inset_pixels=inset_pixels,
            max_step_pixels=max_step_pixels,
            press_settle_seconds=press_settle_seconds,
            release_settle_seconds=release_settle_seconds,
        )

    def execute(
        self,
        binding: WindowBinding,
        plan: ShapePlan,
        canvas: PaintCanvasRegion,
        *,
        authorization: BoundInputAuthorization,
        step_delay_seconds: float = 0.006,
        inset_pixels: int = 0,
        max_step_pixels: float = 8.0,
        press_settle_seconds: float = 0.012,
        release_settle_seconds: float = 0.012,
    ) -> PaintDrawResult:
        if binding.app_id != "paint" or binding.snapshot.process_name != "mspaint.exe":
            raise PaintAdapterError("PaintAdapter refuses a non-Paint binding.")
        if not canvas.matches(binding):
            raise PaintAdapterError(
                "Paint canvas capability does not match the current window binding/layout."
            )

        fresh = self._binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )
        if not _rect_inside(fresh.client_rect, canvas.rect):
            raise PaintAdapterError(
                "Paint canvas rectangle is no longer inside the verified client area."
            )

        screen_strokes = plan.to_screen_strokes(
            canvas.rect,
            inset_pixels=inset_pixels,
            preserve_aspect=True,
        )

        results: list[PointerStrokeResult] = []
        for points in screen_strokes:
            # No automatic retry: a completed earlier stroke is a real side effect.
            result = self._inputs.stroke.stroke(
                binding,
                points,
                authorization=authorization,
                allowed_rect=canvas.rect,
                step_delay_seconds=step_delay_seconds,
                max_step_pixels=max_step_pixels,
                press_settle_seconds=press_settle_seconds,
                release_settle_seconds=release_settle_seconds,
            )
            results.append(result)

        return PaintDrawResult(
            plan_name=plan.name,
            canvas=canvas,
            strokes=tuple(results),
        )


def _rect_inside(outer: WindowRect, inner: WindowRect) -> bool:
    return (
        inner.usable
        and outer.left <= inner.left
        and outer.top <= inner.top
        and inner.right <= outer.right
        and inner.bottom <= outer.bottom
    )
