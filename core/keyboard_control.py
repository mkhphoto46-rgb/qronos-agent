from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core.input_safety import BoundInputAuthorization, BoundInputGuard
from core.window_binding import WindowBinder, WindowBinding
from core.windows_input_native import send_unicode_text, send_virtual_key, virtual_key_code


TextFn = Callable[[str], None]
KeyFn = Callable[[str, bool], None]


@dataclass(frozen=True)
class KeyboardResult:
    hwnd: int
    identity_token: str
    layout_token: str
    kind: str
    value: str


class KeyboardController:
    """Reusable bounded keyboard capability independent of mouse control."""

    def __init__(
        self,
        binder: WindowBinder,
        *,
        text_fn: TextFn = send_unicode_text,
        key_fn: KeyFn = send_virtual_key,
    ) -> None:
        self._guard = BoundInputGuard(binder)
        self._text = text_fn
        self._key = key_fn

    def type_text(
        self,
        binding: WindowBinding,
        text: str,
        *,
        authorization: BoundInputAuthorization,
    ) -> KeyboardResult:
        value = str(text)
        if not value:
            raise ValueError("type_text requires non-empty text.")
        self._guard.revalidate(binding, authorization=authorization)
        self._text(value)
        self._guard.revalidate(binding, authorization=authorization)
        return self._result(binding, "text", value)

    def press(
        self,
        binding: WindowBinding,
        key: str,
        *,
        authorization: BoundInputAuthorization,
    ) -> KeyboardResult:
        name = str(key or "").strip().lower()
        # Validate before touching SendInput, including injected test senders.
        virtual_key_code(name)
        self._guard.revalidate(binding, authorization=authorization)
        self._key(name, True)
        self._key(name, False)
        self._guard.revalidate(binding, authorization=authorization)
        return self._result(binding, "key", name)

    @staticmethod
    def _result(binding: WindowBinding, kind: str, value: str) -> KeyboardResult:
        return KeyboardResult(
            hwnd=binding.hwnd,
            identity_token=binding.identity_token,
            layout_token=binding.layout_token,
            kind=kind,
            value=value,
        )
