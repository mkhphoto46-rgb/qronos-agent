from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "brains.json"
)
BRAIN_CONFIG_ENV = "QRONOS_BRAIN_CONFIG"


@dataclass(frozen=True)
class GenerationProfile:
    """Generation policy for one brain/effort mode."""

    think: bool | str = False
    num_predict: int | None = None
    keep_alive: str = "0"


@dataclass(frozen=True)
class ModelProfile:
    """Runtime-neutral configuration for one concrete model profile."""

    profile_id: str
    name: str
    role: str
    estimated_vram_gb: float
    priority: int
    context_tokens: int
    runtime: str
    generation: dict[str, GenerationProfile]

    def generation_for(self, effort: str = "default") -> GenerationProfile:
        cleaned = (effort or "default").strip().lower()

        if cleaned in self.generation:
            return self.generation[cleaned]

        if "default" in self.generation:
            return self.generation["default"]

        if self.generation:
            return next(iter(self.generation.values()))

        return GenerationProfile()


def _config_path() -> Path:
    override = os.environ.get(BRAIN_CONFIG_ENV, "").strip()
    return Path(override) if override else DEFAULT_CONFIG_PATH


def _parse_think(value: Any) -> bool | str:
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        cleaned = value.strip().lower()

        if cleaned in {"low", "medium", "high"}:
            return cleaned

        if cleaned in {"false", "off", "no", "0"}:
            return False

        if cleaned in {"true", "on", "yes", "1"}:
            return True

    raise ValueError(
        "Generation 'think' must be a boolean or one of: low, medium, high."
    )


def _parse_generation(raw: Any) -> dict[str, GenerationProfile]:
    if not isinstance(raw, dict) or not raw:
        return {"default": GenerationProfile()}

    parsed: dict[str, GenerationProfile] = {}

    for key, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError(f"Generation profile {key!r} must be an object.")

        raw_num_predict = value.get("num_predict")
        num_predict = (
            None
            if raw_num_predict is None
            else max(1, int(raw_num_predict))
        )

        parsed[str(key).strip().lower()] = GenerationProfile(
            think=_parse_think(value.get("think", False)),
            num_predict=num_predict,
            keep_alive=str(value.get("keep_alive", "0")).strip() or "0",
        )

    return parsed


def _load_registry() -> tuple[dict[str, ModelProfile], dict[str, ModelProfile]]:
    path = _config_path()

    if not path.exists():
        raise RuntimeError(
            f"Qronos brain config does not exist: {path}"
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not read Qronos brain config: {path}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError("Qronos brain config root must be a JSON object.")

    raw_profiles = data.get("profiles")
    raw_active = data.get("active_roles")

    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ValueError("Qronos brain config requires a non-empty 'profiles' object.")

    if not isinstance(raw_active, dict) or not raw_active:
        raise ValueError("Qronos brain config requires a non-empty 'active_roles' object.")

    profiles: dict[str, ModelProfile] = {}

    for profile_id, raw in raw_profiles.items():
        if not isinstance(raw, dict):
            raise ValueError(f"Model profile {profile_id!r} must be an object.")

        pid = str(profile_id).strip()
        name = str(raw.get("name", "")).strip()

        if not pid or not name:
            raise ValueError("Every model profile needs a non-empty id and name.")

        profiles[pid] = ModelProfile(
            profile_id=pid,
            name=name,
            role=str(raw.get("role", "")).strip() or "brain",
            estimated_vram_gb=max(0.0, float(raw.get("estimated_vram_gb", 0.0))),
            priority=int(raw.get("priority", 100)),
            context_tokens=max(256, int(raw.get("context_tokens", 4096))),
            runtime=str(raw.get("runtime", "ollama")).strip().lower() or "ollama",
            generation=_parse_generation(raw.get("generation", {})),
        )

    active: dict[str, ModelProfile] = {}

    for role, profile_id in raw_active.items():
        role_name = str(role).strip().lower()
        pid = str(profile_id).strip()

        try:
            active[role_name] = profiles[pid]
        except KeyError as exc:
            raise ValueError(
                f"Active role {role_name!r} references unknown profile {pid!r}."
            ) from exc

    for required_role in ("fast", "heavy"):
        if required_role not in active:
            raise ValueError(
                f"Qronos brain config is missing active role: {required_role}"
            )

    return profiles, active


PROFILES, MODELS = _load_registry()


def reload_models() -> None:
    """Reload active model roles after the JSON file changes."""

    global PROFILES, MODELS
    PROFILES, MODELS = _load_registry()


def get_model(role: str) -> ModelProfile:
    cleaned = role.strip().lower()

    try:
        return MODELS[cleaned]
    except KeyError as exc:
        raise ValueError(f"Unknown model role: {role}") from exc


def configured_model_names() -> set[str]:
    """Names Qronos is allowed to unload during role swaps."""

    return {
        profile.name
        for profile in PROFILES.values()
        if profile.name
    }


if __name__ == "__main__":
    print(f"CONFIG={_config_path()}")

    for role, model in MODELS.items():
        print(
            f"{role}: {model.profile_id} -> {model.name} | "
            f"{model.role} | {model.estimated_vram_gb} GB | "
            f"ctx={model.context_tokens}"
        )