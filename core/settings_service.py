"""
One authoritative place a setting lives.

Settings are persistent user preferences, not React component state. This
service owns validation, default resolution and durable writes. Runtime code may
read the same file, but UI mutations go through one serialized backend command.

Safety contract:

    A setting has a declared type and a default in one place.

    Reading a corrupt/newer store does not crash Qronos; defaults remain
    readable and the problem is reported.

    A store that could not be understood becomes read-only for mutations. This
    prevents a later click from silently overwriting recoverable user data.

    Writes are validate -> persist atomically -> commit in memory. A failed
    disk write therefore cannot produce "changed for this session but not
    actually saved" state.

    Unknown keys from the same schema version are preserved on later writes so
    an older build does not erase preferences it does not understand.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from core.config import CONFIG
from core.json_store import (
    JsonStore,
    MigrationMissing,
    StoreCorrupt,
    StoreTooNew,
)


DEFAULT_SETTINGS_PATH = CONFIG.paths.data / "settings.json"

SCHEMA_VERSION = 2


def _migrate_v1_to_v2(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert the dormant Stage-1 voice keys into the first runtime-owned contract.

    Stage 1 exposed no functional Voice/Wake UI and runtime_bridge did not read
    these values. Therefore an old stored False for wake_word_enabled cannot be
    treated as an authoritative user decision. Drop only that old False value so
    the accepted production baseline (Wake enabled) survives the transition.
    An explicitly stored True is harmless and is preserved.
    """
    migrated = dict(payload)

    if migrated.get("voice.wake_word_enabled") is False:
        migrated.pop(
            "voice.wake_word_enabled",
            None,
        )

    return migrated


SETTINGS_MIGRATIONS = {
    1: _migrate_v1_to_v2,
}

_ALLOWED_TYPES = (bool, int, float, str)

ProblemReporter = Callable[[str], None]


class UnknownSetting(KeyError):
    """No setting is defined under that key."""


class InvalidSettingValue(ValueError):
    """A value did not match its definition."""


class SettingsWriteBlocked(RuntimeError):
    """
    Persistence is intentionally blocked after an unreadable/unsupported store.

    Defaults may still be read so Qronos can start, but a mutation must not
    overwrite the file that failed to load.
    """


@dataclass(frozen=True)
class SettingDefinition:
    """One setting: what it is called, what it holds, what it defaults to."""

    key: str
    default: Any
    description: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[Any, ...] | None = None

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("A setting needs a key.")

        if not isinstance(self.default, _ALLOWED_TYPES):
            raise ValueError(
                f"{self.key}: a setting holds a bool, number or string, "
                f"not {type(self.default).__name__}."
            )

        self.validate(self.default)

    @property
    def value_type(self) -> type:
        return type(self.default)

    def validate(self, value: Any) -> Any:
        """Return the value, or raise saying why it is not acceptable."""
        # bool is a subclass of int, so an unguarded isinstance check would let
        # True through as a number and 1 through as a flag.
        if isinstance(value, bool) != isinstance(self.default, bool):
            raise InvalidSettingValue(
                f"{self.key} holds a {self.value_type.__name__}, "
                f"not a {type(value).__name__}."
            )

        if isinstance(self.default, (int, float)) and not isinstance(
            self.default, bool
        ):
            if not isinstance(value, (int, float)):
                raise InvalidSettingValue(
                    f"{self.key} holds a number, not a "
                    f"{type(value).__name__}."
                )
        elif not isinstance(value, self.value_type):
            raise InvalidSettingValue(
                f"{self.key} holds a {self.value_type.__name__}, "
                f"not a {type(value).__name__}."
            )

        if self.choices is not None and value not in self.choices:
            raise InvalidSettingValue(
                f"{self.key} must be one of {list(self.choices)}."
            )

        if self.minimum is not None and value < self.minimum:
            raise InvalidSettingValue(
                f"{self.key} must be at least {self.minimum}."
            )

        if self.maximum is not None and value > self.maximum:
            raise InvalidSettingValue(
                f"{self.key} must be at most {self.maximum}."
            )

        return value


class SettingsService:
    """
    The settings, loaded once and written through.

    Thread-safe inside one process. Desktop-side writes are additionally
    serialized by the Tauri settings command so two UI mutations cannot race
    each other through separate Python CLI processes.
    """

    def __init__(
        self,
        definitions: Iterable[SettingDefinition],
        path: str | Path | None = DEFAULT_SETTINGS_PATH,
        on_problem: ProblemReporter | None = None,
    ) -> None:
        self._definitions: dict[str, SettingDefinition] = {}

        for definition in definitions:
            if definition.key in self._definitions:
                raise ValueError(
                    f"{definition.key} is defined twice."
                )

            self._definitions[definition.key] = definition

        self._store = JsonStore(
            path,
            version=SCHEMA_VERSION,
            migrations=SETTINGS_MIGRATIONS,
        )
        self._on_problem = on_problem
        self._lock = threading.RLock()
        self._values: dict[str, Any] = {}
        self._unknown_values: dict[str, Any] = {}
        self._write_blocked_reason: str | None = None
        self._load()

    # ------------------------------------------------------------- reading

    @property
    def path(self) -> Path | None:
        return self._store.path

    @property
    def write_blocked_reason(self) -> str | None:
        with self._lock:
            return self._write_blocked_reason

    def definitions(self) -> tuple[SettingDefinition, ...]:
        return tuple(self._definitions.values())

    def get(self, key: str) -> Any:
        with self._lock:
            definition = self._require(key)

            return self._values.get(key, definition.default)

    def all_values(self) -> dict[str, Any]:
        """Every defined setting, stored or defaulted."""
        with self._lock:
            return {
                key: self._values.get(key, definition.default)
                for key, definition in self._definitions.items()
            }

    def is_default(self, key: str) -> bool:
        with self._lock:
            self._require(key)

            return key not in self._values

    # ------------------------------------------------------------- writing

    def set(self, key: str, value: Any) -> Any:
        """Validate, persist, then commit one setting."""
        with self._lock:
            definition = self._require(key)
            accepted = definition.validate(value)

            candidate = dict(self._values)
            candidate[key] = accepted

            self._persist_candidate(candidate)
            self._values = candidate

            return accepted

    def update(self, values: Mapping[str, Any]) -> None:
        """
        Set several settings atomically.

        The entire batch is validated before the disk write. If validation or
        persistence fails, neither the file nor in-memory state is partially
        applied.
        """
        with self._lock:
            accepted = {
                key: self._require(key).validate(value)
                for key, value in values.items()
            }

            candidate = dict(self._values)
            candidate.update(accepted)

            self._persist_candidate(candidate)
            self._values = candidate

    def reset(self, key: str) -> Any:
        """Put one setting back to its default."""
        with self._lock:
            definition = self._require(key)
            candidate = dict(self._values)
            candidate.pop(key, None)

            self._persist_candidate(candidate)
            self._values = candidate

            return definition.default

    def reset_all(self) -> None:
        """Reset every setting known to this build without erasing unknown keys."""
        with self._lock:
            candidate: dict[str, Any] = {}

            self._persist_candidate(candidate)
            self._values = candidate

    # ------------------------------------------------------------ internals

    def _require(self, key: str) -> SettingDefinition:
        try:
            return self._definitions[key]
        except KeyError:
            raise UnknownSetting(
                f"{key} is not a defined setting."
            ) from None

    def _load(self) -> None:
        try:
            stored = self._store.load_payload()
        except (
            StoreCorrupt,
            StoreTooNew,
            MigrationMissing,
        ) as error:
            reason = str(error)
            self._write_blocked_reason = reason
            self._report(reason)
            return

        kept: dict[str, Any] = {}
        unknown: dict[str, Any] = {}

        for key, value in stored.items():
            definition = self._definitions.get(key)

            if definition is None:
                unknown[key] = value
                continue

            try:
                kept[key] = definition.validate(value)
            except InvalidSettingValue as error:
                self._report(f"{error} Using the default.")

        self._values = kept
        self._unknown_values = unknown
        self._write_blocked_reason = None

    def _persist_candidate(
        self,
        candidate_values: Mapping[str, Any],
    ) -> None:
        if self._write_blocked_reason is not None:
            raise SettingsWriteBlocked(
                "Settings are read-only because the existing store could not "
                f"be loaded safely: {self._write_blocked_reason}"
            )

        payload = dict(self._unknown_values)
        payload.update(candidate_values)

        try:
            self._store.save(payload)
        except OSError as error:
            message = f"Settings could not be saved: {error}"
            self._report(message)
            raise

    def _report(self, message: str) -> None:
        if self._on_problem is not None:
            self._on_problem(message)


#: Keep this list deliberately short. A definition is a compatibility promise;
#: runtime/UI wiring for additional controls is added only after that capability
#: passes its own gate.
DEFAULT_DEFINITIONS: tuple[SettingDefinition, ...] = (
    SettingDefinition(
        key="voice.wake_word_enabled",
        default=True,
        description=(
            "Listen for the trained Qronos wake word."
        ),
    ),
    SettingDefinition(
        key="voice.wake_sensitivity",
        default=56,
        description=(
            "User-facing wake sensitivity from 0 to 100. "
            "Runtime maps this conservatively around the accepted threshold."
        ),
        minimum=0,
        maximum=100,
    ),
    SettingDefinition(
        key="voice.language",
        default="fa",
        description="The language speech recognition is asked for.",
        choices=("auto", "fa", "en"),
    ),
    SettingDefinition(
        key="voice.input_device_id",
        default="",
        description=(
            "Stable identifier of the selected microphone. Empty follows "
            "the current Windows / PortAudio default."
        ),
    ),
    SettingDefinition(
        key="voice.output_device_id",
        default="",
        description=(
            "Native Windows/PortAudio output endpoint selected for Qronos "
            "voice playback. Empty follows the current Windows system default."
        ),
    ),
    SettingDefinition(
        key="voice.silence_timeout_seconds",
        default=2.0,
        description="How long a pause ends a spoken command.",
        minimum=0.5,
        maximum=10.0,
    ),
    SettingDefinition(
        key="appearance.ui_scale_percent",
        default=100,
        description="User interface scale percentage.",
        choices=(90, 100, 110),
    ),
    SettingDefinition(
        key="system.keep_in_tray",
        default=False,
        description=(
            "Keep Qronos running in the Windows system tray when the "
            "main window is closed."
        ),
    ),
    SettingDefinition(
        key="resources.pause_in_performance_mode",
        default=True,
        description=(
            "Stay out of the way while a game or a render is running."
        ),
    ),
    SettingDefinition(
        key="privacy.keep_conversation_history",
        default=True,
        description=(
            "Keep past conversations on this machine. Turning this off "
            "stops new conversations being written; it does not delete "
            "what is already stored."
        ),
    ),
    SettingDefinition(
        key="link.enabled",
        default=False,
        description=(
            "Let paired phones reach Qronos on this network. Off until "
            "the user turns it on."
        ),
    ),
    SettingDefinition(
        key="link.remote_access_enabled",
        default=False,
        description=(
            "Let paired phones that were each given remote access reach "
            "Qronos from outside this network through the configured relay. "
            "Off until the user turns it on at this PC; it has no effect "
            "while the local link is off. Saving a relay never turns it on."
        ),
    ),
)
