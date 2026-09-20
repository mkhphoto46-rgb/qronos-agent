from __future__ import annotations

from dataclasses import dataclass

from core.window_binding import WindowBinder, WindowBinding, WindowRect, WindowSnapshot


class InputPermissionRequired(PermissionError):
    """A bounded input primitive was called without GUI-automation approval."""


class InputOutOfBounds(ValueError):
    """A requested pointer coordinate was outside the verified client area."""


@dataclass(frozen=True)
class BoundInputPoint:
    """One screen-space point tied to the currently verified window layout."""

    x: int
    y: int
    hwnd: int
    identity_token: str
    layout_token: str


class BoundInputGuard:
    """
    Shared fail-closed guard for every mouse/keyboard capability.

    This class deliberately knows nothing about Paint or any other app.  It
    only enforces that input is sent to the same foreground HWND/process/layout
    that was previously verified by :class:`WindowBinder`.
    """

    def __init__(self, binder: WindowBinder) -> None:
        self._binder = binder

    @staticmethod
    def require_approval(approved: bool) -> None:
        if approved is not True:
            raise InputPermissionRequired(
                "GUI automation requires an explicit approved=True capability call."
            )

    def revalidate(
        self,
        binding: WindowBinding,
        *,
        approved: bool,
        require_foreground: bool = True,
        require_same_layout: bool = True,
    ) -> WindowSnapshot:
        self.require_approval(approved)
        return self._binder.revalidate(
            binding,
            require_foreground=require_foreground,
            require_same_layout=require_same_layout,
        )

    def point(
        self,
        binding: WindowBinding,
        x: int,
        y: int,
        *,
        approved: bool,
        allowed_rect: WindowRect | None = None,
    ) -> tuple[WindowSnapshot, BoundInputPoint]:
        snapshot = self.revalidate(binding, approved=approved)
        x = int(x)
        y = int(y)
        client = snapshot.client_rect

        if not _contains(client, x, y):
            raise InputOutOfBounds(
                f"Input point ({x}, {y}) is outside the verified client area "
                f"({client.left}, {client.top})-({client.right}, {client.bottom})."
            )

        if allowed_rect is not None:
            if not _rect_inside(client, allowed_rect):
                raise InputOutOfBounds(
                    "The requested allowed input region is not fully contained in the "
                    "verified client area."
                )
            if not _contains(allowed_rect, x, y):
                raise InputOutOfBounds(
                    f"Input point ({x}, {y}) is outside the caller's allowed region."
                )

        return snapshot, BoundInputPoint(
            x=x,
            y=y,
            hwnd=binding.hwnd,
            identity_token=binding.identity_token,
            layout_token=binding.layout_token,
        )


def _contains(rect: WindowRect, x: int, y: int) -> bool:
    # right/bottom are exclusive, matching Win32 RECT semantics.
    return rect.left <= x < rect.right and rect.top <= y < rect.bottom


def _rect_inside(outer: WindowRect, inner: WindowRect) -> bool:
    return (
        inner.usable
        and outer.left <= inner.left
        and outer.top <= inner.top
        and inner.right <= outer.right
        and inner.bottom <= outer.bottom
    )
