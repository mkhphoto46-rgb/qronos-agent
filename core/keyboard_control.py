from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable

from core.input_safety import BoundInputAuthorization, BoundInputGuard
from core.window_binding import WindowBinder, WindowBinding
from core.windows_input_native import send_unicode_text, send_virtual_key, virtual_key_code


TextFn = Callable[[str], None]
KeyFn = Callable[[str, bool], None]
SleepFn = Callable[[float], None]


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
        sleep_fn: SleepFn = time.sleep,
        default_text_interval_seconds: float = 0.0,
    ) -> None:
        self._guard = BoundInputGuard(
            binder
        )
        self._text = text_fn
        self._key = key_fn
        self._sleep = sleep_fn

        interval = float(
            default_text_interval_seconds
        )

        if (
            not math.isfinite(
                interval
            )
            or interval < 0
        ):
            raise ValueError(
                "default_text_interval_seconds must be finite and non-negative."
            )

        self._default_text_interval_seconds = interval

    def type_text(
        self,
        binding: WindowBinding,
        text: str,
        *,
        authorization: BoundInputAuthorization,
        interval_seconds: float | None = None,
    ) -> KeyboardResult:
        value = str(
            text
        )

        if not value:
            raise ValueError(
                "type_text requires non-empty text."
            )

        delay = (
            self._default_text_interval_seconds
            if interval_seconds is None
            else float(
                interval_seconds
            )
        )

        if (
            not math.isfinite(
                delay
            )
            or delay < 0
        ):
            raise ValueError(
                "interval_seconds must be finite and non-negative."
            )

        self._guard.revalidate(
            binding,
            authorization=authorization,
        )

        if delay == 0:
            try:
                self._text(
                    value
                )
            finally:
                self._guard.note_state_change(binding)

        else:
            for index, char in enumerate(
                value
            ):
                self._guard.revalidate(
                    binding,
                    authorization=authorization,
                )

                try:
                    self._text(
                        char
                    )
                finally:
                    self._guard.note_state_change(binding)

                if (
                    index + 1
                    < len(
                        value
                    )
                ):
                    self._sleep(
                        delay
                    )

        self._guard.revalidate(
            binding,
            authorization=authorization,
        )

        return self._result(
            binding,
            "text",
            value,
        )

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
        try:
            self._key(name, True)
            self._key(name, False)
        finally:
            self._guard.note_state_change(binding)
        self._guard.revalidate(binding, authorization=authorization)
        return self._result(binding, "key", name)

    def press_chord(
        self,
        binding: WindowBinding,
        keys: tuple[str, ...],
        *,
        authorization: BoundInputAuthorization,
    ) -> KeyboardResult:
        names = tuple(
            str(
                key
            )
            .strip()
            .lower()
            for key in keys
        )

        if (
            len(
                names
            )
            < 2
            or len(
                names
            )
            > 4
        ):
            raise ValueError(
                "A keyboard chord requires between 2 and 4 keys."
            )

        if (
            any(
                not name
                for name in names
            )
            or len(
                set(
                    names
                )
            )
            != len(
                names
            )
        ):
            raise ValueError(
                "Keyboard chord keys must be unique non-empty names."
            )

        for name in names:
            virtual_key_code(
                name
            )

        self._guard.revalidate(
            binding,
            authorization=authorization,
        )

        pressed: list[str] = []

        try:
            for name in names[:-1]:
                self._key(
                    name,
                    True,
                )
                pressed.append(
                    name
                )

            final = names[-1]

            self._key(
                final,
                True,
            )

            pressed.append(
                final
            )

            self._key(
                final,
                False,
            )

            pressed.pop()

            while pressed:
                name = pressed.pop()

                self._key(
                    name,
                    False,
                )

        finally:
            while pressed:
                name = pressed.pop()

                try:
                    self._key(
                        name,
                        False,
                    )
                except Exception:
                    pass

            self._guard.note_state_change(binding)

        self._guard.revalidate(
            binding,
            authorization=authorization,
        )

        return self._result(
            binding,
            "chord",
            "+".join(
                names
            ),
        )

    @staticmethod
    def _result(binding: WindowBinding, kind: str, value: str) -> KeyboardResult:
        return KeyboardResult(
            hwnd=binding.hwnd,
            identity_token=binding.identity_token,
            layout_token=binding.layout_token,
            kind=kind,
            value=value,
        )
