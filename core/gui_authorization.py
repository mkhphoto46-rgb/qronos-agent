from __future__ import annotations

from dataclasses import dataclass
import time

from core.window_binding import (
    WindowBinding,
)
from security.gate import (
    ActionGrant,
)
from security.permissions import (
    ActionCategory,
)


class GuiPermissionRequired(PermissionError):
    """Semantic GUI action requires a valid ActionGrant."""


class GuiAuthorizationMismatch(PermissionError):
    """Semantic GUI authorization does not match target/window."""


@dataclass(frozen=True, init=False)
class BoundGuiAuthorization:
    """
    Short-lived semantic GUI capability.

    This intentionally does NOT contain layout coordinates. It is for
    accessibility actions such as InvokePattern whose identity is semantic,
    not pixel-based.
    """

    action_id: str
    category: ActionCategory
    target: str
    expires_at: float

    hwnd: int
    identity_token: str

    allowed_runtime_ids: frozenset[str]

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
        allowed_runtime_ids: frozenset[str],
        _issuer: object,
    ) -> None:

        if _issuer is not self._ISSUER:
            raise TypeError(
                "BoundGuiAuthorization may only be created by bind()."
            )

        object.__setattr__(
            self,
            "action_id",
            action_id,
        )

        object.__setattr__(
            self,
            "category",
            category,
        )

        object.__setattr__(
            self,
            "target",
            target,
        )

        object.__setattr__(
            self,
            "expires_at",
            float(expires_at),
        )

        object.__setattr__(
            self,
            "hwnd",
            int(hwnd),
        )

        object.__setattr__(
            self,
            "identity_token",
            identity_token,
        )

        object.__setattr__(
            self,
            "allowed_runtime_ids",
            allowed_runtime_ids,
        )

    @classmethod
    def bind(
        cls,
        grant: ActionGrant,
        binding: WindowBinding,
        *,
        allowed_runtime_ids,
    ) -> "BoundGuiAuthorization":

        if not isinstance(
            grant,
            ActionGrant,
        ):
            raise GuiPermissionRequired(
                "Semantic GUI action requires an ActionGrant."
            )

        if (
            grant.category
            is not ActionCategory.GUI_AUTOMATION
        ):
            raise GuiAuthorizationMismatch(
                "ActionGrant category is not GUI_AUTOMATION."
            )

        expected_target = (
            f"app:{binding.app_id}"
        )

        if grant.target != expected_target:
            raise GuiAuthorizationMismatch(
                "ActionGrant target does not match bound application."
            )

        if not grant.active():
            raise GuiPermissionRequired(
                "GUI ActionGrant has expired."
            )

        runtime_ids = frozenset(
            str(value or "").strip()
            for value in allowed_runtime_ids
            if str(value or "").strip()
        )

        if not runtime_ids:
            raise ValueError(
                "At least one allowed RuntimeId is required."
            )

        if len(runtime_ids) > 32:
            raise ValueError(
                "Semantic GUI authorization is too broad."
            )

        return cls(
            action_id=grant.action_id,
            category=grant.category,
            target=grant.target,
            expires_at=grant.expires_at,
            hwnd=binding.hwnd,
            identity_token=binding.identity_token,
            allowed_runtime_ids=runtime_ids,
            _issuer=cls._ISSUER,
        )

    def require_for(
        self,
        binding: WindowBinding,
        runtime_id: str,
    ) -> None:

        if time.monotonic() >= self.expires_at:
            raise GuiPermissionRequired(
                "Semantic GUI authorization has expired."
            )

        if (
            self.category
            is not ActionCategory.GUI_AUTOMATION
        ):
            raise GuiAuthorizationMismatch(
                "Authorization category changed."
            )

        if self.target != f"app:{binding.app_id}":
            raise GuiAuthorizationMismatch(
                "Authorization application target changed."
            )

        if binding.hwnd != self.hwnd:
            raise GuiAuthorizationMismatch(
                "Authorization HWND changed."
            )

        if (
            binding.identity_token
            != self.identity_token
        ):
            raise GuiAuthorizationMismatch(
                "Authorization process identity changed."
            )

        wanted = str(
            runtime_id or ""
        ).strip()

        if wanted not in self.allowed_runtime_ids:
            raise GuiAuthorizationMismatch(
                "RuntimeId is outside semantic GUI authorization."
            )
