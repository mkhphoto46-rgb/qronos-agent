from __future__ import annotations

from core.brain_runtime import BrainRuntime, BrainRuntimeModelStatus
from core.sketch_brain import SKETCH_RESPONSE_FORMAT, SketchBrainPlanner
from core.sketch_dsl import SketchDslError


GOOD = """
{
  "version": 1,
  "name": "unknown-object",
  "operations": [
    {"type": "ellipse", "cx": 0.45, "cy": 0.50, "rx": 0.20, "ry": 0.12},
    {"type": "polygon", "points": [[0.65,0.50],[0.84,0.35],[0.84,0.65]]}
  ]
}
"""


class FakeRuntime(BrainRuntime):
    def __init__(self, response: str | list[str]):
        self.responses = list(response) if isinstance(response, list) else [response]
        self.calls = []
        self._index = 0

    def health_check(self) -> bool:
        return True

    def chat(self, model_name, prompt="", messages=None, think=False, num_predict=None,
             num_ctx=None, keep_alive="5m", response_format=None, timeout_seconds=None):
        self.calls.append(
            {
                "model_name": model_name,
                "messages": messages,
                "response_format": response_format,
                "timeout_seconds": timeout_seconds,
                "num_predict": num_predict,
            }
        )
        response = self.responses[min(self._index, len(self.responses) - 1)]
        self._index += 1
        return response

    def list_running_models(self):
        return []

    def stop_model(self, model_name):
        return None

    def unload_all(self):
        return None


def test_local_brain_output_is_schema_constrained_then_strictly_reparsed():
    runtime = FakeRuntime(GOOD)
    result = SketchBrainPlanner(runtime, role="fast").plan("draw an unfamiliar simple object")

    assert result.plan.name == "unknown-object"
    assert len(result.plan.strokes) == 2
    assert runtime.calls[0]["response_format"] is SKETCH_RESPONSE_FORMAT
    assert runtime.calls[0]["messages"][0].role.value == "system"
    assert runtime.calls[0]["num_predict"] >= 768
    assert result.attempts == 1


def test_brain_prose_is_rejected_even_if_it_contains_json():
    runtime = FakeRuntime("Sure! " + GOOD)
    try:
        SketchBrainPlanner(runtime, role="fast").plan("draw something")
    except SketchDslError:
        pass
    else:
        raise AssertionError("Surrounding model prose must fail closed.")


def test_malformed_json_is_repaired_by_bounded_regeneration():
    broken = '{"version":1,"operations":[{"type":"line","start":[0.1,0.1],}]}'
    runtime = FakeRuntime([broken, GOOD])

    result = SketchBrainPlanner(runtime, role="fast").plan("draw something")

    assert result.attempts == 2
    assert len(runtime.calls) == 2
    repair_user = runtime.calls[1]["messages"][1].content
    assert "previous response was rejected" in repair_user
    assert "Validation error:" in repair_user
    assert broken not in repair_user
    assert runtime.calls[1]["response_format"] is SKETCH_RESPONSE_FORMAT


def test_repair_loop_stays_fail_closed_after_bounded_attempts():
    broken = '{"version":1,"operations":[}'
    runtime = FakeRuntime(broken)

    try:
        SketchBrainPlanner(runtime, role="fast").plan("draw something")
    except SketchDslError as exc:
        assert "after 3 attempt(s)" in str(exc)
    else:
        raise AssertionError("Repeated malformed model output must fail closed.")

    assert len(runtime.calls) == 3


def test_repair_can_be_disabled_for_single_attempt_callers():
    runtime = FakeRuntime("Sure! " + GOOD)

    try:
        SketchBrainPlanner(
            runtime, role="fast", max_repair_attempts=0
        ).plan("draw something")
    except SketchDslError:
        pass
    else:
        raise AssertionError("Disabled repair must preserve one-shot fail-closed behavior.")

    assert len(runtime.calls) == 1


def test_invalid_repair_attempt_bound_is_rejected():
    runtime = FakeRuntime(GOOD)
    for value in (-1, 3, True, 1.5):
        try:
            SketchBrainPlanner(runtime, role="fast", max_repair_attempts=value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Invalid max_repair_attempts accepted: {value!r}")
