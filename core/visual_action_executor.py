from __future__ import annotations

from dataclasses import dataclass

from core.action_cancellation import CancellationToken, checkpoint
from core.input_capabilities import InputCapabilities
from core.keyboard_control import KeyboardResult
from core.input_safety import BoundInputAuthorization
from core.mouse_click import MouseClickResult
from core.screen_capture import ScreenCapture
from core.visual_targeting import VisualTarget, VisualTargetLocator
from core.confirmed_press import press_grounded_point
from core.vision_grounding import target_fingerprint, viewport_fingerprint
from core.window_binding import (
    WindowBinder,
    WindowBinding,
    WindowRect,
)
from security.gate import ActionGrant


@dataclass(frozen=True)
class VisualClickRun:
    """Result of one generic vision-grounded mouse click."""

    target: VisualTarget
    click: MouseClickResult
    authorized_rect: WindowRect


@dataclass(frozen=True)
class VisualClickTypeRun:
    """Result of one generic visual click followed by bounded keyboard text."""

    click_run: VisualClickRun
    keyboard: KeyboardResult


class VisualActionExecutor:
    """
    Generic vision -> validated target -> bounded native input bridge.

    This module contains no Paint, browser, Telegram, Explorer, Photoshop,
    or other application-specific execution logic.

    Application adapters may supply target descriptions, but all real mouse
    input goes through the same exact-window and authorization boundaries.
    """

    def __init__(
        self,
        binder: WindowBinder,
        computer_executor,
        capture: ScreenCapture,
        locator: VisualTargetLocator,
        *,
        inputs: InputCapabilities | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        # One executor serves one user-visible action. The token is checked
        # between steps and before every native input; it never relaxes an
        # authorization check. One click or one typed string is a single
        # native call: Stop takes effect at the next boundary after it.
        self.cancel_token = cancel_token
        self.binder = binder
        self.computer_executor = computer_executor
        self.capture = capture
        self.locator = locator
        self.inputs = (
            inputs
            if inputs is not None
            else InputCapabilities.for_binder(binder)
        )

    def _checkpoint(self) -> None:
        checkpoint(self.cancel_token)

    def _acquire_foreground(self, binding: WindowBinding) -> None:
        self._checkpoint()
        if self.cancel_token is None:
            self.computer_executor.acquire_foreground(binding)
        else:
            self.computer_executor.acquire_foreground(
                binding,
                cancel_token=self.cancel_token,
            )
        self._checkpoint()

    def click_target(
        self,
        binding: WindowBinding,
        target_description: str,
        *,
        grant: ActionGrant,
        screen_approved: bool,
        click_radius: int = 8,
    ) -> VisualClickRun:
        """
        Locate and click one visible target inside one exact verified window.

        Safety sequence:

        exact binding
          -> foreground
          -> bounded screenshot
          -> vision location
          -> foreground reacquisition
          -> identity/layout revalidation
          -> point revalidation
          -> action-bound authorization
          -> tiny point-local input scope
          -> native click
        """

        description = str(target_description or "").strip()

        if not description:
            raise ValueError(
                "click_target requires a visual target description."
            )

        radius = int(click_radius)

        if radius < 1 or radius > 32:
            raise ValueError(
                "click_radius must be between 1 and 32 pixels."
            )

        # The window must be the exact foreground target when pixels are read.
        self._acquire_foreground(binding)

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        frame = self.capture.capture_bound(
            binding,
            self.binder,
            approved=screen_approved,
            require_foreground=True,
            reason=(
                "Locate one visible UI target for a bounded "
                "mouse action in the verified application window."
            ),
        )

        target = self.locator.locate(
            description,
            frame,
        )
        # The pixels around the located point, as the model saw them.
        fingerprint = target_fingerprint(
            frame,
            _point_scope(binding.snapshot.client_rect, target.point.screen_x,
                         target.point.screen_y, radius=radius),
        )

        # Vision inference may take seconds and another window can steal
        # foreground. Never click until the exact bound HWND is foreground
        # again. A Stop during inference ends the action here.
        self._acquire_foreground(binding)

        # This is intentionally strict. If focus acquisition resized/moved
        # the application, old vision coordinates expire and no click occurs.
        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        client = binding.snapshot.client_rect

        scope = _point_scope(
            client,
            target.point.screen_x,
            target.point.screen_y,
            radius=radius,
        )

        authorization = BoundInputAuthorization.bind(
            grant,
            binding,
        ).restrict(scope)

        self._checkpoint()

        # Provenance is not enough: a fresh frame must show the target (and
        # its row/column and window) unchanged where Vision found it; the
        # cursor then moves, one more frame must show nothing opened because
        # of the motion, and only then is the button pressed, without motion.
        # A locator that grounded a box with the point (VisionGrounder) lets
        # the whole control show its own hover highlight after the motion;
        # confirmed_press checks, clips and caps that box. The point-only
        # VisualTargetLocator has none, so only the point scope may change.
        clicked = press_grounded_point(
            target.point,
            fingerprint,
            viewport_fingerprint(frame),
            binding,
            self.binder,
            self.capture,
            self.inputs,
            authorization=authorization,
            allowed_rect=scope,
            screen_approved=screen_approved,
            reason="Confirm the located target is still in place before the click.",
            cancel_token=self.cancel_token,
            grounded_box=getattr(target, "screen_box", None),
        )

        # Native input is considered successful only if the exact window and
        # layout still exist after mouse-up.
        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        return VisualClickRun(
            target=target,
            click=clicked,
            authorized_rect=scope,
        )


    def click_and_type_target(
        self,
        binding: WindowBinding,
        target_description: str,
        text: str,
        *,
        grant: ActionGrant,
        screen_approved: bool,
        click_radius: int = 8,
    ) -> VisualClickTypeRun:
        """
        Locate one visible UI target, click it, then type text.

        The mouse and keyboard operations share the same exact ActionGrant,
        exact HWND/process identity, and exact layout token.

        No Enter/submit action is implied by this method.
        """
        value = str(text)

        if not value:
            raise ValueError(
                "click_and_type_target requires non-empty text."
            )

        click_run = self.click_target(
            binding,
            target_description,
            grant=grant,
            screen_approved=screen_approved,
            click_radius=click_radius,
        )

        # Vision inference can take time. Before keyboard input, reacquire
        # and revalidate the exact application window again.
        self._acquire_foreground(binding)

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        authorization = BoundInputAuthorization.bind(
            grant,
            binding,
        )

        self._checkpoint()

        typed = self.inputs.keyboard.type_text(
            binding,
            value,
            authorization=authorization,
        )

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        return VisualClickTypeRun(
            click_run=click_run,
            keyboard=typed,
        )

    def press_key(
        self,
        binding: WindowBinding,
        key: str,
        *,
        grant: ActionGrant,
    ) -> KeyboardResult:
        """
        Press one bounded keyboard key in one exact verified application.

        Intended both for normal actions and bounded recovery operations such
        as Escape.
        """
        name = str(key or "").strip()

        if not name:
            raise ValueError("press_key requires a key name.")

        self._acquire_foreground(binding)

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        authorization = BoundInputAuthorization.bind(
            grant,
            binding,
        )

        self._checkpoint()

        result = self.inputs.keyboard.press(
            binding,
            name,
            authorization=authorization,
        )

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        return result


def _point_scope(
    client: WindowRect,
    x: int,
    y: int,
    *,
    radius: int,
) -> WindowRect:
    """Create a tiny authorized rectangle around one verified click point."""

    x = int(x)
    y = int(y)
    radius = int(radius)

    if not (
        client.left <= x < client.right
        and client.top <= y < client.bottom
    ):
        raise ValueError(
            "Visual click point is outside the verified client area."
        )

    left = max(client.left, x - radius)
    top = max(client.top, y - radius)

    right = min(
        client.right,
        x + radius + 1,
    )

    bottom = min(
        client.bottom,
        y + radius + 1,
    )

    rect = WindowRect(
        left,
        top,
        right,
        bottom,
    )

    if not rect.usable:
        raise ValueError(
            "Visual click authorization scope is not usable."
        )

    return rect
