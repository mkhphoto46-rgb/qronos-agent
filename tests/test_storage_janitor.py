from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.artifact_ownership import (
    ArtifactOwnershipRegistry,
    InMemoryOwnershipStore,
)
from core.storage_budget import (
    BudgetComponent,
    CleanupTrigger,
    ComponentBudget,
    measure_component,
)
from core.storage_janitor import (
    DeleteReason,
    SkipReason,
    StorageJanitor,
)


NOW = 1_800_000_000.0
DAY = 86_400.0


class JanitorTestCase(unittest.TestCase):
    """A real temporary tree, so path safety is exercised for real."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.base = Path(self._temporary.name)

        self.managed = self.base / "managed"
        self.managed.mkdir()

        self.outside = self.base / "user_documents"
        self.outside.mkdir()

        counter = {"n": 0}

        def deterministic_id() -> str:
            counter["n"] += 1
            return f"artifact-{counter['n']}"

        self.registry = ArtifactOwnershipRegistry(
            store=InMemoryOwnershipStore(),
            id_factory=deterministic_id,
        )

        self.janitor = StorageJanitor(registry=self.registry)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def budget(
        self,
        hard_cap_bytes: int = 1_000,
        max_age_seconds: float | None = None,
        disposable: bool = True,
        root: Path | None = None,
    ) -> ComponentBudget:
        return ComponentBudget(
            component=BudgetComponent.VISION_TEMP,
            root=root if root is not None else self.managed,
            hard_cap_bytes=hard_cap_bytes,
            max_age_seconds=max_age_seconds,
            disposable=disposable,
        )

    def make_file(
        self,
        name: str,
        size: int,
        age_seconds: float = 0.0,
        directory: Path | None = None,
    ) -> Path:
        target = (
            directory if directory is not None else self.managed
        ) / name

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)

        stamp = NOW - age_seconds
        os.utime(target, (stamp, stamp))

        return target

    def register(self, path: Path, size: int) -> str:
        record = self.registry.register(
            path=path,
            component=BudgetComponent.VISION_TEMP,
            size_bytes=size,
            created_at=NOW,
        )

        return record.artifact_id


class TestPlanIsPure(JanitorTestCase):
    def test_planning_deletes_nothing(self) -> None:
        target = self.make_file("big.bin", 900)
        budget = self.budget(hard_cap_bytes=100)

        self.janitor.plan(budget, measure_component(budget), NOW)

        self.assertTrue(target.is_file())

    def test_nothing_to_do_yields_an_empty_plan(self) -> None:
        self.make_file("small.bin", 10)
        budget = self.budget(hard_cap_bytes=100_000)

        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        self.assertTrue(plan.is_empty)
        self.assertIs(plan.trigger, CleanupTrigger.NONE)
        self.assertEqual(plan.reclaimable_bytes, 0)

    def test_plan_records_usage_and_target(self) -> None:
        self.make_file("a.bin", 900)
        budget = self.budget(hard_cap_bytes=1_000)

        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        self.assertEqual(plan.usage_before_bytes, 900)
        self.assertEqual(plan.target_bytes, 600)


class TestAgeTrigger(JanitorTestCase):
    def test_expired_files_are_selected_even_when_tiny(self) -> None:
        # The OR rule: a nearly empty component is still cleaned when its data
        # is stale.
        self.make_file("stale.bin", 10, age_seconds=8 * DAY)
        self.make_file("fresh.bin", 10, age_seconds=1 * DAY)

        budget = self.budget(
            hard_cap_bytes=10_000_000,
            max_age_seconds=7 * DAY,
        )

        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        names = [candidate.path.name for candidate in plan.candidates]

        self.assertEqual(names, ["stale.bin"])
        self.assertIs(plan.trigger, CleanupTrigger.AGE)
        self.assertIs(
            plan.candidates[0].reason,
            DeleteReason.EXPIRED,
        )

    def test_file_exactly_at_the_age_limit_is_selected(self) -> None:
        self.make_file("edge.bin", 10, age_seconds=7 * DAY)

        budget = self.budget(
            hard_cap_bytes=10_000_000,
            max_age_seconds=7 * DAY,
        )

        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        self.assertEqual(len(plan.candidates), 1)

    def test_no_age_limit_means_no_age_based_selection(self) -> None:
        self.make_file("ancient.bin", 10, age_seconds=5_000 * DAY)

        budget = self.budget(hard_cap_bytes=10_000_000)

        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        self.assertTrue(plan.is_empty)


class TestSizeTrigger(JanitorTestCase):
    def test_oldest_files_go_first_until_the_target_is_met(self) -> None:
        self.make_file("oldest.bin", 400, age_seconds=30 * DAY)
        self.make_file("middle.bin", 400, age_seconds=20 * DAY)
        self.make_file("newest.bin", 400, age_seconds=1 * DAY)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        names = [candidate.path.name for candidate in plan.candidates]

        # 1200 bytes against a 600-byte target: two oldest files clear it.
        self.assertEqual(names, ["oldest.bin", "middle.bin"])
        self.assertIs(plan.trigger, CleanupTrigger.SIZE)
        self.assertTrue(plan.meets_target)

    def test_selection_stops_as_soon_as_the_target_is_reached(self) -> None:
        self.make_file("oldest.bin", 900, age_seconds=30 * DAY)
        self.make_file("newest.bin", 100, age_seconds=1 * DAY)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        self.assertEqual(
            [candidate.path.name for candidate in plan.candidates],
            ["oldest.bin"],
        )

    def test_reason_is_the_size_cap_when_nothing_has_expired(self) -> None:
        self.make_file("a.bin", 900, age_seconds=1 * DAY)

        budget = self.budget(
            hard_cap_bytes=1_000,
            max_age_seconds=90 * DAY,
        )
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        self.assertIs(
            plan.candidates[0].reason,
            DeleteReason.OVER_SIZE_CAP,
        )

    def test_expired_files_are_not_double_counted(self) -> None:
        # Both triggers fire. The file must appear once, marked EXPIRED.
        self.make_file("stale.bin", 900, age_seconds=30 * DAY)

        budget = self.budget(
            hard_cap_bytes=1_000,
            max_age_seconds=7 * DAY,
        )
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        self.assertEqual(len(plan.candidates), 1)
        self.assertIs(plan.trigger, CleanupTrigger.AGE_AND_SIZE)
        self.assertIs(plan.candidates[0].reason, DeleteReason.EXPIRED)

    def test_meets_target_is_false_when_cleanup_cannot_fix_it(self) -> None:
        # Everything eligible is selected and the component is still over
        # target, because the only large file belongs to the user. Cleanup
        # cannot fix this component, and that is worth surfacing rather than
        # retrying forever.
        kept = self.make_file("kept.bin", 900, age_seconds=30 * DAY)
        artifact_id = self.register(kept, 900)
        self.registry.transfer_to_user(artifact_id, kept, NOW)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        self.assertTrue(plan.is_empty)
        self.assertFalse(plan.meets_target)


class TestSafetyRules(JanitorTestCase):
    def test_user_owned_files_are_never_selected(self) -> None:
        # The central rule. Ownership transfer is permanent and a transferred
        # artifact leaves Qronos's reach entirely.
        target = self.make_file("kept.bin", 900, age_seconds=100 * DAY)
        artifact_id = self.register(target, 900)
        self.registry.transfer_to_user(artifact_id, target, NOW)

        budget = self.budget(hard_cap_bytes=100, max_age_seconds=1 * DAY)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        self.assertTrue(plan.is_empty)
        self.assertIn(
            SkipReason.USER_OWNED,
            [item.reason for item in plan.skipped],
        )

    def test_a_non_disposable_component_is_never_swept(self) -> None:
        # Memory holds meaningful state. It is consolidated by the memory
        # manager, never deleted by a storage sweep.
        self.make_file("memory.db", 5_000, age_seconds=500 * DAY)

        budget = self.budget(
            hard_cap_bytes=1_000,
            max_age_seconds=1 * DAY,
            disposable=False,
        )
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        self.assertTrue(plan.is_empty)
        self.assertIn(
            SkipReason.COMPONENT_NOT_DISPOSABLE,
            [item.reason for item in plan.skipped],
        )

    @unittest.skipUnless(
        hasattr(os, "symlink"),
        "platform does not support symlinks",
    )
    def test_symlinks_are_refused(self) -> None:
        victim = self.outside / "user_photo.jpg"
        victim.write_bytes(b"v" * 5_000)

        link = self.managed / "link.jpg"

        try:
            os.symlink(victim, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted")

        budget = self.budget(hard_cap_bytes=100)

        # The measurement never counts a symlink, so ask the screen directly.
        reason = self.janitor._screen(link, self.managed, True)

        self.assertIs(reason, SkipReason.SYMLINK)
        self.assertTrue(victim.is_file())

    def test_a_path_outside_the_root_is_refused(self) -> None:
        stray = self.outside / "important.docx"
        stray.write_bytes(b"d" * 100)

        reason = self.janitor._screen(stray, self.managed, True)

        self.assertIs(reason, SkipReason.OUTSIDE_ROOT)

    def test_a_traversal_path_is_refused(self) -> None:
        stray = self.managed / ".." / "user_documents" / "important.docx"
        (self.outside / "important.docx").write_bytes(b"d" * 100)

        reason = self.janitor._screen(stray, self.managed, True)

        self.assertIs(reason, SkipReason.OUTSIDE_ROOT)

    def test_a_directory_is_refused(self) -> None:
        directory = self.managed / "subdir"
        directory.mkdir()

        reason = self.janitor._screen(directory, self.managed, True)

        self.assertIs(reason, SkipReason.NOT_A_FILE)

    def test_a_missing_path_is_refused(self) -> None:
        reason = self.janitor._screen(
            self.managed / "gone.bin", self.managed, True
        )

        self.assertIs(reason, SkipReason.MISSING)

    def test_a_malformed_path_is_refused_rather_than_raising(self) -> None:
        # The failure mode of screening is always inaction.
        reason = self.janitor._screen(
            Path("\0bad"), self.managed, True
        )

        self.assertIs(reason, SkipReason.UNREADABLE)

    def test_orphans_are_refused_when_not_eligible(self) -> None:
        orphan = self.make_file("orphan.bin", 10)

        reason = self.janitor._screen(orphan, self.managed, False)

        self.assertIs(reason, SkipReason.ORPHAN_NOT_ELIGIBLE)

    def test_orphans_are_allowed_for_disposable_components(self) -> None:
        # Crashed workers leave files behind, and orphan cleanup is an explicit
        # Storage Manager responsibility.
        self.make_file("orphan.bin", 900, age_seconds=30 * DAY)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        self.assertEqual(len(plan.candidates), 1)

    def test_orphan_inclusion_can_be_overridden(self) -> None:
        self.make_file("orphan.bin", 900, age_seconds=30 * DAY)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(
            budget,
            measure_component(budget),
            NOW,
            include_orphans=False,
        )

        self.assertTrue(plan.is_empty)


class TestRegistryIsUnreachable(unittest.TestCase):
    """
    The ownership registry must never be sweepable.

    Deleting it would erase the record of which files belong to the user, after
    which every one of them would look like an orphan on the next pass. This is
    the single worst failure the storage subsystem could have, so the layout
    that prevents it is asserted rather than assumed.
    """

    def test_the_registry_file_sits_outside_every_budgeted_root(self) -> None:
        from core.artifact_ownership import DEFAULT_REGISTRY_PATH
        from core.storage_budget import DEFAULT_BUDGETS

        registry = DEFAULT_REGISTRY_PATH.resolve()

        for budget in DEFAULT_BUDGETS:
            with self.subTest(component=budget.component.value):
                self.assertFalse(
                    registry.is_relative_to(budget.root.resolve()),
                    f"{registry} is inside {budget.root}, so a cleanup sweep "
                    "could delete the ownership registry.",
                )


class TestExecute(JanitorTestCase):
    def test_execute_deletes_the_selected_files(self) -> None:
        doomed = self.make_file("old.bin", 900, age_seconds=30 * DAY)
        kept = self.make_file("new.bin", 50, age_seconds=1 * DAY)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        outcome = self.janitor.execute(plan)

        self.assertFalse(doomed.exists())
        self.assertTrue(kept.is_file())
        self.assertEqual(outcome.deleted_count, 1)
        self.assertEqual(outcome.reclaimed_bytes, 900)
        self.assertTrue(outcome.fully_successful)

    def test_dry_run_deletes_nothing_but_reports_the_same(self) -> None:
        doomed = self.make_file("old.bin", 900, age_seconds=30 * DAY)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        outcome = self.janitor.execute(plan, dry_run=True)

        self.assertTrue(doomed.is_file())
        self.assertEqual(outcome.deleted_count, 1)
        self.assertEqual(outcome.reclaimed_bytes, 900)

    def test_a_file_rewritten_after_planning_is_not_deleted(self) -> None:
        # A plan may be minutes old. A file that changed is no longer the file
        # that was approved for deletion.
        target = self.make_file("old.bin", 900, age_seconds=30 * DAY)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        target.write_bytes(b"y" * 123)

        outcome = self.janitor.execute(plan)

        self.assertTrue(target.is_file())
        self.assertEqual(outcome.deleted_count, 0)
        self.assertIn(
            SkipReason.CHANGED_SINCE_PLAN,
            [item.reason for item in outcome.rejected],
        )

    def test_a_file_handed_to_the_user_after_planning_is_not_deleted(
        self,
    ) -> None:
        # The most important re-check: ownership may have moved between
        # planning and execution.
        target = self.make_file("preview.bin", 900, age_seconds=30 * DAY)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        artifact_id = self.register(target, 900)
        self.registry.transfer_to_user(artifact_id, target, NOW)

        outcome = self.janitor.execute(plan)

        self.assertTrue(target.is_file())
        self.assertEqual(outcome.deleted_count, 0)
        self.assertIn(
            SkipReason.USER_OWNED,
            [item.reason for item in outcome.rejected],
        )

    def test_a_file_deleted_externally_is_reported_as_missing(self) -> None:
        target = self.make_file("old.bin", 900, age_seconds=30 * DAY)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        target.unlink()

        outcome = self.janitor.execute(plan)

        self.assertEqual(outcome.deleted_count, 0)
        self.assertIn(
            SkipReason.MISSING,
            [item.reason for item in outcome.rejected],
        )

    def test_deleting_a_registered_artifact_drops_its_record(self) -> None:
        target = self.make_file("preview.bin", 900, age_seconds=30 * DAY)
        self.register(target, 900)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        self.janitor.execute(plan)

        self.assertEqual(len(self.registry.records), 0)

    def test_an_empty_plan_executes_cleanly(self) -> None:
        budget = self.budget(hard_cap_bytes=10_000_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        outcome = self.janitor.execute(plan)

        self.assertEqual(outcome.deleted_count, 0)
        self.assertTrue(outcome.fully_successful)

    def test_outcome_names_the_component(self) -> None:
        self.make_file("old.bin", 900, age_seconds=30 * DAY)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        outcome = self.janitor.execute(plan)

        self.assertIs(outcome.component, BudgetComponent.VISION_TEMP)


class TestPlanDescription(JanitorTestCase):
    def test_describe_mentions_the_component_and_the_total(self) -> None:
        self.make_file("old.bin", 900, age_seconds=30 * DAY)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        text = plan.describe()

        self.assertIn("vision_temp", text)
        self.assertIn("reclaimable", text)

    def test_projected_usage_reflects_the_selection(self) -> None:
        self.make_file("a.bin", 400, age_seconds=30 * DAY)
        self.make_file("b.bin", 400, age_seconds=20 * DAY)
        self.make_file("c.bin", 400, age_seconds=1 * DAY)

        budget = self.budget(hard_cap_bytes=1_000)
        plan = self.janitor.plan(budget, measure_component(budget), NOW)

        self.assertEqual(plan.projected_usage_bytes, 400)


if __name__ == "__main__":
    unittest.main()
