"""
The battery-saver reader, with ``GetSystemPowerStatus`` replaced by a fake that
fills the structure the way Windows documents it. No test calls Windows.
"""

from __future__ import annotations

import ast
import ctypes
from pathlib import Path

import pytest

from core import windows_power_status
from core.windows_power_status import SystemPowerStatus, read_battery_saver


def windows_reports(flag: int, succeeded: int = 1):
    calls = []

    def get_system_power_status(pointer) -> int:
        status = ctypes.cast(pointer, ctypes.POINTER(SystemPowerStatus)).contents
        status.ACLineStatus = 0
        status.BatteryLifePercent = 55
        status.SystemStatusFlag = flag
        calls.append(flag)
        return succeeded

    get_system_power_status.calls = calls  # type: ignore[attr-defined]
    return get_system_power_status


def test_saver_on_and_off_are_read_from_the_status_flag():
    assert read_battery_saver(windows_reports(1)) is True
    assert read_battery_saver(windows_reports(0)) is False


@pytest.mark.parametrize("flag", [2, 127, 255])
def test_undocumented_flag_values_are_unknown_not_off(flag):
    assert read_battery_saver(windows_reports(flag)) is None


def test_a_failed_call_is_unknown_even_if_the_flag_looks_set():
    assert read_battery_saver(windows_reports(1, succeeded=0)) is None


def test_a_raising_call_is_unknown():
    def broken(pointer) -> int:
        raise OSError("call failed")

    assert read_battery_saver(broken) is None


def test_the_structure_matches_winbase_h():
    # BYTE x4 then DWORD x2: 12 bytes, flag at offset 3.
    assert ctypes.sizeof(SystemPowerStatus) == 12
    assert SystemPowerStatus.SystemStatusFlag.offset == 3


def test_nothing_is_read_off_windows(monkeypatch):
    monkeypatch.setattr(windows_power_status.sys, "platform", "linux")
    assert windows_power_status._kernel32_get_system_power_status() is None
    assert read_battery_saver() is None


def test_the_module_only_calls_get_system_power_status():
    source = Path(windows_power_status.__file__).read_text(encoding="utf-8")
    called = {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "kernel32"
    }
    assert called == {"GetSystemPowerStatus"}
    for forbidden in ("SetSystemPowerState", "SetThreadExecutionState", "PowerSetActiveScheme", "subprocess"):
        assert forbidden not in source
