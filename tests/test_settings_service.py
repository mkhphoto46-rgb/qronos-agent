from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.settings_service import (
    DEFAULT_DEFINITIONS,
    InvalidSettingValue,
    SettingDefinition,
    SettingsService,
    UnknownSetting,
)


DEFINITIONS = (
    SettingDefinition(
        key="voice.enabled",
        default=True,
        description="A flag.",
    ),
    SettingDefinition(
        key="voice.timeout",
        default=2.0,
        description="A number with bounds.",
        minimum=0.5,
        maximum=10.0,
    ),
    SettingDefinition(
        key="voice.language",
        default="fa",
        description="A string from a fixed set.",
        choices=("fa", "en"),
    ),
)


def service(
    path=None,
    problems: list[str] | None = None,
) -> SettingsService:
    return SettingsService(
        DEFINITIONS,
        path=path,
        on_problem=problems.append if problems is not None else None,
    )


class TestDefaults(unittest.TestCase):
    def test_an_unset_setting_reads_as_its_default(self) -> None:
        self.assertEqual(service().get("voice.language"), "fa")

    def test_defaults_are_reported_as_defaults(self) -> None:
        instance = service()

        self.assertTrue(instance.is_default("voice.enabled"))

        instance.set("voice.enabled", False)

        self.assertFalse(instance.is_default("voice.enabled"))

    def test_every_shipped_definition_is_valid(self) -> None:
        # A definition validates its own default at construction, so this
        # catches a default that violates its own bounds or choices.
        SettingsService(DEFAULT_DEFINITIONS, path=None)

    def test_all_values_covers_every_definition(self) -> None:
        self.assertEqual(
            set(service().all_values()),
            {definition.key for definition in DEFINITIONS},
        )


class TestUnknownKeys(unittest.TestCase):
    def test_reading_an_undefined_setting_raises(self) -> None:
        # Without this, a typo reads as None forever and the feature behind it
        # silently never turns on.
        with self.assertRaises(UnknownSetting):
            service().get("voice.enabld")

    def test_writing_an_undefined_setting_raises(self) -> None:
        with self.assertRaises(UnknownSetting):
            service().set("voice.enabld", True)

    def test_a_key_cannot_be_defined_twice(self) -> None:
        with self.assertRaises(ValueError):
            SettingsService(
                (DEFINITIONS[0], DEFINITIONS[0]),
                path=None,
            )


class TestValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.service = service()

    def test_a_wrong_type_is_refused(self) -> None:
        with self.assertRaises(InvalidSettingValue):
            self.service.set("voice.language", 7)

    def test_a_number_is_not_a_flag(self) -> None:
        # bool is a subclass of int, so an unguarded isinstance check lets 1
        # through as a flag and True through as a number.
        with self.assertRaises(InvalidSettingValue):
            self.service.set("voice.enabled", 1)

    def test_a_flag_is_not_a_number(self) -> None:
        with self.assertRaises(InvalidSettingValue):
            self.service.set("voice.timeout", True)

    def test_an_integer_is_acceptable_for_a_float_setting(self) -> None:
        self.assertEqual(self.service.set("voice.timeout", 3), 3)

    def test_a_value_below_the_minimum_is_refused(self) -> None:
        with self.assertRaises(InvalidSettingValue):
            self.service.set("voice.timeout", 0.1)

    def test_a_value_above_the_maximum_is_refused(self) -> None:
        with self.assertRaises(InvalidSettingValue):
            self.service.set("voice.timeout", 99.0)

    def test_a_value_outside_the_choices_is_refused(self) -> None:
        with self.assertRaises(InvalidSettingValue):
            self.service.set("voice.language", "de")

    def test_a_rejected_value_is_not_stored(self) -> None:
        with self.assertRaises(InvalidSettingValue):
            self.service.set("voice.language", "de")

        self.assertEqual(self.service.get("voice.language"), "fa")


class TestBatchUpdate(unittest.TestCase):
    def test_a_batch_applies_together(self) -> None:
        instance = service()
        instance.update(
            {"voice.enabled": False, "voice.language": "en"}
        )

        self.assertFalse(instance.get("voice.enabled"))
        self.assertEqual(instance.get("voice.language"), "en")

    def test_one_bad_value_rejects_the_whole_batch(self) -> None:
        # A settings page applying six changes must not leave the file with
        # three of them because the fourth was wrong.
        instance = service()

        with self.assertRaises(InvalidSettingValue):
            instance.update(
                {"voice.enabled": False, "voice.language": "de"}
            )

        self.assertTrue(instance.get("voice.enabled"))


class TestPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "settings.json"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_setting_survives_a_restart(self) -> None:
        service(self.path).set("voice.language", "en")

        self.assertEqual(service(self.path).get("voice.language"), "en")

    def test_only_changed_settings_are_written(self) -> None:
        # Writing every default means a later change to a default silently does
        # not reach anybody who ever opened the settings page.
        service(self.path).set("voice.language", "en")

        raw = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(list(raw["payload"]), ["voice.language"])

    def test_resetting_removes_the_stored_value(self) -> None:
        instance = service(self.path)
        instance.set("voice.language", "en")
        instance.reset("voice.language")

        self.assertEqual(service(self.path).get("voice.language"), "fa")

    def test_resetting_everything_clears_the_file(self) -> None:
        instance = service(self.path)
        instance.update({"voice.language": "en", "voice.enabled": False})
        instance.reset_all()

        self.assertEqual(service(self.path).all_values()["voice.language"], "fa")


class TestDamagedFilesDoNotStopQronos(unittest.TestCase):
    """
    The opposite choice from the device registry, deliberately.

    A corrupt key file may mean tampering, so that one refuses to start. A
    corrupt preference file means a bad shutdown, and refusing to start a voice
    assistant over it would be absurd. The user is told either way.
    """

    def test_a_corrupt_file_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "settings.json"
            path.write_text("{ not json", encoding="utf-8")

            problems: list[str] = []
            instance = service(path, problems)

            self.assertEqual(instance.get("voice.language"), "fa")
            self.assertEqual(len(problems), 1)

    def test_a_newer_file_falls_back_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "settings.json"
            path.write_text(
                json.dumps({"schemaVersion": 99, "payload": {}}),
                encoding="utf-8",
            )

            problems: list[str] = []
            service(path, problems)

            self.assertEqual(len(problems), 1)

    def test_an_out_of_range_stored_value_is_dropped(self) -> None:
        # Bounds can tighten between versions, and the stored value is then
        # invalid through nobody's fault.
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "settings.json"
            path.write_text(
                json.dumps(
                    {"schemaVersion": 1, "payload": {"voice.timeout": 999.0}}
                ),
                encoding="utf-8",
            )

            problems: list[str] = []
            instance = service(path, problems)

            self.assertEqual(instance.get("voice.timeout"), 2.0)
            self.assertEqual(len(problems), 1)

    def test_a_setting_this_version_does_not_know_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "payload": {"from.the.future": True},
                    }
                ),
                encoding="utf-8",
            )

            problems: list[str] = []
            instance = service(path, problems)

            self.assertEqual(problems, [])
            self.assertEqual(instance.all_values()["voice.language"], "fa")


if __name__ == "__main__":
    unittest.main()
