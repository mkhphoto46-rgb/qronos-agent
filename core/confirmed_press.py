"""Press a Vision-grounded point only after the last motion has been checked (D3).

A click used to be: confirm the target from a fresh frame, then animate the
cursor to it (up to about 0.3 s) and press. Anything the motion itself caused
- a tooltip, a hover-opened menu, a flyout - happened after the confirmation
and was never seen before the button went down.

The order here is:

1. revalidate the coordinate (window identity, layout, focus, age, input epoch);
2. confirm a fresh frame against the frame the model saw (target strictly, its
   row/column bands strictly, the rest of the window within a small
   allowance - core.vision_grounding.confirm_frame);
3. move the cursor to the point (the only motion);
4. take one more frame and require it to equal the step-2 frame everywhere
   except the target box itself plus a small margin, where the control's own
   hover highlight may appear. A tooltip, menu or anything else that appeared
   because of the motion refuses the press. For a point target the "target
   box" of this allowance may be the box the locator grounded (a whole button
   highlights as a whole), but only after it is checked: it must contain the
   point, it is clipped to the bound window's client area, and a box larger
   than a button (HOVER_BOX_MAX_*, scaled by the window's DPI, and never more
   than half the client width or height) is not used at all, so the strict
   point scope stays. The checks of step 2 are never widened by it;
5. check Stop, then press and release at the cursor's current position: the
   cursor is already there, so no further motion happens before the press.

Limits (stated, not hidden): a change that happens between step 4's frame and
the press (milliseconds) is not seen; a change confined to the hover-allowance
box (the target box, or the checked grounded box) during the motion cannot be
told apart from its hover highlight; the native press itself is one call and
is not interrupted by Stop.
"""

from __future__ import annotations

from typing import Any, Callable

from core.action_cancellation import CancellationToken, checkpoint
from core.grounding_freshness import (
    GROUNDING_MAX_AGE_SECONDS,
    Clock,
    FreshnessExpired,
    monotonic,
    require_fresh,
    require_region_unchanged,
)
from core.vision_grounding import (
    GroundedPoint,
    GroundedTarget,
    GroundingStale,
    _validate_bound_capture,
    confirm_frame,
    revalidate_grounded_point,
    viewport_fingerprint,
)
from core.window_binding import WindowBinder, WindowBinding, WindowRect

#: Where the control's own hover highlight may appear after the motion.
HOVER_MARGIN_PIXELS = 4
#: The largest grounded box (logical pixels at 96 DPI) accepted as a control's
#: own hover area. Larger boxes are not buttons; they are not used.
HOVER_BOX_MAX_WIDTH = 320
HOVER_BOX_MAX_HEIGHT = 96


def hover_allowance_box(
    box: WindowRect | None,
    x: int,
    y: int,
    client: WindowRect,
    *,
    dpi: int = 96,
) -> WindowRect | None:
    """The checked grounded box usable as the after-motion hover allowance, or None.

    None (keep the strict target box) when there is no box, when it does not
    contain the point, or when, clipped to the client area, it is larger than a
    button: wider or taller than HOVER_BOX_MAX_* at the window's DPI, or than
    half the client's width or height.
    """
    if box is None:
        return None
    if not (box.left <= x < box.right and box.top <= y < box.bottom):
        return None
    clipped = WindowRect(
        max(box.left, client.left),
        max(box.top, client.top),
        min(box.right, client.right),
        min(box.bottom, client.bottom),
    )
    if not (clipped.left <= x < clipped.right and clipped.top <= y < clipped.bottom):
        return None
    scale = max(1.0, int(dpi) / 96.0)
    too_wide = clipped.width > min(HOVER_BOX_MAX_WIDTH * scale, client.width / 2)
    too_tall = clipped.height > min(HOVER_BOX_MAX_HEIGHT * scale, client.height / 2)
    if too_wide or too_tall:
        return None
    return clipped


def press_grounded_target(
    target: GroundedTarget,
    binding: WindowBinding,
    binder: WindowBinder,
    capture_source: Any,
    inputs: Any,
    *,
    authorization: Any,
    allowed_rect: WindowRect,
    screen_approved: bool,
    reason: str,
    cancel_token: CancellationToken | None = None,
    before_press: Callable[[], None] | None = None,
    max_age_seconds: float = GROUNDING_MAX_AGE_SECONDS,
    clock: Clock = monotonic,
) -> Any:
    """Confirm, move, re-confirm after the motion, then press a GroundedTarget. Returns the click result."""
    if target.fingerprint is None or target.context is None:
        raise GroundingStale("The target carries no pixel evidence; ground it again.")
    return _press(
        target.point,
        target.fingerprint,
        target.context,
        target.screen_box,
        f"The target '{target.label}'",
        binding,
        binder,
        capture_source,
        inputs,
        authorization=authorization,
        allowed_rect=allowed_rect,
        screen_approved=screen_approved,
        reason=reason,
        cancel_token=cancel_token,
        before_press=before_press,
        max_age_seconds=max_age_seconds,
        clock=clock,
    )


def press_grounded_point(
    point: GroundedPoint,
    fingerprint: Any,
    context: Any,
    binding: WindowBinding,
    binder: WindowBinder,
    capture_source: Any,
    inputs: Any,
    *,
    authorization: Any,
    allowed_rect: WindowRect,
    screen_approved: bool,
    reason: str,
    cancel_token: CancellationToken | None = None,
    before_press: Callable[[], None] | None = None,
    max_age_seconds: float = GROUNDING_MAX_AGE_SECONDS,
    clock: Clock = monotonic,
    grounded_box: WindowRect | None = None,
) -> Any:
    """press_grounded_target for a point-only target with its recorded pixels.

    ``grounded_box`` is the box the locator returned with the point, if any. It
    is used only as the after-motion hover allowance, and only as
    hover_allowance_box checks it; the confirmation before the motion stays the
    strict one of ``fingerprint``.
    """
    return _press(
        point,
        fingerprint,
        context,
        fingerprint.screen_rect,
        f"The target '{point.label}'",
        binding,
        binder,
        capture_source,
        inputs,
        authorization=authorization,
        allowed_rect=allowed_rect,
        screen_approved=screen_approved,
        reason=reason,
        cancel_token=cancel_token,
        before_press=before_press,
        max_age_seconds=max_age_seconds,
        clock=clock,
        hover_box=hover_allowance_box(
            grounded_box,
            point.screen_x,
            point.screen_y,
            binding.snapshot.client_rect,
            dpi=binding.snapshot.dpi,
        ),
    )


def _press(
    point: GroundedPoint,
    fingerprint: Any,
    context: Any,
    target_box: WindowRect,
    what: str,
    binding: WindowBinding,
    binder: WindowBinder,
    capture_source: Any,
    inputs: Any,
    *,
    authorization: Any,
    allowed_rect: WindowRect,
    screen_approved: bool,
    reason: str,
    cancel_token: CancellationToken | None,
    before_press: Callable[[], None] | None,
    max_age_seconds: float,
    clock: Clock,
    hover_box: WindowRect | None = None,
) -> Any:
    x, y = revalidate_grounded_point(
        point,
        binding,
        binder,
        require_foreground=True,
        max_age_seconds=max_age_seconds,
        clock=clock,
    )
    before_motion = confirm_frame(
        fingerprint,
        context,
        target_box,
        what,
        binding,
        binder,
        capture_source,
        screen_approved=screen_approved,
        reason=reason,
        max_age_seconds=max_age_seconds,
        clock=clock,
    )
    checkpoint(cancel_token)

    # The only motion: the cursor goes to the point.
    inputs.hover.smooth_hover(binding, x, y, authorization=authorization, allowed_rect=allowed_rect)

    after_motion = capture_source.capture_bound(
        binding,
        binder,
        approved=screen_approved,
        require_foreground=True,
        reason=f"Check nothing opened while the cursor moved, before pressing: {reason}",
    )
    _validate_bound_capture(after_motion)
    if after_motion.binding_token != binding.identity_token or after_motion.layout_token != binding.layout_token:
        raise GroundingStale("The window or its layout changed while the cursor moved.")
    # The checked grounded box adds to the target box; it never shrinks it.
    hover = target_box if hover_box is None else WindowRect(
        min(target_box.left, hover_box.left),
        min(target_box.top, hover_box.top),
        max(target_box.right, hover_box.right),
        max(target_box.bottom, hover_box.bottom),
    )
    highlight = WindowRect(
        hover.left - HOVER_MARGIN_PIXELS,
        hover.top - HOVER_MARGIN_PIXELS,
        hover.right + HOVER_MARGIN_PIXELS,
        hover.bottom + HOVER_MARGIN_PIXELS,
    )
    try:
        require_region_unchanged(
            viewport_fingerprint(before_motion),
            after_motion,
            what=f"The window outside {what[4:] if what.startswith('The ') else what} after the cursor moved",
            exclude=highlight,
        )
        require_fresh(
            captured_at=after_motion.captured_at,
            captured_epoch=after_motion.state_epoch,
            identity_token=binding.identity_token,
            max_age_seconds=max_age_seconds,
            clock=clock,
            what="The frame after the motion",
        )
    except FreshnessExpired as exc:
        raise GroundingStale(str(exc)) from exc

    checkpoint(cancel_token)
    if before_press is not None:
        before_press()  # e.g. count the click as a possible side effect from here on
    # The cursor is already at (x, y): click_at's approach is a zero-length
    # move, so the press follows the checked frame without further motion.
    return inputs.click.click_at(binding, x, y, authorization=authorization, allowed_rect=allowed_rect)
