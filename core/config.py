from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class QronosPaths:
    """Important directories used by Qronos."""

    root: Path = PROJECT_ROOT
    data: Path = PROJECT_ROOT / "data"
    logs: Path = PROJECT_ROOT / "logs"
    temp: Path = PROJECT_ROOT / "temp"
    memory: Path = PROJECT_ROOT / "memory"
    models: Path = PROJECT_ROOT / "models"
    assets: Path = PROJECT_ROOT
    packaged: bool = False

    @property
    def runtime_assets(self) -> Path:
        return self.assets / "runtime"

    @property
    def voice_temp(self) -> Path:
        return (
            self.temp / "Chatterbox"
            if self.packaged
            else self.runtime_assets / "chatterbox" / "temp"
        )

    @property
    def voice_cache(self) -> Path:
        return (
            self.data / "Cache" / "Chatterbox"
            if self.packaged
            else self.runtime_assets / "chatterbox" / "cache"
        )


def resolve_paths(
    *,
    root: Path = PROJECT_ROOT,
    environ: Mapping[str, str] | None = None,
    packaged: bool | None = None,
) -> QronosPaths:
    """Separate installed read-only code from writable per-user state.

    Importing configuration never creates directories. Development keeps the
    established checkout layout; a frozen runtime uses LocalAppData instead
    of attempting to write into its installation directory.
    """
    environment = os.environ if environ is None else environ
    installed = (
        bool(getattr(sys, "frozen", False))
        or environment.get("QRONOS_PACKAGED_RUNTIME") == "1"
        if packaged is None
        else packaged
    )
    root = root.resolve()
    if not installed:
        return QronosPaths(
            root=root,
            data=root / "data",
            logs=root / "logs",
            temp=root / "temp",
            memory=root / "memory",
            models=root / "models",
            assets=root,
        )

    local = environment.get("LOCALAPPDATA", "").strip()
    default_data = (Path(local) if local else Path.home() / ".local" / "share") / "Qronos"

    def absolute_override(name: str, default: Path) -> Path:
        configured = environment.get(name, "").strip()
        path = Path(configured) if configured else default
        if not path.is_absolute():
            raise ValueError(f"{name} must be an absolute path.")
        return path.resolve()

    data = absolute_override("QRONOS_DATA_ROOT", default_data)
    if data == root or root in data.parents:
        raise ValueError("Installed Qronos data must be outside the runtime bundle.")
    assets = absolute_override("QRONOS_ASSET_ROOT", data / "RuntimeAssets")
    return QronosPaths(
        root=root,
        data=data,
        logs=data / "Logs",
        temp=data / "Temp",
        memory=data / "Memory",
        models=assets / "models",
        assets=assets,
        packaged=True,
    )


@dataclass(frozen=True)
class QronosSecurityConfig:
    """Security defaults for Qronos."""

    camera_enabled: bool = False
    microphone_enabled: bool = False

    # The device link, Layer 1: a phone on the same network. Off until the user
    # turns it on, and nothing in the link starts by itself.
    link_enabled: bool = False

    # The device link, Layer 2: reaching the PC from the internet through a
    # relay. This is the global switch; each device also has to be opted in
    # individually, so turning this on does not expose every paired phone.
    remote_access_enabled: bool = False
    external_ai_enabled: bool = False
    destructive_actions_require_approval: bool = True


@dataclass(frozen=True)
class QronosConfig:
    """Central configuration for Qronos."""

    name: str = "Qronos"
    paths: QronosPaths = resolve_paths()
    security: QronosSecurityConfig = QronosSecurityConfig()


CONFIG = QronosConfig()
