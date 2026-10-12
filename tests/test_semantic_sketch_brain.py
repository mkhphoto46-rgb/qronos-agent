from __future__ import annotations

from core.brain_runtime import BrainRuntime
from core.semantic_sketch import SEMANTIC_SKETCH_RESPONSE_FORMAT
from core.semantic_sketch_brain import SemanticSketchPlanner


GOOD = """
{
  "version":1,
  "name":"semantic",
  "requirements":[
    {"subject":"body","primitive":"ellipse","relation":"inside","target":"scene_root","direction":"any"},
    {"subject":"tail","primitive":"triangle","relation":"attached","target":"body","direction":"rightward"}
  ],
  "parts":[
    {"id":"scene_root","object_id":"__scene__","primitive":"group","relation":"scene_center","target":"__scene__",
     "placement":"center","scale":"large","proportion":"default","direction":"auto"},
    {"id":"body","object_id":"object","primitive":"ellipse","relation":"inside","target":"scene_root",
     "placement":"center","scale":"large","proportion":"wide","direction":"auto"},
    {"id":"tail","object_id":"object","primitive":"triangle","relation":"attached_right","target":"body",
     "placement":"center","scale":"small","proportion":"default","direction":"right"}
  ]
}
"""


class FakeRuntime(BrainRuntime):
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.stopped = []

    def health_check(self): return True

    def chat(self, model_name, prompt="", messages=None, think=False, num_predict=None,
             num_ctx=None, keep_alive="5m", response_format=None, timeout_seconds=None):
        self.calls.append({
            "model_name": model_name,
            "messages": messages,
            "think": think,
            "num_predict": num_predict,
            "keep_alive": keep_alive,
            "response_format": response_format,
        })
        return self.response

    def list_running_models(self): return []
    def stop_model(self, model_name): self.stopped.append(model_name)
    def unload_all(self): return None


def test_semantic_planner_uses_fast_brain_with_categorical_geometry_contract():
    runtime = FakeRuntime(GOOD)
    planner = SemanticSketchPlanner(runtime)
    result = planner.plan("draw an unfamiliar simple object")

    call = runtime.calls[0]
    assert result.document.name == "semantic"
    assert planner.model_name == "hf.co/unsloth/gemma-3n-E4B-it-GGUF:Q5_K_M"
    assert call["model_name"] == planner.model_name
    assert call["response_format"] is SEMANTIC_SKETCH_RESPONSE_FORMAT
    part_schema = call["response_format"]["properties"]["parts"]["items"]
    assert set(part_schema["required"]) == {
        "id", "object_id", "primitive", "relation", "target", "placement", "scale", "proportion", "direction"
    }
    assert part_schema["properties"]["target"]["type"] == "string"
    assert "allOf" not in part_schema
    assert "aspect" not in part_schema["properties"]
    assert "placement" in part_schema["properties"]
    assert "proportion" in part_schema["properties"]
    assert "requirements" in call["response_format"]["required"]
    assert "group" in part_schema["properties"]["primitive"]["enum"]
    assert "object_id" in part_schema["properties"]
    assert call["think"] is False
    assert call["num_predict"] >= 1024
    assert call["keep_alive"] == "0"
    system = call["messages"][0].content.lower()
    assert "not allowed" in system
    assert "x/y coordinates" in system
    assert "aspect ratios" in system
    assert "invisible root" in system
    assert "requirements array" in system
    assert "only allowed group" in system
    assert "every other part must be directly drawable" in system
    assert runtime.stopped == [planner.model_name]


def test_visual_feedback_is_sent_as_semantic_repair_not_raw_coordinates():
    runtime = FakeRuntime(GOOD)
    planner = SemanticSketchPlanner(runtime)
    planner.plan(
        "draw something",
        visual_feedback="- head detached from body\n- requested pose missing",
    )
    user = runtime.calls[0]["messages"][1].content
    assert "head detached from body" in user
    assert "regenerate" in user.lower()


class BudgetThenSuccessRuntime(FakeRuntime):
    def __init__(self):
        super().__init__(GOOD)
        self.chat_count = 0

    def chat(self, model_name, prompt="", messages=None, think=False, num_predict=None,
             num_ctx=None, keep_alive="5m", response_format=None, timeout_seconds=None):
        self.calls.append({
            "model_name": model_name,
            "messages": messages,
            "think": think,
            "num_predict": num_predict,
            "keep_alive": keep_alive,
            "response_format": response_format,
        })
        self.chat_count += 1
        if self.chat_count == 1:
            raise RuntimeError(
                "GPT-OSS used the full generation budget for reasoning before producing "
                "a final answer. Increase the live-test budget or use a lower reasoning level."
            )
        return GOOD


def test_semantic_planner_retries_reasoning_budget_exhaustion_once_without_thinking():
    runtime = BudgetThenSuccessRuntime()
    planner = SemanticSketchPlanner(runtime, role="heavy")

    result = planner.plan("draw a stick figure")

    assert result.document.name == "semantic"
    assert len(runtime.calls) == 2
    assert runtime.calls[0]["think"] == "low"
    assert runtime.calls[1]["think"] is False
    assert runtime.calls[0]["num_predict"] >= 1024
    assert runtime.calls[1]["num_predict"] == runtime.calls[0]["num_predict"]


class BrokenRuntime(FakeRuntime):
    def chat(self, model_name, prompt="", messages=None, think=False, num_predict=None,
             num_ctx=None, keep_alive="5m", response_format=None, timeout_seconds=None):
        self.calls.append({"think": think, "num_predict": num_predict})
        raise RuntimeError("Ollama connection failed")


def test_semantic_planner_does_not_mask_unrelated_runtime_failures():
    runtime = BrokenRuntime(GOOD)
    planner = SemanticSketchPlanner(runtime)

    try:
        planner.plan("draw something")
    except RuntimeError as exc:
        assert "connection failed" in str(exc).lower()
    else:
        raise AssertionError("Expected the unrelated runtime failure to propagate.")

    assert len(runtime.calls) == 1


MISSING_TARGET = r"""
{
  "version":1,
  "name":"bad",
  "requirements":[
    {"subject":"head","primitive":"ellipse","relation":"attached","target":"torso","direction":"any"}
  ],
  "parts":[
    {"id":"scene_root","primitive":"group","relation":"scene_center","target":"__scene__",
     "placement":"center","scale":"large","proportion":"default","direction":"auto"},
    {"id":"torso","primitive":"rectangle","relation":"inside","target":"scene_root",
     "placement":"center","scale":"medium","proportion":"tall","direction":"auto"},
    {"id":"head","primitive":"ellipse","relation":"attached_top",
     "placement":"center","scale":"small","proportion":"default","direction":"auto"}
  ]
}
"""


class MissingTargetThenSuccessRuntime(FakeRuntime):
    def __init__(self):
        super().__init__(GOOD)
        self.chat_count = 0

    def chat(self, model_name, prompt="", messages=None, think=False, num_predict=None,
             num_ctx=None, keep_alive="5m", response_format=None, timeout_seconds=None):
        self.calls.append({
            "model_name": model_name,
            "messages": messages,
            "think": think,
            "num_predict": num_predict,
            "keep_alive": keep_alive,
            "response_format": response_format,
        })
        self.chat_count += 1
        return MISSING_TARGET if self.chat_count == 1 else GOOD


def test_semantic_planner_repair_prompt_calls_out_categorical_contract():
    runtime = MissingTargetThenSuccessRuntime()
    planner = SemanticSketchPlanner(runtime)

    result = planner.plan("draw a stick figure")

    assert result.attempts == 2
    repair_user = runtime.calls[1]["messages"][1].content.lower()
    assert "missing keys" in repair_user
    assert "include requirements plus parts" in repair_user
    assert "invisible" in repair_user
    assert "scene_root" in repair_user
    assert "target='__scene__'" in repair_user
    assert "no other group is allowed" in repair_user
    assert "never output aspect ratios" in repair_user


BAD_DIRECTION = r"""
{
  "version":1,
  "name":"bad",
  "requirements":[
    {"subject":"arm","primitive":"line","relation":"attached","target":"torso","direction":"upward"}
  ],
  "parts":[
    {"id":"scene_root","primitive":"group","relation":"scene_center","target":"__scene__",
     "placement":"center","scale":"large","proportion":"default","direction":"auto"},
    {"id":"torso","primitive":"rectangle","relation":"inside","target":"scene_root",
     "placement":"center","scale":"medium","proportion":"tall","direction":"auto"},
    {"id":"arm","primitive":"line","relation":"attached_right","target":"torso",
     "placement":"top","scale":"medium","proportion":"default","direction":"vertical"}
  ]
}
"""


class BadDirectionThenSuccessRuntime(FakeRuntime):
    def __init__(self):
        super().__init__(GOOD)
        self.chat_count = 0

    def chat(self, model_name, prompt="", messages=None, think=False, num_predict=None,
             num_ctx=None, keep_alive="5m", response_format=None, timeout_seconds=None):
        self.calls.append({
            "model_name": model_name,
            "messages": messages,
            "think": think,
            "num_predict": num_predict,
            "keep_alive": keep_alive,
            "response_format": response_format,
        })
        self.chat_count += 1
        return BAD_DIRECTION if self.chat_count == 1 else GOOD


def test_semantic_planner_repair_prompt_reiterates_categorical_geometry_contract():
    runtime = BadDirectionThenSuccessRuntime()
    planner = SemanticSketchPlanner(runtime)

    result = planner.plan("draw a stick figure")

    assert result.attempts == 2
    repair_user = runtime.calls[1]["messages"][1].content.lower()
    assert "requires direction 'upward'" in repair_user
    assert "requirements plus parts" in repair_user
    assert "never output aspect ratios" in repair_user
