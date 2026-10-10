from __future__ import annotations

from dataclasses import dataclass

from core.action_cancellation import CancellationToken, checkpoint
from core.grounding_freshness import (
    CANVAS_MAX_AGE_SECONDS,
    Clock,
    FreshnessExpired,
    RegionFingerprint,
    monotonic,
    require_fresh,
)
from core.paint_canvas_watch import CanvasChanged, PaintCanvasWatch
from core.input_capabilities import InputCapabilities
from core.input_safety import BoundInputAuthorization
from core.pointer_stroke import PointerStrokeResult, StrokeProgress
from core.shape_planner import ShapePlan
from core.sketch_approval import VisualApprovedSketch
from core.sketch_preview import digest_shape_plan
from core.window_binding import WindowBinder, WindowBinding, WindowRect


class PaintAdapterError(RuntimeError):
    """Paint-specific execution preconditions were not satisfied."""


class PaintCanvasExpired(PaintAdapterError):
    """The grounded canvas is too old, or Qronos used Paint after it was grounded."""


class PaintCanvasChanged(PaintAdapterError):
    """Paint's canvas, its surroundings or its selected tools changed during drawing."""


class PaintVerificationUnavailable(PaintAdapterError):
    """The drawing cannot be verified stroke by stroke (no capture or no tool regions)."""


class PaintPlanNotApproved(PaintAdapterError):
    """The plan to draw is not exactly the visually approved one; nothing about the window."""



@dataclass(frozen=True)
class PaintCanvasRegion:
    """A short-lived canvas capability tied to one verified Paint layout."""

    rect: WindowRect
    hwnd: int
    identity_token: str
    layout_token: str
    #: Provenance of the frame the canvas was found in (core.grounding_freshness).
    #: A region without it is refused before any stroke.
    captured_at: float = 0.0
    state_epoch: int = 0
    #: Full-resolution canvas pixels from the grounding frame; the canvas must
    #: still look like this immediately before the first stroke.
    fingerprint: RegionFingerprint | None = None

    @classmethod
    def verified(
        cls,
        binding: WindowBinding,
        rect: WindowRect,
        *,
        captured_at: float,
        state_epoch: int,
        fingerprint: RegionFingerprint | None,
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
            captured_at=float(captured_at),
            state_epoch=int(state_epoch),
            fingerprint=fingerprint,
        )

    def matches(self, binding: WindowBinding) -> bool:
        return (
            binding.hwnd == self.hwnd
            and binding.identity_token == self.identity_token
            and binding.layout_token == self.layout_token
        )


class PaintCanvasProgress:
    """
    What the adapter actually did to the canvas, filled in as it goes.

    Exceptions from ``execute`` propagate unchanged; the caller reads this
    afterwards to report an honest partial outcome and to decide whether an
    exact reversal is possible.

    ``committed_marks`` counts strokes (complete or partial) whose mouse-up
    provably landed in the exact bound Paint window: each is one Paint edit.
    ``unproven_marks`` counts presses whose release could not be proven to
    land there (window/focus changed mid-stroke). Any unproven mark makes the
    number of Paint edits uncertain.
    """

    __slots__ = ("planned_strokes", "completed_strokes", "committed_marks", "unproven_marks")

    def __init__(self) -> None:
        self.planned_strokes = 0
        self.completed_strokes = 0
        self.committed_marks = 0
        self.unproven_marks = 0

    @property
    def touched_canvas(self) -> bool:
        return self.committed_marks > 0 or self.unproven_marks > 0

    @property
    def edit_count_certain(self) -> bool:
        return self.unproven_marks == 0


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
        capture=None,
        clock: Clock = monotonic,
        canvas_max_age_seconds: float = CANVAS_MAX_AGE_SECONDS,
    ) -> None:
        self._binder = binder
        # The bound, permission-gated window capture used to verify the canvas
        # before every stroke. Without one, drawing is refused.
        self._capture = capture
        self._inputs = inputs if inputs is not None else InputCapabilities.for_binder(binder)
        self._clock = clock
        self._canvas_max_age_seconds = float(canvas_max_age_seconds)

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
        cancel_token: CancellationToken | None = None,
        progress: PaintCanvasProgress | None = None,
        screen_approved: bool = False,
        tool_regions: tuple[WindowRect, ...] = (),
    ) -> PaintDrawResult:
        """
        Production sketch path: only a visually approved frozen plan may reach Paint.

        The low-level ``execute`` method remains available for primitive tests and
        trusted callers, but agent sketch execution should use this gate.
        """
        if not isinstance(approved_sketch, VisualApprovedSketch):
            raise PaintPlanNotApproved(
                "Paint sketch execution requires a VisualApprovedSketch capability."
            )
        if digest_shape_plan(approved_sketch.plan) != approved_sketch.plan_digest:
            raise PaintPlanNotApproved(
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
            cancel_token=cancel_token,
            progress=progress,
            screen_approved=screen_approved,
            tool_regions=tool_regions,
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
        cancel_token: CancellationToken | None = None,
        progress: PaintCanvasProgress | None = None,
        screen_approved: bool = False,
        tool_regions: tuple[WindowRect, ...] = (),
    ) -> PaintDrawResult:
        tracker = progress if progress is not None else PaintCanvasProgress()
        checkpoint(cancel_token)
        if binding.app_id != "paint" or binding.snapshot.process_name != "mspaint.exe":
            raise PaintAdapterError("PaintAdapter refuses a non-Paint binding.")
        if not canvas.matches(binding):
            raise PaintAdapterError(
                "Paint canvas capability does not match the current window binding/layout."
            )
        # The canvas must come from a recent frame taken after Qronos's last
        # input to Paint (toolbar clicks included). The strokes below advance
        # the epoch themselves, so this is checked once, before the first one.
        try:
            require_fresh(
                captured_at=canvas.captured_at,
                captured_epoch=canvas.state_epoch,
                identity_token=binding.identity_token,
                max_age_seconds=self._canvas_max_age_seconds,
                clock=self._clock,
                what="The grounded Paint canvas bounds",
            )
        except FreshnessExpired as exc:
            raise PaintCanvasExpired(str(exc)) from exc

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

        if self._capture is None:
            raise PaintVerificationUnavailable(
                "Paint drawing needs a bound window capture to verify the canvas before each stroke."
            )
        if not tool_regions:
            raise PaintVerificationUnavailable(
                "Paint drawing needs the verified tool, colour and size indicators to watch before each stroke."
            )
        if not all(_rect_inside(fresh.client_rect, region) for region in tool_regions):
            raise PaintVerificationUnavailable(
                "A watched Paint tool indicator lies outside the verified client area."
            )
        watch = PaintCanvasWatch(
            self._capture, self._binder, binding, canvas, screen_approved=screen_approved,
            tool_regions=tuple(tool_regions),
        )
        try:
            watch.start()
        except CanvasChanged as exc:
            raise PaintCanvasChanged(str(exc)) from exc

        tracker.planned_strokes = len(screen_strokes)
        results: list[PointerStrokeResult] = []
        for index, points in enumerate(screen_strokes):
            # Stop is honoured between strokes as well as inside each stroke.
            checkpoint(cancel_token)
            if index > 0:
                # Each stroke is authorised by its own fresh frame: anything
                # other than Qronos's earlier strokes changed means stop now,
                # with the button up.
                try:
                    watch.before_stroke()
                except CanvasChanged as exc:
                    raise PaintCanvasChanged(str(exc)) from exc
                checkpoint(cancel_token)
            stroke_progress = StrokeProgress()
            try:
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
                    cancel_token=cancel_token,
                    progress=stroke_progress,
                )
            except BaseException:
                if stroke_progress.pressed:
                    if stroke_progress.clean_release:
                        tracker.committed_marks += 1
                    else:
                        tracker.unproven_marks += 1
                raise
            tracker.completed_strokes += 1
            tracker.committed_marks += 1
            watch.stroke_drawn(points)
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
