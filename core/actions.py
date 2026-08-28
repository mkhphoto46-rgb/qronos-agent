"""
The vocabulary every executed action is described in.

Nothing here executes anything. This module exists so that the four things
which must agree about an action — the permission gate, the audit trail, the
undo journal and the executor that eventually runs it — agree by sharing one
type rather than by four separate conventions that drift.

Three properties are deliberate.

    An action is data, not a call. A frozen dataclass that survives a round
    trip through JSON can be logged before it runs, replayed to a user for
    approval, written to an undo journal, and compared afterwards against what
    actually happened. A closure can do none of that.

    An action carries its category, and the category is what the permission
    engine reasons about. There is no free-text risk field for a caller to
    fill in optimistically.

    Parameters are flat and JSON-safe. An action is shown to a person before
    it runs — that is the whole point of the confirmation levels — and a
    nested object graph is not something a confirmation dialog can render
    honestly.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from security.permissions import ActionCategory


SCHEMA_VERSION = 1

# An action id is only ever compared and logged, never parsed, so the shape is
# chosen to be unmistakable in a log line rather than to carry meaning.
ACTION_ID_BYTES = 8

# What a parameter value is allowed to be. Anything else cannot be rendered in
# a confirmation dialog, written to the journal, or compared after the fact.
_ALLOWED_VALUE_TYPES = (str, int, float, bool, type(None))


class ActionOutcome(Enum):
    """How an action finished."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"

    # Refused before anything happened. Distinct from FAILED: nothing was
    # attempted, so there is nothing to undo and nothing to retry differently.
    REFUSED = "refused"

    # The permission engine wants a human first. The action is well-formed and
    # may run later, unchanged, once somebody says yes.
    AWAITING_APPROVAL = "awaiting_approval"


class InvalidAction(ValueError):
    """An action could not be built or read back."""


def new_action_id() -> str:
    return secrets.token_hex(ACTION_ID_BYTES)


def _validated_parameters(
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Reject anything that could not survive being shown, stored and replayed.

    The check is at construction rather than at serialisation on purpose. An
    action that cannot be written to the audit trail must not exist in the
    first place, or the failure surfaces at the moment of logging — after the
    decision to run it has already been taken.
    """
    clean: dict[str, Any] = {}

    for key, value in parameters.items():
        if not isinstance(key, str) or not key:
            raise InvalidAction(
                f"Action parameter names must be non-empty strings: {key!r}."
            )

        if not isinstance(value, _ALLOWED_VALUE_TYPES):
            raise InvalidAction(
                f"Action parameter {key!r} is a {type(value).__name__}; "
                "only strings, numbers, booleans and null are allowed."
            )

        clean[key] = value

    return clean


@dataclass(frozen=True)
class ActionRequest:
    """One thing Qronos has been asked to do, before it is allowed to."""

    category: ActionCategory

    # What the action operates on, in the user's terms: an application name, a
    # file path, a URL. Shown to the person approving it, so it is a
    # description rather than a handle.
    target: str

    # Why this action exists, in one line, for the audit trail and the
    # confirmation dialog. Not a log message: it is read by the person deciding
    # whether to allow it.
    summary: str

    parameters: Mapping[str, Any] = field(default_factory=dict)
    action_id: str = field(default_factory=new_action_id)

    def __post_init__(self) -> None:
        if not isinstance(self.category, ActionCategory):
            raise InvalidAction(
                "An action must carry an ActionCategory; "
                f"got {type(self.category).__name__}."
            )

        if not self.target.strip():
            raise InvalidAction("An action must name a target.")

        if not self.summary.strip():
            raise InvalidAction("An action must carry a summary.")

        if not self.action_id.strip():
            raise InvalidAction("An action must carry an id.")

        object.__setattr__(
            self,
            "parameters",
            _validated_parameters(self.parameters),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "actionId": self.action_id,
            "category": self.category.value,
            "target": self.target,
            "summary": self.summary,
            "parameters": dict(self.parameters),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ActionRequest":
        version = data.get("schemaVersion")

        if version != SCHEMA_VERSION:
            raise InvalidAction(
                f"Unsupported action schema version: {version!r}."
            )

        try:
            category = ActionCategory(str(data["category"]))
        except (KeyError, ValueError) as error:
            raise InvalidAction(
                f"Unknown action category: {data.get('category')!r}."
            ) from error

        parameters = data.get("parameters", {})

        if not isinstance(parameters, Mapping):
            raise InvalidAction("Action parameters must be an object.")

        try:
            return cls(
                category=category,
                target=str(data["target"]),
                summary=str(data["summary"]),
                parameters=parameters,
                action_id=str(data["actionId"]),
            )
        except KeyError as error:
            raise InvalidAction(
                f"Action is missing {error.args[0]!r}."
            ) from error

    def describe(self) -> str:
        """One line for a log or a confirmation prompt."""
        return f"{self.category.value}: {self.summary} [{self.target}]"


@dataclass(frozen=True)
class ActionResult:
    """What happened to one action."""

    action_id: str
    outcome: ActionOutcome

    # Free text for the person, not for a parser. Branch on ``outcome``.
    detail: str = ""

    @property
    def ran(self) -> bool:
        """True only when the action actually touched the machine."""
        return self.outcome in {
            ActionOutcome.SUCCEEDED,
            ActionOutcome.FAILED,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "actionId": self.action_id,
            "outcome": self.outcome.value,
            "detail": self.detail,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ActionResult":
        if data.get("schemaVersion") != SCHEMA_VERSION:
            raise InvalidAction(
                f"Unsupported result schema version: "
                f"{data.get('schemaVersion')!r}."
            )

        try:
            return cls(
                action_id=str(data["actionId"]),
                outcome=ActionOutcome(str(data["outcome"])),
                detail=str(data.get("detail", "")),
            )
        except (KeyError, ValueError) as error:
            raise InvalidAction(f"Malformed action result: {error}.") from error


def to_line(request: ActionRequest) -> str:
    """Serialise an action to one line, for a journal or a pipe."""
    return json.dumps(
        request.to_json(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def from_line(line: str) -> ActionRequest:
    """Read an action back from one line."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError as error:
        raise InvalidAction(f"Action is not valid JSON: {error}.") from error

    if not isinstance(data, Mapping):
        raise InvalidAction("An action must be a JSON object.")

    return ActionRequest.from_json(data)
