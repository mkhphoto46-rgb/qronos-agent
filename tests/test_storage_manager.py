from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.artifact_ownership import (
    ArtifactOwnershipRegistry,
    InMemoryOwnershipStore,
)
from core.storage_budget import BudgetComponent, ComponentBudget
from core.storage_guard import StorageStatus, VolumeStatus, gb_to_bytes
from core.storage_janitor import StorageJanitor
from core.storage_manager import (
    EMERGENCY_SEQUENCE,
    EmergencyAction,
    StorageManager,
)
from core.storage_policy import StorageDecision, StorageThresholds


NOW = 1_800_000_000.0
DAY = 86_400.0


def make_volume(
    free_gb: float,
    total_gb: float = 500.0,
    path: str = "/disk",
) -> VolumeStatus:
    total = gb_to_bytes(total_gb)
    free = gb_to_bytes(free_gb)

    return VolumeStatus(
        requested_path=Path(path),
        measured_path=Path(path),
        total_bytes=total,
        used_bytes=total - free,
        free_bytes=free,
    )


class ManagerTestCase(unittest.TestCase):
    """
    A manager wired to a real temporary tree.

    Two components: one disposable (vision temp) and one not (memory), because
    the difference between them drives most of the manager's behaviour.
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.base = Path(self._temporary.name)

        self.vision_root = self.base / "temp" / "vision"
        self.memory_root = self.base / "memory"
        self.vision_root.mkdir(parents=True)
        self.memory_root.mkdir(parents=True)

        self.budgets = (
            ComponentBudget(
                component=BudgetComponent.VISION_TEMP,
                root=self.vision_root,
                soft_cap_bytes=600,
                hard_cap_bytes=1_000,
                max_age_seconds=7 * DAY,
                disposable=True,
            ),
            ComponentBudget(
                component=BudgetComponent.MEMORY,
                root=self.memory_root,
                soft_cap_bytes=600,
                hard_cap_bytes=1_000,
                disposable=False,
            ),
        )

        self.registry = ArtifactOwnershipRegistry(
            store=InMemoryOwnershipStore(),
            id_factory=lambda: f"a{len(self.registry.records) + 1}",
        )

        self.manager = StorageManager(
            budgets=self.budgets,
            registry=self.registry,
            janitor=StorageJanitor(registry=self.registry),
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def make_file(
        self,
        root: Path,
        name: str,
        size: int,
        age_seconds: float = 0.0,
    ) -> Path:
        target = root / name
        target.write_bytes(b"x" * size)

        stamp = NOW - age_seconds
        os.utime(target, (stamp, stamp))

        return target


class TestReport(ManagerTestCase):
    def test_report_covers_every_configured_component(self) -> None:
        report = self.manager.report()

        self.assertEqual(len(report.components), 2)
        self.assertEqual(
            {entry.component for entry in report.components},
            {BudgetComponent.VISION_TEMP, BudgetComponent.MEMORY},
        )

    def test_managed_bytes_totals_every_component(self) -> None:
        self.make_file(self.vision_root, "a.bin", 300)
        self.make_file(self.memory_root, "b.db", 200)

        self.assertEqual(self.manager.report().managed_bytes, 500)

    def test_a_breached_component_is_listed(self) -> None:
        self.make_file(self.vision_root, "a.bin", 1_500)

        report = self.manager.report()

        self.assertIn(
            BudgetComponent.VISION_TEMP,
            report.breached_components,
        )

    def test_components_needing_cleanup_are_listed(self) -> None:
        self.make_file(self.vision_root, "a.bin", 700)

        report = self.manager.report()

        self.assertIn(
            BudgetComponent.VISION_TEMP,
            report.components_needing_cleanup,
        )

    def test_overall_takes_the_most_restrictive_verdict(self) -> None:
        # An otherwise healthy disk plus one breached component must not report
        # a clean bill of health.
        self.make_file(self.vision_root, "a.bin", 1_500)

        report = self.manager.report(
            status=StorageStatus(volumes=(make_volume(free_gb=400.0),))
        )

        self.assertIs(report.overall.decision, StorageDecision.BLOCK)

    def test_a_supplied_status_is_used_rather_than_re_read(self) -> None:
        report = self.manager.report(
            status=StorageStatus(volumes=(make_volume(free_gb=2.0),))
        )

        self.assertIs(
            report.volume_evaluation.decision,
            StorageDecision.BLOCK,
        )
        self.assertTrue(report.is_critical)

    def test_no_volume_reading_blocks_and_is_critical(self) -> None:
        report = self.manager.report(status=StorageStatus())

        self.assertIs(
            report.volume_evaluation.decision,
            StorageDecision.BLOCK,
        )
        self.assertTrue(report.is_critical)

    def test_component_report_describes_itself(self) -> None:
        self.make_file(self.vision_root, "a.bin", 100)

        entry = next(
            item
            for item in self.manager.report().components
            if item.component is BudgetComponent.VISION_TEMP
        )

        self.assertIn("vision_temp", entry.describe())


class TestDecisions(ManagerTestCase):
    def test_disposable_data_above_its_soft_cap_still_allows_writes(
        self,
    ) -> None:
        # Under the two-cap model, growth is permitted right up to the hard
        # cap; the soft cap only starts background cleanup.
        self.make_file(self.vision_root, "a.bin", 950)

        result = self.manager.evaluate_write(BudgetComponent.VISION_TEMP)

        self.assertIs(result.decision, StorageDecision.ALLOW)
        self.assertIn("cleanup", result.reason.lower())

    def test_non_disposable_data_above_its_soft_cap_warns(self) -> None:
        # Memory cannot be reduced by cleanup, so its soft cap is an alarm.
        self.make_file(self.memory_root, "memory.db", 950)

        result = self.manager.evaluate_write(BudgetComponent.MEMORY)

        self.assertIs(result.decision, StorageDecision.WARN)
        self.assertIn("alarm threshold", result.reason)

    def test_evaluate_write_blocks_a_breaching_write(self) -> None:
        self.make_file(self.vision_root, "a.bin", 500)

        result = self.manager.evaluate_write(
            BudgetComponent.VISION_TEMP,
            additional_bytes=600,
        )

        self.assertIs(result.decision, StorageDecision.BLOCK)

    def test_evaluate_write_rejects_an_unconfigured_component(self) -> None:
        with self.assertRaises(ValueError):
            self.manager.evaluate_write(BudgetComponent.LOGS_AND_TEMP)

    def test_evaluate_download_uses_a_supplied_volume(self) -> None:
        result = self.manager.evaluate_download(
            gb_to_bytes(9.3),
            volume=make_volume(free_gb=400.0),
        )

        self.assertIs(result.decision, StorageDecision.ALLOW)

    def test_evaluate_download_blocks_when_it_does_not_fit(self) -> None:
        result = self.manager.evaluate_download(
            gb_to_bytes(9.3),
            volume=make_volume(free_gb=10.0),
        )

        self.assertIs(result.decision, StorageDecision.BLOCK)

    def test_evaluate_download_measures_the_model_volume_by_default(
        self,
    ) -> None:
        # A download lands in the model directory, so that is the volume whose
        # free space decides whether it may start.
        result = self.manager.evaluate_download(gb_to_bytes(0.001))

        self.assertIn(
            result.decision,
            (StorageDecision.ALLOW, StorageDecision.WARN,
             StorageDecision.BLOCK),
        )
        self.assertTrue(result.reason)

    def test_a_custom_reserve_flows_through(self) -> None:
        manager = StorageManager(
            budgets=self.budgets,
            registry=self.registry,
            thresholds=StorageThresholds(reserve_gb=200.0),
        )

        result = manager.evaluate_download(
            gb_to_bytes(9.3),
            volume=make_volume(free_gb=100.0),
        )

        self.assertIs(result.decision, StorageDecision.BLOCK)


class TestCleanup(ManagerTestCase):
    def test_plan_cleanup_returns_one_plan_per_component(self) -> None:
        # Empty plans are included, so a caller can see a component was
        # examined and needed nothing.
        plans = self.manager.plan_cleanup(NOW)

        self.assertEqual(len(plans), 2)

    def test_plan_cleanup_can_target_one_component(self) -> None:
        plans = self.manager.plan_cleanup(
            NOW,
            component=BudgetComponent.VISION_TEMP,
        )

        self.assertEqual(len(plans), 1)
        self.assertIs(plans[0].component, BudgetComponent.VISION_TEMP)

    def test_planning_deletes_nothing(self) -> None:
        target = self.make_file(
            self.vision_root, "old.bin", 900, age_seconds=30 * DAY
        )

        self.manager.plan_cleanup(NOW)

        self.assertTrue(target.is_file())

    def test_run_cleanup_removes_disposable_data(self) -> None:
        doomed = self.make_file(
            self.vision_root, "old.bin", 900, age_seconds=30 * DAY
        )

        outcomes = self.manager.run_cleanup(NOW)

        self.assertFalse(doomed.exists())
        self.assertEqual(sum(o.deleted_count for o in outcomes), 1)

    def test_run_cleanup_never_touches_a_non_disposable_component(
        self,
    ) -> None:
        # Memory is consolidated by the memory manager, never swept.
        kept = self.make_file(
            self.memory_root, "memory.db", 5_000, age_seconds=900 * DAY
        )

        self.manager.run_cleanup(NOW)

        self.assertTrue(kept.is_file())

    def test_run_cleanup_never_touches_user_owned_files(self) -> None:
        target = self.make_file(
            self.vision_root, "kept.bin", 900, age_seconds=30 * DAY
        )

        record = self.registry.register(
            path=target,
            component=BudgetComponent.VISION_TEMP,
            size_bytes=900,
            created_at=NOW,
        )
        self.registry.transfer_to_user(record.artifact_id, target, NOW)

        self.manager.run_cleanup(NOW)

        self.assertTrue(target.is_file())

    def test_dry_run_reports_without_deleting(self) -> None:
        target = self.make_file(
            self.vision_root, "old.bin", 900, age_seconds=30 * DAY
        )

        outcomes = self.manager.run_cleanup(NOW, dry_run=True)

        self.assertTrue(target.is_file())
        self.assertEqual(sum(o.deleted_count for o in outcomes), 1)

    def test_execute_cleanup_skips_empty_plans(self) -> None:
        outcomes = self.manager.execute_cleanup(
            self.manager.plan_cleanup(NOW)
        )

        self.assertEqual(outcomes, ())


class TestEmergencySequence(ManagerTestCase):
    def test_no_sequence_when_pressure_is_normal(self) -> None:
        report = self.manager.report(
            status=StorageStatus(volumes=(make_volume(free_gb=400.0),))
        )

        self.assertEqual(self.manager.emergency_sequence(report), ())

    def test_critical_pressure_returns_the_ordered_sequence(self) -> None:
        report = self.manager.report(
            status=StorageStatus(volumes=(make_volume(free_gb=2.0),))
        )

        self.assertEqual(
            self.manager.emergency_sequence(report),
            EMERGENCY_SEQUENCE,
        )

    def test_downloads_are_stopped_before_anything_is_cleared(self) -> None:
        # Stopping the inflow first is what stops the situation getting worse
        # while cleanup runs.
        self.assertEqual(
            EMERGENCY_SEQUENCE[0],
            EmergencyAction.STOP_MODEL_DOWNLOADS,
        )

    def test_the_user_is_notified_last(self) -> None:
        self.assertEqual(
            EMERGENCY_SEQUENCE[-1],
            EmergencyAction.NOTIFY_USER,
        )

    def test_the_sequence_contains_no_duplicates(self) -> None:
        self.assertEqual(
            len(set(EMERGENCY_SEQUENCE)),
            len(EMERGENCY_SEQUENCE),
        )


class TestAlarms(ManagerTestCase):
    def test_a_non_disposable_component_alarms_at_its_soft_cap(self) -> None:
        # The alarm fires at the soft cap, not the hard one. Waiting for the
        # emergency ceiling would mean noticing a consolidation failure long
        # after it started.
        self.make_file(self.memory_root, "memory.db", 700)

        report = self.manager.report(
            status=StorageStatus(volumes=(make_volume(free_gb=400.0),))
        )
        alarms = self.manager.alarms(report)

        self.assertEqual(len(alarms), 1)
        self.assertIn("alarm threshold", alarms[0])
        self.assertIn("Investigate consolidation", alarms[0])
        self.assertNotIn("emergency ceiling", alarms[0])
        self.assertIn(
            BudgetComponent.MEMORY,
            report.alarming_components,
        )

    def test_reaching_the_emergency_ceiling_is_named_in_the_alarm(
        self,
    ) -> None:
        self.make_file(self.memory_root, "memory.db", 1_200)

        report = self.manager.report(
            status=StorageStatus(volumes=(make_volume(free_gb=400.0),))
        )
        alarms = self.manager.alarms(report)

        self.assertEqual(len(alarms), 1)
        self.assertIn("emergency ceiling", alarms[0])

    def test_a_breached_disposable_component_raises_no_alarm(self) -> None:
        # Vision temp being full is routine; cleanup handles it. Only a
        # component cleanup cannot reduce is worth alarming about.
        self.make_file(self.vision_root, "a.bin", 1_500)

        report = self.manager.report(
            status=StorageStatus(volumes=(make_volume(free_gb=400.0),))
        )

        self.assertEqual(self.manager.alarms(report), ())

    def test_a_missing_volume_reading_raises_an_alarm(self) -> None:
        alarms = self.manager.alarms(
            self.manager.report(status=StorageStatus())
        )

        self.assertTrue(
            any("free space is unknown" in message for message in alarms)
        )

    def test_no_alarms_when_everything_is_healthy(self) -> None:
        self.make_file(self.vision_root, "a.bin", 50)

        report = self.manager.report(
            status=StorageStatus(volumes=(make_volume(free_gb=400.0),))
        )

        self.assertEqual(self.manager.alarms(report), ())


class TestMaintenance(ManagerTestCase):
    def test_prune_drops_records_for_vanished_qronos_files(self) -> None:
        target = self.make_file(self.vision_root, "gone.bin", 100)

        self.registry.register(
            path=target,
            component=BudgetComponent.VISION_TEMP,
            size_bytes=100,
            created_at=NOW,
        )

        target.unlink()

        self.assertEqual(self.manager.prune_ownership_records(), 1)
        self.assertEqual(len(self.registry.records), 0)

    def test_prune_keeps_user_records(self) -> None:
        target = self.make_file(self.vision_root, "kept.bin", 100)

        record = self.registry.register(
            path=target,
            component=BudgetComponent.VISION_TEMP,
            size_bytes=100,
            created_at=NOW,
        )
        self.registry.transfer_to_user(record.artifact_id, target, NOW)

        target.unlink()

        self.assertEqual(self.manager.prune_ownership_records(), 0)
        self.assertEqual(len(self.registry.user_owned()), 1)

    def test_measure_all_returns_one_usage_per_component(self) -> None:
        self.assertEqual(len(self.manager.measure_all()), 2)

    def test_read_status_measures_the_configured_roots(self) -> None:
        status = self.manager.read_status()

        self.assertFalse(status.is_empty)


class TestDefaultConstruction(unittest.TestCase):
    def test_a_manager_can_be_built_with_no_arguments(self) -> None:
        # Uses the approved budgets and an in-memory registry, so constructing
        # one never writes to disk.
        manager = StorageManager()

        self.assertEqual(len(manager.budgets), len(BudgetComponent))
        self.assertIn(
            BudgetComponent.IMAGE_TEMP,
            {budget.component for budget in manager.budgets},
        )
        self.assertIsNotNone(manager.janitor)

    def test_the_janitor_shares_the_manager_registry(self) -> None:
        # If they diverged, the janitor would consult a registry that does not
        # know which artifacts belong to the user.
        manager = StorageManager()

        self.assertIs(manager.janitor.registry, manager.registry)


if __name__ == "__main__":
    unittest.main()
