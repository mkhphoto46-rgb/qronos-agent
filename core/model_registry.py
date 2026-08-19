from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    name: str
    role: str
    estimated_vram_gb: float
    priority: int


MODELS = {
    "fast": ModelProfile(
        name="qwen3.5:9b",
        role="fast_brain",
        estimated_vram_gb=6.6,
        priority=1,
    ),
    "heavy": ModelProfile(
        name="qwen3.6:27b",
        role="heavy_brain",
        estimated_vram_gb=17.0,
        priority=2,
    ),
}


def get_model(role: str) -> ModelProfile:
    try:
        return MODELS[role]
    except KeyError as exc:
        raise ValueError(f"Unknown model role: {role}") from exc


if __name__ == "__main__":
    for role, model in MODELS.items():
        print(
            f"{role}: {model.name} | "
            f"{model.role} | "
            f"{model.estimated_vram_gb} GB"
        )