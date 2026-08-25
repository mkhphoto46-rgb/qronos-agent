from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.storage_budget import (
    DEFAULT_BUDGETS,
    SECONDS_PER_DAY,
    BudgetComponent,
    CapLevel,
    CleanupTrigger,
    ComponentBudget,
    ComponentUsage,
    FileEntry,
    budget_for,
    classify_usage,
    measure_component,
    resolve_trigger,
    total_managed_cap_bytes,
)
from core.storage_guard import gb_to_bytes


NOW = 1_800_000_000.0


def make_budget(
    root: Path,
    hard_cap_bytes: int = 1_000,
    max_age_seconds: float | None = None,
    disposable: bool = True,
) -> ComponentBudget:
    return ComponentBudget(
        component=BudgetComponent.VISION_TEMP,
        root=root,
        hard_cap_bytes=hard_cap_bytes,
        max_age_seconds=max_age_seconds,
        disposable=disposable,
    )


def make_usage(
    total_bytes: int,
    entries: tuple[FileEntry, ...] = (),
    partial: bool = False,
) -> ComponentUsage:
    return ComponentUsage(
        component=BudgetComponent.VISION_TEMP,
        root=Path("/fake"),
        total_bytes=total_bytes,
        entries=entries,
        partial=partial,
    )


def entry(name: str, size: int, age_seconds: float) -> FileEntry:
    return FileEntry(
        path=Path("/fake") / name,
        size_bytes=size,
        modified_at=NOW - age_seconds,
    )


class TestComponentBudgetValidation(unittest.TestCase):
    def test_rejects_non_positive_hard_cap(self) -> None:
        with self.assertRaises(ValueError):
            make_budget(Path("/x"), hard_cap_bytes=0)

    def test_rejects_out_of_order_fractions(self) -> None:
        with self.assertRaises(ValueError):
            ComponentBudget(
                component=BudgetComponent.VISION_TEMP,
                root=Path("/x"),
                hard_cap_bytes=100,
                soft_fraction=0.9,
                medium_fraction=0.5,
            )

    def test_rejects_fraction_at_or_above_one(self) -> None:
        with self.assertRaises(ValueError):
            ComponentBudget(
                component=BudgetComponent.VISION_TEMP,
                root=Path("/x"),
                hard_cap_bytes=100,
                soft_fraction=0.6,
                medium_fraction=1.0,
            )

    def test_rejects_non_positive_age(self) -> None:
        with self.assertRaises(ValueError):
            make_budget(Path("/x"), max_age_seconds=0.0)

    def test_derived_caps(self) -> None:
        budget = make_budget(Path("/x"), hard_cap_bytes=1_000)

        self.assertEqual(budget.soft_cap_bytes, 600)
        self.assertEqual(budget.medium_cap_bytes, 800)

    def test_max_age_days_conversion(self) -> None:
        budget = make_budget(
            Path("/x"),
            max_age_seconds=7 * SECONDS_PER_DAY,
        )

        self.assertAlmostEqual(budget.max_age_days or 0.0, 7.0)

    def test_max_age_days_is_none_when_unset(self) -> None:
        self.assertIsNone(make_budget(Path("/x")).max_age_days)


class TestApprovedBudgets(unittest.TestCase):
    def test_every_component_has_a_budget(self) -> None:
        covered = {budget.component for budget in DEFAULT_BUDGETS}

        self.assertEqual(covered, set(BudgetComponent))

    def test_memory_is_two_gigabytes_and_not_disposable(self) -> None:
        budget = budget_for(BudgetComponent.MEMORY)

        self.assertEqual(budget.hard_cap_bytes, gb_to_bytes(2.0))
        self.assertFalse(budget.disposable)
        self.assertIsNone(budget.max_age_seconds)

    def test_vision_temp_is_half_a_gigabyte_and_seven_days(self) -> None:
        budget = budget_for(BudgetComponent.VISION_TEMP)

        self.assertEqual(budget.hard_cap_bytes, gb_to_bytes(0.5))
        self.assertEqual(budget.max_age_seconds, 7 * SECONDS_PER_DAY)
        self.assertTrue(budget.disposable)

    def test_total_managed_cap_is_about_three_and_a_half_gigabytes(
        self,
    ) -> None:
        total = total_managed_cap_bytes()

        self.assertAlmostEqual(total / gb_to_bytes(1.0), 3.5, places=6)

    def test_budget_for_rejects_an_unknown_component(self) -> None:
        with self.assertRaises(ValueError):
            budget_for(BudgetComponent.MEMORY, budgets=())


class TestFileEntry(unittest.TestCase):
    def test_age_is_measured_from_now(self) -> None:
        self.assertAlmostEqual(
            entry("a", 10, 500.0).age_seconds(NOW),
            500.0,
        )

    def test_future_modification_time_is_treated_as_brand_new(self) -> None:
        # Clock skew or a restored archive can produce a future mtime. Treating
        # it as a huge negative age would make it look impossibly old and get
        # it deleted first.
        future = FileEntry(
            path=Path("/fake/f"),
            size_bytes=1,
            modified_at=NOW + 10_000.0,
        )

        self.assertEqual(future.age_seconds(NOW), 0.0)


class TestComponentUsage(unittest.TestCase):
    def test_file_count_and_total(self) -> None:
        usage = make_usage(
            total_bytes=30,
            entries=(entry("a", 10, 1.0), entry("b", 20, 2.0)),
        )

        self.assertEqual(usage.file_count, 2)
        self.assertEqual(usage.total_bytes, 30)

    def test_entries_oldest_first(self) -> None:
        usage = make_usage(
            total_bytes=3,
            entries=(
                entry("new", 1, 10.0),
                entry("old", 1, 900.0),
                entry("mid", 1, 100.0),
            ),
        )

        names = [item.path.name for item in usage.entries_oldest_first()]

        self.assertEqual(names, ["old", "mid", "new"])

    def test_ordering_is_deterministic_for_equal_timestamps(self) -> None:
        usage = make_usage(
            total_bytes=2,
            entries=(entry("b", 1, 50.0), entry("a", 1, 50.0)),
        )

        names = [item.path.name for item in usage.entries_oldest_first()]

        self.assertEqual(names, ["a", "b"])

    def test_oldest_modified_at_is_none_when_empty(self) -> None:
        self.assertIsNone(make_usage(0).oldest_modified_at())

    def test_expired_entries_respects_the_limit(self) -> None:
        usage = make_usage(
            total_bytes=3,
            entries=(
                entry("fresh", 1, 10.0),
                entry("stale", 1, 1_000.0),
            ),
        )

        expired = usage.expired_entries(100.0, NOW)

        self.assertEqual([item.path.name for item in expired], ["stale"])

    def test_expired_entries_is_empty_without_an_age_limit(self) -> None:
        usage = make_usage(
            total_bytes=1,
            entries=(entry("ancient", 1, 10_000_000.0),),
        )

        self.assertEqual(usage.expired_entries(None, NOW), ())

    def test_entry_exactly_at_the_limit_is_expired(self) -> None:
        usage = make_usage(
            total_bytes=1,
            entries=(entry("edge", 1, 100.0),),
        )

        self.assertEqual(len(usage.expired_entries(100.0, NOW)), 1)


class TestClassifyUsage(unittest.TestCase):
    def setUp(self) -> None:
        self.budget = make_budget(Path("/x"), hard_cap_bytes=1_000)

    def test_below_soft_cap_is_normal(self) -> None:
        self.assertIs(
            classify_usage(self.budget, make_usage(599)),
            CapLevel.NORMAL,
        )

    def test_at_soft_cap_is_soft(self) -> None:
        self.assertIs(
            classify_usage(self.budget, make_usage(600)),
            CapLevel.SOFT,
        )

    def test_at_medium_cap_is_medium(self) -> None:
        self.assertIs(
            classify_usage(self.budget, make_usage(800)),
            CapLevel.MEDIUM,
        )

    def test_at_hard_cap_is_hard(self) -> None:
        self.assertIs(
            classify_usage(self.budget, make_usage(1_000)),
            CapLevel.HARD,
        )

    def test_far_over_the_hard_cap_is_still_hard_not_soft(self) -> None:
        self.assertIs(
            classify_usage(self.budget, make_usage(50_000)),
            CapLevel.HARD,
        )


class TestResolveTrigger(unittest.TestCase):
    def test_no_trigger_when_small_and_fresh(self) -> None:
        budget = make_budget(
            Path("/x"),
            hard_cap_bytes=1_000,
            max_age_seconds=100.0,
        )
        usage = make_usage(10, entries=(entry("a", 10, 5.0),))

        self.assertIs(
            resolve_trigger(budget, usage, NOW),
            CleanupTrigger.NONE,
        )

    def test_size_alone_triggers(self) -> None:
        budget = make_budget(
            Path("/x"),
            hard_cap_bytes=1_000,
            max_age_seconds=10_000.0,
        )
        usage = make_usage(900, entries=(entry("a", 900, 5.0),))

        self.assertIs(
            resolve_trigger(budget, usage, NOW),
            CleanupTrigger.SIZE,
        )

    def test_age_alone_triggers(self) -> None:
        # This is the OR rule that matters: a nearly empty component still gets
        # cleaned when its data is stale.
        budget = make_budget(
            Path("/x"),
            hard_cap_bytes=1_000_000,
            max_age_seconds=100.0,
        )
        usage = make_usage(10, entries=(entry("a", 10, 5_000.0),))

        self.assertIs(
            resolve_trigger(budget, usage, NOW),
            CleanupTrigger.AGE,
        )

    def test_both_triggers_are_reported_together(self) -> None:
        budget = make_budget(
            Path("/x"),
            hard_cap_bytes=1_000,
            max_age_seconds=100.0,
        )
        usage = make_usage(900, entries=(entry("a", 900, 5_000.0),))

        self.assertIs(
            resolve_trigger(budget, usage, NOW),
            CleanupTrigger.AGE_AND_SIZE,
        )

    def test_no_age_limit_means_size_only(self) -> None:
        budget = make_budget(Path("/x"), hard_cap_bytes=1_000)
        usage = make_usage(10, entries=(entry("a", 10, 10_000_000.0),))

        self.assertIs(
            resolve_trigger(budget, usage, NOW),
            CleanupTrigger.NONE,
        )


class TestMeasureComponent(unittest.TestCase):
    def test_missing_root_measures_zero_and_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            budget = make_budget(Path(name) / "not-created")
            usage = measure_component(budget)

            self.assertEqual(usage.total_bytes, 0)
            self.assertEqual(usage.file_count, 0)
            self.assertFalse(usage.root_exists)
            self.assertFalse(usage.partial)

    def test_totals_files_including_nested_directories(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "a.bin").write_bytes(b"x" * 100)

            nested = root / "sub" / "deeper"
            nested.mkdir(parents=True)
            (nested / "b.bin").write_bytes(b"y" * 250)

            usage = measure_component(make_budget(root))

            self.assertEqual(usage.total_bytes, 350)
            self.assertEqual(usage.file_count, 2)
            self.assertTrue(usage.root_exists)

    def test_empty_directory_measures_zero(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            usage = measure_component(make_budget(Path(name)))

            self.assertEqual(usage.total_bytes, 0)
            self.assertTrue(usage.root_exists)

    @unittest.skipUnless(
        hasattr(os, "symlink"),
        "platform does not support symlinks",
    )
    def test_symlinks_are_never_counted(self) -> None:
        # Counting a link would let a link inside a Qronos directory make user
        # data appear to belong to a Qronos quota, and later be deleted.
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            real = root / "real.bin"
            real.write_bytes(b"z" * 500)

            outside = root / "outside.bin"
            outside.write_bytes(b"w" * 999)

            managed = root / "managed"
            managed.mkdir()
            (managed / "own.bin").write_bytes(b"a" * 10)

            try:
                os.symlink(outside, managed / "link.bin")
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation not permitted")

            usage = measure_component(make_budget(managed))

            self.assertEqual(usage.total_bytes, 10)
            self.assertEqual(usage.file_count, 1)

    def test_directories_themselves_are_not_counted(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "empty_dir").mkdir()

            usage = measure_component(make_budget(root))

            self.assertEqual(usage.file_count, 0)

    def test_modification_times_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target = root / "a.bin"
            target.write_bytes(b"x" * 10)

            os.utime(target, (NOW - 5_000.0, NOW - 5_000.0))

            usage = measure_component(make_budget(root))

            self.assertAlmostEqual(
                usage.entries[0].modified_at,
                NOW - 5_000.0,
                places=0,
            )


if __name__ == "__main__":
    unittest.main()
