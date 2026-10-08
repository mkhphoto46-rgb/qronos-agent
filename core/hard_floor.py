"""
The limits no button can lift.

Everything else about the queue is politeness: Qronos waiting because the
machine is yours and you are using it. Politeness is exactly what the override
button is for — it is the user saying "be rude, I want this now", and that is a
perfectly reasonable thing to want.

This module is the other kind of limit. Two things can go wrong that are not
about manners:

    The graphics card is too hot to be given more work.

    The model does not fit. A card at 100% does not fail cleanly on Windows —
    the driver starts spilling over the PCIe bus and it is *the user's*
    application that collapses, not ours. Which means "run it anyway" would
    take down the very thing being polite about.

`docs/qronos_product_architecture.md` requires exactly this split: "Expose
conservative, balanced, and custom limits in the UI, while keeping
non-bypassable thermal and memory safety limits."

It is a separate module from ``core/load_signal.py`` because the two answer
different questions and should be readable separately. Sustained load is a
statement about time and needs a window; fitting is a binary fact about one
model and one instant, and needs no history at all.

Deliberately absent: CPU and GPU utilisation. A busy processor is a reason to
wait, not a reason it is unsafe to proceed, so they belong to politeness. That
sentence is the whole boundary between the two modules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.environment_signals import (
    BATTERY_CRITICAL_PERCENT,
    BATTERY_RESUME_PERCENT,
    DISK_CRITICAL_FREE_MB,
    DISK_RESUME_FREE_MB,
)
from core.load_signal import LoadSample


class FloorBreach(Enum):
    """Why the floor said no."""

    GPU_TEMPERATURE = "gpu_temperature"
    VRAM_EXHAUSTED = "vram_exhausted"
    SYSTEM_MEMORY = "system_memory"

    #: Running from a nearly empty battery. Loading a model drains several
    #: watts for the length of a turn, and a laptop that dies mid-write takes
    #: the conversation history with it.
    BATTERY_CRITICAL = "battery_critical"

    #: The volume Qronos writes to is nearly full. A turn that cannot write
    #: its history or journal fails half-way, and Windows needs the same
    #: space for its page file.
    DISK_CRITICAL = "disk_critical"

    #: Other applications' graphics memory grew while a Qronos model held the
    #: card, and the card has no room left for them. Qronos is the one that
    #: yields. Only meaningful for work already running; see ``check_running``.
    VRAM_SQUEEZED = "vram_squeezed"


@dataclass(frozen=True)
class HardFloorConfig:
    """The limits themselves."""

    #: Refuse at or above this. Not the 82 C in ``resource_policy`` — that is a
    #: politeness number, and a warm card usually means the user's game is
    #: working exactly as they want it to. 85 matches
    #: ``ActivityGuard.CRITICAL_GPU_TEMP_C``, the only existing critical
    #: temperature in this codebase, and sits below the hardware's own thermal
    #: slowdown. Measured maximum on the development card during normal work:
    #: 54 C.
    gpu_critical_temp_c: int = 85

    #: Stop refusing below this. The gap exists because a card that has just
    #: touched 85 will sit near 84 for a while afterwards, and this is the one
    #: limit that must never flap.
    gpu_resume_temp_c: int = 78

    #: Spare VRAM required beyond the model's own footprint. Not padding for a
    #: bad size estimate — the declared figures already exceed the measured
    #: peaks (3,482 against 3,442 for the fast brain). This covers the desktop
    #: compositor's own growth and the fact that filling a card harms the
    #: user's application rather than ours.
    vram_headroom_mb: int = 512

    #: Refuse above this. Distinct from the 85% politeness line: at 95% Windows
    #: is paging, and loading a model risks swapping out whatever the user has
    #: in front of them.
    ram_critical_percent: float = 95.0

    #: A discharging battery at or below this refuses work; once refused it
    #: must reach the resume line. A machine on its charger never trips this.
    battery_critical_percent: float = BATTERY_CRITICAL_PERCENT
    battery_resume_percent: float = BATTERY_RESUME_PERCENT

    #: Free space on the tightest watched volume below which work is refused,
    #: and the space it must recover to.
    disk_critical_free_mb: int = DISK_CRITICAL_FREE_MB
    disk_resume_free_mb: int = DISK_RESUME_FREE_MB

    #: Growth of other applications' graphics memory, since Qronos took the
    #: card, at or above which running Qronos work yields — when the card also
    #: has less than ``vram_headroom_mb`` physically free. Growth alone is not
    #: enough: an application growing into memory nobody needs harms nobody.
    #: Small, because a card a partially offloaded model has filled cannot
    #: show more growth than the little it left free; below it is noise.
    vram_squeeze_growth_mb: int = 64

    def __post_init__(self) -> None:
        if self.battery_resume_percent < self.battery_critical_percent:
            raise ValueError("The battery resume line must not be below the critical one.")

        if self.disk_resume_free_mb < self.disk_critical_free_mb:
            raise ValueError("The disk resume line must not be below the critical one.")

        if self.vram_squeeze_growth_mb <= 0 or self.vram_headroom_mb < 0:
            raise ValueError("VRAM squeeze growth must be positive and headroom non-negative.")


DEFAULT_HARD_FLOOR = HardFloorConfig()


@dataclass(frozen=True)
class FloorVerdict:
    """Whether the work may proceed, and the numbers behind the answer."""

    passed: bool
    breach: Optional[FloorBreach] = None
    required_vram_mb: int = 0
    free_vram_mb: Optional[int] = None
    gpu_temperature_c: Optional[int] = None

    #: True when there was no usable graphics reading. See :func:`check` for
    #: why this passes rather than refusing.
    gpu_unknown: bool = False

    battery_percent: Optional[float] = None
    disk_free_mb: Optional[int] = None
    external_vram_growth_mb: Optional[int] = None

    #: The power or disk reading was asked for and could not be taken. Like
    #: ``gpu_unknown``, recorded and not refused; see :func:`check`.
    power_unknown: bool = False
    disk_unknown: bool = False

    def message(self) -> str:
        """
        Why, in numbers, for a person to read.

        A refusal that says only "not enough memory" invites the user to press
        the button again. One that says how much is needed and how much there
        is does not.
        """
        if self.passed:
            return "Nothing is stopping this from running."

        if self.breach is FloorBreach.GPU_TEMPERATURE:
            return (
                f"The graphics card is at {self.gpu_temperature_c} C. Qronos "
                "waits for it to cool before adding work. This is a safety "
                "limit and cannot be overridden."
            )

        if self.breach is FloorBreach.VRAM_EXHAUSTED:
            free = (
                "none is readable"
                if self.free_vram_mb is None
                else f"{self.free_vram_mb} MB is free"
            )

            return (
                f"This needs {self.required_vram_mb} MB of graphics memory "
                f"and {free}. Running anyway would push another application "
                "out of the card, so this is a safety limit and cannot be "
                "overridden."
            )

        if self.breach is FloorBreach.BATTERY_CRITICAL:
            level = (
                "nearly empty"
                if self.battery_percent is None
                else f"at {self.battery_percent:.0f}%"
            )

            return (
                f"The battery is {level} and the machine is not plugged in. "
                "Qronos waits for the charger before starting a model. This "
                "is a safety limit and cannot be overridden."
            )

        if self.breach is FloorBreach.DISK_CRITICAL:
            free = (
                "nearly full"
                if self.disk_free_mb is None
                else f"down to {self.disk_free_mb} MB free"
            )

            return (
                f"The drive Qronos saves to is {free}. Work that cannot save "
                "its history fails half-way, so Qronos waits for space. This "
                "is a safety limit and cannot be overridden."
            )

        if self.breach is FloorBreach.VRAM_SQUEEZED:
            return (
                "Another application needs more graphics memory than is free "
                "while Qronos is using the card, so Qronos stops its own work "
                "and gives the memory back. It resumes when there is room."
            )

        return (
            "System memory is nearly full. Loading a model now would push "
            "something the user is working in out to disk. This is a safety "
            "limit and cannot be overridden."
        )


def _verdict(
    sample: LoadSample,
    required_vram_mb: int,
    breach: Optional[FloorBreach] = None,
    gpu_unknown: bool = False,
) -> FloorVerdict:
    """A verdict carrying every number the sample had, refused or not."""
    return FloorVerdict(
        passed=breach is None,
        breach=breach,
        required_vram_mb=required_vram_mb,
        free_vram_mb=sample.vram_free_mb,
        gpu_temperature_c=sample.gpu_temperature_c,
        gpu_unknown=gpu_unknown,
        battery_percent=sample.battery_percent,
        disk_free_mb=sample.disk_free_mb,
        external_vram_growth_mb=sample.external_vram_growth_mb,
        power_unknown=sample.power_unknown,
        disk_unknown=sample.disk_unknown,
    )


def _safety_breach(
    sample: LoadSample,
    config: HardFloorConfig,
    currently_refused: bool,
) -> Optional[FloorBreach]:
    """
    The limits that apply to any Qronos work, whether starting or running.

    ``currently_refused`` moves every line to its resume value, so a reading
    hovering at a limit cannot flap the answer.
    """
    if sample.ram_percent >= config.ram_critical_percent:
        return FloorBreach.SYSTEM_MEMORY

    battery = sample.battery_percent

    if sample.on_battery is True and battery is not None:
        limit = (
            config.battery_resume_percent
            if currently_refused
            else config.battery_critical_percent
        )

        # Strictly below the resume line, at or below the critical one.
        if (battery < limit) if currently_refused else (battery <= limit):
            return FloorBreach.BATTERY_CRITICAL

    free = sample.disk_free_mb

    if free is not None:
        limit_mb = (
            config.disk_resume_free_mb
            if currently_refused
            else config.disk_critical_free_mb
        )

        if free < limit_mb:
            return FloorBreach.DISK_CRITICAL

    temperature = sample.gpu_temperature_c

    if temperature is not None:
        limit_c = (
            config.gpu_resume_temp_c
            if currently_refused
            else config.gpu_critical_temp_c
        )

        if temperature >= limit_c:
            return FloorBreach.GPU_TEMPERATURE

    return None


def check(
    sample: LoadSample,
    required_vram_mb: int,
    config: HardFloorConfig = DEFAULT_HARD_FLOOR,
    currently_refused: bool = False,
) -> FloorVerdict:
    """
    Decide whether the work is safe to start.

    ``currently_refused`` carries the previous answer, which is what makes the
    temperature, battery and disk limits hysteretic: once refused, the card
    must come down to ``gpu_resume_temp_c``, the battery up to
    ``battery_resume_percent`` and the drive up to ``disk_resume_free_mb``
    rather than merely back across the critical line.

    **A missing graphics reading passes.** That deviates from the convention in
    ``core/activity_guard.py``, which returns CRITICAL when a reading throws,
    and the deviation is deliberate: ``read_gpu_status()`` returns ``None``
    both for "the read failed" and for "there is no NVIDIA card in this
    machine". Failing closed would mean Qronos refused every piece of work
    forever on every AMD and Intel machine, which is a worse failure than the
    one it would be guarding against. The sustained-load side still covers CPU
    and memory, and the verdict records ``gpu_unknown`` so a caller that wants
    to be stricter can be.

    **A missing power or disk reading passes for the same reason**, and is
    recorded as ``power_unknown`` / ``disk_unknown``. A battery is only ever
    a reason to refuse while the machine reports running from it; a desktop,
    or a laptop on its charger, is never refused for its battery.
    """
    if required_vram_mb < 0:
        raise ValueError("A task cannot need a negative amount of memory.")

    breach = _safety_breach(sample, config, currently_refused)

    if breach is not None:
        return _verdict(sample, required_vram_mb, breach)

    if required_vram_mb == 0:
        # Nothing is going to be loaded, so there is nothing to find room for.
        # Applying the headroom here would refuse work that cannot possibly
        # harm the card — which is what it did, until an end-to-end run put a
        # task needing no graphics memory behind a 512 MB requirement and
        # watched it wait forever.
        return _verdict(sample, 0)

    free = sample.physical_vram_free_mb

    if free is None:
        return _verdict(sample, required_vram_mb, gpu_unknown=True)

    # Physically free, not the external-pressure view: memory a Qronos model
    # or sidecar still holds is not room for another load.
    if free < required_vram_mb + config.vram_headroom_mb:
        return _verdict(sample, required_vram_mb, FloorBreach.VRAM_EXHAUSTED)

    return _verdict(sample, required_vram_mb)


def check_running(
    sample: LoadSample,
    config: HardFloorConfig = DEFAULT_HARD_FLOOR,
    currently_refused: bool = False,
) -> FloorVerdict:
    """
    Decide whether Qronos work that is already running must yield.

    The start-time limits apply, except fitting: the model is already loaded,
    and asking whether it would fit again would count it twice. In its place
    comes the squeeze. Other applications' graphics memory has grown by at
    least ``vram_squeeze_growth_mb`` since Qronos took the card, *and* the
    card has less than ``vram_headroom_mb`` physically free. Then somebody
    else needs the memory a Qronos model is holding, and Qronos is the one
    that yields. The yield is final for this run: the work goes back in line
    and starts again only when its model fits with headroom.

    Growth is measured against the external usage recorded when Qronos took
    its reservation (``core/resource_context.py``), so the memory a partially
    offloaded model takes for itself — often everything that was free — is
    never mistaken for somebody else's.
    """
    breach = _safety_breach(sample, config, currently_refused)

    if breach is not None:
        return _verdict(sample, 0, breach)

    growth = sample.external_vram_growth_mb
    physically_free = sample.physical_vram_free_mb

    if growth is not None and physically_free is not None and sample.own_vram_mb > 0:
        # Somebody else's usage grew since Qronos took the card, and the card
        # is left with less than the headroom physically free. A card that a
        # partially offloaded model filled stays full without anyone else
        # growing; that alone never counts, which is what keeps Qronos from
        # unloading and reloading itself.
        if (
            growth >= config.vram_squeeze_growth_mb
            and physically_free < config.vram_headroom_mb
        ):
            return _verdict(sample, 0, FloorBreach.VRAM_SQUEEZED)

    return _verdict(sample, 0)


def required_vram_mb(estimated_vram_gb: float) -> int:
    """
    A model's declared footprint in whole megabytes, rounded up.

    Rounding up rather than to nearest, because the direction that costs
    nothing is asking for slightly too much.
    """
    return int(math.ceil(estimated_vram_gb * 1024))
