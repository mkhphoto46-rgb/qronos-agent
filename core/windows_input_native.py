from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


class NativeInputUnavailable(RuntimeError):
    """Windows SendInput is unavailable or rejected an input event."""


# SendInput structures. ULONG_PTR must match pointer width on both x86/x64.
_ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

_MOUSE_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


_VIRTUAL_KEYS = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "escape": 0x1B,
    "space": 0x20,
    "page_up": 0x21,
    "page_down": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "delete": 0x2E,
}


def _user32():
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        raise NativeInputUnavailable("Native input control is available on Windows only.")
    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    return user32


def _send_one(value: _INPUT) -> None:
    user32 = _user32()
    sent = int(user32.SendInput(1, ctypes.byref(value), ctypes.sizeof(_INPUT)))
    if sent != 1:
        raise NativeInputUnavailable("Windows SendInput rejected the input event.")


def virtual_desktop_bounds() -> tuple[int, int, int, int]:
    user32 = _user32()
    left = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    top = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    width = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    height = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    if width <= 0 or height <= 0:
        raise NativeInputUnavailable("Windows reported an invalid virtual desktop size.")
    return left, top, width, height


def absolute_mouse_coordinates(
    x: int,
    y: int,
    *,
    bounds: tuple[int, int, int, int],
) -> tuple[int, int]:
    left, top, width, height = (int(part) for part in bounds)
    if width <= 0 or height <= 0:
        raise ValueError("Virtual desktop bounds must have positive width and height.")
    if not (left <= int(x) < left + width and top <= int(y) < top + height):
        raise ValueError("Mouse coordinate is outside the virtual desktop.")

    # Microsoft documents absolute mouse coordinates on the inclusive 0..65535
    # range. width-1/height-1 maps the final physical pixel exactly to 65535.
    dx = 0 if width == 1 else round((int(x) - left) * 65535 / (width - 1))
    dy = 0 if height == 1 else round((int(y) - top) * 65535 / (height - 1))
    return int(dx), int(dy)


def send_mouse_move_absolute(x: int, y: int) -> None:
    bounds = virtual_desktop_bounds()
    dx, dy = absolute_mouse_coordinates(x, y, bounds=bounds)
    event = _INPUT(
        type=INPUT_MOUSE,
        mi=_MOUSEINPUT(
            dx=dx,
            dy=dy,
            mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            time=0,
            dwExtraInfo=0,
        ),
    )
    _send_one(event)


def send_mouse_button(button: str, down: bool) -> None:
    name = str(button or "").strip().lower()
    try:
        down_flag, up_flag = _MOUSE_BUTTON_FLAGS[name]
    except KeyError as exc:
        raise ValueError(f"Unsupported mouse button: {button!r}.") from exc
    event = _INPUT(
        type=INPUT_MOUSE,
        mi=_MOUSEINPUT(
            dx=0,
            dy=0,
            mouseData=0,
            dwFlags=down_flag if down else up_flag,
            time=0,
            dwExtraInfo=0,
        ),
    )
    _send_one(event)


def virtual_key_code(key: str) -> int:
    name = str(key or "").strip().lower()
    if len(name) == 1 and "a" <= name <= "z":
        return ord(name.upper())
    if len(name) == 1 and "0" <= name <= "9":
        return ord(name)
    if name.startswith("f") and name[1:].isdigit():
        number = int(name[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    try:
        return _VIRTUAL_KEYS[name]
    except KeyError as exc:
        raise ValueError(f"Unsupported keyboard key: {key!r}.") from exc


def send_virtual_key(key: str, down: bool) -> None:
    vk = virtual_key_code(key)
    event = _INPUT(
        type=INPUT_KEYBOARD,
        ki=_KEYBDINPUT(
            wVk=vk,
            wScan=0,
            dwFlags=0 if down else KEYEVENTF_KEYUP,
            time=0,
            dwExtraInfo=0,
        ),
    )
    _send_one(event)


def send_unicode_text(text: str) -> None:
    raw = str(text)
    if not raw:
        return
    # KEYEVENTF_UNICODE consumes UTF-16 code units, not Python code points.
    encoded = raw.encode("utf-16-le", "strict")
    for offset in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[offset : offset + 2], "little")
        down = _INPUT(
            type=INPUT_KEYBOARD,
            ki=_KEYBDINPUT(
                wVk=0,
                wScan=unit,
                dwFlags=KEYEVENTF_UNICODE,
                time=0,
                dwExtraInfo=0,
            ),
        )
        up = _INPUT(
            type=INPUT_KEYBOARD,
            ki=_KEYBDINPUT(
                wVk=0,
                wScan=unit,
                dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                time=0,
                dwExtraInfo=0,
            ),
        )
        _send_one(down)
        _send_one(up)
