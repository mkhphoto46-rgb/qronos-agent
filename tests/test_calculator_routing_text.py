"""
Calculator routing markers and replies are real Persian, not "????".

The markers had been saved as literal question marks, so a message made of
question marks routed to the Calculator path while real Persian requests
depended on other markers. These tests pin both directions and keep English
routing unchanged.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

import core.runtime_bridge as runtime_bridge
from core.task_router import TaskRouter, TaskType

from test_runtime_bridge import prepared_runtime


ROOT = Path(__file__).resolve().parents[1]
PERSIAN = re.compile("[؀-ۿ]")


@pytest.mark.parametrize(
    "text",
    [
        "ماشین حساب دو رو ضربدر چهار کن",
        "با ماشین حساب ۳ به علاوه ۵ رو حساب کن",
        "ماشین‌حساب ده منهای سه",
        "ماشین حساب هشت تقسیم بر دو",
        "توی ماشین حساب شش بعلاوه یک بزن",
    ],
)
def test_persian_calculator_requests_route_to_computer(text):
    decision = TaskRouter().route(text)
    assert decision.task_type is TaskType.COMPUTER
    assert "Calculator" in decision.reason


@pytest.mark.parametrize(
    "text",
    [
        "??????????? ???",
        "????? ???? ??? ?????",
        "?????",
        "?? ?????",
    ],
)
def test_question_mark_messages_never_route_to_calculator(text):
    decision = TaskRouter().route(text)
    assert "Calculator" not in decision.reason
    assert decision.task_type is not TaskType.COMPUTER


@pytest.mark.parametrize(
    "text",
    [
        "جمعه با ماشین می‌روم",  # contains جمع and ماشین, not a calculation
        "حساب بانکی من چقدر است",
    ],
)
def test_ordinary_persian_is_not_a_calculator_action(text):
    assert "Calculator" not in TaskRouter().route(text).reason


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("calculator 2 times 4", TaskType.COMPUTER),
        ("calc 9 divide 3", TaskType.COMPUTER),
        ("open calculator", TaskType.COMPUTER),
        ("what is 2 plus 2?", TaskType.FAST),
    ],
)
def test_english_routing_is_unchanged(text, expected):
    assert TaskRouter().route(text).task_type is expected


def test_no_question_mark_mojibake_left_in_routing_or_runtime_text():
    for path in (ROOT / "core" / "task_router.py", ROOT / "core" / "runtime_bridge.py"):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r'"[?\s]*\?\?\?[?\s]*"', source), path.name
        assert "????" not in source, path.name


def test_voice_calculator_parse_error_reply_is_real_persian():
    runtime = prepared_runtime()
    report: dict = {}

    response = runtime._execute_voice_calculator_semantic_action(
        "ماشین حساب دو رو جمع کن",  # recognised, but the right number is missing
        report,
    )

    assert response is not None
    assert "??" not in response
    assert PERSIAN.search(response)
    assert "ماشین حساب" in response


def test_calculator_success_and_failure_templates_are_persian():
    source = (ROOT / "core" / "runtime_bridge.py").read_text(encoding="utf-8")
    assert source.count('"انجام شد. نتیجه "') == 2
    assert source.count("محاسبه در ماشین حساب انجام یا تأیید نشد.") == 2
    assert runtime_bridge._looks_like_calculator_semantic_action(
        "ماشین حساب دو رو ضربدر چهار کن"
    )
