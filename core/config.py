from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Callable, Mapping


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


ASSET_ROOT_VARIABLE = "QRONOS_ASSET_ROOT"
#: Longest value read from the user's persisted environment (the Windows environment-variable limit).
_MAX_USER_VALUE = 32_767


def read_persisted_user_variable(name: str) -> str | None:
    """The current value of one variable in the Windows user environment (HKCU\\Environment), or None.

    Read only and native (winreg, no subprocess). A process started before the variable was set, for example from
    an Explorer session that predates it, does not inherit it; reading the persisted value keeps such a launch on
    the same asset root as a fresh one. REG_EXPAND_SZ is expanded with Windows' own expansion; other value types,
    oversized values and every read error give None. Elsewhere than Windows there is no such store.
    """
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_QUERY_VALUE) as key:
            value, kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
        return None
    if kind == winreg.REG_EXPAND_SZ:
        value = winreg.ExpandEnvironmentStrings(value)
    return value if len(value) <= _MAX_USER_VALUE else None


def resolve_asset_root(
    environment: Mapping[str, str],
    user_environment: Callable[[str], str | None],
) -> Path | None:
    """The asset root a person configured, or None to keep the default.

    Precedence: a non-blank QRONOS_ASSET_ROOT in this process, then a non-blank persisted user value. Either must be
    an absolute path; a relative value is refused, never silently replaced by the default.
    """
    configured = environment.get(ASSET_ROOT_VARIABLE, "").strip()
    source = "process"
    if not configured:
        configured = (user_environment(ASSET_ROOT_VARIABLE) or "").strip()
        source = "user"
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError(f"{ASSET_ROOT_VARIABLE} ({source} environment) must be an absolute path.")
    return path.resolve()


def resolve_paths(
    *,
    root: Path = PROJECT_ROOT,
    environ: Mapping[str, str] | None = None,
    packaged: bool | None = None,
    user_environment: Callable[[str], str | None] | None = None,
) -> QronosPaths:
    """Separate installed read-only code from writable per-user state.

    Importing configuration never creates directories. Development keeps the
    established checkout layout; a frozen runtime uses LocalAppData instead
    of attempting to write into its installation directory. A configured asset
    root (resolve_asset_root) holds the models and runtime assets in both
    modes; development data, logs and settings stay in the checkout.

    The persisted Windows user environment is read only for the real process
    environment; an injected ``environ`` never reaches the user's registry
    unless a ``user_environment`` reader is injected too.
    """
    environment = os.environ if environ is None else environ
    if user_environment is None:
        user_environment = read_persisted_user_variable if environ is None else (lambda _name: None)
    configured_assets = resolve_asset_root(environment, user_environment)
    installed = (
        bool(getattr(sys, "frozen", False))
        or environment.get("QRONOS_PACKAGED_RUNTIME") == "1"
        if packaged is None
        else packaged
    )
    root = root.resolve()
    if not installed:
        assets = configured_assets or root
        return QronosPaths(
            root=root,
            data=root / "data",
            logs=root / "logs",
            temp=root / "temp",
            memory=root / "memory",
            models=assets / "models",
            assets=assets,
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
    assets = configured_assets or (data / "RuntimeAssets").resolve()
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
