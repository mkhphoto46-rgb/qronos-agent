from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
    paths: QronosPaths = QronosPaths()
    security: QronosSecurityConfig = QronosSecurityConfig()


CONFIG = QronosConfig()