"""
What a watching session costs, sampled while it runs.

``CardWatcher`` in ``tools/vision_eval.py`` answers "how much graphics memory
does one generation peak at". This answers a different question: what does an
hour of watching do to the machine somebody is trying to use?

They need different instruments. A single generation is two seconds and the
only number that matters is the peak. A session is minutes of load, idle, load,
idle — and there the shape matters: whether memory is held between frames or
handed back, whether the card gets hotter over time, and how much of the wall
clock the model is actually busy for.

Two notes on the measuring itself.

**CPU is sampled without blocking.** ``psutil.cpu_percent(interval=...)``
sleeps for the interval, which would make the sampler's own pacing a lie.
Non-blocking sampling measures the time since the previous call instead, which
is what a fixed-interval sampler wants anyway.

**The first CPU reading is discarded.** With no previous call to compare
against, ``cpu_percent`` returns 0.0 the first time, and a zero in the middle
of an average is not an idle machine — it is a missing measurement.

**And the model's own share is tracked apart from the machine's.** The
architecture document already requires this and gives the reason: without it
Qronos cannot tell its own load from somebody else's, and a machine that is
busy for another reason looks like a machine Qronos is ruining. Two runs of
this monitor an hour apart measured 43% and 62% of the processor for identical
work, because something else was running the second time.
"""

from __future__ import annotations

import statistics
import threading
import time
from dataclasses import dataclass, field

import psutil

from core.resource_guard import read_gpu_status


#: The process that holds the model. Named rather than discovered, because
#: "whatever is using the GPU" would happily measure a game.
SERVER_PROCESS = "ollama"


def _server_processes() -> list[psutil.Process]:
    found = []

    for process in psutil.process_iter(["name"]):
        name = (process.info.get("name") or "").lower()

        if SERVER_PROCESS in name:
            found.append(process)

    return found


@dataclass
class Series:
    """One thing measured repeatedly."""

    name: str
    unit: str
    values: list[float] = field(default_factory=list)

    def add(self, value: float | None) -> None:
        if value is not None:
            self.values.append(float(value))

    @property
    def ok(self) -> bool:
        return bool(self.values)

    @property
    def peak(self) -> float:
        return max(self.values) if self.values else 0.0

    @property
    def low(self) -> float:
        return min(self.values) if self.values else 0.0

    @property
    def mean(self) -> float:
        return statistics.fmean(self.values) if self.values else 0.0

    def describe(self) -> str:
        if not self.ok:
            return f"{self.name}: not readable on this machine"

        return (
            f"{self.name}: {self.low:.0f}-{self.peak:.0f} {self.unit}, "
            f"mean {self.mean:.0f}"
        )


class SessionMonitor(threading.Thread):
    """
    Samples the machine for as long as something else is running.

    Started before the work and stopped after it, so the numbers cover the
    quiet stretches between frames as well as the busy ones. That is the whole
    point: a session that holds four gigabytes for ten minutes and a session
    that holds it for two seconds at a time look identical if you only sample
    while the model is talking.
    """

    def __init__(self, interval: float = 0.25) -> None:
        super().__init__(daemon=True)

        self.interval = interval
        self.stopping = threading.Event()

        self.vram = Series("graphics memory", "MiB")
        self.gpu = Series("graphics load", "%")
        self.temperature = Series("card temperature", "C")
        self.cpu = Series("processor, whole machine", "%")
        self.ram = Series("memory, whole machine", "%")
        self.server_cpu = Series("processor, the model server", "%")
        self.server_ram = Series("memory, the model server", "MiB")

        self._server = _server_processes()

        # Primes the per-process counters for the same reason as the machine
        # one: the first reading has nothing to compare against.
        for process in self._server:
            try:
                process.cpu_percent(interval=None)
            except psutil.Error:
                pass

        self.samples = 0
        self.started_at = 0.0
        self.stopped_at = 0.0

        # Whatever the machine was doing before this started, so the cost of
        # the session can be told apart from the cost of everything else.
        self.idle_vram = self._gpu_reading()[0]

        # Primes the non-blocking CPU counter. The reading it returns is
        # meaningless and is thrown away.
        psutil.cpu_percent(interval=None)

    @staticmethod
    def _gpu_reading() -> tuple[float | None, float | None, float | None]:
        status = read_gpu_status()

        if status is None:
            return None, None, None

        return (
            status.vram_used_mb,
            status.gpu_utilization_percent,
            status.temperature_c,
        )

    def run(self) -> None:
        self.started_at = time.perf_counter()

        while not self.stopping.is_set():
            used, load, degrees = self._gpu_reading()

            self.vram.add(used)
            self.gpu.add(load)
            self.temperature.add(degrees)
            self.cpu.add(psutil.cpu_percent(interval=None))
            self.ram.add(psutil.virtual_memory().percent)

            share, resident = self._server_usage()

            self.server_cpu.add(share)
            self.server_ram.add(resident)

            self.samples += 1
            self.stopping.wait(self.interval)

        self.stopped_at = time.perf_counter()

    def stop(self) -> None:
        self.stopping.set()
        self.join(timeout=5.0)

        if not self.stopped_at:
            self.stopped_at = time.perf_counter()

    def _server_usage(self) -> tuple[float | None, float | None]:
        """
        What the model server is using, across however many processes it has.

        Rediscovered when they all disappear, because Ollama is started on
        demand and may not have existed when this monitor was built.
        """
        if not self._server or not any(p.is_running() for p in self._server):
            self._server = _server_processes()

            for process in self._server:
                try:
                    process.cpu_percent(interval=None)
                except psutil.Error:
                    pass

            return None, None

        share = 0.0
        resident = 0.0

        for process in self._server:
            try:
                share += process.cpu_percent(interval=None)
                resident += process.memory_info().rss / (1024 * 1024)
            except psutil.Error:
                continue

        # Normalised to the whole machine, so it can be compared against the
        # machine figure above rather than to a per-core percentage that can
        # read 800%.
        cores = psutil.cpu_count() or 1

        return share / cores, resident

    @property
    def seconds(self) -> float:
        return max(0.0, self.stopped_at - self.started_at)

    @property
    def vram_over_idle(self) -> float:
        """How much of the card this session took, above what was already on it."""
        if not self.vram.ok or self.idle_vram <= 0:
            return 0.0

        return self.vram.peak - self.idle_vram

    @property
    def busy_fraction(self) -> float:
        """
        What share of the session the card was actually working.

        The interesting number for a watching session, because the answer
        decides whether the model should be held between frames or handed back.
        A card that is busy a tenth of the time is a card somebody else could
        have been using.
        """
        if not self.gpu.ok:
            return 0.0

        return sum(1 for value in self.gpu.values if value > 30.0) / len(
            self.gpu.values
        )

    def report(self) -> str:
        lines = [
            f"  sampled {self.samples} times over {self.seconds:.0f}s",
            f"  {self.vram.describe()}",
        ]

        if self.vram.ok and self.idle_vram > 0:
            lines.append(
                f"    the card held {self.idle_vram:.0f} MiB before this "
                f"started, so the session's own peak is "
                f"{self.vram_over_idle:+.0f} MiB"
            )

        lines.extend(
            [
                f"  {self.gpu.describe()}",
                f"    busy for {self.busy_fraction:.0%} of the session",
                f"  {self.temperature.describe()}",
                f"  {self.cpu.describe()}",
                f"  {self.server_cpu.describe()}",
                f"  {self.ram.describe()}",
                f"  {self.server_ram.describe()}",
            ]
        )

        return "\n".join(lines)
