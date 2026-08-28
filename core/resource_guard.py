from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Optional

import psutil


class GpuStatusReadError(RuntimeError):
    """An NVIDIA sensor exists but did not return a trustworthy reading."""


@dataclass(frozen=True)
class GpuStatus:
    name: str
    temperature_c: Optional[int]
    gpu_utilization_percent: Optional[int]
    vram_used_mb: Optional[int]
    vram_total_mb: Optional[int]

    @property
    def vram_used_percent(self) -> Optional[float]:
        if self.vram_used_mb is None or self.vram_total_mb in (None, 0):
            return None

        return (self.vram_used_mb / self.vram_total_mb) * 100.0


@dataclass(frozen=True)
class SystemStatus:
    cpu_usage_percent: float
    ram_usage_percent: float
    ram_used_gb: float
    ram_total_gb: float


def read_gpu_status() -> Optional[GpuStatus]:
    """Read NVIDIA GPU status without starting a heavy workload."""
    command = [
        "nvidia-smi",
        "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except FileNotFoundError:
        # NVIDIA support is optional. A missing executable means this machine
        # has no usable NVIDIA sensor; it is not a failed reading.
        return None
    except subprocess.SubprocessError as exc:
        # A present sensor timing out or failing is different from no NVIDIA
        # hardware. Let ActivityGuard fail closed instead of treating an
        # unknown load as an idle GPU.
        raise GpuStatusReadError("nvidia-smi failed") from exc

    lines = result.stdout.strip().splitlines()

    if not lines:
        raise GpuStatusReadError("nvidia-smi returned no GPU data")

    parts = [part.strip() for part in lines[0].split(",")]

    if len(parts) != 5:
        raise GpuStatusReadError("nvidia-smi returned malformed GPU data")

    try:
        name = parts[0]
        temperature_c = int(parts[1])
        gpu_utilization_percent = int(parts[2])
        vram_used_mb = int(parts[3])
        vram_total_mb = int(parts[4])
    except ValueError as exc:
        raise GpuStatusReadError("nvidia-smi returned non-numeric GPU data") from exc

    return GpuStatus(
        name=name,
        temperature_c=temperature_c,
        gpu_utilization_percent=gpu_utilization_percent,
        vram_used_mb=vram_used_mb,
        vram_total_mb=vram_total_mb,
    )


def read_system_status() -> SystemStatus:
    """Read CPU and RAM usage."""
    cpu_usage_percent = psutil.cpu_percent(interval=0.5)

    memory = psutil.virtual_memory()

    ram_used_gb = memory.used / (1024 ** 3)
    ram_total_gb = memory.total / (1024 ** 3)

    return SystemStatus(
        cpu_usage_percent=cpu_usage_percent,
        ram_usage_percent=memory.percent,
        ram_used_gb=ram_used_gb,
        ram_total_gb=ram_total_gb,
    )


def read_system_status_since_last_call() -> SystemStatus:
    """
    Read CPU and RAM without blocking.

    :func:`read_system_status` samples the CPU over half a second, which is
    the right way to answer "what is the CPU doing" from a standing start
    and the wrong way to answer it repeatedly. psutil can instead report the
    average since the previous call, which costs nothing and is more
    accurate for a caller that asks regularly: the gap between two calls is
    a real measurement window rather than an artificial one.

    The catch is the first call, which has no previous call to measure from
    and returns 0.0. A caller that samples repeatedly — which is the only
    caller this is for — should prime it once and discard that reading.
    """
    cpu_usage_percent = psutil.cpu_percent(interval=None)

    memory = psutil.virtual_memory()

    return SystemStatus(
        cpu_usage_percent=cpu_usage_percent,
        ram_usage_percent=memory.percent,
        ram_used_gb=memory.used / (1024 ** 3),
        ram_total_gb=memory.total / (1024 ** 3),
    )


def main() -> None:
    """Display the current Qronos resource status."""
    system = read_system_status()
    gpu = read_gpu_status()

    print("=== Qronos Resource Status ===")
    print(f"CPU usage: {system.cpu_usage_percent:.1f}%")
    print(
        f"RAM usage: {system.ram_usage_percent:.1f}% "
        f"({system.ram_used_gb:.1f} / {system.ram_total_gb:.1f} GB)"
    )

    if gpu is None:
        print("GPU status: unavailable")
        return

    print(f"GPU: {gpu.name}")
    print(f"GPU usage: {gpu.gpu_utilization_percent}%")
    print(f"GPU temperature: {gpu.temperature_c} C")
    print(f"VRAM: {gpu.vram_used_mb} / {gpu.vram_total_mb} MB")

    if gpu.vram_used_percent is not None:
        print(f"VRAM usage: {gpu.vram_used_percent:.1f}%")


if __name__ == "__main__":
    main()
