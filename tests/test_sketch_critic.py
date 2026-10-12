from __future__ import annotations

import pytest

from core.brain_runtime import BrainRuntime
from core.sketch_critic import (
    CRITIC_RESPONSE_FORMAT,
    SketchCriticError,
    SketchVisualCritic,
    parse_critic_json,
)
from core.shape_planner import ShapePlanner
from core.sketch_preview import SketchPreviewRenderer


PASS = """
{
  "verdict":"pass",
  "recognizable":true,
  "request_satisfied":true,
  "composition_coherent":true,
  "confidence":0.91,
  "summary":"The requested simple subject is recognizable.",
  "issues":[]
}
"""

FAIL = """
{
  "verdict":"fail",
  "recognizable":false,
  "request_satisfied":false,
  "composition_coherent":false,
  "confidence":0.88,
  "summary":"Major parts are disconnected.",
  "issues":["The main components do not form a recognizable subject."]
}
"""


class FakeRuntime(BrainRuntime):
    def __init__(self, responses):
        self.responses = list(responses) if isinstance(responses, list) else [responses]
        self.calls = []
        self.stopped = []
        self.index = 0

    def health_check(self): return True

    def chat(self, model_name, prompt="", messages=None, think=False, num_predict=None,
             num_ctx=None, keep_alive="5m", response_format=None, timeout_seconds=None):
        self.calls.append({
            "model_name": model_name,
            "messages": messages,
            "keep_alive": keep_alive,
            "response_format": response_format,
        })
        response = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        return response

    def list_running_models(self): return []
    def stop_model(self, model_name): self.stopped.append(model_name)
    def unload_all(self): return None


def test_critic_pass_requires_all_semantic_flags():
    result = parse_critic_json(PASS, model_name="vision")
    assert result.passes()
    assert result.model_name == "vision"


def test_pass_word_does_not_override_failed_semantic_flag():
    text = PASS.replace('"composition_coherent":true', '"composition_coherent":false')
    result = parse_critic_json(text)
    assert not result.passes()


def test_critic_fail_generates_actionable_feedback():
    result = parse_critic_json(FAIL)
    assert not result.passes()
    assert "disconnected" in result.feedback().lower()


def test_malformed_critic_json_is_retried_once_and_stays_image_grounded():
    runtime = FakeRuntime(["not-json", PASS])
    preview = SketchPreviewRenderer().render(ShapePlanner.circle()).image

    result = SketchVisualCritic(runtime).critique("draw a circle", preview)

    assert result.passes()
    assert len(runtime.calls) == 2
    assert runtime.calls[0]["response_format"] is CRITIC_RESPONSE_FORMAT
    assert runtime.calls[0]["messages"][0].images == (preview,)
    assert runtime.calls[1]["messages"][0].images == (preview,)
    assert runtime.stopped


def test_repeated_malformed_critic_output_fails_closed():
    runtime = FakeRuntime("bad")
    preview = SketchPreviewRenderer().render(ShapePlanner.line()).image
    with pytest.raises(SketchCriticError, match="after 2 attempt"):
        SketchVisualCritic(runtime).critique("draw a line", preview)
