from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from core.ollama_models import (
    InstalledModel,
    OllamaModelCatalog,
    parse_ollama_timestamp,
)


class TestParseOllamaTimestamp(unittest.TestCase):
    def test_nanosecond_precision_is_truncated_not_rejected(self) -> None:
        # Ollama reports nine fractional digits; fromisoformat accepts six.
        result = parse_ollama_timestamp("2026-08-20T10:11:12.123456789Z")

        self.assertIsNotNone(result)
        expected = datetime(
            2026, 8, 20, 10, 11, 12, 123456, tzinfo=timezone.utc
        ).timestamp()
        self.assertAlmostEqual(result or 0.0, expected, places=3)

    def test_microsecond_precision(self) -> None:
        self.assertIsNotNone(
            parse_ollama_timestamp("2026-08-20T10:11:12.123456Z")
        )

    def test_no_fractional_part(self) -> None:
        self.assertIsNotNone(parse_ollama_timestamp("2026-08-20T10:11:12Z"))

    def test_explicit_offset_is_honoured(self) -> None:
        with_offset = parse_ollama_timestamp("2026-08-20T12:11:12+02:00")
        as_utc = parse_ollama_timestamp("2026-08-20T10:11:12Z")

        self.assertIsNotNone(with_offset)
        self.assertAlmostEqual(with_offset or 0.0, as_utc or 0.0, places=3)

    def test_none_returns_none(self) -> None:
        self.assertIsNone(parse_ollama_timestamp(None))

    def test_empty_and_whitespace_return_none(self) -> None:
        self.assertIsNone(parse_ollama_timestamp(""))
        self.assertIsNone(parse_ollama_timestamp("   "))

    def test_unparseable_returns_none_rather_than_raising(self) -> None:
        # A timestamp is only used to order eviction candidates. An unknown one
        # should degrade the ordering, never break the caller.
        self.assertIsNone(parse_ollama_timestamp("not a date"))
        self.assertIsNone(parse_ollama_timestamp("2026-13-45T99:99:99Z"))


class CatalogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = OllamaModelCatalog()

    def make_response(
        self,
        json_data: object,
        status_code: int = 200,
    ) -> Mock:
        response = Mock()
        response.status_code = status_code
        response.json.return_value = json_data

        if status_code >= 400:
            from requests import HTTPError

            response.raise_for_status.side_effect = HTTPError(
                f"HTTP {status_code}"
            )
        else:
            response.raise_for_status.return_value = None

        return response

    def tags_payload(self) -> dict[str, object]:
        return {
            "models": [
                {
                    "name": "qwen3:14b",
                    "size": 9_986_637_824,
                    "digest": "abc123",
                    "modified_at": "2026-08-20T10:11:12.123456789Z",
                },
                {
                    "name": "qwen3-vl:4b",
                    "size": 3_221_225_472,
                    "digest": "def456",
                    "modified_at": "2026-08-24T08:00:00Z",
                },
            ]
        }


class TestListInstalledModels(CatalogTestCase):
    @patch("core.ollama_models.requests.get")
    def test_parses_every_field(self, mock_get: Mock) -> None:
        mock_get.return_value = self.make_response(self.tags_payload())

        models = self.catalog.list_installed_models()

        self.assertEqual(len(models), 2)
        self.assertEqual(models[0].name, "qwen3:14b")
        self.assertEqual(models[0].size_bytes, 9_986_637_824)
        self.assertEqual(models[0].digest, "abc123")
        self.assertTrue(models[0].has_known_age)

    @patch("core.ollama_models.requests.get")
    def test_empty_store_returns_empty(self, mock_get: Mock) -> None:
        mock_get.return_value = self.make_response({"models": []})

        self.assertEqual(self.catalog.list_installed_models(), ())

    @patch("core.ollama_models.requests.get")
    def test_missing_models_key_returns_empty(self, mock_get: Mock) -> None:
        mock_get.return_value = self.make_response({})

        self.assertEqual(self.catalog.list_installed_models(), ())

    @patch("core.ollama_models.requests.get")
    def test_entries_without_a_name_are_skipped(self, mock_get: Mock) -> None:
        mock_get.return_value = self.make_response(
            {"models": [{"size": 100}, {"name": "  ", "size": 100}]}
        )

        self.assertEqual(self.catalog.list_installed_models(), ())

    @patch("core.ollama_models.requests.get")
    def test_non_dict_entries_are_skipped(self, mock_get: Mock) -> None:
        mock_get.return_value = self.make_response(
            {"models": ["nonsense", 42, {"name": "ok", "size": 5}]}
        )

        models = self.catalog.list_installed_models()

        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].name, "ok")

    @patch("core.ollama_models.requests.get")
    def test_unparseable_size_becomes_zero(self, mock_get: Mock) -> None:
        # Zero is treated downstream as an unknown size, which the storage
        # policy refuses rather than approves.
        mock_get.return_value = self.make_response(
            {"models": [{"name": "x", "size": "huge"}]}
        )

        self.assertEqual(
            self.catalog.list_installed_models()[0].size_bytes, 0
        )

    @patch("core.ollama_models.requests.get")
    def test_negative_size_becomes_zero(self, mock_get: Mock) -> None:
        mock_get.return_value = self.make_response(
            {"models": [{"name": "x", "size": -5}]}
        )

        self.assertEqual(
            self.catalog.list_installed_models()[0].size_bytes, 0
        )

    @patch("core.ollama_models.requests.get")
    def test_unreachable_daemon_raises_rather_than_reporting_empty(
        self,
        mock_get: Mock,
    ) -> None:
        # Treating an unreachable daemon as an empty store would make a
        # preflight pass when it should fail.
        from requests import ConnectionError as RequestsConnectionError

        mock_get.side_effect = RequestsConnectionError("refused")

        with self.assertRaises(RuntimeError):
            self.catalog.list_installed_models()

    @patch("core.ollama_models.requests.get")
    def test_http_error_raises(self, mock_get: Mock) -> None:
        mock_get.return_value = self.make_response({}, status_code=500)

        with self.assertRaises(RuntimeError):
            self.catalog.list_installed_models()

    @patch("core.ollama_models.requests.get")
    def test_unexpected_shape_raises(self, mock_get: Mock) -> None:
        mock_get.return_value = self.make_response(["not", "a", "dict"])

        with self.assertRaises(RuntimeError):
            self.catalog.list_installed_models()


class TestLookups(CatalogTestCase):
    @patch("core.ollama_models.requests.get")
    def test_find_installed_matches_exactly(self, mock_get: Mock) -> None:
        mock_get.return_value = self.make_response(self.tags_payload())

        self.assertIsNotNone(self.catalog.find_installed("qwen3:14b"))
        self.assertIsNone(self.catalog.find_installed("qwen3:14"))

    @patch("core.ollama_models.requests.get")
    def test_is_installed(self, mock_get: Mock) -> None:
        mock_get.return_value = self.make_response(self.tags_payload())

        self.assertTrue(self.catalog.is_installed("qwen3-vl:4b"))
        self.assertFalse(self.catalog.is_installed("llama3:70b"))

    @patch("core.ollama_models.requests.get")
    def test_total_installed_bytes_sums_every_model(
        self,
        mock_get: Mock,
    ) -> None:
        mock_get.return_value = self.make_response(self.tags_payload())

        self.assertEqual(
            self.catalog.total_installed_bytes(),
            9_986_637_824 + 3_221_225_472,
        )

    @patch("core.ollama_models.requests.get")
    def test_health_check_true_when_reachable(self, mock_get: Mock) -> None:
        mock_get.return_value = self.make_response({"version": "0.32.14"})

        self.assertTrue(self.catalog.health_check())

    @patch("core.ollama_models.requests.get")
    def test_health_check_false_when_unreachable(self, mock_get: Mock) -> None:
        from requests import ConnectionError as RequestsConnectionError

        mock_get.side_effect = RequestsConnectionError("refused")

        self.assertFalse(self.catalog.health_check())


class TestShowModel(CatalogTestCase):
    @patch("core.ollama_models.requests.post")
    def test_parses_details(self, mock_post: Mock) -> None:
        mock_post.return_value = self.make_response(
            {
                "details": {
                    "family": "qwen3",
                    "parameter_size": "14.8B",
                    "quantization_level": "Q4_K_M",
                }
            }
        )

        details = self.catalog.show_model("qwen3:14b")

        self.assertEqual(details.family, "qwen3")
        self.assertEqual(details.parameter_size, "14.8B")
        self.assertEqual(details.quantization_level, "Q4_K_M")

    @patch("core.ollama_models.requests.post")
    def test_missing_details_yields_blanks(self, mock_post: Mock) -> None:
        mock_post.return_value = self.make_response({})

        details = self.catalog.show_model("x")

        self.assertEqual(details.name, "x")
        self.assertEqual(details.family, "")


class TestMutations(CatalogTestCase):
    def test_delete_rejects_an_empty_name(self) -> None:
        with self.assertRaises(ValueError):
            self.catalog.delete_model("   ")

    def test_pull_rejects_an_empty_name(self) -> None:
        with self.assertRaises(ValueError):
            self.catalog.pull_model("")

    @patch("core.ollama_models.requests.delete")
    def test_delete_sends_the_model_name(self, mock_delete: Mock) -> None:
        mock_delete.return_value = self.make_response({})

        self.catalog.delete_model("qwen3:14b")

        _, kwargs = mock_delete.call_args
        self.assertEqual(kwargs["json"], {"name": "qwen3:14b"})

    @patch("core.ollama_models.requests.delete")
    def test_delete_failure_raises(self, mock_delete: Mock) -> None:
        from requests import ConnectionError as RequestsConnectionError

        mock_delete.side_effect = RequestsConnectionError("refused")

        with self.assertRaises(RuntimeError):
            self.catalog.delete_model("qwen3:14b")

    @patch("core.ollama_models.requests.post")
    def test_pull_requests_a_non_streaming_transfer(
        self,
        mock_post: Mock,
    ) -> None:
        mock_post.return_value = self.make_response({"status": "success"})

        self.catalog.pull_model("qwen3:14b")

        _, kwargs = mock_post.call_args
        self.assertEqual(
            kwargs["json"],
            {"name": "qwen3:14b", "stream": False},
        )

    @patch("core.ollama_models.requests.post")
    def test_pull_uses_a_bounded_but_generous_timeout(
        self,
        mock_post: Mock,
    ) -> None:
        # Unbounded would hang for ever if the daemon stopped responding
        # mid-transfer; too short would fail on a slow link.
        mock_post.return_value = self.make_response({"status": "success"})

        self.catalog.pull_model("qwen3:14b")

        _, kwargs = mock_post.call_args
        self.assertGreaterEqual(kwargs["timeout"], 600)

    @patch("core.ollama_models.requests.post")
    def test_pull_failure_raises(self, mock_post: Mock) -> None:
        from requests import Timeout

        mock_post.side_effect = Timeout("slow")

        with self.assertRaises(RuntimeError):
            self.catalog.pull_model("qwen3:14b")


class TestInstalledModel(unittest.TestCase):
    def test_has_known_age_is_false_without_a_timestamp(self) -> None:
        model = InstalledModel(name="x", size_bytes=1)

        self.assertFalse(model.has_known_age)

    def test_base_url_trailing_slash_is_trimmed(self) -> None:
        catalog = OllamaModelCatalog("http://127.0.0.1:11434/")

        self.assertEqual(catalog.base_url, "http://127.0.0.1:11434")


if __name__ == "__main__":
    unittest.main()
