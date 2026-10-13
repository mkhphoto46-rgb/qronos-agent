"""The hardware wake timer of this PC (core/power_wake.py): the capability verdict, the armed-timer thread over a
fake kernel32, and the native layouts. No real power API is called: the timer API is a recording fake, and the real
WinPowerWake is replaced by the live-device boundary in every test."""
from __future__ import annotations

import ast
import ctypes
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import power_wake
from core.power_wake import (ERROR_NOT_SUPPORTED, ES_CONTINUOUS, ES_SYSTEM_REQUIRED, WAIT_OBJECT_0, WAIT_TIMEOUT,
                             ArmedWake, PowerWakeError, SystemPowerCapabilities, WakeCapability, filetime_utc)


def capability(**overrides) -> WakeCapability:
    values = {"sleep_s3": True, "hibernate_s4": True, "modern_standby": False, "rtc_wake_state": 4,
              "wake_alarm_present": True, "wake_timers_ac": "enabled", "wake_timers_dc": "disabled",
              "power_source": "ac"}
    values.update(overrides)
    return WakeCapability(**values)


@pytest.mark.parametrize("overrides, verdict, reason", [
    ({}, "supported", "wake_timers_enabled"),
    ({"power_source": "battery"}, "blocked_by_power_plan", "wake_timers_disabled"),  # the DC value applies
    ({"wake_timers_ac": "important_only"}, "blocked_by_power_plan", "wake_timers_important_only"),
    ({"wake_timers_ac": None}, "unknown", "wake_timer_setting_unreadable"),
    ({"rtc_wake_state": 0}, "not_supported", "no_rtc_or_always_on_timer_wake"),
    ({"rtc_wake_state": 1}, "not_supported", "no_rtc_or_always_on_timer_wake"),  # "working" is not a sleep state
    ({"rtc_wake_state": 0, "modern_standby": True, "sleep_s3": False}, "supported", "wake_timers_enabled"),
])
def test_the_verdict_follows_the_hardware_and_the_power_plan_only(overrides, verdict, reason):
    found = capability(**overrides)
    assert found.verdict() == (verdict, reason)
    shown = found.to_json()
    assert shown["verdict"] == verdict and shown["displayStaysOffOnModernStandby"] is found.modern_standby


def test_the_native_layouts_match_the_sdk():
    assert ctypes.sizeof(SystemPowerCapabilities) == 76  # winnt.h SYSTEM_POWER_CAPABILITIES (NTDDI >= WINTHRESHOLD)
    assert SystemPowerCapabilities.AoAc.offset == 20 and SystemPowerCapabilities.WakeAlarmPresent.offset == 19
    assert SystemPowerCapabilities.BatteryScale.offset == 32 and SystemPowerCapabilities.RtcWake.offset == 64
    when = datetime(2026, 10, 14, 6, 30, tzinfo=timezone.utc)
    assert filetime_utc(datetime(1970, 1, 1, tzinfo=timezone.utc)) == 116_444_736_000_000_000  # the Unix epoch
    assert filetime_utc(when) == 134_364_330_000_000_000  # 100 ns since 1601-01-01 UTC
    assert filetime_utc(when.astimezone(timezone(timedelta(hours=3, minutes=30)))) == filetime_utc(when)
    with pytest.raises(ValueError):
        filetime_utc(datetime(2026, 10, 14, 6, 30))  # a naive time is never guessed


class FakeTimerApi:
    """Records every kernel32 call; wait() answers from a script."""

    def __init__(self, set_result=(True, 0), waits=None, create_error=None) -> None:
        self.calls: list[tuple] = []
        self.set_result, self.create_error = set_result, create_error
        self.waits = list(waits or [])
        self.waiting = threading.Event()

    def create_timer(self):
        self.calls.append(("create",))
        if self.create_error:
            raise self.create_error
        return 77

    def set_timer(self, handle, due):
        self.calls.append(("set", handle, due))
        return self.set_result

    def wait(self, handle, timeout_ms):
        self.waiting.set()
        result = self.waits.pop(0) if self.waits else WAIT_TIMEOUT
        if result == WAIT_TIMEOUT:
            threading.Event().wait(0.01)
        return result

    def cancel_timer(self, handle):
        self.calls.append(("cancel", handle))

    def close(self, handle):
        self.calls.append(("close", handle))

    def execution_state(self, flags):
        self.calls.append(("execution_state", flags))


DUE = datetime(2026, 10, 14, 6, 30, tzinfo=timezone.utc)


def test_an_armed_timer_lives_on_its_own_thread_and_a_cancel_cancels_and_closes_it():
    api, events = FakeTimerApi(), []
    armed = ArmedWake(api, DUE, 10, lambda name, payload: events.append((name, payload)))
    result = armed.arm()
    assert result.state == "armed" and result.due_utc == "2026-10-14T06:30:00+00:00"
    assert api.calls[:2] == [("create",), ("set", 77, filetime_utc(DUE))]
    assert api.waiting.wait(5) and armed.active  # the setting thread is alive: Windows keeps the timer
    armed.cancel()
    armed.thread.join(5)
    assert not armed.thread.is_alive() and api.calls[-2:] == [("cancel", 77), ("close", 77)]
    assert events == [("wake_cancelled", {"wakeId": armed.wake_id})]


def test_windows_saying_it_cannot_resume_is_not_supported_never_armed():
    api = FakeTimerApi(set_result=(True, ERROR_NOT_SUPPORTED))
    armed = ArmedWake(api, DUE, 10, lambda *a: None)
    assert armed.arm().state == "not_supported"
    armed.thread.join(5)
    assert ("cancel", 77) in api.calls and api.calls[-1] == ("close", 77)


@pytest.mark.parametrize("api, code", [(FakeTimerApi(set_result=(False, 5)), "native_refused"),
                                       (FakeTimerApi(create_error=OSError(5, "denied")), "native_refused")])
def test_a_refused_native_call_is_an_error_and_nothing_stays_armed(api, code):
    armed = ArmedWake(api, DUE, 10, lambda *a: None)
    with pytest.raises(PowerWakeError) as refused:
        armed.arm()
    assert refused.value.code == code
    armed.thread.join(5)
    assert not armed.thread.is_alive()


def test_when_the_timer_fires_the_system_is_kept_awake_for_the_approved_minutes_then_released():
    api, events = FakeTimerApi(waits=[WAIT_TIMEOUT, WAIT_OBJECT_0]), []
    holder = {}

    def sleep(seconds):  # the stay-awake wait: end it as if the minutes had passed (a cancel ends it too)
        holder["armed"].cancel()

    armed = ArmedWake(api, DUE, 1, lambda name, payload: events.append((name, payload)), sleep=sleep)
    holder["armed"] = armed
    armed.arm()
    armed.thread.join(5)
    states = [call[1] for call in api.calls if call[0] == "execution_state"]
    assert states == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS]  # held, then always released
    assert [name for name, _ in events] == ["wake_timer_fired"]
    assert events[0][1]["wakeId"] == armed.wake_id and events[0][1]["stayAwakeMinutes"] == 1
    assert ("cancel", 77) not in api.calls and api.calls[-1] == ("close", 77)


def test_stay_awake_zero_never_touches_the_execution_state_and_bounds_are_enforced():
    api = FakeTimerApi(waits=[WAIT_OBJECT_0])
    armed = ArmedWake(api, DUE, 0, lambda *a: None)
    armed.arm()
    armed.thread.join(5)
    assert not [c for c in api.calls if c[0] == "execution_state"]
    with pytest.raises(ValueError):
        ArmedWake(api, DUE, power_wake.MAX_STAY_AWAKE_MINUTES + 1, lambda *a: None)


def test_the_real_power_layer_is_blocked_in_tests_and_reaches_no_network_process_or_setting_change():
    assert getattr(power_wake.WinPowerWake, "live_device_stand_in", None)
    source = Path(power_wake.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "ctypes", "os", "threading", "time", "uuid", "dataclasses", "datetime",
                        "typing", "core"}, imported
    assert set(re.findall(r'WinDLL\("([a-z0-9]+)"', source)) == {"kernel32", "powrprof"}
    for forbidden in ("PowerWrite", "PowerSetActiveScheme", "SetSuspendState", "InitiateSystemShutdown",
                      "ExitWindowsEx", "windll"):
        assert forbidden not in source, forbidden
