"""
Battery saver, read from Windows. Read-only: nothing here changes the machine.

psutil reports whether a laptop is on battery and how much charge is left, but
not whether Windows battery saver is on — the state in which the user (or
Windows on their behalf) has asked everything to use less power. Windows
reports it through ``GetSystemPowerStatus``: the ``SystemStatusFlag`` byte of
``SYSTEM_POWER_STATUS`` is 1 while battery saver is on and 0 while it is off.

That is the only call made. It takes a pointer to a structure this module owns
and fills it; it has no side effects, needs no privileges and starts nothing.
Any other value, any failure, or any platform other than Windows reads as
"not known", never as "off".

The Win32 function is injectable, so tests fill the structure themselves and
no test calls Windows.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Callable, Optional


class SystemPowerStatus(ctypes.Structure):
    """``SYSTEM_POWER_STATUS`` from winbase.h, field for field."""

    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("SystemStatusFlag", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


#: ``BOOL GetSystemPowerStatus(LPSYSTEM_POWER_STATUS)``: fills the structure
#: behind the pointer and returns non-zero on success.
PowerStatusCall = Callable[[object], int]

_SAVER_OFF = 0
_SAVER_ON = 1


def _kernel32_get_system_power_status() -> Optional[PowerStatusCall]:
    """The real Win32 function, typed; None off Windows or if it cannot load."""
    if sys.platform != "win32":
        return None

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GetSystemPowerStatus
    except (OSError, AttributeError):
        return None

    function.argtypes = [ctypes.POINTER(SystemPowerStatus)]
    function.restype = wintypes.BOOL

    return function


def read_battery_saver(call: Optional[PowerStatusCall] = None) -> Optional[bool]:
    """
    True while Windows battery saver is on, False while it is off, else None.

    ``call`` stands in for ``GetSystemPowerStatus``; when omitted the real
    function is used on Windows and nothing is read elsewhere.
    """
    function = call if call is not None else _kernel32_get_system_power_status()

    if function is None:
        return None

    status = SystemPowerStatus()

    try:
        succeeded = function(ctypes.byref(status))
    except Exception:
        return None

    if not succeeded:
        return None

    flag = int(status.SystemStatusFlag) & 0xFF

    if flag == _SAVER_ON:
        return True

    if flag == _SAVER_OFF:
        return False

    # Documented values are 0 and 1. Anything else is not something to
    # guess about.
    return None
