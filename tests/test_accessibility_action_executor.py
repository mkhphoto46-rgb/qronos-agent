import json

import pytest

from core.accessibility_action_executor import (
    AccessibilityActionExecutor,
    AccessibilityFocusMismatch,
    _focus_matches,
)
from core.accessibility_targeting import (
    AccessibilityTarget,
    AccessibilityTargetLocator,
)
from core.input_safety import BoundInputAuthorization
from core.window_binding import (
    WindowBinder,
    WindowRect,
    WindowSnapshot,
)
from core.windows_accessibility import (
    ActionableUiElement,
    FocusedUiElement,
)
from security.gate import evaluate, issue_grant
from security.permissions import ActionCategory
from core.actions import ActionRequest


def snapshot():
    return WindowSnapshot(
        hwnd=77,
        pid=500,
        thread_id=8,
        title="Example Application",
        process_path=r"C:\Example\example.exe",
        process_start_time_100ns=123456,
        window_rect=WindowRect(
            100,
            50,
            1100,
            750,
        ),
        client_rect=WindowRect(
            108,
            80,
            1092,
            742,
        ),
        dpi=96,
        visible=True,
        minimized=False,
        foreground=True,
    )


def address():
    return ActionableUiElement(
        runtime_id="42.1.2.3",
        name="Address Bar",
        automation_id="TextBox",
        class_name="TextBox",
        control_type="ControlType.Edit",
        value=r"F:\\",
        enabled=True,
        focusable=True,
        focused=False,
        value_pattern=True,
        value_read_only=False,
        text_pattern=True,
        invoke_pattern=False,
        expand_pattern=False,
        left=200,
        top=100,
        width=500,
        height=40,
    )


class FakeComputer:
    def __init__(self, binder):
        self.binder = binder

    def acquire_foreground(self, binding):
        return self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )


class FakeClick:
    def __init__(self):
        self.calls = []

    def click_at(
        self,
        binding,
        x,
        y,
        *,
        authorization,
        allowed_rect=None,
    ):
        authorization.require_for(
            binding
        )

        self.calls.append(
            (
                x,
                y,
                allowed_rect,
            )
        )

        class Result:
            point = type(
                "Point",
                (),
                {
                    "x": x,
                    "y": y,
                },
            )()

        return Result()


class FakeKeyboard:
    def __init__(self):
        self.texts = []
        self.keys = []

    def type_text(
        self,
        binding,
        text,
        *,
        authorization,
    ):
        authorization.require_for(
            binding
        )

        self.texts.append(text)

        return object()

    def press(
        self,
        binding,
        key,
        *,
        authorization,
    ):
        authorization.require_for(
            binding
        )

        self.keys.append(key)

        return object()


class FakeInputs:
    def __init__(self):
        self.click = FakeClick()
        self.keyboard = FakeKeyboard()


class NoRuntime:
    def chat(self, *args, **kwargs):
        raise AssertionError(
            "Model must not be used."
        )

    def stop_model(self, name):
        raise AssertionError(
            "Model must not be stopped."
        )


def grant(binding):
    request = ActionRequest(
        category=ActionCategory.GUI_AUTOMATION,
        target=f"app:{binding.app_id}",
        summary="test",
        parameters={},
    )

    verdict = evaluate(
        request
    )

    approval = (
        verdict.decision
        if verdict.needs_approval
        else None
    )

    return issue_grant(
        request,
        verdict,
        approval=approval,
    )


def context(focus):
    snap = snapshot()

    binder = WindowBinder(
        enumerate_fn=lambda: (snap,),
        snapshot_fn=lambda hwnd: snap,
    )

    binding = binder.bind_snapshot(
        snap,
        app_id="example",
        display_name="Example Application",
        require_foreground=True,
    )

    item = address()

    locator = AccessibilityTargetLocator(
        None,
        enumerate_fn=lambda hwnd: (
            item,
        ),
    )

    inputs = FakeInputs()

    executor = AccessibilityActionExecutor(
        binder,
        FakeComputer(binder),
        locator,
        inputs=inputs,
        focus_fn=lambda: focus,
    )

    return (
        binder,
        binding,
        item,
        inputs,
        executor,
    )


def good_focus():
    return FocusedUiElement(
        ok=True,
        name="Address Bar",
        automation_id="TextBox",
        class_name="TextBox",
        control_type="ControlType.Edit",
        native_hwnd=77,
        ancestor_hwnds=(77,),
        has_keyboard_focus=True,
        is_keyboard_focusable=True,
        is_enabled=True,
        left=200,
        top=100,
        width=500,
        height=40,
    )


def test_platform_match_requires_no_runtime():
    item = address()

    locator = AccessibilityTargetLocator(
        None,
        enumerate_fn=lambda hwnd: (
            item,
        ),
    )

    result = locator.locate(
        "the address bar where a folder path can be typed",
        77,
        role="text_input",
    )

    assert result.element.name == "Address Bar"
    assert result.selection_source == "platform_metadata"


def test_focus_identity_matches_real_address_bar():
    assert _focus_matches(
        address(),
        good_focus(),
    )


def test_click_uses_trusted_uia_center_and_small_scope():
    (
        binder,
        binding,
        item,
        inputs,
        executor,
    ) = context(
        good_focus()
    )

    run = executor.click_target(
        binding,
        "the address bar where a folder path can be typed",
        role="text_input",
        grant=grant(binding),
    )

    assert len(
        inputs.click.calls
    ) == 1

    x, y, scope = (
        inputs.click.calls[0]
    )

    assert (x, y) == item.center

    assert (
        scope.left
        <= x
        < scope.right
    )

    assert (
        scope.top
        <= y
        < scope.bottom
    )

    assert scope.width <= 17
    assert scope.height <= 17


def test_keyboard_runs_only_after_focus_matches():
    (
        binder,
        binding,
        item,
        inputs,
        executor,
    ) = context(
        good_focus()
    )

    executor.click_and_type_target(
        binding,
        "the address bar where a folder path can be typed",
        "QRONOS",
        role="text_input",
        grant=grant(binding),
    )

    assert inputs.keyboard.texts == [
        "QRONOS"
    ]


def test_wrong_focus_blocks_keyboard():
    wrong = FocusedUiElement(
        ok=True,
        name="View",
        automation_id="",
        class_name="AppBarButton",
        control_type="ControlType.Button",
        native_hwnd=77,
        ancestor_hwnds=(77,),
        has_keyboard_focus=True,
        is_keyboard_focusable=True,
        is_enabled=True,
    )

    (
        binder,
        binding,
        item,
        inputs,
        executor,
    ) = context(
        wrong
    )

    with pytest.raises(
        AccessibilityFocusMismatch
    ):
        executor.click_and_type_target(
            binding,
            "the address bar where a folder path can be typed",
            "QRONOS",
            role="text_input",
            grant=grant(binding),
            focus_timeout=0.06,
        )

    assert (
        inputs.keyboard.texts
        == []
    )



def test_native_uia_focus_uses_exact_runtime_id_without_mouse():
    snap = snapshot()

    binder = WindowBinder(
        enumerate_fn=lambda: (snap,),
        snapshot_fn=lambda hwnd: snap,
    )

    binding = binder.bind_snapshot(
        snap,
        app_id="example",
        display_name="Example Application",
        require_foreground=True,
    )

    item = address()

    locator = AccessibilityTargetLocator(
        None,
        enumerate_fn=lambda hwnd: (
            item,
        ),
    )

    inputs = FakeInputs()

    calls = []

    def focus_target_fn(hwnd, runtime_id):
        calls.append(
            (
                hwnd,
                runtime_id,
            )
        )

        return good_focus()

    executor = AccessibilityActionExecutor(
        binder,
        FakeComputer(binder),
        locator,
        inputs=inputs,
        focus_fn=good_focus,
        focus_target_fn=focus_target_fn,
    )

    run = executor.focus_target(
        binding,
        "the address bar where a folder path can be typed",
        role="text_input",
        grant=grant(binding),
    )

    assert calls == [
        (
            binding.hwnd,
            item.runtime_id,
        )
    ]

    assert run.target.element.name == "Address Bar"
    assert run.focus.name == "Address Bar"

    # The preferred UIA focus path must not secretly synthesize a click.
    assert inputs.click.calls == []



def test_focus_and_type_never_uses_mouse():
    snap = snapshot()

    binder = WindowBinder(
        enumerate_fn=lambda: (snap,),
        snapshot_fn=lambda hwnd: snap,
    )

    binding = binder.bind_snapshot(
        snap,
        app_id="example",
        display_name="Example Application",
        require_foreground=True,
    )

    item = address()

    locator = AccessibilityTargetLocator(
        None,
        enumerate_fn=lambda hwnd: (
            item,
        ),
    )

    inputs = FakeInputs()

    executor = AccessibilityActionExecutor(
        binder,
        FakeComputer(binder),
        locator,
        inputs=inputs,
        focus_fn=good_focus,
        focus_target_fn=lambda hwnd, runtime_id: good_focus(),
    )

    run = executor.focus_and_type_target(
        binding,
        "the address bar where a folder path can be typed",
        "QRONOS TEST",
        role="text_input",
        grant=grant(binding),
    )

    assert inputs.click.calls == []

    assert inputs.keyboard.texts == [
        "QRONOS TEST"
    ]

    assert run.focus_run.target.element.runtime_id == item.runtime_id


def test_focus_change_between_setfocus_and_typing_blocks_keyboard():
    snap = snapshot()

    binder = WindowBinder(
        enumerate_fn=lambda: (snap,),
        snapshot_fn=lambda hwnd: snap,
    )

    binding = binder.bind_snapshot(
        snap,
        app_id="example",
        display_name="Example Application",
        require_foreground=True,
    )

    item = address()

    locator = AccessibilityTargetLocator(
        None,
        enumerate_fn=lambda hwnd: (
            item,
        ),
    )

    inputs = FakeInputs()

    wrong = FocusedUiElement(
        ok=True,
        name="Some File",
        automation_id="1",
        class_name="UIItem",
        control_type="ControlType.ListItem",
        native_hwnd=77,
        ancestor_hwnds=(77,),
        has_keyboard_focus=True,
        is_keyboard_focusable=True,
        is_enabled=True,
    )

    executor = AccessibilityActionExecutor(
        binder,
        FakeComputer(binder),
        locator,
        inputs=inputs,
        focus_fn=lambda: wrong,
        focus_target_fn=lambda hwnd, runtime_id: good_focus(),
    )

    with pytest.raises(
        AccessibilityFocusMismatch
    ):
        executor.focus_and_type_target(
            binding,
            "the address bar where a folder path can be typed",
            "QRONOS TEST",
            role="text_input",
            grant=grant(binding),
        )

    assert inputs.keyboard.texts == []



def test_restore_text_value_uses_exact_runtime_id_and_no_mouse():
    from dataclasses import replace

    snap = snapshot()

    binder = WindowBinder(
        enumerate_fn=lambda: (snap,),
        snapshot_fn=lambda hwnd: snap,
    )

    binding = binder.bind_snapshot(
        snap,
        app_id="example",
        display_name="Example Application",
        require_foreground=True,
    )

    original = address()

    current = replace(
        original,
        value="QRONOS TEMP",
    )

    locator = AccessibilityTargetLocator(
        None,
        enumerate_fn=lambda hwnd: (
            current,
        ),
    )

    inputs = FakeInputs()

    calls = []

    def set_value_fn(
        hwnd,
        runtime_id,
        value,
    ):
        calls.append(
            (
                hwnd,
                runtime_id,
                value,
            )
        )

        return replace(
            current,
            value=value,
        )

    executor = AccessibilityActionExecutor(
        binder,
        FakeComputer(binder),
        locator,
        inputs=inputs,
        focus_fn=good_focus,
        focus_target_fn=lambda hwnd, runtime_id: good_focus(),
        set_value_fn=set_value_fn,
        lookup_runtime_fn=lambda hwnd, runtime_id: current,
    )

    target = AccessibilityTarget(
        candidate_id="uia-001",
        confidence=1.0,
        element=current,
        selection_source="platform_metadata",
    )

    result = executor.restore_text_value(
        binding,
        target,
        r"F:\\",
        grant=grant(binding),
    )

    assert calls == [
        (
            binding.hwnd,
            current.runtime_id,
            r"F:\\",
        )
    ]

    assert result.previous_value == "QRONOS TEMP"
    assert result.restored_value == r"F:\\"

    assert inputs.click.calls == []
    assert inputs.keyboard.texts == []



def test_restore_runtime_lookup_is_dependency_injected():
    from dataclasses import replace

    snap = snapshot()

    binder = WindowBinder(
        enumerate_fn=lambda: (snap,),
        snapshot_fn=lambda hwnd: snap,
    )

    binding = binder.bind_snapshot(
        snap,
        app_id="example",
        display_name="Example Application",
        require_foreground=True,
    )

    original = address()

    current = replace(
        original,
        value="TEMP",
    )

    locator = AccessibilityTargetLocator(
        None,
        enumerate_fn=lambda hwnd: (
            current,
        ),
    )

    lookup_calls = []

    def lookup_runtime_fn(
        hwnd,
        runtime_id,
    ):
        lookup_calls.append(
            (
                hwnd,
                runtime_id,
            )
        )

        return current

    def set_value_fn(
        hwnd,
        runtime_id,
        value,
    ):
        return replace(
            current,
            value=value,
        )

    executor = AccessibilityActionExecutor(
        binder,
        FakeComputer(binder),
        locator,
        inputs=FakeInputs(),
        focus_fn=good_focus,
        focus_target_fn=lambda hwnd, runtime_id: good_focus(),
        set_value_fn=set_value_fn,
        lookup_runtime_fn=lookup_runtime_fn,
    )

    target = AccessibilityTarget(
        candidate_id="uia-001",
        confidence=1.0,
        element=current,
        selection_source="platform_metadata",
    )

    result = executor.restore_text_value(
        binding,
        target,
        r"F:\\",
        grant=grant(binding),
    )

    assert lookup_calls == [
        (
            binding.hwnd,
            current.runtime_id,
        )
    ]

    assert result.restored_value == r"F:\\"



def test_invoke_target_uses_exact_runtime_id_without_mouse_or_keyboard():
    from dataclasses import replace

    snap = snapshot()

    binder = WindowBinder(
        enumerate_fn=lambda: (snap,),
        snapshot_fn=lambda hwnd: snap,
    )

    binding = binder.bind_snapshot(
        snap,
        app_id="example",
        display_name="Example Application",
        require_foreground=True,
    )

    item = replace(
        address(),
        runtime_id="42.77.4.50",
        name="One",
        automation_id="num1Button",
        class_name="Button",
        control_type="ControlType.Button",
        value="",
        enabled=True,
        focusable=True,
        focused=False,
        value_pattern=False,
        value_read_only=None,
        text_pattern=False,
        invoke_pattern=True,
        expand_pattern=False,
    )

    locator = AccessibilityTargetLocator(
        None,
        enumerate_fn=lambda hwnd: (
            item,
        ),
    )

    inputs = FakeInputs()

    calls = []

    def invoke_fn(
        hwnd,
        runtime_id,
    ):
        calls.append(
            (
                hwnd,
                runtime_id,
            )
        )

        return item

    executor = AccessibilityActionExecutor(
        binder,
        FakeComputer(binder),
        locator,
        inputs=inputs,
        focus_fn=good_focus,
        focus_target_fn=lambda hwnd, runtime_id: good_focus(),
        invoke_fn=invoke_fn,
        lookup_runtime_fn=lambda hwnd, runtime_id: item,
    )

    run = executor.invoke_target(
        binding,
        "One",
        role="button",
        grant=grant(binding),
    )

    assert calls == [
        (
            binding.hwnd,
            item.runtime_id,
        )
    ]

    assert run.target.element.name == "One"
    assert run.element.runtime_id == item.runtime_id

    assert inputs.click.calls == []
    assert inputs.keyboard.texts == []
    assert inputs.keyboard.keys == []



def test_invoke_target_does_not_require_foreground():
    from dataclasses import replace

    background = replace(
        snapshot(),
        foreground=False,
    )

    binder = WindowBinder(
        enumerate_fn=lambda: (
            background,
        ),
        snapshot_fn=lambda hwnd: background,
    )

    binding = binder.bind_snapshot(
        background,
        app_id="example",
        display_name="Example Application",
        require_foreground=False,
    )

    item = replace(
        address(),
        runtime_id="42.77.4.50",
        name="One",
        automation_id="num1Button",
        class_name="Button",
        control_type="ControlType.Button",
        value="",
        enabled=True,
        focusable=True,
        focused=False,
        value_pattern=False,
        value_read_only=None,
        text_pattern=False,
        invoke_pattern=True,
        expand_pattern=False,
    )

    locator = AccessibilityTargetLocator(
        None,
        enumerate_fn=lambda hwnd: (
            item,
        ),
    )

    inputs = FakeInputs()

    calls = []

    def invoke_fn(
        hwnd,
        runtime_id,
    ):
        calls.append(
            (
                hwnd,
                runtime_id,
            )
        )

        return item

    executor = AccessibilityActionExecutor(
        binder,
        FakeComputer(binder),
        locator,
        inputs=inputs,
        focus_fn=good_focus,
        focus_target_fn=lambda hwnd, runtime_id: good_focus(),
        invoke_fn=invoke_fn,
        lookup_runtime_fn=lambda hwnd, runtime_id: item,
    )

    run = executor.invoke_target(
        binding,
        "One",
        role="button",
        grant=grant(binding),
    )

    assert calls == [
        (
            binding.hwnd,
            item.runtime_id,
        )
    ]

    assert (
        run.element.runtime_id
        == item.runtime_id
    )

    assert inputs.click.calls == []
    assert inputs.keyboard.texts == []
    assert inputs.keyboard.keys == []



def test_semantic_invoke_survives_foreground_and_layout_change():
    from dataclasses import replace
    from core.windows_accessibility import UiInvokeReceipt

    original = snapshot()

    state = {
        "snapshot": original,
    }

    binder = WindowBinder(
        enumerate_fn=lambda: (
            state["snapshot"],
        ),
        snapshot_fn=lambda hwnd: state["snapshot"],
    )

    binding = binder.bind_snapshot(
        original,
        app_id="example",
        display_name="Example Application",
        require_foreground=True,
    )

    # Window moves and loses foreground after capability creation.
    state["snapshot"] = replace(
        original,
        foreground=False,
        window_rect=WindowRect(
            140,
            90,
            1140,
            790,
        ),
        client_rect=WindowRect(
            148,
            120,
            1132,
            782,
        ),
    )

    from dataclasses import replace as dc_replace

    item = dc_replace(
        address(),
        runtime_id="42.77.4.50",
        name="One",
        automation_id="num1Button",
        class_name="Button",
        control_type="ControlType.Button",
        value="",
        enabled=True,
        focusable=True,
        focused=False,
        value_pattern=False,
        value_read_only=None,
        text_pattern=False,
        invoke_pattern=True,
        expand_pattern=False,
    )

    locator = AccessibilityTargetLocator(
        None,
        enumerate_fn=lambda hwnd: (
            item,
        ),
    )

    calls = []

    def invoke_fn(
        hwnd,
        runtime_id,
    ):
        calls.append(
            (
                hwnd,
                runtime_id,
            )
        )

        return UiInvokeReceipt(
            runtime_id=runtime_id,
        )

    executor = AccessibilityActionExecutor(
        binder,
        FakeComputer(binder),
        locator,
        inputs=FakeInputs(),
        focus_fn=good_focus,
        focus_target_fn=lambda hwnd, runtime_id: good_focus(),
        invoke_fn=invoke_fn,
        lookup_runtime_fn=lambda hwnd, runtime_id: item,
    )

    result = executor.invoke_target(
        binding,
        "One",
        role="button",
        grant=grant(binding),
    )

    assert calls == [
        (
            binding.hwnd,
            item.runtime_id,
        )
    ]

    assert result.element.runtime_id == item.runtime_id


def test_semantic_authorization_rejects_different_runtime_id():
    from core.gui_authorization import (
        BoundGuiAuthorization,
        GuiAuthorizationMismatch,
    )

    snap = snapshot()

    binder = WindowBinder(
        enumerate_fn=lambda: (
            snap,
        ),
        snapshot_fn=lambda hwnd: snap,
    )

    binding = binder.bind_snapshot(
        snap,
        app_id="example",
        display_name="Example Application",
        require_foreground=True,
    )

    authorization = BoundGuiAuthorization.bind(
        grant(binding),
        binding,
        allowed_runtime_ids=(
            "42.77.4.50",
        ),
    )

    authorization.require_for(
        binding,
        "42.77.4.50",
    )

    with pytest.raises(
        GuiAuthorizationMismatch
    ):
        authorization.require_for(
            binding,
            "42.77.4.25",
        )
