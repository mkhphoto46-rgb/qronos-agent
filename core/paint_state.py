from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol

from core.action_cancellation import CancellationToken, checkpoint
from core.brain_runtime import BrainMessage, BrainMessageRole, BrainRuntime
from core.input_capabilities import InputCapabilities
from core.input_safety import BoundInputAuthorization
from core.model_registry import get_model
from core.screen_capture import Capture, ScreenCapture
from core.vision_grounding import (
    GROUNDING_RESPONSE_SCHEMA,
    GroundedTarget,
    GroundingUnavailable,
    GroundingStale,
    ground_structured_response,
    grounding_prompt,
    revalidate_grounded_target,
    validate_grounding_capture,
)
from core.window_binding import WindowBinder, WindowBinding, WindowRect


class PaintStateError(RuntimeError):
    """Paint's drawing-tool state could not be made safe and deterministic."""


class _Grounder(Protocol):
    def locate(self, target: str, capture: Capture) -> GroundedTarget: ...


class PaintStateGrounder:
    """Structured Paint UI grounding that keeps the vision model warm for one normalization."""

    def __init__(self, runtime: BrainRuntime) -> None:
        self._runtime = runtime
        profile = get_model("vision")
        self._model_name = profile.name
        self._num_ctx = profile.context_tokens
        self._closed = False

    def locate(self, target: str, capture: Capture) -> GroundedTarget:
        if self._closed:
            raise PaintStateError("PaintStateGrounder is already closed.")
        validate_grounding_capture(capture)
        asked = grounding_prompt(
            target,
            image_width=int(capture.image.width),
            image_height=int(capture.image.height),
        )
        raw = self._runtime.chat(
            model_name=self._model_name,
            messages=(
                BrainMessage(
                    role=BrainMessageRole.USER,
                    content=asked,
                    images=(capture.image,),
                ),
            ),
            think=False,
            num_predict=384,
            num_ctx=self._num_ctx,
            # Six short grounding calls are made in one normalization. Keeping
            # Qwen resident avoids six multi-GB unload/reload cycles.
            keep_alive="90s",
            response_format=GROUNDING_RESPONSE_SCHEMA,
        )
        return ground_structured_response(capture, raw)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._runtime.stop_model(self._model_name)


@dataclass(frozen=True)
class PaintStateNormalization:
    """Evidence that Paint drawing state was normalized before canvas input."""

    pencil_confidence: float
    black_confidence: float
    minimum_size_confidence: float
    pencil_selected_confidence: float
    black_selected_confidence: float
    minimum_size_selected_confidence: float


SleepFn = Callable[[float], None]


class PaintStateNormalizer:
    """
    Put Microsoft Paint into one bounded line-art state before drawing.

    This deliberately does not use hard-coded toolbar coordinates or assumed
    keyboard shortcuts. Every toolbar coordinate is grounded from a fresh
    capture of the exact verified Paint HWND and is revalidated immediately
    before the click. If Vision cannot find one unique target, Qronos stops
    before it touches the canvas.

    The normalizer selects:
    - Pencil (not Brush/Marker/Eraser),
    - black as the primary drawing colour,
    - the thinnest available line width.

    It then takes another fresh frame and requires Vision to identify Pencil as
    the active/selected tool and black as the active primary colour. There is no
    retry loop: toolbar clicks are state changes, and a failed normalization is
    reported instead of guessing.
    """

    PENCIL_TARGET = (
        "the Pencil drawing tool icon in Microsoft Paint's top toolbar; "
        "specifically Pencil, not Brushes, Marker, Eraser, Fill, or Shapes"
    )
    BLACK_TARGET = (
        "the solid black colour swatch in Microsoft Paint's Colors palette; "
        "the swatch to use as the primary drawing colour, not a toolbar icon"
    )
    MINIMUM_SIZE_TARGET = (
        "the smallest-size setting position on the visible Pencil size slider "
        "in Microsoft Paint; after Pencil is selected, this size slider appears "
        "along the left side of the canvas. Locate the minimum-size endpoint or "
        "clickable minimum position on the slider itself, not the drawing canvas"
    )
    SELECTED_PENCIL_TARGET = (
        "the Pencil tool that is visibly selected or highlighted as the active "
        "drawing tool in Microsoft Paint"
    )
    SELECTED_BLACK_TARGET = (
        "the current primary/foreground colour indicator in Microsoft Paint "
        "showing black; locate the current-colour indicator, not a palette swatch"
    )
    SELECTED_MINIMUM_SIZE_TARGET = (
        "the Pencil size slider in Microsoft Paint showing its handle or size "
        "indicator at the smallest available stroke size; locate the slider "
        "handle or minimum-size indicator, not the drawing canvas"
    )

    def __init__(
        self,
        binder: WindowBinder,
        capture: ScreenCapture,
        grounder: _Grounder,
        *,
        inputs: InputCapabilities | None = None,
        sleep_fn: SleepFn = time.sleep,
        settle_seconds: float = 0.12,
        close_grounder: bool = True,
    ) -> None:
        self._binder = binder
        self._capture = capture
        self._grounder = grounder
        self._inputs = inputs if inputs is not None else InputCapabilities.for_binder(binder)
        self._sleep = sleep_fn
        self._settle_seconds = float(settle_seconds)
        self._close_grounder = bool(close_grounder)
        if self._settle_seconds < 0.0:
            raise ValueError("settle_seconds cannot be negative.")
        # Toolbar clicks change Paint's persisted tool/colour/size state. That
        # is a real side effect Qronos does not reverse; callers report it.
        self.toolbar_clicks_attempted = 0
        self._cancel_token: CancellationToken | None = None

    def normalize(
        self,
        binding: WindowBinding,
        *,
        authorization: BoundInputAuthorization,
        screen_approved: bool,
        cancel_token: CancellationToken | None = None,
    ) -> PaintStateNormalization:
        self.toolbar_clicks_attempted = 0
        self._cancel_token = cancel_token
        checkpoint(cancel_token)
        if binding.app_id != "paint" or binding.snapshot.process_name != "mspaint.exe":
            raise PaintStateError("PaintStateNormalizer refuses a non-Paint binding.")

        client = binding.snapshot.client_rect
        if not client.usable:
            raise PaintStateError("Paint client rectangle is not usable.")

        try:
            pencil = self._locate_and_click(
                binding, self.PENCIL_TARGET, client, authorization, screen_approved
            )
            black = self._locate_and_click(
                binding, self.BLACK_TARGET, client, authorization, screen_approved
            )

            # Current Windows 11 Paint exposes Pencil width as a size slider on
            # the left side of the canvas, not as a stable top-toolbar Size
            # dropdown. Ground the minimum-size position directly and confine
            # the click to the left-side control region so a bad grounding
            # cannot become an accidental canvas mark.
            minimum_size = self._locate_and_click(
                binding,
                self.MINIMUM_SIZE_TARGET,
                _left_size_control_region(client),
                authorization,
                screen_approved,
            )

            checkpoint(cancel_token)

            # Selection verification is observational. It happens before the canvas
            # capture and before any drawing stroke, so failure remains side-effect
            # safe with respect to the user's document.
            verification_frame = self._capture_frame(
                binding,
                "Verify the selected Paint Pencil tool and primary black colour "
                "before any canvas drawing occurs.",
                screen_approved,
            )
            selected_pencil = self._locate(
                self.SELECTED_PENCIL_TARGET,
                verification_frame,
                step="verify selected Pencil",
            )
            selected_black = self._locate(
                self.SELECTED_BLACK_TARGET,
                verification_frame,
                step="verify selected black colour",
            )
            selected_minimum_size = self._locate(
                self.SELECTED_MINIMUM_SIZE_TARGET,
                verification_frame,
                step="verify minimum Pencil size",
            )

            # Revalidate one last time after model inference. The toolbar clicks and
            # verification frame are useful only while the exact HWND/layout remains
            # current and foreground.
            self._binder.revalidate(
                binding,
                require_foreground=True,
                require_same_layout=True,
            )

            return PaintStateNormalization(
                pencil_confidence=pencil.confidence,
                black_confidence=black.confidence,
                minimum_size_confidence=minimum_size.confidence,
                pencil_selected_confidence=selected_pencil.confidence,
                black_selected_confidence=selected_black.confidence,
                minimum_size_selected_confidence=selected_minimum_size.confidence,
            )
        finally:
            self._cancel_token = None
            if self._close_grounder:
                close = getattr(self._grounder, "close", None)
                if callable(close):
                    close()

    def _locate_and_click(
        self,
        binding: WindowBinding,
        target: str,
        allowed_rect: WindowRect,
        authorization: BoundInputAuthorization,
        screen_approved: bool,
    ) -> GroundedTarget:
        checkpoint(self._cancel_token)
        frame = self._capture_frame(
            binding,
            f"Locate one Paint toolbar control before drawing: {target}",
            screen_approved,
        )
        grounded = self._locate(target, frame, step=target)
        try:
            x, y = revalidate_grounded_target(
                grounded,
                binding,
                self._binder,
                require_foreground=True,
            )
        except GroundingStale as exc:
            raise PaintStateError(
                f"Paint toolbar target expired before click: {target}. {exc}"
            ) from exc

        if not _contains(allowed_rect, x, y):
            raise PaintStateError(
                f"Paint UI target fell outside its verified safe control region: {target}."
            )

        # Vision inference can take seconds; honour Stop before the click.
        checkpoint(self._cancel_token)
        self.toolbar_clicks_attempted += 1
        self._inputs.click.click_at(
            binding,
            x,
            y,
            authorization=authorization,
            allowed_rect=allowed_rect,
        )
        if self._settle_seconds:
            if self._cancel_token is not None:
                self._cancel_token.sleep(self._settle_seconds)
            else:
                self._sleep(self._settle_seconds)
        return grounded

    def _capture_frame(
        self,
        binding: WindowBinding,
        reason: str,
        screen_approved: bool,
    ) -> Capture:
        return self._capture.capture_bound(
            binding,
            self._binder,
            approved=screen_approved,
            require_foreground=True,
            reason=reason,
        )

    def _locate(self, target: str, frame: Capture, *, step: str) -> GroundedTarget:
        try:
            return self._grounder.locate(target, frame)
        except GroundingUnavailable as exc:
            raise PaintStateError(
                f"Could not ground Paint state step '{step}': {exc}"
            ) from exc


def _contains(rect: WindowRect, x: int, y: int) -> bool:
    return rect.left <= int(x) < rect.right and rect.top <= int(y) < rect.bottom


def _left_size_control_region(client: WindowRect) -> WindowRect:
    """Conservative region where modern Paint exposes the Pencil size slider."""
    width = min(max(220, round(client.width * 0.28)), max(220, client.width // 2))
    top_inset = max(70, round(client.height * 0.10))
    bottom_inset = max(40, round(client.height * 0.05))
    region = WindowRect(
        left=client.left,
        top=client.top + top_inset,
        right=min(client.right, client.left + width),
        bottom=max(client.top + top_inset + 1, client.bottom - bottom_inset),
    )
    if not region.usable:
        raise PaintStateError("Paint left-side size-control region is not usable.")
    return region
