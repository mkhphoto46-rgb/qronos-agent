from __future__ import annotations

import unittest

from core.actions import ActionRequest
from core.input_safety import InputAuthorizationMismatch
from core.screen_capture import Capture, DisplayGeometry
from core.visual_action_executor import VisualActionExecutor
from core.visual_targeting import VisualTarget
from core.vision_grounding import ground_point
from core.vision_image import PreparedImage
from core.window_binding import (
    WindowBinder,
    WindowRect,
    WindowSnapshot,
)
from security.gate import evaluate, issue_grant
from security.permissions import ActionCategory


def snapshot():
    return WindowSnapshot(
        hwnd=77,
        pid=501,
        thread_id=8,
        title="Example Application",
        process_path=r"C:\Example\example.exe",
        process_start_time_100ns=123456789,
        window_rect=WindowRect(
            100,
            50,
            1700,
            950,
        ),
        client_rect=WindowRect(
            108,
            80,
            1692,
            942,
        ),
        dpi=96,
        visible=True,
        minimized=False,
        foreground=True,
    )


def setup_context():
    current = snapshot()

    binder = WindowBinder(
        enumerate_fn=lambda: (current,),
        snapshot_fn=lambda hwnd: current,
    )

    binding = binder.bind_snapshot(
        current,
        app_id="example",
        display_name="Example Application",
        require_foreground=True,
    )

    capture = Capture(
        image=PreparedImage(
            data=b"png",
            width=1280,
            height=720,
            format="png",
        ),
        geometry=DisplayGeometry(
            physical_width=1920,
            physical_height=1080,
            logical_width=1920,
            logical_height=1080,
        ),
        window=binding.hwnd,
        binding_token=binding.identity_token,
        layout_token=binding.layout_token,
        source_width=1600,
        source_height=900,
        frame_rect=WindowRect(
            100,
            50,
            1700,
            950,
        ),
    )

    point = ground_point(
        capture,
        image_x=640,
        image_y=360,
        label="target",
    )

    target = VisualTarget(
        label="target",
        confidence=0.98,
        point=point,
    )

    return binder, binding, capture, target


class FakeComputerExecutor:
    def __init__(self, binder):
        self.binder = binder
        self.calls = 0

    def acquire_foreground(self, binding):
        self.calls += 1
        return self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )


class FakeCapture:
    def __init__(self, capture):
        self.value = capture
        self.calls = 0

    def capture_bound(self, *args, **kwargs):
        self.calls += 1
        return self.value


class FakeLocator:
    def __init__(self, target):
        self.target = target
        self.calls = []

    def locate(self, description, capture):
        self.calls.append(
            (description, capture)
        )
        return self.target


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
        authorization.require_for(binding)

        self.calls.append(
            {
                "binding": binding,
                "x": x,
                "y": y,
                "authorization": authorization,
                "allowed_rect": allowed_rect,
            }
        )

        return object()


class FakeKeyboard:
    def __init__(self):
        self.text_calls = []
        self.key_calls = []

    def type_text(
        self,
        binding,
        text,
        *,
        authorization,
    ):
        authorization.require_for(binding)

        self.text_calls.append(
            {
                "binding": binding,
                "text": text,
                "authorization": authorization,
            }
        )

        return object()

    def press(
        self,
        binding,
        key,
        *,
        authorization,
    ):
        authorization.require_for(binding)

        self.key_calls.append(
            {
                "binding": binding,
                "key": key,
                "authorization": authorization,
            }
        )

        return object()


class FakeInputs:
    def __init__(self):
        self.click = FakeClick()
        self.keyboard = FakeKeyboard()


def grant_for(binding, *, target=None):
    request = ActionRequest(
        category=ActionCategory.GUI_AUTOMATION,
        target=(
            target
            if target is not None
            else f"app:{binding.app_id}"
        ),
        summary="Test one bounded visual mouse action.",
        parameters={
            "operation": "visual_click",
        },
    )

    verdict = evaluate(request)

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


class TestVisualActionExecutor(unittest.TestCase):
    def test_click_is_restricted_to_tiny_visual_scope(self):
        binder, binding, capture, target = setup_context()

        inputs = FakeInputs()

        executor = VisualActionExecutor(
            binder,
            FakeComputerExecutor(binder),
            FakeCapture(capture),
            FakeLocator(target),
            inputs=inputs,
        )

        run = executor.click_target(
            binding,
            "the requested control",
            grant=grant_for(binding),
            screen_approved=True,
            click_radius=8,
        )

        self.assertEqual(len(inputs.click.calls), 1)

        click = inputs.click.calls[0]

        self.assertEqual(
            (click["x"], click["y"]),
            (
                target.screen_x,
                target.screen_y,
            ),
        )

        self.assertEqual(
            click["allowed_rect"],
            run.authorized_rect,
        )

        self.assertTrue(
            run.authorized_rect.left
            <= target.screen_x
            < run.authorized_rect.right
        )

        self.assertTrue(
            run.authorized_rect.top
            <= target.screen_y
            < run.authorized_rect.bottom
        )


    def test_wrong_application_grant_fails_before_click(self):
        binder, binding, capture, target = setup_context()

        inputs = FakeInputs()

        executor = VisualActionExecutor(
            binder,
            FakeComputerExecutor(binder),
            FakeCapture(capture),
            FakeLocator(target),
            inputs=inputs,
        )

        with self.assertRaises(
            InputAuthorizationMismatch
        ):
            executor.click_target(
                binding,
                "the requested control",
                grant=grant_for(
                    binding,
                    target="app:wrong-application",
                ),
                screen_approved=True,
            )

        self.assertEqual(
            inputs.click.calls,
            [],
        )


    def test_click_and_type_composes_generic_mouse_and_keyboard(self):
        binder, binding, capture, target = setup_context()

        inputs = FakeInputs()

        executor = VisualActionExecutor(
            binder,
            FakeComputerExecutor(binder),
            FakeCapture(capture),
            FakeLocator(target),
            inputs=inputs,
        )

        run = executor.click_and_type_target(
            binding,
            "the requested text field",
            "QRONOS_TEST",
            grant=grant_for(binding),
            screen_approved=True,
        )

        self.assertEqual(
            len(inputs.click.calls),
            1,
        )

        self.assertEqual(
            len(inputs.keyboard.text_calls),
            1,
        )

        self.assertEqual(
            inputs.keyboard.text_calls[0]["text"],
            "QRONOS_TEST",
        )

        self.assertIs(
            run.keyboard,
            inputs.keyboard.text_calls[0].get(
                "result",
                run.keyboard,
            ),
        )


    def test_press_key_uses_bound_keyboard_authorization(self):
        binder, binding, capture, target = setup_context()

        inputs = FakeInputs()

        executor = VisualActionExecutor(
            binder,
            FakeComputerExecutor(binder),
            FakeCapture(capture),
            FakeLocator(target),
            inputs=inputs,
        )

        executor.press_key(
            binding,
            "escape",
            grant=grant_for(binding),
        )

        self.assertEqual(
            len(inputs.keyboard.key_calls),
            1,
        )

        self.assertEqual(
            inputs.keyboard.key_calls[0]["key"],
            "escape",
        )


if __name__ == "__main__":
    unittest.main()
