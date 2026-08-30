from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from core.ollama_controller import (
    OllamaController,
    OllamaModelStatus,
)


class TestOllamaController(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = OllamaController()

    def make_response(
        self,
        json_data: dict,
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

    @patch("core.ollama_controller.requests.get")
    def test_health_check_returns_true_when_api_is_available(
        self,
        mock_get: Mock,
    ) -> None:
        mock_get.return_value = self.make_response(
            {
                "version": "0.32.14",
            }
        )

        result = self.controller.health_check()

        self.assertTrue(result)

        mock_get.assert_called_once_with(
            "http://127.0.0.1:11434/api/version",
            timeout=3,
        )

    @patch("core.ollama_controller.requests.get")
    def test_health_check_returns_false_when_api_is_unavailable(
        self,
        mock_get: Mock,
    ) -> None:
        import requests

        mock_get.side_effect = requests.RequestException(
            "connection refused"
        )

        result = self.controller.health_check()

        self.assertFalse(result)

    @patch("core.ollama_controller.requests.get")
    def test_list_running_models_returns_empty_list(
        self,
        mock_get: Mock,
    ) -> None:
        mock_get.return_value = self.make_response(
            {
                "models": [],
            }
        )

        result = self.controller.list_running_models()

        self.assertEqual(result, [])

        mock_get.assert_called_once_with(
            "http://127.0.0.1:11434/api/ps",
            timeout=3,
        )

    @patch("core.ollama_controller.requests.get")
    def test_list_running_models_parses_model_data(
        self,
        mock_get: Mock,
    ) -> None:
        mock_get.return_value = self.make_response(
            {
                "models": [
                    {
                        "name": "qwen3:4b",
                        "size": "6594462816",
                        "processor": "100% GPU",
                        "context_length": 4096,
                        "expires_at": "2026-08-19T16:30:00Z",
                    }
                ]
            }
        )

        result = self.controller.list_running_models()

        self.assertEqual(len(result), 1)
        self.assertIsInstance(
            result[0],
            OllamaModelStatus,
        )

        self.assertEqual(
            result[0].name,
            "qwen3:4b",
        )

        self.assertEqual(
            result[0].size,
            "6594462816",
        )

        self.assertEqual(
            result[0].processor,
            "100% GPU",
        )

        self.assertEqual(
            result[0].context,
            4096,
        )

        self.assertEqual(
            result[0].until,
            "2026-08-19T16:30:00Z",
        )

    @patch("core.ollama_controller.requests.get")
    def test_list_running_models_raises_when_api_unavailable(
        self,
        mock_get: Mock,
    ) -> None:
        import requests

        mock_get.side_effect = requests.RequestException(
            "connection refused"
        )

        with self.assertRaises(RuntimeError) as context:
            self.controller.list_running_models()

        self.assertEqual(
            str(context.exception),
            "Ollama API is unavailable.",
        )

    @patch("core.ollama_controller.requests.post")
    def test_stop_model_sends_unload_request(
        self,
        mock_post: Mock,
    ) -> None:
        mock_post.return_value = self.make_response(
            {
                "response": "",
            }
        )

        self.controller.stop_model(
            "qwen3:4b"
        )

        mock_post.assert_called_once_with(
            "http://127.0.0.1:11434/api/generate",
            json={
                "model": "qwen3:4b",
                "prompt": "",
                "keep_alive": 0,
                "stream": False,
            },
            timeout=10,
        )

    @patch("core.ollama_controller.requests.post")
    def test_stop_model_raises_when_request_fails(
        self,
        mock_post: Mock,
    ) -> None:
        import requests

        mock_post.side_effect = requests.RequestException(
            "connection refused"
        )

        with self.assertRaises(RuntimeError) as context:
            self.controller.stop_model(
                "qwen3:4b"
            )

        self.assertEqual(
            str(context.exception),
            "Could not stop model: qwen3:4b",
        )

    @patch.object(
        OllamaController,
        "list_running_models",
    )
    @patch.object(
        OllamaController,
        "stop_model",
    )
    def test_unload_all_stops_every_running_model(
        self,
        mock_stop_model: Mock,
        mock_list_running_models: Mock,
    ) -> None:
        mock_list_running_models.return_value = [
            OllamaModelStatus(
                name="qwen3:4b",
                size="6594462816",
                processor="100% GPU",
                context=4096,
                until="later",
            ),
            OllamaModelStatus(
                name="qwen3:14b",
                size="17420420832",
                processor="100% GPU",
                context=4096,
                until="later",
            ),
        ]

        self.controller.unload_all()

        self.assertEqual(
            mock_stop_model.call_count,
            2,
        )

        mock_stop_model.assert_any_call(
            "qwen3:4b"
        )

        mock_stop_model.assert_any_call(
            "qwen3:14b"
        )

    @patch("core.ollama_controller.requests.post")
    def test_chat_sends_expected_payload(
        self,
        mock_post: Mock,
    ) -> None:
        mock_post.return_value = self.make_response(
            {
                "message": {
                    "content": "Qronos test passed."
                }
            }
        )

        result = self.controller.chat(
            model_name="qwen3:4b",
            prompt="Reply with exactly: Qronos test passed.",
            think=False,
            num_predict=20,
            keep_alive="0",
        )

        self.assertEqual(
            result,
            "Qronos test passed.",
        )

        mock_post.assert_called_once_with(
            "http://127.0.0.1:11434/api/chat",
            json={
                "model": "qwen3:4b",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Reply with exactly: "
                            "Qronos test passed."
                        ),
                    }
                ],
                "think": False,
                "stream": False,
                "keep_alive": "0",
                "options": {
                    "num_predict": 20,
                },
            },
            timeout=600,
        )

    @patch("core.ollama_controller.requests.post")
    def test_chat_without_num_predict_uses_empty_options(
        self,
        mock_post: Mock,
    ) -> None:
        mock_post.return_value = self.make_response(
            {
                "message": {
                    "content": "ready"
                }
            }
        )

        result = self.controller.chat(
            model_name="qwen3:4b",
            prompt="Reply with one word: ready.",
            think=False,
            keep_alive="5m",
        )

        self.assertEqual(
            result,
            "ready",
        )

        self.assertEqual(
            mock_post.call_args.kwargs["json"]["options"],
            {},
        )

    @patch("core.ollama_controller.requests.post")
    def test_chat_raises_when_request_fails(
        self,
        mock_post: Mock,
    ) -> None:
        import requests

        mock_post.side_effect = requests.RequestException(
            "connection refused"
        )

        with self.assertRaises(RuntimeError) as context:
            self.controller.chat(
                model_name="qwen3:4b",
                prompt="test",
            )

        self.assertEqual(
            str(context.exception),
            "Could not send request to model: qwen3:4b",
        )


if __name__ == "__main__":
    unittest.main()
