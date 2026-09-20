from __future__ import annotations

from dataclasses import dataclass
import time

from core.window_binding import WindowBinder, WindowBinding, WindowRect, WindowSnapshot
from security.gate import ActionGrant
from security.permissions import ActionCategory


class InputPermissionRequired(PermissionError):
    """A native input primitive was called without a valid GUI authorization."""


class InputAuthorizationMismatch(PermissionError):
    """The supplied authorization does not match the requested action/window/scope."""


class InputOutOfBounds(ValueError):
    """A requested pointer coordinate was outside the verified client area."""


@dataclass(frozen=True)
class BoundInputPoint:
    x: int
    y: int
    hwnd: int
    identity_token: str
    layout_token: str


@dataclass(frozen=True, init=False)
class BoundInputAuthorization:
    action_id: str
    category: ActionCategory
    target: str
    expires_at: float
    hwnd: int
    identity_token: str
    layout_token: str
    allowed_rect: WindowRect

    _ISSUER = object()

    def __init__(
        self,
        *,
        action_id: str,
        category: ActionCategory,
        target: str,
        expires_at: float,
        hwnd: int,
        identity_token: str,
        layout_token: str,
        allowed_rect: WindowRect,
        _issuer: object,
    ) -> None:
        if _issuer is not self._ISSUER:
            raise TypeError(
                "BoundInputAuthorization may only be created by bind()/restrict()."
            )
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "expires_at", float(expires_at))
        object.__setattr__(self, "hwnd", int(hwnd))
        object.__setattr__(self, "identity_token", identity_token)
        object.__setattr__(self, "layout_token", layout_token)
        object.__setattr__(self, "allowed_rect", allowed_rect)

    @classmethod
    def bind(
        cls,
        grant: ActionGrant,
        binding: WindowBinding,
        *,
        allowed_rect: WindowRect | None = None,
    ) -> "BoundInputAuthorization":
        if not isinstance(grant, ActionGrant):
            raise InputPermissionRequired("GUI input requires an ActionGrant.")
        if grant.category is not ActionCategory.GUI_AUTOMATION:
            raise InputAuthorizationMismatch("ActionGrant category is not GUI_AUTOMATION.")
        expected_target = f"app:{binding.app_id}"
        if grant.target != expected_target:
            raise InputAuthorizationMismatch(
                f"ActionGrant target {grant.target!r} does not match {expected_target!r}."
            )
        if not grant.active():
            raise InputPermissionRequired("GUI ActionGrant has expired.")

        client = binding.snapshot.client_rect
        scope = allowed_rect if allowed_rect is not None else client
        if not _rect_inside(client, scope):
            raise InputAuthorizationMismatch(
                "Input authorization scope must stay inside the verified client area."
            )

        return cls(
            action_id=grant.action_id,
            category=grant.category,
            target=grant.target,
            expires_at=grant.expires_at,
            hwnd=binding.hwnd,
            identity_token=binding.identity_token,
            layout_token=binding.layout_token,
            allowed_rect=scope,
            _issuer=cls._ISSUER,
        )

    def restrict(self, allowed_rect: WindowRect) -> "BoundInputAuthorization":
        if not _rect_inside(self.allowed_rect, allowed_rect):
            raise InputAuthorizationMismatch(
                "Input authorization scope cannot be widened or moved outside its parent scope."
            )
        return type(self)(
            action_id=self.action_id,
            category=self.category,
            target=self.target,
            expires_at=self.expires_at,
            hwnd=self.hwnd,
            identity_token=self.identity_token,
            layout_token=self.layout_token,
            allowed_rect=allowed_rect,
            _issuer=self._ISSUER,
        )

    def require_for(self, binding: WindowBinding) -> None:
        if time.monotonic() >= self.expires_at:
            raise InputPermissionRequired("GUI input authorization has expired.")
        if self.category is not ActionCategory.GUI_AUTOMATION:
            raise InputAuthorizationMismatch("Authorization category changed.")
        if self.target != f"app:{binding.app_id}":
            raise InputAuthorizationMismatch("Authorization target does not match the app.")
        if binding.hwnd != self.hwnd:
            raise InputAuthorizationMismatch("Authorization HWND does not match.")
        if binding.identity_token != self.identity_token:
            raise InputAuthorizationMismatch("Authorization process identity does not match.")
        if binding.layout_token != self.layout_token:
            raise InputAuthorizationMismatch("Authorization layout token does not match.")


class BoundInputGuard:
    def __init__(self, binder: WindowBinder) -> None:
        self._binder = binder

    @staticmethod
    def require_authorization(
        authorization: BoundInputAuthorization | None,
        binding: WindowBinding,
    ) -> BoundInputAuthorization:
        if not isinstance(authorization, BoundInputAuthorization):
            raise InputPermissionRequired(
                "GUI automation requires a bound input authorization."
            )
        authorization.require_for(binding)
        return authorization

    def revalidate(
        self,
        binding: WindowBinding,
        *,
        authorization: BoundInputAuthorization,
        require_foreground: bool = True,
        require_same_layout: bool = True,
    ) -> WindowSnapshot:
        auth = self.require_authorization(authorization, binding)
        fresh = self._binder.revalidate(
            binding,
            require_foreground=require_foreground,
            require_same_layout=require_same_layout,
        )
        auth.require_for(binding)
        return fresh

    def point(
        self,
        binding: WindowBinding,
        x: int,
        y: int,
        *,
        authorization: BoundInputAuthorization,
        allowed_rect: WindowRect | None = None,
    ) -> tuple[WindowSnapshot, BoundInputPoint]:
        auth = self.require_authorization(authorization, binding)
        snapshot = self.revalidate(binding, authorization=auth)
        x = int(x)
        y = int(y)
        client = snapshot.client_rect

        if not _contains(client, x, y):
            raise InputOutOfBounds(
                f"Input point ({x}, {y}) is outside the verified client area "
                f"({client.left}, {client.top})-({client.right}, {client.bottom})."
            )

        effective = auth.allowed_rect
        if allowed_rect is not None:
            if not _rect_inside(auth.allowed_rect, allowed_rect):
                raise InputAuthorizationMismatch(
                    "Caller requested an input region outside the authorization scope."
                )
            effective = allowed_rect

        if not _contains(effective, x, y):
            raise InputOutOfBounds(
                f"Input point ({x}, {y}) is outside the authorized input region."
            )

        return snapshot, BoundInputPoint(
            x=x,
            y=y,
            hwnd=binding.hwnd,
            identity_token=binding.identity_token,
            layout_token=binding.layout_token,
        )


def _contains(rect: WindowRect, x: int, y: int) -> bool:
    return rect.left <= x < rect.right and rect.top <= y < rect.bottom


def _rect_inside(outer: WindowRect, inner: WindowRect) -> bool:
    return (
        inner.usable
        and outer.left <= inner.left
        and outer.top <= inner.top
        and inner.right <= outer.right
        and inner.bottom <= outer.bottom
    )
