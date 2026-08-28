from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.storage_guard import (
    BYTES_PER_GB,
    StorageStatus,
    VolumeStatus,
    bytes_to_gb,
    gb_to_bytes,
    is_nameable,
    read_storage_status,
    read_volume_status,
    resolve_measurable_path,
)


def make_volume(
    free_gb: float,
    total_gb: float = 100.0,
    path: str = "/fake/models",
) -> VolumeStatus:
    """Build a volume reading without touching a real disk."""
    total = gb_to_bytes(total_gb)
    free = gb_to_bytes(free_gb)

    return VolumeStatus(
        requested_path=Path(path),
        measured_path=Path(path),
        total_bytes=total,
        used_bytes=total - free,
        free_bytes=free,
    )


class TestUnitConversion(unittest.TestCase):
    def test_bytes_to_gb_uses_binary_units(self) -> None:
        self.assertEqual(bytes_to_gb(BYTES_PER_GB), 1.0)

    def test_gb_to_bytes_round_trips(self) -> None:
        self.assertEqual(bytes_to_gb(gb_to_bytes(4.0)), 4.0)

    def test_gb_to_bytes_returns_whole_bytes(self) -> None:
        self.assertIsInstance(gb_to_bytes(1.5), int)


class TestVolumeStatus(unittest.TestCase):
    def test_percentages(self) -> None:
        volume = make_volume(free_gb=25.0, total_gb=100.0)

        self.assertAlmostEqual(volume.free_percent, 25.0)
        self.assertAlmostEqual(volume.used_percent, 75.0)

    def test_zero_total_does_not_divide_by_zero(self) -> None:
        volume = VolumeStatus(
            requested_path=Path("/x"),
            measured_path=Path("/x"),
            total_bytes=0,
            used_bytes=0,
            free_bytes=0,
        )

        self.assertEqual(volume.used_percent, 0.0)
        self.assertEqual(volume.free_percent, 0.0)

    def test_can_fit_without_reserve(self) -> None:
        volume = make_volume(free_gb=10.0)

        self.assertTrue(volume.can_fit(gb_to_bytes(10.0)))
        self.assertFalse(volume.can_fit(gb_to_bytes(10.1)))

    def test_can_fit_respects_reserve(self) -> None:
        volume = make_volume(free_gb=10.0)

        self.assertTrue(
            volume.can_fit(gb_to_bytes(5.0), gb_to_bytes(5.0))
        )
        self.assertFalse(
            volume.can_fit(gb_to_bytes(6.0), gb_to_bytes(5.0))
        )

    def test_gb_properties(self) -> None:
        volume = make_volume(free_gb=12.0, total_gb=64.0)

        self.assertAlmostEqual(volume.free_gb, 12.0)
        self.assertAlmostEqual(volume.total_gb, 64.0)
        self.assertAlmostEqual(volume.used_gb, 52.0)


class TestResolveMeasurablePath(unittest.TestCase):
    def test_existing_directory_resolves_to_itself(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)

            self.assertEqual(resolve_measurable_path(root), root.resolve())

    def test_missing_directory_walks_up_to_an_existing_ancestor(self) -> None:
        # This is the fresh-install case: Qronos declares its directories in
        # config before anything has created them.
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            missing = root / "models" / "whisper" / "deep"

            self.assertEqual(resolve_measurable_path(missing), root.resolve())

    def test_file_walks_up_to_its_directory(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target = root / "a.txt"
            target.write_text("x", encoding="utf-8")

            self.assertEqual(resolve_measurable_path(target), root.resolve())


class TestReadVolumeStatus(unittest.TestCase):
    def test_reads_a_real_directory(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            volume = read_volume_status(name)

            self.assertIsNotNone(volume)
            assert volume is not None
            self.assertGreater(volume.total_bytes, 0)
            self.assertEqual(volume.measured_path, Path(name).resolve())

    def test_records_requested_and_measured_paths_separately(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            requested = Path(name) / "not" / "created" / "yet"
            volume = read_volume_status(requested)

            self.assertIsNotNone(volume)
            assert volume is not None
            self.assertEqual(volume.requested_path, requested.resolve())
            self.assertEqual(volume.measured_path, Path(name).resolve())

    def test_used_and_free_are_consistent_with_total(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            volume = read_volume_status(name)

            assert volume is not None
            self.assertLessEqual(
                volume.used_bytes + volume.free_bytes,
                volume.total_bytes + volume.total_bytes,
            )
            self.assertGreaterEqual(volume.free_bytes, 0)


class TestStorageStatus(unittest.TestCase):
    def test_empty_status_reports_empty(self) -> None:
        status = StorageStatus()

        self.assertTrue(status.is_empty)
        self.assertIsNone(status.tightest)
        self.assertEqual(status.distinct_volumes(), ())

    def test_tightest_returns_least_free_space(self) -> None:
        roomy = make_volume(free_gb=200.0, path="/a")
        tight = make_volume(free_gb=3.0, path="/b")

        status = StorageStatus(volumes=(roomy, tight))

        self.assertEqual(status.tightest, tight)

    def test_distinct_volumes_deduplicates_by_measured_path(self) -> None:
        # Several Qronos directories normally sit on the same disk. Counting
        # that disk's free space repeatedly would misrepresent the total.
        first = VolumeStatus(
            requested_path=Path("/disk/models"),
            measured_path=Path("/disk"),
            total_bytes=gb_to_bytes(100.0),
            used_bytes=gb_to_bytes(50.0),
            free_bytes=gb_to_bytes(50.0),
        )
        second = VolumeStatus(
            requested_path=Path("/disk/logs"),
            measured_path=Path("/disk"),
            total_bytes=gb_to_bytes(100.0),
            used_bytes=gb_to_bytes(50.0),
            free_bytes=gb_to_bytes(50.0),
        )

        status = StorageStatus(volumes=(first, second))

        self.assertEqual(len(status.distinct_volumes()), 1)
        self.assertEqual(status.distinct_volumes()[0], first)

    def test_for_path_finds_a_reading(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            status = read_storage_status((Path(name),))

            found = status.for_path(name)

            self.assertIsNotNone(found)

    def test_for_path_returns_none_when_absent(self) -> None:
        status = StorageStatus(volumes=(make_volume(free_gb=10.0, path="/a"),))

        self.assertIsNone(status.for_path("/definitely/not/measured"))


class TestReadStorageStatus(unittest.TestCase):
    def test_omits_unmeasurable_paths_rather_than_reporting_zero(self) -> None:
        # Reporting zero would be indistinguishable from a genuinely full disk.
        # Omission lets the policy layer tell "no reading" from "no space".
        with tempfile.TemporaryDirectory() as name:
            status = read_storage_status(
                (Path(name), Path("\0invalid"))
            )

            self.assertEqual(len(status.volumes), 1)

    def test_a_malformed_path_is_not_measured_as_another_volume(self) -> None:
        # The failure this guards against is quieter than a crash. On Windows a
        # malformed path resolves against the working directory, so walking up
        # to the nearest existing ancestor lands on a real drive and reports
        # its free space under the malformed name. A caller would see a plain
        # reading with no sign that it answered a different question.
        status = read_storage_status((Path("\0invalid"),))

        self.assertEqual(status.volumes, ())

    def test_control_characters_are_not_nameable(self) -> None:
        for text in ("\0invalid", "bad\x01name", "tab\tseparated"):
            with self.subTest(text=text):
                self.assertFalse(is_nameable(text))

    def test_an_ordinary_path_is_nameable(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            self.assertTrue(is_nameable(Path(name)))

    def test_reads_multiple_paths(self) -> None:
        with tempfile.TemporaryDirectory() as first:
            with tempfile.TemporaryDirectory() as second:
                status = read_storage_status(
                    (Path(first), Path(second))
                )

                self.assertEqual(len(status.volumes), 2)


if __name__ == "__main__":
    unittest.main()
