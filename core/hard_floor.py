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

from core.load_signal import LoadSample


class FloorBreach(Enum):
    """Why the floor said no."""

    GPU_TEMPERATURE = "gpu_temperature"
    VRAM_EXHAUSTED = "vram_exhausted"
    SYSTEM_MEMORY = "system_memory"


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

        return (
            "System memory is nearly full. Loading a model now would push "
            "something the user is working in out to disk. This is a safety "
            "limit and cannot be overridden."
        )


def check(
    sample: LoadSample,
    required_vram_mb: int,
    config: HardFloorConfig = DEFAULT_HARD_FLOOR,
    currently_refused: bool = False,
) -> FloorVerdict:
    """
    Decide whether the work is safe to start.

    ``currently_refused`` carries the previous answer, which is what makes the
    temperature limit hysteretic: once refused, the card must come down to
    ``gpu_resume_temp_c`` rather than merely back under the critical line.

    **A missing graphics reading passes.** That deviates from the convention in
    ``core/activity_guard.py``, which returns CRITICAL when a reading throws,
    and the deviation is deliberate: ``read_gpu_status()`` returns ``None``
    both for "the read failed" and for "there is no NVIDIA card in this
    machine". Failing closed would mean Qronos refused every piece of work
    forever on every AMD and Intel machine, which is a worse failure than the
    one it would be guarding against. The sustained-load side still covers CPU
    and memory, and the verdict records ``gpu_unknown`` so a caller that wants
    to be stricter can be.
    """
    if required_vram_mb < 0:
        raise ValueError("A task cannot need a negative amount of memory.")

    if sample.ram_percent >= config.ram_critical_percent:
        return FloorVerdict(
            passed=False,
            breach=FloorBreach.SYSTEM_MEMORY,
            required_vram_mb=required_vram_mb,
            free_vram_mb=sample.vram_free_mb,
            gpu_temperature_c=sample.gpu_temperature_c,
        )

    temperature = sample.gpu_temperature_c

    if temperature is not None:
        limit = (
            config.gpu_resume_temp_c
            if currently_refused
            else config.gpu_critical_temp_c
        )

        if temperature >= limit:
            return FloorVerdict(
                passed=False,
                breach=FloorBreach.GPU_TEMPERATURE,
                required_vram_mb=required_vram_mb,
                free_vram_mb=sample.vram_free_mb,
                gpu_temperature_c=temperature,
            )

    if required_vram_mb == 0:
        # Nothing is going to be loaded, so there is nothing to find room for.
        # Applying the headroom here would refuse work that cannot possibly
        # harm the card — which is what it did, until an end-to-end run put a
        # task needing no graphics memory behind a 512 MB requirement and
        # watched it wait forever.
        return FloorVerdict(
            passed=True,
            required_vram_mb=0,
            free_vram_mb=sample.vram_free_mb,
            gpu_temperature_c=temperature,
        )

    if sample.vram_free_mb is None:
        return FloorVerdict(
            passed=True,
            required_vram_mb=required_vram_mb,
            free_vram_mb=None,
            gpu_temperature_c=temperature,
            gpu_unknown=True,
        )

    if sample.vram_free_mb < required_vram_mb + config.vram_headroom_mb:
        return FloorVerdict(
            passed=False,
            breach=FloorBreach.VRAM_EXHAUSTED,
            required_vram_mb=required_vram_mb,
            free_vram_mb=sample.vram_free_mb,
            gpu_temperature_c=temperature,
        )

    return FloorVerdict(
        passed=True,
        required_vram_mb=required_vram_mb,
        free_vram_mb=sample.vram_free_mb,
        gpu_temperature_c=temperature,
    )


def required_vram_mb(estimated_vram_gb: float) -> int:
    """
    A model's declared footprint in whole megabytes, rounded up.

    Rounding up rather than to nearest, because the direction that costs
    nothing is asking for slightly too much.
    """
    return int(math.ceil(estimated_vram_gb * 1024))
