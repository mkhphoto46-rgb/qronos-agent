from __future__ import annotations

import unittest
from pathlib import Path

from core.model_registry import MODELS
from core.ollama_models import InstalledModel
from core.model_store import ModelStore
from core.storage_guard import VolumeStatus, gb_to_bytes
from core.storage_policy import StorageDecision, StorageThresholds


def make_volume(
    free_gb: float,
    total_gb: float = 500.0,
) -> VolumeStatus:
    total = gb_to_bytes(total_gb)
    free = gb_to_bytes(free_gb)

    return VolumeStatus(
        requested_path=Path("/disk/models"),
        measured_path=Path("/disk"),
        total_bytes=total,
        used_bytes=total - free,
        free_bytes=free,
    )


class FakeCatalog:
    """
    Stand-in for the Ollama HTTP catalog.

    Raising is a first-class behaviour here: the store has to treat an
    unreachable daemon differently from an empty store, and the only way to
    test that is to be able to simulate the failure.
    """

    def __init__(
        self,
        models: tuple[InstalledModel, ...] = (),
        raises: bool = False,
    ) -> None:
        self.models = models
        self.raises = raises
        self.deleted: list[str] = []

    def _check(self) -> None:
        if self.raises:
            raise RuntimeError("Ollama API is unavailable: /api/tags")

    def health_check(self) -> bool:
        return not self.raises

    def list_installed_models(self) -> tuple[InstalledModel, ...]:
        self._check()
        return self.models

    def find_installed(self, model_name: str) -> InstalledModel | None:
        self._check()

        for model in self.models:
            if model.name == model_name:
                return model

        return None

    def total_installed_bytes(self) -> int:
        self._check()
        return sum(model.size_bytes for model in self.models)

    def delete_model(self, model_name: str) -> None:
        self._check()
        self.deleted.append(model_name)


def model(
    name: str,
    size_gb: float,
    modified_at_epoch: float | None = 1_800_000_000.0,
) -> InstalledModel:
    return InstalledModel(
        name=name,
        size_bytes=gb_to_bytes(size_gb),
        modified_at_epoch=modified_at_epoch,
    )


REQUIRED_NAMES = tuple(profile.name for profile in MODELS.values())


class TestInventory(unittest.TestCase):
    def test_required_names_come_from_the_registry(self) -> None:
        # Read from the registry rather than duplicated, so the store follows
        # the approved roster instead of drifting from it.
        store = ModelStore(catalog=FakeCatalog())

        self.assertEqual(
            set(store.required_model_names()),
            set(REQUIRED_NAMES),
        )

    def test_inventory_reports_missing_models(self) -> None:
        store = ModelStore(catalog=FakeCatalog())

        inventory = store.inventory()

        self.assertEqual(len(inventory), len(MODELS))
        self.assertTrue(all(not entry.installed for entry in inventory))
        self.assertEqual(
            set(store.missing_required_models()),
            set(REQUIRED_NAMES),
        )

    def test_inventory_reports_real_sizes_for_installed_models(self) -> None:
        first = REQUIRED_NAMES[0]
        store = ModelStore(catalog=FakeCatalog((model(first, 9.3),)))

        entry = next(
            item for item in store.inventory() if item.name == first
        )

        self.assertTrue(entry.installed)
        self.assertAlmostEqual(entry.size_gb, 9.3, places=3)

    def test_nothing_missing_when_everything_is_present(self) -> None:
        installed = tuple(model(name, 1.0) for name in REQUIRED_NAMES)
        store = ModelStore(catalog=FakeCatalog(installed))

        self.assertEqual(store.missing_required_models(), ())

    def test_total_installed_bytes(self) -> None:
        store = ModelStore(
            catalog=FakeCatalog((model("a", 2.0), model("b", 3.0)))
        )

        self.assertAlmostEqual(
            store.total_installed_bytes() / gb_to_bytes(1.0),
            5.0,
            places=6,
        )


class TestEvictionCandidates(unittest.TestCase):
    def test_required_models_are_never_candidates(self) -> None:
        # Removing one would leave the configuration unable to run, and a
        # deleted model is the strongest possible form of unknown requirement.
        installed = tuple(model(name, 5.0) for name in REQUIRED_NAMES)
        store = ModelStore(catalog=FakeCatalog(installed))

        self.assertEqual(store.eviction_candidates(), ())

    def test_non_required_models_are_candidates(self) -> None:
        store = ModelStore(
            catalog=FakeCatalog((model("some-old-experiment", 4.0),))
        )

        candidates = store.eviction_candidates()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].name, "some-old-experiment")

    def test_oldest_modification_time_comes_first(self) -> None:
        store = ModelStore(
            catalog=FakeCatalog(
                (
                    model("newer", 1.0, modified_at_epoch=2_000.0),
                    model("older", 1.0, modified_at_epoch=1_000.0),
                )
            )
        )

        names = [c.name for c in store.eviction_candidates()]

        self.assertEqual(names, ["older", "newer"])

    def test_models_of_unknown_age_sort_last(self) -> None:
        # A model whose age is unknown is evicted only after every model whose
        # age is known.
        store = ModelStore(
            catalog=FakeCatalog(
                (
                    model("unknown", 1.0, modified_at_epoch=None),
                    model("known", 1.0, modified_at_epoch=5_000.0),
                )
            )
        )

        names = [c.name for c in store.eviction_candidates()]

        self.assertEqual(names, ["known", "unknown"])

    def test_ordering_is_deterministic_for_equal_timestamps(self) -> None:
        store = ModelStore(
            catalog=FakeCatalog(
                (
                    model("b-model", 1.0, modified_at_epoch=1_000.0),
                    model("a-model", 1.0, modified_at_epoch=1_000.0),
                )
            )
        )

        names = [c.name for c in store.eviction_candidates()]

        self.assertEqual(names, ["a-model", "b-model"])

    def test_an_explicit_protected_list_overrides_the_registry(self) -> None:
        store = ModelStore(catalog=FakeCatalog((model("keep-me", 1.0),)))

        self.assertEqual(
            store.eviction_candidates(protected=("keep-me",)),
            (),
        )


class TestPlanEviction(unittest.TestCase):
    def test_nothing_needed_selects_nothing(self) -> None:
        store = ModelStore(catalog=FakeCatalog((model("old", 5.0),)))

        chosen, freed = store.plan_eviction_for(0)

        self.assertEqual(chosen, ())
        self.assertEqual(freed, 0)

    def test_selects_the_fewest_oldest_models_that_suffice(self) -> None:
        store = ModelStore(
            catalog=FakeCatalog(
                (
                    model("oldest", 4.0, modified_at_epoch=1_000.0),
                    model("middle", 4.0, modified_at_epoch=2_000.0),
                    model("newest", 4.0, modified_at_epoch=3_000.0),
                )
            )
        )

        chosen, freed = store.plan_eviction_for(gb_to_bytes(5.0))

        self.assertEqual([c.name for c in chosen], ["oldest", "middle"])
        self.assertGreaterEqual(freed, gb_to_bytes(5.0))

    def test_reports_an_insufficient_total_honestly(self) -> None:
        store = ModelStore(catalog=FakeCatalog((model("small", 1.0),)))

        chosen, freed = store.plan_eviction_for(gb_to_bytes(50.0))

        self.assertEqual(len(chosen), 1)
        self.assertLess(freed, gb_to_bytes(50.0))

    def test_models_of_unknown_size_are_not_counted(self) -> None:
        # An unknown size cannot be credited against the shortfall.
        store = ModelStore(
            catalog=FakeCatalog(
                (
                    InstalledModel(name="unknown-size", size_bytes=0),
                    model("known", 6.0),
                )
            )
        )

        chosen, freed = store.plan_eviction_for(gb_to_bytes(5.0))

        self.assertEqual([c.name for c in chosen], ["known"])
        self.assertGreaterEqual(freed, gb_to_bytes(5.0))


class TestPlanInstall(unittest.TestCase):
    def test_empty_name_is_rejected(self) -> None:
        store = ModelStore(catalog=FakeCatalog())

        with self.assertRaises(ValueError):
            store.plan_install("  ", 100, make_volume(400.0))

    def test_already_installed_needs_no_download(self) -> None:
        store = ModelStore(catalog=FakeCatalog((model("qwen3:14b", 9.3),)))

        plan = store.plan_install(
            "qwen3:14b", gb_to_bytes(9.3), make_volume(400.0)
        )

        self.assertTrue(plan.already_installed)
        self.assertIs(plan.decision, StorageDecision.ALLOW)
        self.assertIn("already installed", plan.reason)

    def test_a_fitting_download_is_allowed(self) -> None:
        store = ModelStore(catalog=FakeCatalog())

        plan = store.plan_install(
            "qwen3:14b", gb_to_bytes(9.3), make_volume(400.0)
        )

        self.assertIs(plan.decision, StorageDecision.ALLOW)
        self.assertFalse(plan.needs_eviction)

    def test_an_unreachable_daemon_blocks(self) -> None:
        # Must not read as "not installed", which would invite a redundant
        # multi-gigabyte download.
        store = ModelStore(catalog=FakeCatalog(raises=True))

        plan = store.plan_install(
            "qwen3:14b", gb_to_bytes(9.3), make_volume(400.0)
        )

        self.assertIs(plan.decision, StorageDecision.BLOCK)
        self.assertIn("could not be inspected", plan.reason)

    def test_no_volume_reading_blocks_and_proposes_nothing(self) -> None:
        store = ModelStore(catalog=FakeCatalog((model("old", 50.0),)))

        plan = store.plan_install("qwen3:14b", gb_to_bytes(9.3), None)

        self.assertIs(plan.decision, StorageDecision.BLOCK)
        self.assertEqual(plan.evictions, ())

    def test_insufficient_space_proposes_eviction_when_it_would_help(
        self,
    ) -> None:
        store = ModelStore(
            catalog=FakeCatalog(
                (model("old-experiment", 30.0, modified_at_epoch=1_000.0),)
            )
        )

        plan = store.plan_install(
            "qwen3:14b", gb_to_bytes(9.3), make_volume(10.0)
        )

        self.assertIs(plan.decision, StorageDecision.BLOCK)
        self.assertTrue(plan.needs_eviction)
        self.assertIn("old-experiment", plan.reason)
        self.assertIn("requires approval", plan.reason)

    def test_eviction_is_a_proposal_and_deletes_nothing(self) -> None:
        catalog = FakeCatalog(
            (model("old-experiment", 30.0, modified_at_epoch=1_000.0),)
        )
        store = ModelStore(catalog=catalog)

        store.plan_install("qwen3:14b", gb_to_bytes(9.3), make_volume(10.0))

        self.assertEqual(catalog.deleted, [])

    def test_says_so_plainly_when_eviction_cannot_help(self) -> None:
        store = ModelStore(catalog=FakeCatalog((model("tiny", 0.5),)))

        plan = store.plan_install(
            "qwen3:14b", gb_to_bytes(9.3), make_volume(10.0)
        )

        self.assertIs(plan.decision, StorageDecision.BLOCK)
        self.assertIn("No combination", plan.reason)

    def test_required_models_are_not_proposed_for_eviction(self) -> None:
        installed = tuple(model(name, 30.0) for name in REQUIRED_NAMES)
        store = ModelStore(catalog=FakeCatalog(installed))

        plan = store.plan_install(
            "something-new", gb_to_bytes(9.3), make_volume(10.0)
        )

        self.assertIs(plan.decision, StorageDecision.BLOCK)
        self.assertEqual(plan.evictions, ())

    def test_a_custom_reserve_is_honoured(self) -> None:
        store = ModelStore(
            catalog=FakeCatalog(),
            thresholds=StorageThresholds(reserve_gb=100.0),
        )

        plan = store.plan_install(
            "qwen3:14b", gb_to_bytes(9.3), make_volume(50.0)
        )

        self.assertIs(plan.decision, StorageDecision.BLOCK)

    def test_a_warning_volume_yields_a_warning_not_a_block(self) -> None:
        store = ModelStore(catalog=FakeCatalog())

        plan = store.plan_install(
            "qwen3:14b", gb_to_bytes(2.0), make_volume(19.0, total_gb=100.0)
        )

        self.assertIs(plan.decision, StorageDecision.WARN)
        self.assertFalse(plan.is_allowed)


if __name__ == "__main__":
    unittest.main()
