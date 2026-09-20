from __future__ import annotations

from core.semantic_sketch import parse_semantic_sketch_json
from core.semantic_sketch_layout import compile_semantic_sketch


SCENE = """
{
  "version":1,
  "name":"scene",
  "parts":[
    {"id":"body","primitive":"ellipse","relation":"scene_center","size":"large","aspect":1.5},
    {"id":"tail","primitive":"triangle","relation":"attached_right","target":"body",
     "size":"medium","orientation":"right"},
    {"id":"eye1","primitive":"ellipse","relation":"inside","target":"body",
     "anchor":"top_left","size":"tiny"},
    {"id":"eye2","primitive":"ellipse","relation":"inside","target":"body",
     "anchor":"top_right","size":"tiny"},
    {"id":"mark","primitive":"line","relation":"attached_top","target":"body",
     "anchor":"center","size":"small","orientation":"up_right"}
  ]
}
"""


def test_semantic_layout_is_deterministic_and_bounded():
    doc = parse_semantic_sketch_json(SCENE)
    first = compile_semantic_sketch(doc)
    second = compile_semantic_sketch(doc)

    assert first == second
    assert len(first.strokes) == 5
    for stroke in first.strokes:
        for point in stroke.points:
            assert 0.0 <= point.x <= 1.0
            assert 0.0 <= point.y <= 1.0


def test_two_inside_anchors_do_not_collapse_to_same_center():
    plan = compile_semantic_sketch(parse_semantic_sketch_json(SCENE))
    eye1 = plan.strokes[2]
    eye2 = plan.strokes[3]

    mean1 = sum(point.x for point in eye1.points) / len(eye1.points)
    mean2 = sum(point.x for point in eye2.points) / len(eye2.points)
    assert mean1 < mean2


def test_attached_part_lands_on_requested_side():
    plan = compile_semantic_sketch(parse_semantic_sketch_json(SCENE))
    body = plan.strokes[0]
    tail = plan.strokes[1]

    body_mean = sum(point.x for point in body.points) / len(body.points)
    tail_mean = sum(point.x for point in tail.points) / len(tail.points)
    assert tail_mean > body_mean


def test_attached_lines_share_visible_parent_endpoints_instead_of_floating():
    doc = parse_semantic_sketch_json("""
    {
      "version":1,
      "parts":[
        {"id":"torso","primitive":"line","relation":"scene_center",
         "size":"medium","orientation":"vertical"},
        {"id":"left_leg","primitive":"line","relation":"attached_bottom",
         "target":"torso","size":"medium","orientation":"down_left"},
        {"id":"right_leg","primitive":"line","relation":"attached_bottom",
         "target":"torso","size":"medium","orientation":"down_right"}
      ]
    }
    """)
    plan = compile_semantic_sketch(doc)
    torso_end = plan.strokes[0].points[-1]
    assert plan.strokes[1].points[0] == torso_end
    assert plan.strokes[2].points[0] == torso_end


def test_model_invisible_root_is_not_rendered():
    from core.semantic_sketch import parse_semantic_model_json

    doc = parse_semantic_model_json(r'''
    {
      "version":1,
      "name":"person",
      "requirements":[
        {"subject":"body","primitive":"rectangle","relation":"any","target":"__any__","direction":"any"}
      ],
      "parts":[
        {"id":"scene_root","primitive":"ellipse","relation":"scene_center","target":"__scene__",
         "placement":"center","scale":"large","proportion":"wide","direction":"down"},
        {"id":"body","primitive":"rectangle","relation":"inside","target":"scene_root",
         "placement":"center","scale":"medium","proportion":"tall","direction":"auto"},
        {"id":"head","primitive":"ellipse","relation":"attached_top","target":"body",
         "placement":"center","scale":"small","proportion":"default","direction":"auto"}
      ]
    }
    ''')
    plan = compile_semantic_sketch(doc)
    assert len(plan.strokes) == 2


def test_attached_line_obeys_explicit_upward_direction_hint():
    from core.semantic_sketch import parse_semantic_model_json

    doc = parse_semantic_model_json(r'''
    {
      "version":1,
      "name":"raised-arm",
      "requirements":[
        {"subject":"arm","primitive":"line","relation":"attached","target":"body","direction":"upward"}
      ],
      "parts":[
        {"id":"scene_root","primitive":"group","relation":"scene_center","target":"__scene__",
         "placement":"center","scale":"large","proportion":"default","direction":"auto"},
        {"id":"body","primitive":"rectangle","relation":"inside","target":"scene_root",
         "placement":"center","scale":"medium","proportion":"tall","direction":"auto"},
        {"id":"arm","primitive":"line","relation":"attached_right","target":"body",
         "placement":"top","scale":"medium","proportion":"default","direction":"up_right"},
        {"id":"hand","primitive":"ellipse","relation":"attached_top","target":"arm",
         "placement":"top","scale":"tiny","proportion":"default","direction":"auto"}
      ]
    }
    ''')
    plan = compile_semantic_sketch(doc)
    body, arm, hand = plan.strokes
    assert arm.points[-1].y < arm.points[0].y
    hand_mean_y = sum(point.y for point in hand.points) / len(hand.points)
    assert abs(hand_mean_y - arm.points[-1].y) < 0.08
