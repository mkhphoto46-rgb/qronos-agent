from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.semantic_sketch import SemanticSketchError, parse_semantic_sketch_json
from core.semantic_sketch_pipeline import (
    SemanticSketchPipeline,
    SemanticSketchRejected,
)
from core.sketch_critic import parse_critic_json
from core.sketch_preview import SketchPreviewRenderer


DOC = parse_semantic_sketch_json("""
{
  "version":1,
  "name":"candidate",
  "parts":[
    {"id":"body","primitive":"ellipse","relation":"scene_center","size":"large","aspect":1.4},
    {"id":"tail","primitive":"triangle","relation":"attached_right","target":"body",
     "size":"small","orientation":"right"}
  ]
}
""")

FAIL = parse_critic_json("""
{
 "verdict":"fail","recognizable":false,"request_satisfied":false,
 "composition_coherent":false,"confidence":0.9,
 "summary":"Not recognizable.","issues":["Tail is disconnected."]
}
""", model_name="vision")

PASS = parse_critic_json("""
{
 "verdict":"pass","recognizable":true,"request_satisfied":true,
 "composition_coherent":true,"confidence":0.9,
 "summary":"Recognizable simple subject.","issues":[]
}
""", model_name="vision")


class FakePlanner:
    def __init__(self):
        self.calls = []

    def plan(self, request, *, visual_feedback=""):
        self.calls.append((request, visual_feedback))
        return SimpleNamespace(document=DOC)


class FakeCritic:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def critique(self, request, preview):
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


class DummyRuntime:
    pass


def test_pipeline_never_approves_until_visual_gate_passes():
    planner = FakePlanner()
    critic = FakeCritic([FAIL, PASS])
    pipeline = SemanticSketchPipeline(
        DummyRuntime(),
        planner=planner,
        critic=critic,
        renderer=SketchPreviewRenderer(),
    )

    approved, diagnostics = pipeline.plan("draw a simple unfamiliar object")

    assert approved.candidate_attempts == 2
    assert approved.critic_confidence == 0.9
    assert diagnostics.candidate_count == 2
    assert len(diagnostics.critiques) == 2
    assert "Tail is disconnected" in planner.calls[1][1]
    assert approved.preview_image.data.startswith(b"\x89PNG")


def test_pipeline_aborts_after_bounded_visual_rejections():
    pipeline = SemanticSketchPipeline(
        DummyRuntime(),
        planner=FakePlanner(),
        critic=FakeCritic([FAIL]),
        renderer=SketchPreviewRenderer(),
    )
    with pytest.raises(SemanticSketchRejected, match="3 candidate"):
        pipeline.plan("draw something")


def test_visual_repair_bound_can_disable_replanning():
    pipeline = SemanticSketchPipeline(
        DummyRuntime(),
        planner=FakePlanner(),
        critic=FakeCritic([FAIL]),
        renderer=SketchPreviewRenderer(),
        max_visual_repair_attempts=0,
    )
    with pytest.raises(SemanticSketchRejected, match="1 candidate"):
        pipeline.plan("draw something")


def test_pipeline_semantic_requirement_gate_runs_before_visual_critic():
    from core.semantic_sketch import (
        SemanticPart,
        SemanticRequirement,
        SemanticSketchDocument,
        SemanticSketchError,
    )

    bad_doc = SemanticSketchDocument(
        version=1,
        name="bad-pose",
        parts=(
            SemanticPart(
                part_id="root", primitive="rectangle", relation="scene_center",
                target=None, anchor="center", size="medium", aspect=1.0,
                orientation=None,
            ),
            SemanticPart(
                part_id="arm", primitive="line", relation="attached_right",
                target="root", anchor="top", size="small", aspect=1.0,
                orientation="vertical", direction_hint="vertical",
            ),
        ),
        requirements=(
            SemanticRequirement(
                subject="arm", primitive="line", relation="attached",
                target="root", direction="upward",
            ),
        ),
    )

    class Planner:
        def plan(self, request, *, visual_feedback=""):
            return SimpleNamespace(document=bad_doc)

    critic = FakeCritic([PASS])
    pipeline = SemanticSketchPipeline(
        DummyRuntime(), planner=Planner(), critic=critic,
        renderer=SketchPreviewRenderer(),
    )

    with pytest.raises(SemanticSketchRejected, match="requires direction 'upward'"):
        pipeline.plan("raise an arm")
    assert critic.calls == 0


class StructuralFailThenPassPlanner:
    def __init__(self):
        self.calls = []

    def plan(self, request, *, visual_feedback=""):
        self.calls.append((request, visual_feedback))
        if len(self.calls) == 1:
            raise SemanticSketchError("target 'missing' does not exist")
        return SimpleNamespace(document=DOC)


def test_pipeline_continues_after_structural_candidate_failure():
    planner = StructuralFailThenPassPlanner()
    critic = FakeCritic([PASS])
    pipeline = SemanticSketchPipeline(
        DummyRuntime(), planner=planner, critic=critic,
        renderer=SketchPreviewRenderer(),
    )

    approved, diagnostics = pipeline.plan("draw something")

    assert approved.candidate_attempts == 2
    assert diagnostics.candidate_count == 2
    assert diagnostics.structural_errors == ("target 'missing' does not exist",)
    assert critic.calls == 1
    assert "semantic structure/layout" in planner.calls[1][1].lower()
    assert "target 'missing'" in planner.calls[1][1]


class AlwaysStructuralFailPlanner:
    def plan(self, request, *, visual_feedback=""):
        raise SemanticSketchError("broken semantic graph")


def test_pipeline_turns_repeated_structural_failures_into_bounded_rejection():
    critic = FakeCritic([PASS])
    pipeline = SemanticSketchPipeline(
        DummyRuntime(), planner=AlwaysStructuralFailPlanner(), critic=critic,
        renderer=SketchPreviewRenderer(),
    )

    with pytest.raises(SemanticSketchRejected, match="3 candidate") as excinfo:
        pipeline.plan("draw something")

    assert "broken semantic graph" in str(excinfo.value)
    assert critic.calls == 0
