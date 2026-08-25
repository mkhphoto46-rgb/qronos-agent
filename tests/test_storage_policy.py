from __future__ import annotations

import unittest
from pathlib import Path

from core.storage_budget import BudgetComponent, ComponentBudget, ComponentUsage
from core.storage_guard import StorageStatus, VolumeStatus, gb_to_bytes
from core.storage_policy import (
    DEFAULT_STORAGE_THRESHOLDS,
    StorageDecision,
    StorageEvaluation,
    StorageThresholds,
    evaluate_component_write,
    evaluate_download,
    evaluate_storage,
    evaluate_volume,
    worst_decision,
)


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


def make_budget(
    hard_cap_bytes: int = 1_000,
    disposable: bool = True,
) -> ComponentBudget:
    return ComponentBudget(
        component=BudgetComponent.VISION_TEMP,
        root=Path("/fake"),
        hard_cap_bytes=hard_cap_bytes,
        disposable=disposable,
    )


def make_usage(
    total_bytes: int,
    partial: bool = False,
) -> ComponentUsage:
    return ComponentUsage(
        component=BudgetComponent.VISION_TEMP,
        root=Path("/fake"),
        total_bytes=total_bytes,
        partial=partial,
    )


class TestEvaluateVolume(unittest.TestCase):
    def test_roomy_volume_is_allowed(self) -> None:
        result = evaluate_volume(make_volume(free_gb=300.0))

        self.assertIs(result.decision, StorageDecision.ALLOW)
        self.assertTrue(result.is_allowed)

    def test_low_free_space_warns(self) -> None:
        # A small disk, so the absolute free-space band is reached without the
        # percentage limit also firing.
        result = evaluate_volume(make_volume(free_gb=15.0, total_gb=100.0))

        self.assertIs(result.decision, StorageDecision.WARN)

    def test_very_low_free_space_blocks(self) -> None:
        result = evaluate_volume(make_volume(free_gb=4.0))

        self.assertIs(result.decision, StorageDecision.BLOCK)
        self.assertTrue(result.is_blocked)

    def test_exactly_at_the_block_limit_blocks(self) -> None:
        result = evaluate_volume(make_volume(free_gb=8.0))

        self.assertIs(result.decision, StorageDecision.BLOCK)

    def test_percentage_limit_catches_a_nearly_full_large_disk(self) -> None:
        # 30 GB free clears the absolute limit, but on a 4 TB disk that is
        # over 99% full. The percentage limit exists for exactly this case.
        result = evaluate_volume(make_volume(free_gb=30.0, total_gb=4_000.0))

        self.assertIs(result.decision, StorageDecision.BLOCK)

    def test_absolute_limit_catches_a_small_nearly_empty_disk(self) -> None:
        # 40% used clears the percentage limit, but 6 GB free is not enough
        # for a model download.
        result = evaluate_volume(make_volume(free_gb=6.0, total_gb=10.0))

        self.assertIs(result.decision, StorageDecision.BLOCK)

    def test_low_absolute_free_on_a_large_disk_blocks_on_percentage(
        self,
    ) -> None:
        # 15 GB free clears nothing on a 500 GB disk: it is 97% full. The two
        # limits are independent safety nets and the stricter one governs.
        result = evaluate_volume(make_volume(free_gb=15.0, total_gb=500.0))

        self.assertIs(result.decision, StorageDecision.BLOCK)

    def test_reason_is_always_populated(self) -> None:
        for free in (300.0, 15.0, 4.0):
            result = evaluate_volume(make_volume(free_gb=free))

            self.assertTrue(result.reason)


class TestEvaluateStorageIsFailClosed(unittest.TestCase):
    def test_no_reading_blocks_rather_than_allowing(self) -> None:
        # The central fail-closed rule. A missing sensor is not evidence of
        # health, and treating it as "all clear" is the defect this prevents.
        result = evaluate_storage(StorageStatus())

        self.assertIs(result.decision, StorageDecision.BLOCK)
        self.assertIn("unsafe", result.reason.lower())

    def test_worst_volume_governs(self) -> None:
        status = StorageStatus(
            volumes=(
                make_volume(free_gb=400.0, path="/roomy"),
                make_volume(free_gb=2.0, path="/full"),
            )
        )

        result = evaluate_storage(status)

        self.assertIs(result.decision, StorageDecision.BLOCK)
        assert result.volume is not None
        self.assertEqual(result.volume.measured_path, Path("/full"))

    def test_warning_volume_beats_allowing_volume(self) -> None:
        status = StorageStatus(
            volumes=(
                make_volume(free_gb=400.0, path="/roomy"),
                make_volume(free_gb=15.0, total_gb=100.0, path="/tight"),
            )
        )

        self.assertIs(
            evaluate_storage(status).decision,
            StorageDecision.WARN,
        )

    def test_all_clear_reports_the_tightest_volume(self) -> None:
        status = StorageStatus(
            volumes=(
                make_volume(free_gb=400.0, path="/roomy"),
                make_volume(free_gb=100.0, path="/tighter"),
            )
        )

        result = evaluate_storage(status)

        self.assertIs(result.decision, StorageDecision.ALLOW)
        assert result.volume is not None
        self.assertEqual(result.volume.measured_path, Path("/tighter"))


class TestEvaluateDownload(unittest.TestCase):
    def test_unknown_volume_blocks(self) -> None:
        result = evaluate_download(None, gb_to_bytes(9.3))

        self.assertIs(result.decision, StorageDecision.BLOCK)

    def test_unknown_size_blocks(self) -> None:
        # A model whose size is unknown cannot be verified against free space,
        # mirroring the rule that an unbenchmarked requirement forbids loading.
        result = evaluate_download(make_volume(free_gb=400.0), 0)

        self.assertIs(result.decision, StorageDecision.BLOCK)

    def test_negative_size_blocks(self) -> None:
        result = evaluate_download(make_volume(free_gb=400.0), -1)

        self.assertIs(result.decision, StorageDecision.BLOCK)

    def test_download_that_fits_comfortably_is_allowed(self) -> None:
        result = evaluate_download(
            make_volume(free_gb=200.0),
            gb_to_bytes(9.3),
        )

        self.assertIs(result.decision, StorageDecision.ALLOW)

    def test_download_that_does_not_fit_blocks_and_states_the_shortfall(
        self,
    ) -> None:
        result = evaluate_download(
            make_volume(free_gb=10.0),
            gb_to_bytes(9.3),
        )

        self.assertIs(result.decision, StorageDecision.BLOCK)
        self.assertIn("Short by", result.reason)

    def test_reserve_is_applied_on_top_of_the_download(self) -> None:
        # 12 GB free, a 9.3 GB model and a 5 GB reserve means 14.3 GB is
        # needed. A download that would exactly fill the disk is refused.
        result = evaluate_download(
            make_volume(free_gb=12.0),
            gb_to_bytes(9.3),
        )

        self.assertIs(result.decision, StorageDecision.BLOCK)

    def test_fitting_on_a_warning_volume_warns(self) -> None:
        result = evaluate_download(
            make_volume(free_gb=19.0, total_gb=100.0),
            gb_to_bytes(2.0),
        )

        self.assertIs(result.decision, StorageDecision.WARN)
        self.assertIn("fits", result.reason)

    def test_a_custom_reserve_is_honoured(self) -> None:
        thresholds = StorageThresholds(reserve_gb=50.0)

        result = evaluate_download(
            make_volume(free_gb=60.0),
            gb_to_bytes(20.0),
            thresholds,
        )

        self.assertIs(result.decision, StorageDecision.BLOCK)


class TestEvaluateComponentWrite(unittest.TestCase):
    def test_partial_measurement_blocks(self) -> None:
        # A partial walk understates usage, so it can never prove headroom.
        result = evaluate_component_write(
            make_budget(),
            make_usage(10, partial=True),
        )

        self.assertIs(result.decision, StorageDecision.BLOCK)
        self.assertIs(result.component, BudgetComponent.VISION_TEMP)

    def test_normal_usage_allows(self) -> None:
        result = evaluate_component_write(make_budget(), make_usage(100))

        self.assertIs(result.decision, StorageDecision.ALLOW)

    def test_soft_level_allows_but_says_cleanup_should_run(self) -> None:
        result = evaluate_component_write(make_budget(), make_usage(650))

        self.assertIs(result.decision, StorageDecision.ALLOW)
        self.assertIn("cleanup", result.reason.lower())

    def test_medium_level_warns(self) -> None:
        result = evaluate_component_write(make_budget(), make_usage(850))

        self.assertIs(result.decision, StorageDecision.WARN)

    def test_hard_cap_blocks(self) -> None:
        result = evaluate_component_write(make_budget(), make_usage(1_000))

        self.assertIs(result.decision, StorageDecision.BLOCK)

    def test_projected_write_that_would_breach_blocks(self) -> None:
        # The write has not happened yet, but allowing it would breach the cap.
        result = evaluate_component_write(
            make_budget(),
            make_usage(900),
            additional_bytes=200,
        )

        self.assertIs(result.decision, StorageDecision.BLOCK)

    def test_negative_additional_bytes_are_ignored(self) -> None:
        result = evaluate_component_write(
            make_budget(),
            make_usage(100),
            additional_bytes=-5_000,
        )

        self.assertIs(result.decision, StorageDecision.ALLOW)


class TestWorstDecision(unittest.TestCase):
    def test_empty_input_blocks(self) -> None:
        self.assertIs(
            worst_decision(()).decision,
            StorageDecision.BLOCK,
        )

    def test_block_beats_warn_and_allow(self) -> None:
        evaluations = (
            StorageEvaluation(StorageDecision.ALLOW, "a"),
            StorageEvaluation(StorageDecision.WARN, "w"),
            StorageEvaluation(StorageDecision.BLOCK, "b"),
        )

        self.assertIs(
            worst_decision(evaluations).decision,
            StorageDecision.BLOCK,
        )

    def test_warn_beats_allow(self) -> None:
        evaluations = (
            StorageEvaluation(StorageDecision.ALLOW, "a"),
            StorageEvaluation(StorageDecision.WARN, "w"),
        )

        self.assertIs(
            worst_decision(evaluations).decision,
            StorageDecision.WARN,
        )

    def test_all_allow_returns_the_first(self) -> None:
        evaluations = (
            StorageEvaluation(StorageDecision.ALLOW, "first"),
            StorageEvaluation(StorageDecision.ALLOW, "second"),
        )

        self.assertEqual(worst_decision(evaluations).reason, "first")


class TestThresholdDefaults(unittest.TestCase):
    def test_reserve_bytes_matches_reserve_gb(self) -> None:
        self.assertEqual(
            DEFAULT_STORAGE_THRESHOLDS.reserve_bytes,
            gb_to_bytes(DEFAULT_STORAGE_THRESHOLDS.reserve_gb),
        )

    def test_block_limits_are_stricter_than_warn_limits(self) -> None:
        thresholds = DEFAULT_STORAGE_THRESHOLDS

        self.assertLess(thresholds.free_block_gb, thresholds.free_warn_gb)
        self.assertGreater(
            thresholds.used_block_percent,
            thresholds.used_warn_percent,
        )


if __name__ == "__main__":
    unittest.main()
