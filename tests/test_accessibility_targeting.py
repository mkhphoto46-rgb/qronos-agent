import json

from core.accessibility_targeting import (
    AccessibilityTargetLocator,
    selection_schema,
)
from core.windows_accessibility import (
    ActionableUiElement,
)


def candidate(
    *,
    name,
    control_type="ControlType.Edit",
    value="",
    focusable=True,
    value_pattern=True,
    value_read_only=False,
    text_pattern=True,
):
    return ActionableUiElement(
        runtime_id=name,
        name=name,
        automation_id="TextBox",
        class_name="TextBox",
        control_type=control_type,
        value=value,
        enabled=True,
        focusable=focusable,
        focused=False,
        value_pattern=value_pattern,
        value_read_only=value_read_only,
        text_pattern=text_pattern,
        invoke_pattern=False,
        expand_pattern=False,
        left=100,
        top=100,
        width=400,
        height=40,
    )


class FakeRuntime:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self.stopped = []

    def chat(self, *args, **kwargs):
        self.calls.append(
            (args, kwargs)
        )
        return self.reply

    def stop_model(self, name):
        self.stopped.append(name)


def test_writable_focusable_edit_is_text_input():
    item = candidate(
        name="Address Bar",
    )

    assert item.role == "text_input"


def test_non_focusable_file_property_is_not_text_input():
    item = candidate(
        name="Name",
        focusable=False,
    )

    assert item.role == "other"


def test_read_only_edit_is_not_text_input():
    item = candidate(
        name="Size",
        value_read_only=True,
        text_pattern=False,
    )

    assert item.role == "other"


def test_candidate_schema_contains_no_coordinates():
    schema = selection_schema(
        ("uia-001", "uia-002")
    )

    encoded = json.dumps(
        schema
    )

    assert '"x"' not in encoded
    assert '"y"' not in encoded
    assert "bbox" not in encoded


def test_model_selects_only_supplied_candidate_id():
    items = (
        candidate(
            name="Search folder",
            value="query",
        ),
        candidate(
            name="Address Bar",
            value=r"F:\\",
        ),
    )

    runtime = FakeRuntime(
        json.dumps(
            {
                "status": "selected",
                "candidate_id": "uia-002",
                "confidence": 0.97,
            }
        )
    )

    locator = AccessibilityTargetLocator(
        runtime,
        enumerate_fn=lambda hwnd: items,
    )

    result = locator.locate(
        "the address bar where a folder path can be typed",
        100,
        role="text_input",
    )

    assert result.candidate_id == "uia-002"
    assert result.element.name == "Address Bar"
    assert result.element.center == (300, 120)


def test_role_filter_removes_list_items():
    edit = candidate(
        name="Address Bar",
    )

    list_item = candidate(
        name="Some file",
        control_type="ControlType.ListItem",
        value_pattern=True,
        text_pattern=False,
    )

    locator = AccessibilityTargetLocator(
        FakeRuntime("{}"),
        enumerate_fn=lambda hwnd: (
            list_item,
            edit,
        ),
    )

    candidates = locator.candidates(
        100,
        role="text_input",
    )

    assert len(candidates) == 1
    assert candidates[0][1].name == "Address Bar"



def test_explicit_platform_name_bypasses_model():
    items = (
        candidate(
            name="Address Bar",
            value=r"F:\\",
        ),
        candidate(
            name="Search DND WEB VTT",
            value="",
        ),
    )

    # Deliberately unusable reply.
    # If the model is touched, the test will fail.
    runtime = FakeRuntime(
        "THIS MUST NEVER BE READ"
    )

    locator = AccessibilityTargetLocator(
        runtime,
        enumerate_fn=lambda hwnd: items,
    )

    result = locator.locate(
        "the address bar where a folder path can be typed",
        100,
        role="text_input",
    )

    assert result.element.name == "Address Bar"
    assert result.selection_source == "platform_metadata"
    assert result.confidence == 1.0

    assert runtime.calls == []
    assert runtime.stopped == []


def test_duplicate_explicit_names_do_not_guess():
    items = (
        candidate(
            name="Search",
        ),
        candidate(
            name="Search",
        ),
    )

    runtime = FakeRuntime(
        json.dumps(
            {
                "status": "ambiguous",
                "candidate_id": "",
                "confidence": 0.5,
            }
        )
    )

    locator = AccessibilityTargetLocator(
        runtime,
        enumerate_fn=lambda hwnd: items,
    )

    import pytest

    from core.accessibility_targeting import (
        AccessibilityTargetAmbiguous,
    )

    with pytest.raises(
        AccessibilityTargetAmbiguous
    ):
        locator.locate(
            "the Search control",
            100,
            role="text_input",
        )

    # Duplicate deterministic names must fall through rather than guess.
    assert len(runtime.calls) == 1


def test_nonmatching_request_still_uses_model_fallback():
    items = (
        candidate(
            name="Search",
        ),
        candidate(
            name="Address Bar",
        ),
    )

    runtime = FakeRuntime(
        json.dumps(
            {
                "status": "selected",
                "candidate_id": "uia-001",
                "confidence": 0.93,
            }
        )
    )

    locator = AccessibilityTargetLocator(
        runtime,
        enumerate_fn=lambda hwnd: items,
    )

    result = locator.locate(
        "the field for finding files",
        100,
        role="text_input",
    )

    assert result.candidate_id == "uia-001"
    assert result.selection_source == "model"
    assert len(runtime.calls) == 1
