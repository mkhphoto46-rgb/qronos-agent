from __future__ import annotations

import pytest

from core.semantic_sketch import (
    CARDINAL_ORIENTATIONS,
    LINE_ORIENTATIONS,
    MODEL_ORIENTATION_NONE,
    MODEL_ROOT_TARGET,
    SEMANTIC_SKETCH_RESPONSE_FORMAT,
    SemanticSketchError,
    parse_semantic_model_json,
    parse_semantic_sketch_json,
)


GOOD = """
{
  "version": 1,
  "name": "generic-object",
  "parts": [
    {
      "id": "body",
      "primitive": "ellipse",
      "relation": "scene_center",
      "size": "large",
      "aspect": 1.6
    },
    {
      "id": "tail",
      "primitive": "triangle",
      "relation": "attached_right",
      "target": "body",
      "anchor": "center",
      "size": "medium",
      "orientation": "right"
    },
    {
      "id": "eye",
      "primitive": "ellipse",
      "relation": "inside",
      "target": "body",
      "anchor": "top_left",
      "size": "tiny"
    }
  ]
}
"""


def test_valid_semantic_scene_graph_parses_without_coordinates():
    doc = parse_semantic_sketch_json(GOOD)
    assert doc.name == "generic-object"
    assert [part.part_id for part in doc.parts] == ["body", "tail", "eye"]
    assert doc.parts[1].target == "body"


def test_raw_coordinates_are_not_part_of_semantic_contract():
    bad = GOOD.replace('"aspect": 1.6', '"aspect": 1.6, "x": 0.4')
    with pytest.raises(SemanticSketchError, match="unknown keys"):
        parse_semantic_sketch_json(bad)


def test_exactly_one_scene_root_is_required():
    bad = GOOD.replace('"relation": "attached_right"', '"relation": "scene_center"', 1).replace(
        '"target": "body",', "", 1
    )
    with pytest.raises(SemanticSketchError, match="exactly one"):
        parse_semantic_sketch_json(bad)


def test_relation_cycle_is_rejected():
    text = """
    {
      "version":1,
      "parts":[
        {"id":"root","primitive":"ellipse","relation":"scene_center","size":"medium"},
        {"id":"a","primitive":"rectangle","relation":"inside","target":"b","size":"small"},
        {"id":"b","primitive":"rectangle","relation":"inside","target":"a","size":"small"}
      ]
    }
    """
    with pytest.raises(SemanticSketchError, match="cycle"):
        parse_semantic_sketch_json(text)


def test_unknown_target_is_rejected():
    bad = GOOD.replace('"target": "body"', '"target": "missing"', 1)
    with pytest.raises(SemanticSketchError, match="unknown part"):
        parse_semantic_sketch_json(bad)


def test_line_requires_semantic_orientation():
    text = """
    {"version":1,"parts":[
      {"id":"root","primitive":"line","relation":"scene_center","size":"medium"}
    ]}
    """
    with pytest.raises(SemanticSketchError, match="orientation"):
        parse_semantic_sketch_json(text)


def test_non_directional_primitive_refuses_orientation():
    text = """
    {"version":1,"parts":[
      {"id":"root","primitive":"ellipse","relation":"scene_center","size":"medium",
       "orientation":"up"}
    ]}
    """
    with pytest.raises(SemanticSketchError, match="only valid"):
        parse_semantic_sketch_json(text)


def test_duplicate_json_keys_are_rejected():
    text = '{"version":1,"version":1,"parts":[]}'
    with pytest.raises(SemanticSketchError, match="Duplicate"):
        parse_semantic_sketch_json(text)


def test_nonfinite_aspect_is_rejected():
    text = """
    {"version":1,"parts":[
      {"id":"root","primitive":"ellipse","relation":"scene_center","size":"medium",
       "aspect":NaN}
    ]}
    """
    with pytest.raises(SemanticSketchError, match="Non-finite"):
        parse_semantic_sketch_json(text)


def test_explicit_null_target_is_valid_only_for_scene_center():
    good = r'''
    {"version":1,"parts":[
      {"id":"root","primitive":"ellipse","relation":"scene_center","target":null,"size":"medium"},
      {"id":"child","primitive":"ellipse","relation":"inside","target":"root","size":"small"}
    ]}
    '''
    doc = parse_semantic_sketch_json(good)
    assert doc.parts[0].target is None

    bad = good.replace('"target":"root"', '"target":null')
    with pytest.raises(SemanticSketchError, match="target is required"):
        parse_semantic_sketch_json(bad)



def test_model_response_schema_is_flat_categorical_and_has_no_numeric_geometry():
    item = SEMANTIC_SKETCH_RESPONSE_FORMAT["properties"]["parts"]["items"]

    assert set(item["required"]) == {
        "id", "object_id", "primitive", "relation", "target", "placement",
        "scale", "proportion", "direction"
    }
    assert "allOf" not in item
    assert "oneOf" not in item
    assert "anyOf" not in item
    assert "not" not in item
    assert item["properties"]["target"]["type"] == "string"
    assert MODEL_ROOT_TARGET in item["properties"]["target"]["pattern"]
    assert "aspect" not in item["properties"]
    assert "x" not in item["properties"]
    assert "y" not in item["properties"]
    assert set(item["properties"]["proportion"]["enum"]) == {
        "default", "tall", "wide", "thin"
    }


def test_model_wire_format_converts_categories_into_internal_geometry():
    doc = parse_semantic_model_json(_model_scene())
    root, torso, arm = doc.parts
    assert root.primitive == "group"
    assert root.target is None
    assert root.object_id == "__scene__"
    assert torso.primitive == "rectangle"
    assert torso.object_id == "person"
    assert torso.aspect == pytest.approx(0.55)
    assert arm.target == "torso"
    assert arm.object_id == "person"
    assert arm.orientation == "up_right"
    assert arm.direction_hint == "up_right"


def test_model_wire_ignores_irrelevant_direction_for_non_directional_primitive():
    text = r'''
    {
      "version":1,
      "name":"body",
      "requirements":[
        {"subject":"body","primitive":"ellipse","relation":"any","target":"__any__","direction":"any"}
      ],
      "parts":[
        {"id":"scene_root","primitive":"group","relation":"scene_center",
         "target":"__scene__","placement":"center","scale":"large",
         "proportion":"default","direction":"auto"},
        {"id":"body","primitive":"ellipse","relation":"inside",
         "target":"scene_root","placement":"center","scale":"large",
         "proportion":"wide","direction":"down_left"}
      ]
    }
    '''
    doc = parse_semantic_model_json(text)
    assert doc.parts[1].orientation is None
    assert doc.parts[1].aspect == pytest.approx(1.8)


def test_model_wire_infers_safe_line_direction_from_relation_when_auto():
    text = r'''
    {
      "version":1,
      "name":"limb",
      "requirements":[
        {"subject":"leg","primitive":"line","relation":"attached","target":"torso","direction":"any"}
      ],
      "parts":[
        {"id":"scene_root","primitive":"group","relation":"scene_center",
         "target":"__scene__","placement":"center","scale":"large",
         "proportion":"default","direction":"auto"},
        {"id":"torso","primitive":"rectangle","relation":"inside",
         "target":"scene_root","placement":"center","scale":"medium",
         "proportion":"tall","direction":"auto"},
        {"id":"leg","primitive":"line","relation":"attached_bottom",
         "target":"torso","placement":"center","scale":"medium",
         "proportion":"default","direction":"auto"}
      ]
    }
    '''
    doc = parse_semantic_model_json(text)
    assert doc.parts[2].orientation == "vertical"
    assert doc.parts[2].aspect == pytest.approx(0.35)


def test_model_wire_rejects_numeric_geometry_keys():
    text = r'''
    {
      "version":1,
      "name":"bad",
      "requirements":[
        {"subject":"body","primitive":"ellipse","relation":"any","target":"__any__","direction":"any"}
      ],
      "parts":[
        {"id":"scene_root","primitive":"group","relation":"scene_center",
         "target":"__scene__","placement":"center","scale":"large",
         "proportion":"default","direction":"auto"},
        {"id":"body","primitive":"ellipse","relation":"inside",
         "target":"scene_root","placement":"center","scale":"medium",
         "proportion":"default","direction":"auto","aspect":7.2}
      ]
    }
    '''
    with pytest.raises(SemanticSketchError, match="unknown keys"):
        parse_semantic_model_json(text)


def test_model_wire_format_requires_root_target_sentinel():
    text = r'''
    {
      "version":1,
      "name":"bad",
      "requirements":[
        {"subject":"body","primitive":"ellipse","relation":"any","target":"__any__","direction":"any"}
      ],
      "parts":[
        {"id":"scene_root","primitive":"group","relation":"scene_center",
         "target":"scene_root","placement":"center","scale":"large",
         "proportion":"default","direction":"auto"},
        {"id":"body","primitive":"ellipse","relation":"inside",
         "target":"scene_root","placement":"center","scale":"medium",
         "proportion":"default","direction":"auto"}
      ]
    }
    '''
    with pytest.raises(SemanticSketchError, match="__scene__"):
        parse_semantic_model_json(text)



def _model_scene(requirement_direction="upward", arm_direction="up_right"):
    return f"""
    {{
      "version":1,
      "name":"person",
      "requirements":[
        {{"subject":"torso","primitive":"rectangle","relation":"inside","target":"scene_root","direction":"any"}},
        {{"subject":"raised_arm","primitive":"line","relation":"attached","target":"torso","direction":"{requirement_direction}"}}
      ],
      "parts":[
        {{"id":"scene_root","object_id":"__scene__","primitive":"group","relation":"scene_center","target":"__scene__",
         "placement":"center","scale":"large","proportion":"default","direction":"auto"}},
        {{"id":"torso","object_id":"person","primitive":"rectangle","relation":"inside","target":"scene_root",
         "placement":"center","scale":"medium","proportion":"tall","direction":"auto"}},
        {{"id":"raised_arm","object_id":"person","primitive":"line","relation":"attached_right","target":"torso",
         "placement":"top","scale":"medium","proportion":"default","direction":"{arm_direction}"}}
      ]
    }}
    """


def test_model_scene_root_is_normalized_to_invisible_group():
    doc = parse_semantic_model_json(_model_scene())
    root = doc.parts[0]
    assert root.relation == "scene_center"
    assert root.primitive == "group"
    assert root.target is None


def test_semantic_requirement_gate_accepts_explicit_upward_arm():
    doc = parse_semantic_model_json(_model_scene())
    assert doc.requirements[1].subject == "raised_arm"
    assert doc.parts[-1].direction_hint == "up_right"


def test_semantic_requirement_gate_rejects_missing_requested_pose():
    with pytest.raises(SemanticSketchError, match="requires direction 'upward'"):
        parse_semantic_model_json(_model_scene(arm_direction="vertical"))


def test_model_structure_rejects_drawable_attached_directly_to_scene_root():
    bad = _model_scene().replace(
        '"id":"torso","object_id":"person","primitive":"rectangle","relation":"inside"',
        '"id":"torso","object_id":"person","primitive":"rectangle","relation":"attached_bottom"',
    )
    with pytest.raises(SemanticSketchError, match="invisible scene root"):
        parse_semantic_model_json(bad)


def test_model_response_schema_includes_requirements_and_group_primitive():
    root = SEMANTIC_SKETCH_RESPONSE_FORMAT
    assert "requirements" in root["required"]
    requirement = root["properties"]["requirements"]["items"]
    assert set(requirement["required"]) == {
        "subject", "primitive", "relation", "target", "direction"
    }
    assert "group" in root["properties"]["parts"]["items"]["properties"]["primitive"]["enum"]


def test_non_root_group_is_rejected_even_when_it_has_visible_children():
    text = r"""
    {
      "version":1,
      "name":"hidden-body",
      "requirements":[
        {"subject":"arm","primitive":"line","relation":"attached","target":"torso","direction":"upward"}
      ],
      "parts":[
        {"id":"scene_root","object_id":"__scene__","primitive":"group","relation":"scene_center",
         "target":"__scene__","placement":"center","scale":"large",
         "proportion":"default","direction":"auto"},
        {"id":"body_group","object_id":"person","primitive":"group","relation":"inside",
         "target":"scene_root","placement":"center","scale":"medium",
         "proportion":"default","direction":"auto"},
        {"id":"torso","object_id":"person","primitive":"rectangle","relation":"inside",
         "target":"body_group","placement":"center","scale":"medium",
         "proportion":"tall","direction":"auto"},
        {"id":"arm","object_id":"person","primitive":"line","relation":"attached_right",
         "target":"torso","placement":"top","scale":"small",
         "proportion":"default","direction":"up_right"}
      ]
    }
    """
    with pytest.raises(SemanticSketchError, match="Only the scene_center root"):
        parse_semantic_model_json(text)


def test_flat_visible_graph_preserves_object_id_metadata():
    doc = parse_semantic_model_json(_model_scene())
    root, torso, arm = doc.parts
    assert root.object_id == "__scene__"
    assert torso.object_id == "person"
    assert arm.object_id == "person"
    assert all(part.primitive != "group" for part in doc.parts[1:])


def test_attachment_cannot_cross_object_id_boundary():
    bad = _model_scene().replace(
        '"id":"raised_arm","object_id":"person"',
        '"id":"raised_arm","object_id":"other"',
    )
    with pytest.raises(SemanticSketchError, match="different object_id"):
        parse_semantic_model_json(bad)


def test_model_response_schema_requires_object_id_and_only_requirements_for_drawables():
    root = SEMANTIC_SKETCH_RESPONSE_FORMAT
    item = root["properties"]["parts"]["items"]
    assert "object_id" in item["required"]
    assert item["properties"]["object_id"]["type"] == "string"
    requirement_primitive_enum = set(
        root["properties"]["requirements"]["items"]["properties"]["primitive"]["enum"]
    )
    assert "group" not in requirement_primitive_enum
    assert "ellipse" in requirement_primitive_enum
