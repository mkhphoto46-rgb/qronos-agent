from __future__ import annotations

from pathlib import Path

import pytest

from core.actions import ActionOutcome
from core.computer_executor import ComputerExecutor
from core.paint_draw_integration import (
    EnvironmentPaintReferenceProvider,
    PaintDrawAction,
    PaintDrawExecutor,
    PaintReferenceUnavailable,
)
from core.paint_draw_parser import PaintDrawParseError, parse_paint_draw_action
from core.runtime_bridge import QronosRuntime
from core.task_router import TaskRouter, TaskType
from security.gate import Verdict
from security.permissions import PermissionDecision


def _two_gate_evaluate(request, audit=None):
    if request.category.value == "gui_automation":
        decision = PermissionDecision.REQUIRE_VOICE_CONFIRMATION
    elif request.category.value == "read_screen":
        decision = PermissionDecision.REQUIRE_UI_CONFIRMATION
    else:  # pragma: no cover - this executor only creates the two requests above.
        decision = PermissionDecision.DENY
    verdict = Verdict(
        request=request,
        decision=decision,
        outcome=(
            ActionOutcome.REFUSED
            if decision is PermissionDecision.DENY
            else ActionOutcome.AWAITING_APPROVAL
        ),
        reason=decision.value,
        policy=None,
    )
    if audit is not None:
        audit(verdict)
    return verdict


def _executor():
    computer = ComputerExecutor(dynamic_apps=False)
    return PaintDrawExecutor(
        computer,
        evaluate_fn=_two_gate_evaluate,
    )


def test_persian_natural_draw_command_extracts_description():
    action = parse_paint_draw_action(
        "توی پینت یه آدم ساده با یک دست بالا بکش"
    )
    assert action.prompt == "یه آدم ساده با یک دست بالا"


def test_english_natural_draw_command_extracts_description():
    action = parse_paint_draw_action(
        "draw a simple cat with a hat in Paint"
    )
    assert action.prompt == "a simple cat with a hat"


def test_paint_draw_parser_requires_paint_and_draw_verb():
    with pytest.raises(PaintDrawParseError):
        parse_paint_draw_action("یه آدم ساده بکش")
    with pytest.raises(PaintDrawParseError):
        parse_paint_draw_action("Paint را باز کن")


def test_paint_draw_parser_rejects_shell_syntax():
    with pytest.raises(PaintDrawParseError):
        parse_paint_draw_action("draw a cat in Paint && calc")


def test_router_keeps_paint_drawing_on_computer_path():
    commands = (
        "توی پینت یه آدم ساده با یک دست بالا بکش",
        "در Paint یک آدم ساده با یک دست بالا بکش",
        "داخل paint یک آدم ساده بکش",
        "با Paint یک آدم ساده بکش",
        "Paint رو باز کن و یک آدم ساده بکش",
        "draw a simple person with Paint",
    )
    for command in commands:
        assert TaskRouter().route(command).task_type is TaskType.COMPUTER


def test_mixed_language_paint_location_is_removed_from_prompt():
    action = parse_paint_draw_action(
        "در Paint یک آدم ساده با یک دست بالا بکش"
    )
    assert action.prompt == "یک آدم ساده با یک دست بالا"


def test_persian_open_paint_compound_extracts_only_draw_subject():
    action = parse_paint_draw_action(
        "Paint رو باز کن و یک آدم ساده بکش"
    )
    assert action.prompt == "یک آدم ساده"


def test_prepare_requires_voice_for_gui_and_ui_for_screen():
    prepared = _executor().prepare(PaintDrawAction("simple person"))
    assert prepared.gui_verdict.decision is PermissionDecision.REQUIRE_VOICE_CONFIRMATION
    assert prepared.screen_verdict.decision is PermissionDecision.REQUIRE_UI_CONFIRMATION


def test_voice_approval_alone_cannot_bypass_screen_ui_gate():
    executor = _executor()
    prepared = executor.prepare(PaintDrawAction("simple person"))
    result = executor.execute_prepared(
        prepared,
        gui_approval=PermissionDecision.REQUIRE_VOICE_CONFIRMATION,
        screen_approval=None,
        reevaluate=False,
    )
    assert result.result.outcome is ActionOutcome.AWAITING_APPROVAL
    assert "screen verification" in result.result.detail.lower()


def test_runtime_voice_command_becomes_pending_screen_approval():
    runtime = QronosRuntime()
    runtime.computer_executor = ComputerExecutor(dynamic_apps=False)
    runtime.paint_draw_executor = PaintDrawExecutor(
        runtime.computer_executor,
        evaluate_fn=_two_gate_evaluate,
    )
    report = {}

    response = runtime._prepare_voice_paint_draw_action(
        "توی پینت یه آدم ساده با یک دست بالا بکش",
        report,
    )

    assert response == ""
    assert runtime.pending_paint_draw is not None
    assert report["paintDrawScreenApprovalPending"] is True
    assert report["computerOperation"] == "draw_in_paint"


def test_environment_reference_provider_is_explicit_and_local(tmp_path, monkeypatch):
    provider = EnvironmentPaintReferenceProvider()
    monkeypatch.delenv("QRONOS_PAINT_REFERENCE_PATH", raising=False)
    with pytest.raises(PaintReferenceUnavailable, match="no image-generator/reference"):
        provider.reference_for("cat")

    image = tmp_path / "reference.png"
    image.write_bytes(b"not-decoded-here")
    monkeypatch.setenv("QRONOS_PAINT_REFERENCE_PATH", str(image))
    assert provider.reference_for("cat") == image.resolve()
