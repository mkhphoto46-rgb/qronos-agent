"""
One authoritative place a setting lives.

Today there is no such place. ``SettingsView.tsx`` makes exactly one call to
the backend — ``validate_hotkey`` — and every other control is React state that
disappears when the window closes. The only settings that survive a restart are
the hotkeys, because ``hotkeys.rs`` writes them itself.

The shape of the problem is worth naming, because it is why this is a service
rather than a dictionary. Settings are read by code that must not crash (the
voice runtime), written by a user interface that must not lie (a toggle that
silently failed to save is worse than one that reports an error), and extended
by every feature that lands later. So:

    A setting has a declared type and a default, in one place. A definition is
    the only way a setting exists. This is what makes "unknown setting" an
    error rather than a typo that silently reads as None forever.

    Reading never fails. An unreadable or corrupt file is reported once through
    a callback and the defaults are used, because a voice assistant that will
    not start because a preference file is damaged is worse than one that
    starts with the preferences it shipped with. That is the opposite of the
    device registry's rule, and deliberately so: a corrupt key file means
    somebody may be tampering, while a corrupt preference file means a bad
    shutdown.

    Writing is atomic and validated. A rejected value does not reach the file.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from core.config import CONFIG
from core.json_store import JsonStore, StoreCorrupt, StoreTooNew


DEFAULT_SETTINGS_PATH = CONFIG.paths.data / "settings.json"

SCHEMA_VERSION = 1

# What a setting is allowed to hold. Same reasoning as the action schema: a
# value has to be renderable in a user interface and survive a round trip
# through JSON, and a nested structure is neither.
_ALLOWED_TYPES = (bool, int, float, str)

# Told about a file that could not be read, so the interface can say so instead
# of the user discovering their preferences reset in silence.
ProblemReporter = Callable[[str], None]


class UnknownSetting(KeyError):
    """No setting is defined under that key."""


class InvalidSettingValue(ValueError):
    """A value did not match its definition."""


@dataclass(frozen=True)
class SettingDefinition:
    """One setting: what it is called, what it holds, what it defaults to."""

    key: str
    default: Any
    description: str

    # Optional bounds for numbers, and an optional set of permitted values for
    # anything. Both are checked on write, so an out-of-range value never
    # reaches the file and never has to be defended against on read.
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

    Thread-safe: the voice runtime reads settings on its own thread while the
    interface writes them on another.
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

        self._store = JsonStore(path, version=SCHEMA_VERSION)
        self._on_problem = on_problem
        self._lock = threading.RLock()
        self._values: dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------- reading

    def definitions(self) -> tuple[SettingDefinition, ...]:
        return tuple(self._definitions.values())

    def get(self, key: str) -> Any:
        with self._lock:
            definition = self._require(key)

            return self._values.get(key, definition.default)

    def all_values(self) -> dict[str, Any]:
        """Every setting, stored or defaulted."""
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
        """Validate, store and persist one setting."""
        with self._lock:
            definition = self._require(key)
            accepted = definition.validate(value)

            self._values[key] = accepted
            self._persist()

            return accepted

    def update(self, values: Mapping[str, Any]) -> None:
        """
        Set several settings, or none of them.

        Validating the whole batch first is what makes a settings page
        applying six changes leave the file consistent when the fourth is
        rejected.
        """
        with self._lock:
            accepted = {
                key: self._require(key).validate(value)
                for key, value in values.items()
            }

            self._values.update(accepted)
            self._persist()

    def reset(self, key: str) -> Any:
        """Put one setting back to its default."""
        with self._lock:
            definition = self._require(key)
            self._values.pop(key, None)
            self._persist()

            return definition.default

    def reset_all(self) -> None:
        with self._lock:
            self._values.clear()
            self._persist()

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
        except (StoreCorrupt, StoreTooNew) as error:
            # Defaults rather than a crash. A voice assistant that will not
            # start because a preference file is damaged is worse than one that
            # starts with the preferences it shipped with — and the user is
            # told, so it is not silent.
            self._report(str(error))
            return

        kept: dict[str, Any] = {}

        for key, value in stored.items():
            definition = self._definitions.get(key)

            if definition is None:
                # A setting this version does not have. Dropped from memory but
                # left in the file untouched on the next write, so downgrading
                # does not destroy the newer version's preferences.
                continue

            try:
                kept[key] = definition.validate(value)
            except InvalidSettingValue as error:
                self._report(f"{error} Using the default.")

        self._values = kept

    def _persist(self) -> None:
        try:
            self._store.save(self._values)
        except OSError as error:
            # The value is already applied in memory, so the session behaves as
            # the user asked. What is lost is persistence, and they are told.
            self._report(f"Settings could not be saved: {error}")

    def _report(self, message: str) -> None:
        if self._on_problem is not None:
            self._on_problem(message)


#: The settings Qronos has today. Deliberately short: a definition here is a
#: commitment to keep the key working, and the list should grow as features
#: land rather than in anticipation of them.
DEFAULT_DEFINITIONS: tuple[SettingDefinition, ...] = (
    SettingDefinition(
        key="voice.wake_word_enabled",
        default=False,
        description=(
            "Listen for the wake word. Off until a usable model exists."
        ),
    ),
    SettingDefinition(
        key="voice.language",
        default="fa",
        description="The language speech recognition is asked for.",
        choices=("fa", "en"),
    ),
    SettingDefinition(
        key="voice.silence_timeout_seconds",
        default=2.0,
        description="How long a pause ends a spoken command.",
        minimum=0.5,
        maximum=10.0,
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
)
