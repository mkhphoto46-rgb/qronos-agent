"""
Power and disk readings, with every operating-system call injected.

Nothing here reads the machine the tests run on: batteries are SimpleNamespace
objects shaped like ``psutil.sensors_battery()`` and disks are fake
``shutil.disk_usage`` results.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core import environment_signals as env
from core.environment_signals import (
    DiskReading,
    EnvironmentReader,
    EnvironmentReading,
    EnvironmentThresholds,
    PowerReading,
    environment_decision,
    read_disk,
    read_power,
)
from core.resource_policy import ResourceDecision

MIB = 1024 * 1024


def battery(percent, plugged):
    return lambda: SimpleNamespace(percent=percent, power_plugged=plugged, secsleft=0)


def no_saver():
    return None


def usage(free_mb, total_mb=100_000):
    return lambda path: SimpleNamespace(free=free_mb * MIB, total=total_mb * MIB, used=0)


# ----------------------------------------------------------------- power


def test_desktop_without_a_battery_is_known_not_unknown():
    reading = read_power(battery=lambda: None, saver=no_saver)
    assert reading == PowerReading(available=True, has_battery=False, on_battery=False, battery_saver=None)
    assert not reading.discharging


def test_unplugged_laptop_is_discharging_with_its_share():
    reading = read_power(battery=battery(42, False), saver=lambda: True)
    assert reading.available and reading.has_battery and reading.on_battery
    assert reading.battery_percent == 42.0 and reading.battery_saver is True
    assert reading.discharging


def test_plugged_in_laptop_is_not_discharging():
    reading = read_power(battery=battery(5, True), saver=no_saver)
    assert reading.on_battery is False and not reading.discharging


def test_unknown_plug_state_is_recorded_as_unknown_not_guessed():
    reading = read_power(battery=battery(30, None), saver=no_saver)
    assert reading.has_battery is True and reading.on_battery is None
    assert not reading.discharging


@pytest.mark.parametrize("percent", [-1, 101, "50", None, float("nan")])
def test_implausible_battery_share_is_dropped(percent):
    assert read_power(battery=battery(percent, False), saver=no_saver).battery_percent is None


def test_failed_power_read_is_unavailable_and_never_raises():
    def broken():
        raise OSError("power API failed")

    reading = read_power(battery=broken, saver=no_saver)
    assert reading == PowerReading(available=False)
    assert EnvironmentReading(power=reading).power_unknown


def test_failed_saver_read_does_not_lose_the_battery_reading():
    def broken():
        raise OSError("no saver flag")

    reading = read_power(battery=battery(80, False), saver=broken)
    assert reading.battery_percent == 80.0 and reading.battery_saver is None


def test_battery_saver_comes_from_the_injected_reader():
    assert env.no_battery_saver_reading() is None
    assert read_power(battery=battery(90, False), saver=no_saver).battery_saver is None
    assert read_power(battery=battery(90, True), saver=lambda: True).battery_saver is True
    assert read_power(battery=battery(90, True), saver=lambda: False).battery_saver is False


def test_production_defaults_read_battery_saver_from_windows():
    import inspect

    # Checked by reference, without calling it: no test reads the real machine.
    assert inspect.signature(read_power).parameters["saver"].default is env.windows_battery_saver
    assert inspect.signature(EnvironmentReader).parameters["saver"].default is env.windows_battery_saver


def test_windows_battery_saver_delegates_to_the_read_only_helper(monkeypatch):
    from core import windows_power_status

    monkeypatch.setattr(windows_power_status, "read_battery_saver", lambda: True)
    assert env.windows_battery_saver() is True


def test_default_disk_reader_is_psutil_not_a_system_access_module():
    import ast
    import inspect

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(inspect.getsource(env)))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(ast.parse(inspect.getsource(env)))
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"subprocess", "shutil", "winreg", "ctypes"}


# ------------------------------------------------------------------ disk


def test_disk_reading_uses_the_nearest_existing_folder_and_creates_nothing(tmp_path):
    seen = []

    def disk_usage(path):
        seen.append(path)
        return SimpleNamespace(free=4096 * MIB, total=10_000 * MIB, used=0)

    missing = tmp_path / "not" / "yet" / "created"
    reading = read_disk("data", missing, disk_usage)
    assert reading == DiskReading(label="data", available=True, free_mb=4096, total_mb=10_000)
    assert seen == [str(tmp_path)]
    assert not (tmp_path / "not").exists()


def test_unreadable_disk_is_unavailable_and_never_raises(tmp_path):
    def broken(path):
        raise PermissionError("denied")

    assert read_disk("data", tmp_path, broken) == DiskReading(label="data", available=False)


def test_tightest_readable_volume_decides_and_an_unreadable_one_is_flagged():
    reading = EnvironmentReading(
        power=PowerReading(available=True, has_battery=False, on_battery=False),
        disks=(
            DiskReading("data", True, 9000, 50_000),
            DiskReading("history", True, 3000, 50_000),
            DiskReading("backups", False),
        ),
    )
    assert reading.disk_free_mb == 3000
    assert reading.disk_unknown


def test_no_watched_volume_is_unknown_rather_than_plenty():
    reading = EnvironmentReading(power=PowerReading(available=True, has_battery=False, on_battery=False))
    assert reading.disk_free_mb is None and reading.disk_unknown


def test_reader_takes_a_fresh_reading_every_call(tmp_path):
    levels = iter([90, 60, 12])
    reader = EnvironmentReader(
        watch_paths=lambda: (("data", tmp_path),),
        battery=lambda: SimpleNamespace(percent=next(levels), power_plugged=False),
        saver=no_saver,
        disk_usage=usage(50_000),
    )
    assert [reader().power.battery_percent for _ in range(3)] == [90.0, 60.0, 12.0]


def test_reader_survives_a_failing_path_resolver():
    def broken():
        raise RuntimeError("config unavailable")

    reading = EnvironmentReader(watch_paths=broken, battery=lambda: None, saver=no_saver)()
    assert reading.disks == () and reading.disk_unknown and not reading.power_unknown


# --------------------------------------------------------------- verdict


def make(power=None, free_mb=None):
    disks = () if free_mb is None else (DiskReading("data", True, free_mb, 100_000),)
    return EnvironmentReading(
        power=power or PowerReading(available=True, has_battery=False, on_battery=False),
        disks=disks,
    )


def on_battery(percent, saver=None):
    return PowerReading(available=True, has_battery=True, on_battery=True, battery_percent=percent, battery_saver=saver)


@pytest.mark.parametrize(
    "reading, expected",
    [
        (make(free_mb=50_000), ResourceDecision.ALLOW),
        (make(on_battery(80), 50_000), ResourceDecision.WARN),
        (make(on_battery(20), 50_000), ResourceDecision.BLOCK),
        (make(on_battery(21), 50_000), ResourceDecision.WARN),
        (make(PowerReading(True, True, False, 5.0, True), 50_000), ResourceDecision.WARN),
        (make(free_mb=10_239), ResourceDecision.WARN),
        (make(free_mb=10_240), ResourceDecision.ALLOW),
        (make(free_mb=5_119), ResourceDecision.BLOCK),
        (make(PowerReading(available=False)), ResourceDecision.ALLOW),
        (make(PowerReading(True, True, None, 3.0)), ResourceDecision.ALLOW),
    ],
)
def test_environment_verdict(reading, expected):
    assert environment_decision(reading) is expected


def test_thresholds_refuse_inconsistent_lines():
    with pytest.raises(ValueError):
        EnvironmentThresholds(disk_low_free_mb=20_000, disk_warn_free_mb=10_000)
    with pytest.raises(ValueError):
        EnvironmentThresholds(battery_low_percent=0)


def test_default_watch_path_is_the_qronos_data_root():
    from core.config import CONFIG

    assert env.default_watch_paths() == (("data", CONFIG.paths.data),)
    assert isinstance(CONFIG.paths.data, Path)
