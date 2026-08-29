from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    name: str
    role: str
    estimated_vram_gb: float
    priority: int

    #: Tokens of context to allocate. Declared per model rather than left
    #: to the server, because the server's default is enormous and the
    #: memory it costs is not the model's weights.
    #:
    #: Measured on a 16 GB card with qwen3:4b-instruct, whose weights are
    #: 2.3 GB on disk:
    #:
    #:     num_ctx  262144 (server default)  ->  15,665 MiB of GPU
    #:     num_ctx   32768                   ->   7,184 MiB
    #:     num_ctx    8192                   ->   5,258 MiB
    #:
    #: The default filled 96% of the card with key/value cache for a
    #: conversation nobody was going to have, which then tripped Qronos's
    #: own VRAM ceiling and made it refuse the next thing the user said.
    context_tokens: int


# 8k for the fast brain: a spoken command and its answer, with the
# conversation history the session keeps, fits several times over. 16k for
# the heavy brain, which is the one asked to reason over a chapter.
#
# estimated_vram_gb is measured on an RTX 5080 at the context declared here,
# not taken from the file size: 3.34 GB and 9.97 GB above idle. The policy
# compares these
# against a real card, so a number that ignores the key/value cache is
# worse than no number.
MODELS = {
    "fast": ModelProfile(
        name="qwen3:4b-instruct",
        role="fast_brain",
        estimated_vram_gb=3.4,
        priority=1,
        context_tokens=8_192,
    ),
    "heavy": ModelProfile(
        name="qwen3:14b",
        role="heavy_brain",
        estimated_vram_gb=10.0,
        priority=2,
        context_tokens=16_384,
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
