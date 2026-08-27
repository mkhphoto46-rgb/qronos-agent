from __future__ import annotations

from dataclasses import dataclass

from core.model_registry import MODELS
from core.ollama_models import InstalledModel, OllamaModelCatalog
from core.storage_guard import VolumeStatus, bytes_to_gb
from core.storage_policy import (
    DEFAULT_STORAGE_THRESHOLDS,
    StorageDecision,
    StorageThresholds,
    evaluate_download,
)


@dataclass(frozen=True)
class ModelPresence:
    """Whether a model the configuration requires is actually installed."""

    role: str
    name: str
    installed: bool
    size_bytes: int = 0

    @property
    def size_gb(self) -> float:
        return bytes_to_gb(self.size_bytes)


@dataclass(frozen=True)
class EvictionCandidate:
    """
    A model that could be removed to make room.

    ``modified_at_epoch`` is Ollama's modification time, which is when the model
    was pulled or last changed — **not** when it was last used. Ollama exposes
    no last-used timestamp, so eviction ordering is least-recently-modified, an
    approximation of least-recently-used. It is named accurately here rather
    than described as LRU, because a caller who believes this is true usage data
    would draw the wrong conclusion from it.
    """

    model: InstalledModel
    modified_at_epoch: float | None

    @property
    def name(self) -> str:
        return self.model.name

    @property
    def size_bytes(self) -> int:
        return self.model.size_bytes


@dataclass(frozen=True)
class InstallPlan:
    """
    The outcome of an install preflight.

    ``decision`` is the storage verdict. ``evictions`` is a proposal only:
    nothing is deleted by building a plan, and a caller must decide whether
    removing those models is acceptable.
    """

    model_name: str
    required_bytes: int
    decision: StorageDecision
    reason: str
    already_installed: bool = False
    evictions: tuple[EvictionCandidate, ...] = ()
    freed_bytes: int = 0

    @property
    def is_allowed(self) -> bool:
        return self.decision is StorageDecision.ALLOW

    @property
    def needs_eviction(self) -> bool:
        return bool(self.evictions)


class ModelStore:
    """
    Storage-aware view of the local model store.

    Answers three questions the Resource Governor and Storage Manager need:
    which required models are missing, whether a model will fit before it is
    pulled, and what could be removed if it will not.

    Never deletes or downloads anything itself. It produces plans; the caller
    executes them through :class:`core.ollama_models.OllamaModelCatalog`. That
    separation means a preflight can be shown to the user before several
    gigabytes move in either direction.
    """

    def __init__(
        self,
        catalog: OllamaModelCatalog | None = None,
        thresholds: StorageThresholds = DEFAULT_STORAGE_THRESHOLDS,
    ) -> None:
        self.catalog = (
            catalog
            if catalog is not None
            else OllamaModelCatalog()
        )

        self.thresholds = thresholds

    # ------------------------------------------------------------- inventory

    def required_model_names(self) -> tuple[str, ...]:
        """
        Names of the models the current configuration depends on.

        Read from :mod:`core.model_registry` rather than hardcoded, so that the
        store follows the approved roster instead of duplicating it. These names
        are protected from eviction.
        """
        return tuple(
            profile.name
            for profile in MODELS.values()
        )

    def inventory(self) -> tuple[ModelPresence, ...]:
        """Report which required models are present, and their real sizes."""
        installed = {
            model.name: model
            for model in self.catalog.list_installed_models()
        }

        presence: list[ModelPresence] = []

        for role, profile in MODELS.items():
            found = installed.get(profile.name)

            presence.append(
                ModelPresence(
                    role=role,
                    name=profile.name,
                    installed=found is not None,
                    size_bytes=(
                        found.size_bytes
                        if found is not None
                        else 0
                    ),
                )
            )

        return tuple(presence)

    def missing_required_models(self) -> tuple[str, ...]:
        """Required models that are not installed."""
        return tuple(
            entry.name
            for entry in self.inventory()
            if not entry.installed
        )

    def total_installed_bytes(self) -> int:
        return self.catalog.total_installed_bytes()

    # ------------------------------------------------------------- eviction

    def eviction_candidates(
        self,
        protected: tuple[str, ...] | None = None,
    ) -> tuple[EvictionCandidate, ...]:
        """
        Models that may be removed, oldest modification first.

        Required models are never candidates. Removing one would leave the
        configuration unable to run, and the resource rules forbid loading a
        model whose requirements are unknown — a deleted model is the strongest
        possible form of unknown.

        Models with no parseable timestamp sort last, so a model of unknown age
        is evicted only after every model whose age is known.
        """
        protected_names = set(
            protected
            if protected is not None
            else self.required_model_names()
        )

        candidates = [
            EvictionCandidate(
                model=model,
                modified_at_epoch=model.modified_at_epoch,
            )
            for model in self.catalog.list_installed_models()
            if model.name not in protected_names
        ]

        return tuple(
            sorted(
                candidates,
                key=lambda candidate: (
                    candidate.modified_at_epoch is None,
                    candidate.modified_at_epoch or 0.0,
                    candidate.name,
                ),
            )
        )

    def plan_eviction_for(
        self,
        needed_bytes: int,
        protected: tuple[str, ...] | None = None,
    ) -> tuple[tuple[EvictionCandidate, ...], int]:
        """
        Choose the fewest oldest models that free at least ``needed_bytes``.

        Returns the selection and the total it frees. When nothing eligible
        exists, or the eligible total is insufficient, the selection is
        everything available and the caller can see from ``freed`` that it is
        not enough.
        """
        if needed_bytes <= 0:
            return (), 0

        chosen: list[EvictionCandidate] = []
        freed = 0

        for candidate in self.eviction_candidates(protected):
            if freed >= needed_bytes:
                break

            # A model of unknown size cannot be counted toward the shortfall.
            if candidate.size_bytes <= 0:
                continue

            chosen.append(candidate)
            freed += candidate.size_bytes

        return tuple(chosen), freed

    # ------------------------------------------------------------- preflight

    def plan_install(
        self,
        model_name: str,
        required_bytes: int,
        volume: VolumeStatus | None,
        protected: tuple[str, ...] | None = None,
    ) -> InstallPlan:
        """
        Decide whether a model may be downloaded, and what it would take.

        Fail-closed throughout. An unreadable volume, an unknown download size
        or an unreachable daemon all produce a blocking decision, because none
        of them is evidence that the download is safe.

        When the download does not fit, the plan proposes the oldest
        non-required models to remove and states plainly whether removing them
        would be enough.
        """
        if not model_name.strip():
            raise ValueError("model_name must not be empty.")

        try:
            existing = self.catalog.find_installed(model_name)
        except RuntimeError as exc:
            # An unreachable daemon must not read as "not installed", which
            # would invite a redundant multi-gigabyte download.
            return InstallPlan(
                model_name=model_name,
                required_bytes=required_bytes,
                decision=StorageDecision.BLOCK,
                reason=(
                    "The local model store could not be inspected, so it is "
                    f"unknown whether {model_name} is installed: {exc}"
                ),
            )

        if existing is not None:
            return InstallPlan(
                model_name=model_name,
                required_bytes=required_bytes,
                decision=StorageDecision.ALLOW,
                reason=(
                    f"{model_name} is already installed "
                    f"({bytes_to_gb(existing.size_bytes):.2f} GB). No "
                    "download is required."
                ),
                already_installed=True,
            )

        evaluation = evaluate_download(
            volume=volume,
            required_bytes=required_bytes,
            thresholds=self.thresholds,
        )

        if evaluation.decision is not StorageDecision.BLOCK:
            return InstallPlan(
                model_name=model_name,
                required_bytes=required_bytes,
                decision=evaluation.decision,
                reason=evaluation.reason,
            )

        # Blocked. Work out whether eviction could rescue it. With no volume
        # reading there is no shortfall to compute, so no proposal is possible.
        if volume is None:
            return InstallPlan(
                model_name=model_name,
                required_bytes=required_bytes,
                decision=StorageDecision.BLOCK,
                reason=evaluation.reason,
            )

        shortfall = (
            required_bytes
            + self.thresholds.reserve_bytes
            - volume.free_bytes
        )

        try:
            evictions, freed = self.plan_eviction_for(shortfall, protected)
        except RuntimeError as exc:
            return InstallPlan(
                model_name=model_name,
                required_bytes=required_bytes,
                decision=StorageDecision.BLOCK,
                reason=(
                    f"{evaluation.reason} Eviction candidates could not be "
                    f"listed: {exc}"
                ),
            )

        if freed >= shortfall and evictions:
            names = ", ".join(candidate.name for candidate in evictions)

            return InstallPlan(
                model_name=model_name,
                required_bytes=required_bytes,
                decision=StorageDecision.BLOCK,
                reason=(
                    f"{evaluation.reason} Removing {names} would free "
                    f"{bytes_to_gb(freed):.2f} GB, which is enough. Removal "
                    "requires approval."
                ),
                evictions=evictions,
                freed_bytes=freed,
            )

        return InstallPlan(
            model_name=model_name,
            required_bytes=required_bytes,
            decision=StorageDecision.BLOCK,
            reason=(
                f"{evaluation.reason} No combination of removable models "
                f"would free the required {bytes_to_gb(shortfall):.2f} GB."
            ),
            evictions=evictions,
            freed_bytes=freed,
        )


def main() -> None:
    """Report the local model inventory."""
    from core.storage_guard import read_volume_status
    from core.config import CONFIG

    store = ModelStore()

    print("=== Qronos Model Store ===")

    if not store.catalog.health_check():
        print("Ollama API: unavailable")
        return

    for entry in store.inventory():
        state = (
            f"{entry.size_gb:.2f} GB"
            if entry.installed
            else "MISSING"
        )
        print(f"{entry.role}: {entry.name} -> {state}")

    print(
        f"Total installed: "
        f"{bytes_to_gb(store.total_installed_bytes()):.2f} GB"
    )

    volume = read_volume_status(CONFIG.paths.models)

    if volume is not None:
        print(f"Model volume free: {volume.free_gb:.1f} GB")


if __name__ == "__main__":
    main()
