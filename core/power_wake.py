"""Wake THIS PC from sleep at a set time with a hardware wake timer (requirement H3), and say truthfully whether it can.

A sleeping PC runs nothing: not Qronos's voice, wake word or brain. What can bring it back is hardware: the
real-time clock / always-on timer that Windows programs for a waitable timer created to resume the system. This
module uses only documented Windows power APIs, in process (no helper, script, service, network or setting change):

* capability, read-only: GetPwrCapabilities (SYSTEM_POWER_CAPABILITIES, laid out from the SDK's winnt.h 10.0.26100),
  the power plan's "Allow wake timers" (GUID_SLEEP_SUBGROUP / GUID_ALLOW_RTC_WAKE via PowerGetActiveScheme and
  PowerReadAC/DCValueIndex: 0 off, 1 on, 2 important timers only) and the power source (GetSystemPowerStatus);
* arming: CreateWaitableTimerExW + SetWaitableTimer(fResume=TRUE) on a dedicated thread that stays alive while the
  timer is armed (Windows cancels a timer whose setting thread exits). Windows accepting the call is "armed"; if it
  succeeds with ERROR_NOT_SUPPORTED, Windows says this system cannot resume on a timer: "not_supported";
* when the timer is signalled: SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) for the approved
  stay-awake minutes, then cleared; the event says the timer fired, never that the PC "woke" (it may have been on).

What it cannot promise (said in the product): the PC must not be shut down, hibernated without wake support or
signed out, and Qronos must keep running; on Modern Standby the processor wakes but Windows keeps the display off.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

ERROR_NOT_SUPPORTED = 50
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 0x102
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
TIMER_ALL_ACCESS = 0x1F0003
POWER_SYSTEM_SLEEPING1 = 2  # SYSTEM_POWER_STATE: PowerSystemSleeping1..3 = 2..4, Hibernate = 5
GUID_SLEEP_SUBGROUP = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
GUID_ALLOW_RTC_WAKE = "bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d"
WAKE_TIMERS_SETTING = {0: "disabled", 1: "enabled", 2: "important_only"}
POLL_MS = 1000
MAX_STAY_AWAKE_MINUTES = 120


class PowerWakeError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


# ------------------------------------------------------------------ capability (the verdict is pure)


@dataclass(frozen=True)
class WakeCapability:
    sleep_s3: bool
    hibernate_s4: bool
    modern_standby: bool
    rtc_wake_state: int  # SYSTEM_POWER_STATE; 0 = unspecified (no RTC wake reported)
    wake_alarm_present: bool
    wake_timers_ac: str | None  # "enabled" | "disabled" | "important_only" | None (unreadable)
    wake_timers_dc: str | None
    power_source: str | None  # "ac" | "battery" | None

    @property
    def platform_can_wake(self) -> bool:
        # Modern Standby platforms must have an always-on timer that wakes the SoC (Microsoft's wake-source rules);
        # otherwise the firmware must report an RTC wake from a sleeping state.
        return self.modern_standby or self.rtc_wake_state >= POWER_SYSTEM_SLEEPING1

    def verdict(self) -> tuple[str, str]:
        """("supported" | "blocked_by_power_plan" | "not_supported" | "unknown", reason code)."""
        if not self.platform_can_wake:
            return "not_supported", "no_rtc_or_always_on_timer_wake"
        current = self.wake_timers_dc if self.power_source == "battery" else self.wake_timers_ac
        if current is None:
            return "unknown", "wake_timer_setting_unreadable"
        if current == "disabled":
            return "blocked_by_power_plan", "wake_timers_disabled"
        if current == "important_only":
            return "blocked_by_power_plan", "wake_timers_important_only"
        return "supported", "wake_timers_enabled"

    def to_json(self) -> dict[str, Any]:
        verdict, reason = self.verdict()
        return {"verdict": verdict, "reason": reason, "sleepS3": self.sleep_s3, "hibernateS4": self.hibernate_s4,
                "modernStandby": self.modern_standby, "rtcWakeState": self.rtc_wake_state,
                "wakeAlarmPresent": self.wake_alarm_present, "wakeTimersAc": self.wake_timers_ac,
                "wakeTimersDc": self.wake_timers_dc, "powerSource": self.power_source,
                "displayStaysOffOnModernStandby": self.modern_standby}


# ------------------------------------------------------------------ the armed timer (pure logic over an injected API)


class TimerApi(Protocol):
    """The few kernel32 calls the armed timer needs; WinTimerApi is the real one, tests inject a fake."""

    def create_timer(self) -> int: ...
    def set_timer(self, handle: int, due_filetime: int) -> tuple[bool, int]: ...  # (ok, last_error)
    def wait(self, handle: int, timeout_ms: int) -> int: ...
    def cancel_timer(self, handle: int) -> None: ...
    def close(self, handle: int) -> None: ...
    def execution_state(self, flags: int) -> None: ...


def filetime_utc(when: datetime) -> int:
    """An absolute FILETIME (100 ns since 1601-01-01 UTC) for an aware datetime."""
    if when.tzinfo is None:
        raise ValueError("an aware datetime is required")
    return int(round((when.astimezone(timezone.utc).timestamp() + 11_644_473_600) * 10_000_000))


@dataclass(frozen=True)
class ArmResult:
    state: str  # "armed" | "not_supported"
    wake_id: str
    due_utc: str


class ArmedWake:
    """One armed wake timer on its own thread (the thread that sets a timer must outlive it)."""

    def __init__(self, api: TimerApi, due: datetime, stay_awake_minutes: int,
                 on_event: Callable[[str, dict[str, Any]], None], *, sleep: Callable[[float], None] = time.sleep) -> None:
        if not 0 <= stay_awake_minutes <= MAX_STAY_AWAKE_MINUTES:
            raise ValueError("stay_awake_minutes out of range")
        self.api, self.due, self.stay = api, due, stay_awake_minutes
        self.wake_id = "wk-" + uuid.uuid4().hex[:16]
        self._on_event, self._sleep = on_event, sleep
        self._cancel = threading.Event()
        self._ready = threading.Event()
        self._result: ArmResult | PowerWakeError | None = None
        self.thread = threading.Thread(target=self._run, name="qronos-wake-timer", daemon=True)

    def arm(self, timeout: float = 10.0) -> ArmResult:
        self.thread.start()
        if not self._ready.wait(timeout):
            self._cancel.set()
            raise PowerWakeError("timeout", "Windows did not answer the wake-timer request in time")
        if isinstance(self._result, PowerWakeError):
            raise self._result
        assert isinstance(self._result, ArmResult)
        return self._result

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def active(self) -> bool:
        return self.thread.is_alive() and not self._cancel.is_set()

    def _run(self) -> None:
        due_text = self.due.astimezone(timezone.utc).isoformat(timespec="seconds")
        try:
            handle = self.api.create_timer()
        except Exception as error:  # noqa: BLE001 - reported to the caller, never raised in the thread
            self._result = PowerWakeError("native_refused", f"CreateWaitableTimerEx failed ({type(error).__name__})")
            self._ready.set()
            return
        try:
            ok, last_error = self.api.set_timer(handle, filetime_utc(self.due))
            if not ok:
                self._result = PowerWakeError("native_refused", f"SetWaitableTimer failed ({last_error})")
                return
            if last_error == ERROR_NOT_SUPPORTED:  # documented: succeeds, but the system cannot resume on it
                self.api.cancel_timer(handle)
                self._result = ArmResult("not_supported", self.wake_id, due_text)
                return
            self._result = ArmResult("armed", self.wake_id, due_text)
            self._ready.set()
            while not self._cancel.is_set():
                if self.api.wait(handle, POLL_MS) == WAIT_OBJECT_0:
                    self._fired()
                    return
            self.api.cancel_timer(handle)
            self._on_event("wake_cancelled", {"wakeId": self.wake_id})
        finally:
            self._ready.set()
            self.api.close(handle)

    def _fired(self) -> None:
        if self.stay:
            self.api.execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)  # keep the system from idling back to sleep
        self._on_event("wake_timer_fired", {"wakeId": self.wake_id, "firedAtUtc": datetime.now(timezone.utc)
                                            .isoformat(timespec="seconds"), "stayAwakeMinutes": self.stay})
        try:
            deadline = time.monotonic() + self.stay * 60
            while self.stay and not self._cancel.is_set() and time.monotonic() < deadline:
                self._sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        finally:
            if self.stay:
                self.api.execution_state(ES_CONTINUOUS)  # back to normal idle behaviour


# ------------------------------------------------------------------ Windows


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class _BatteryReportingScale(ctypes.Structure):
    _fields_ = [("Granularity", wintypes.DWORD), ("Capacity", wintypes.DWORD)]


class SystemPowerCapabilities(ctypes.Structure):
    """SYSTEM_POWER_CAPABILITIES (winnt.h, NTDDI >= WINTHRESHOLD), field for field: 76 bytes."""

    _fields_ = [(name, ctypes.c_ubyte) for name in (
        "PowerButtonPresent", "SleepButtonPresent", "LidPresent", "SystemS1", "SystemS2", "SystemS3", "SystemS4",
        "SystemS5", "HiberFilePresent", "FullWake", "VideoDimPresent", "ApmPresent", "UpsPresent", "ThermalControl",
        "ProcessorThrottle", "ProcessorMinThrottle", "ProcessorMaxThrottle", "FastSystemS4", "Hiberboot",
        "WakeAlarmPresent", "AoAc", "DiskSpinDown", "HiberFileType", "AoAcConnectivitySupported")] + [
        ("spare3", ctypes.c_ubyte * 6), ("SystemBatteriesPresent", ctypes.c_ubyte),
        ("BatteriesAreShortTerm", ctypes.c_ubyte), ("BatteryScale", _BatteryReportingScale * 3),
        ("AcOnLineWake", ctypes.c_int), ("SoftLidWake", ctypes.c_int), ("RtcWake", ctypes.c_int),
        ("MinDeviceWakeState", ctypes.c_int), ("DefaultLowLatencyWake", ctypes.c_int)]


class WinTimerApi:
    def __init__(self) -> None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateWaitableTimerExW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        k.CreateWaitableTimerExW.restype = wintypes.HANDLE
        k.SetWaitableTimer.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong), wintypes.LONG,
                                       ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL]
        k.SetWaitableTimer.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.CancelWaitableTimer.argtypes = [wintypes.HANDLE]
        k.CancelWaitableTimer.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.SetThreadExecutionState.argtypes = [wintypes.DWORD]
        k.SetThreadExecutionState.restype = wintypes.DWORD
        self._k = k

    def create_timer(self) -> int:
        handle = self._k.CreateWaitableTimerExW(None, None, 0, TIMER_ALL_ACCESS)  # unnamed, synchronization timer
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateWaitableTimerExW failed")
        return int(handle)

    def set_timer(self, handle: int, due_filetime: int) -> tuple[bool, int]:
        due = ctypes.c_longlong(due_filetime)  # positive = absolute UTC FILETIME
        ctypes.set_last_error(0)
        ok = bool(self._k.SetWaitableTimer(handle, ctypes.byref(due), 0, None, None, True))  # fResume = TRUE
        return ok, ctypes.get_last_error()

    def wait(self, handle: int, timeout_ms: int) -> int:
        return int(self._k.WaitForSingleObject(handle, timeout_ms))

    def cancel_timer(self, handle: int) -> None:
        self._k.CancelWaitableTimer(handle)

    def close(self, handle: int) -> None:
        self._k.CloseHandle(handle)

    def execution_state(self, flags: int) -> None:
        self._k.SetThreadExecutionState(flags)


class WinPowerWake:
    """The real power layer: read-only capability and wake timers on this PC (constructed only when requested)."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise PowerWakeError("unavailable", "hardware wake timers are only available on Windows")
        self._timers = WinTimerApi()

    def capability(self) -> WakeCapability:
        powrprof = ctypes.WinDLL("powrprof")
        kernel = ctypes.WinDLL("kernel32")
        caps = SystemPowerCapabilities()
        powrprof.GetPwrCapabilities.argtypes = [ctypes.POINTER(SystemPowerCapabilities)]
        powrprof.GetPwrCapabilities.restype = ctypes.c_ubyte
        if not powrprof.GetPwrCapabilities(ctypes.byref(caps)):
            raise PowerWakeError("capability_unreadable", "GetPwrCapabilities failed")
        ac, dc = self._wake_timer_setting(powrprof, kernel)
        from core.windows_power_status import SystemPowerStatus

        status = SystemPowerStatus()
        kernel.GetSystemPowerStatus.argtypes = [ctypes.POINTER(SystemPowerStatus)]
        kernel.GetSystemPowerStatus.restype = wintypes.BOOL
        source = None
        if kernel.GetSystemPowerStatus(ctypes.byref(status)):
            source = {0: "battery", 1: "ac"}.get(int(status.ACLineStatus) & 0xFF)
        return WakeCapability(bool(caps.SystemS3), bool(caps.SystemS4), bool(caps.AoAc), int(caps.RtcWake),
                              bool(caps.WakeAlarmPresent), ac, dc, source)

    @staticmethod
    def _wake_timer_setting(powrprof, kernel) -> tuple[str | None, str | None]:
        scheme = ctypes.POINTER(_GUID)()
        powrprof.PowerGetActiveScheme.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_GUID))]
        powrprof.PowerGetActiveScheme.restype = wintypes.DWORD
        if powrprof.PowerGetActiveScheme(None, ctypes.byref(scheme)) != 0 or not scheme:
            return None, None
        values: list[str | None] = []
        try:
            for name in ("PowerReadACValueIndex", "PowerReadDCValueIndex"):
                function = getattr(powrprof, name)
                function.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(_GUID),
                                     ctypes.POINTER(_GUID), ctypes.POINTER(wintypes.DWORD)]
                function.restype = wintypes.DWORD
                value = wintypes.DWORD()
                status = function(None, scheme, ctypes.byref(_GUID.parse(GUID_SLEEP_SUBGROUP)),
                                  ctypes.byref(_GUID.parse(GUID_ALLOW_RTC_WAKE)), ctypes.byref(value))
                values.append(WAKE_TIMERS_SETTING.get(int(value.value)) if status == 0 else None)
        finally:
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(scheme)
        return values[0], values[1]

    def arm(self, due: datetime, stay_awake_minutes: int,
            on_event: Callable[[str, dict[str, Any]], None]) -> tuple[ArmedWake, ArmResult]:
        armed = ArmedWake(self._timers, due, stay_awake_minutes, on_event)
        return armed, armed.arm()


def default_power() -> WinPowerWake:
    return WinPowerWake()
