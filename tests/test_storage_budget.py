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
    total_hard_cap_bytes,
    total_soft_cap_bytes,
)
from core.storage_guard import gb_to_bytes


NOW = 1_800_000_000.0


def make_budget(
    root: Path,
    soft_cap_bytes: int = 600,
    hard_cap_bytes: int = 1_000,
    max_age_seconds: float | None = None,
    disposable: bool = True,
) -> ComponentBudget:
    return ComponentBudget(
        component=BudgetComponent.VISION_TEMP,
        root=root,
        soft_cap_bytes=soft_cap_bytes,
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
    def test_rejects_non_positive_soft_cap(self) -> None:
        with self.assertRaises(ValueError):
            make_budget(Path("/x"), soft_cap_bytes=0)

    def test_rejects_hard_cap_below_soft_cap(self) -> None:
        with self.assertRaises(ValueError):
            make_budget(Path("/x"), soft_cap_bytes=900, hard_cap_bytes=500)

    def test_rejects_equal_caps(self) -> None:
        # Equal caps would collapse the two-cap model into one.
        with self.assertRaises(ValueError):
            make_budget(Path("/x"), soft_cap_bytes=500, hard_cap_bytes=500)

    def test_rejects_non_positive_age(self) -> None:
        with self.assertRaises(ValueError):
            make_budget(Path("/x"), max_age_seconds=0.0)

    def test_caps_are_absolute_not_a_ratio(self) -> None:
        # Memory sits at 2 GB soft against a 15 GB hard cap, a ratio no shared
        # percentage could express alongside vision temp's 500 MB / 1.5 GB.
        budget = make_budget(
            Path("/x"), soft_cap_bytes=200, hard_cap_bytes=9_000
        )

        self.assertEqual(budget.soft_cap_bytes, 200)
        self.assertEqual(budget.hard_cap_bytes, 9_000)

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

    def test_memory_alarms_at_two_gigabytes_with_a_fifteen_gb_ceiling(
        self,
    ) -> None:
        # The case the two-cap model exists for: 2 GB is a defect detector,
        # 15 GB is the emergency ceiling.
        budget = budget_for(BudgetComponent.MEMORY)

        self.assertEqual(budget.soft_cap_bytes, gb_to_bytes(2.0))
        self.assertEqual(budget.hard_cap_bytes, gb_to_bytes(15.0))
        self.assertFalse(budget.disposable)
        self.assertIsNone(budget.max_age_seconds)

    def test_vision_temp_is_half_a_gig_soft_and_seven_days(self) -> None:
        budget = budget_for(BudgetComponent.VISION_TEMP)

        self.assertEqual(budget.soft_cap_bytes, gb_to_bytes(0.5))
        self.assertEqual(budget.hard_cap_bytes, gb_to_bytes(1.5))
        self.assertEqual(budget.max_age_seconds, 7 * SECONDS_PER_DAY)
        self.assertTrue(budget.disposable)

    def test_image_temp_exists_because_image_generation_is_on_demand(
        self,
    ) -> None:
        # Image generation is an approved on-demand capability, so its
        # temporary previews and rejected generations need a budget.
        budget = budget_for(BudgetComponent.IMAGE_TEMP)

        self.assertEqual(budget.soft_cap_bytes, gb_to_bytes(3.0))
        self.assertEqual(budget.hard_cap_bytes, gb_to_bytes(10.0))
        self.assertEqual(budget.max_age_seconds, 30 * SECONDS_PER_DAY)
        self.assertTrue(budget.disposable)

    def test_every_soft_cap_is_below_its_hard_cap(self) -> None:
        for budget in DEFAULT_BUDGETS:
            with self.subTest(component=budget.component.value):
                self.assertLess(
                    budget.soft_cap_bytes,
                    budget.hard_cap_bytes,
                )

    def test_normal_footprint_is_much_smaller_than_the_envelope(self) -> None:
        # The point of the model: routine operation stays small while a large
        # emergency envelope remains available.
        soft = total_soft_cap_bytes()
        hard = total_hard_cap_bytes()

        self.assertAlmostEqual(soft / gb_to_bytes(1.0), 6.25, places=6)
        self.assertAlmostEqual(hard / gb_to_bytes(1.0), 29.5, places=6)
        self.assertLess(soft * 4, hard)

    def test_budget_for_rejects_an_unknown_component(self) -> None:
        with self.assertRaises(ValueError):
            budget_for(BudgetComponent.MEMORY, budgets=())

    def test_image_temp_has_its_own_directory(self) -> None:
        vision = budget_for(BudgetComponent.VISION_TEMP)
        image = budget_for(BudgetComponent.IMAGE_TEMP)

        self.assertNotEqual(vision.root, image.root)


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
        self.budget = make_budget(Path("/x"), soft_cap_bytes=600, hard_cap_bytes=1_000)

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

    def test_at_hard_cap_is_hard(self) -> None:
        self.assertIs(
            classify_usage(self.budget, make_usage(1_000)),
            CapLevel.HARD,
        )

    def test_between_the_caps_is_soft_not_hard(self) -> None:
        self.assertIs(
            classify_usage(self.budget, make_usage(999)),
            CapLevel.SOFT,
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
            soft_cap_bytes=600,
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
            soft_cap_bytes=600,
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
            soft_cap_bytes=600_000,
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
            soft_cap_bytes=600,
            hard_cap_bytes=1_000,
            max_age_seconds=100.0,
        )
        usage = make_usage(900, entries=(entry("a", 900, 5_000.0),))

        self.assertIs(
            resolve_trigger(budget, usage, NOW),
            CleanupTrigger.AGE_AND_SIZE,
        )

    def test_no_age_limit_means_size_only(self) -> None:
        budget = make_budget(Path("/x"), soft_cap_bytes=600, hard_cap_bytes=1_000)
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


class TestReparsePointsAreNotWalked(unittest.TestCase):
    """
    A junction inside a Qronos directory must not import the user's files.

    ``measure_component`` documented that symlinks are never followed, because
    "following them would allow a link inside a Qronos directory to make user
    data appear to belong to a Qronos quota, and later to be selected for
    deletion". Exactly right, and it did not hold on Windows.

    A junction is a reparse point but not a symlink: ``is_symlink`` answers
    False, ``os.walk(followlinks=False)`` descends into it anyway, and the
    files behind it are ordinary files that no per-file check would catch.
    Junctions also need no privileges, unlike symlinks, so this is the easier
    of the two to create by accident or on purpose.

    Measured before the fix: a junction pointing at a documents folder added
    1.2 MB of the user's files to a 250 KB Qronos quota. Nothing was deleted —
    containment held — but the component then looked permanently over its cap,
    so cleanup would keep removing genuine scratch data chasing a limit it
    could never reach.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.work = Path(self.directory.name)
        self.root = self.work / "component"
        self.elsewhere = self.work / "elsewhere"
        self.root.mkdir()
        self.elsewhere.mkdir()

        (self.root / "scratch.bin").write_bytes(b"x" * 1_000)
        (self.elsewhere / "the_users_file.bin").write_bytes(b"y" * 50_000)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _budget(self) -> ComponentBudget:
        return ComponentBudget(
            component=BudgetComponent.VISION_TEMP,
            root=self.root,
            soft_cap_bytes=10_000,
            hard_cap_bytes=20_000,
        )

    def _make_junction(self) -> bool:
        import subprocess

        result = subprocess.run(
            [
                "cmd", "/c", "mklink", "/J",
                str(self.root / "looks_ordinary"),
                str(self.elsewhere),
            ],
            capture_output=True,
            text=True,
        )

        return result.returncode == 0

    @unittest.skipUnless(os.name == "nt", "junctions are a Windows thing")
    def test_a_junction_does_not_add_to_the_quota(self) -> None:
        if not self._make_junction():
            self.skipTest("this machine would not create a junction")

        usage = measure_component(self._budget())

        self.assertEqual(usage.total_bytes, 1_000)
        self.assertEqual(usage.file_count, 1)

    @unittest.skipUnless(os.name == "nt", "junctions are a Windows thing")
    def test_nothing_behind_a_junction_becomes_an_entry(self) -> None:
        if not self._make_junction():
            self.skipTest("this machine would not create a junction")

        names = {entry.path.name for entry in measure_component(
            self._budget()
        ).entries}

        self.assertNotIn("the_users_file.bin", names)

    def test_a_symlinked_directory_is_not_walked_either(self) -> None:
        # The case the docstring already claimed. Skipped where the platform
        # will not create one, which on Windows is most machines.
        try:
            (self.root / "linked").symlink_to(
                self.elsewhere, target_is_directory=True
            )
        except (OSError, NotImplementedError):
            self.skipTest("this machine would not create a symlink")

        self.assertEqual(measure_component(self._budget()).total_bytes, 1_000)

    def test_an_ordinary_subdirectory_is_still_walked(self) -> None:
        # The fix must not stop the measurement seeing real nested files.
        nested = self.root / "real_subdirectory"
        nested.mkdir()
        (nested / "more_scratch.bin").write_bytes(b"z" * 2_000)

        usage = measure_component(self._budget())

        self.assertEqual(usage.total_bytes, 3_000)
        self.assertEqual(usage.file_count, 2)
