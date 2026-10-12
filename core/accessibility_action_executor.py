from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from core.accessibility_targeting import (
    AccessibilityTarget,
    AccessibilityTargetLocator,
)
from core.action_cancellation import CancellationToken, checkpoint
from core.gui_authorization import BoundGuiAuthorization
from core.input_capabilities import InputCapabilities
from core.input_safety import BoundInputAuthorization
from core.keyboard_control import KeyboardResult
from core.mouse_click import MouseClickResult
from core.window_binding import (
    WindowBinder,
    WindowBinding,
    WindowRect,
)
from core.windows_accessibility import (
    ActionableUiElement,
    FocusedUiElement,
    actionable_element_by_runtime_id,
    focused_element,
)
from core.windows_accessibility_mutation import (
    UiInvokeReceipt,
    focus_runtime_id,
    invoke_runtime_id,
    set_value_runtime_id,
)
from security.gate import ActionGrant


class AccessibilityActionError(RuntimeError):
    pass


class AccessibilityTargetStale(AccessibilityActionError):
    pass


class AccessibilityFocusMismatch(AccessibilityActionError):
    pass


@dataclass(frozen=True)
class AccessibilityFocusRun:
    target: AccessibilityTarget
    focus: FocusedUiElement


@dataclass(frozen=True)
class AccessibilityFocusTypeRun:
    focus_run: AccessibilityFocusRun
    keyboard: KeyboardResult


@dataclass(frozen=True)
class AccessibilityValueRestoreRun:
    runtime_id: str
    previous_value: str
    restored_value: str
    element: ActionableUiElement


@dataclass(frozen=True)
class AccessibilityInvokeRun:
    target: AccessibilityTarget
    element: ActionableUiElement


@dataclass(frozen=True)
class AccessibilityClickRun:
    target: AccessibilityTarget
    click: MouseClickResult
    authorized_rect: WindowRect


@dataclass(frozen=True)
class AccessibilityClickTypeRun:
    click_run: AccessibilityClickRun
    focus: FocusedUiElement
    keyboard: KeyboardResult


FocusFn = Callable[[], FocusedUiElement]


class FocusTargetFn(Protocol):
    def __call__(
        self, hwnd: int, runtime_id: str, *, binding: WindowBinding,
        authorization: BoundGuiAuthorization, input_authorization: BoundInputAuthorization,
        binder: WindowBinder,
    ) -> FocusedUiElement: ...


class SetValueFn(Protocol):
    def __call__(
        self, hwnd: int, runtime_id: str, value: str, *, binding: WindowBinding,
        authorization: BoundGuiAuthorization, input_authorization: BoundInputAuthorization,
        binder: WindowBinder,
    ) -> ActionableUiElement: ...


class InvokeFn(Protocol):
    def __call__(
        self, hwnd: int, runtime_id: str, *, binding: WindowBinding,
        authorization: BoundGuiAuthorization, binder: WindowBinder,
    ) -> UiInvokeReceipt: ...


LookupRuntimeFn = Callable[
    [int, str],
    ActionableUiElement | None,
]


class AccessibilityActionExecutor:
    """
    Generic trusted-UI automation input path.

    Natural language selects a typed platform candidate. The model, when
    needed at all, may choose only an opaque candidate ID. Geometry comes
    from Windows UI Automation, never from model-generated coordinates.
    """

    def __init__(
        self,
        binder: WindowBinder,
        computer_executor,
        locator: AccessibilityTargetLocator,
        *,
        inputs: InputCapabilities | None = None,
        focus_fn: FocusFn = focused_element,
        focus_target_fn: FocusTargetFn = focus_runtime_id,
        set_value_fn: SetValueFn = set_value_runtime_id,
        invoke_fn: InvokeFn = invoke_runtime_id,
        lookup_runtime_fn: LookupRuntimeFn = actionable_element_by_runtime_id,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        # One executor serves one user-visible action. Its token is checked
        # before every native mutation and between steps; it never relaxes
        # any authorization check. A single native call (one Invoke, one
        # focus, one click, one typed string) completes or fails as a unit:
        # Stop takes effect at the next boundary after it returns.
        self.cancel_token = cancel_token
        self.binder = binder
        self.computer_executor = computer_executor
        self.locator = locator

        self.inputs = (
            inputs
            if inputs is not None
            else InputCapabilities.for_binder(
                binder
            )
        )

        self.focus_fn = focus_fn
        self.focus_target_fn = focus_target_fn
        self.set_value_fn = set_value_fn
        self.invoke_fn = invoke_fn
        self.lookup_runtime_fn = lookup_runtime_fn

    def _checkpoint(self) -> None:
        checkpoint(self.cancel_token)

    def _acquire_foreground(self, binding: WindowBinding) -> None:
        self._checkpoint()
        if self.cancel_token is None:
            self.computer_executor.acquire_foreground(binding)
        else:
            self.computer_executor.acquire_foreground(
                binding,
                cancel_token=self.cancel_token,
            )
        self._checkpoint()

    def focus_target(
        self,
        binding: WindowBinding,
        description: str,
        *,
        role: str,
        grant: ActionGrant,
    ) -> AccessibilityFocusRun:
        """
        Focus one exact trusted UI Automation element.

        This is the preferred activation path for platform controls that
        expose a real accessibility identity. It does not synthesize a mouse
        coordinate.
        """

        if not str(description or "").strip():
            raise ValueError(
                "description is required."
            )

        BoundInputAuthorization.bind(grant, binding)

        self._acquire_foreground(binding)

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        target = self.locator.locate(
            description,
            binding.hwnd,
            role=role,
        )

        element = self._revalidate_target(
            target,
            binding.hwnd,
            role=role,
        )

        # UIA enumeration can take time; honour Stop before focusing.
        self._checkpoint()

        # Validate the same short-lived exact-app grant used by the native
        # mouse/keyboard capabilities. UIA focus is still an action.
        authorization = BoundInputAuthorization.bind(
            grant,
            binding,
        )

        authorization.require_for(
            binding
        )

        result = self.focus_target_fn(
            binding.hwnd,
            element.runtime_id,
            binding=binding,
            authorization=BoundGuiAuthorization.bind(
                grant, binding, allowed_runtime_ids=(element.runtime_id,),
            ),
            input_authorization=authorization,
            binder=self.binder,
        )

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        if not result.ok:
            raise AccessibilityFocusMismatch(
                "UI Automation could not focus the trusted target: "
                + result.reason
            )

        if not result.belongs_to_hwnd(
            binding.hwnd
        ):
            raise AccessibilityFocusMismatch(
                "Focused UI element does not belong to the bound window."
            )

        if not (
            result.has_keyboard_focus
            and result.is_keyboard_focusable
            and result.is_enabled
        ):
            raise AccessibilityFocusMismatch(
                "Trusted UI target did not acquire usable keyboard focus."
            )

        if not _focus_matches(
            element,
            result,
        ):
            raise AccessibilityFocusMismatch(
                "Windows focused a different UI element than the trusted "
                "target."
            )

        fresh_target = AccessibilityTarget(
            candidate_id=target.candidate_id,
            confidence=target.confidence,
            element=element,
            selection_source=target.selection_source,
        )

        return AccessibilityFocusRun(
            target=fresh_target,
            focus=result,
        )

    def focus_and_type_target(
        self,
        binding: WindowBinding,
        description: str,
        text: str,
        *,
        role: str,
        grant: ActionGrant,
    ) -> AccessibilityFocusTypeRun:
        """
        Focus one trusted UIA control, prove that exact control owns keyboard
        focus, then type text.

        No mouse coordinate is generated or used.
        """

        value = str(text)

        if not value:
            raise ValueError(
                "text must not be empty."
            )

        focus_run = self.focus_target(
            binding,
            description,
            role=role,
            grant=grant,
        )

        # Re-check the exact foreground window immediately before input.
        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        # SetFocus succeeded earlier, but keyboard input must not rely on
        # stale focus evidence.
        current_focus = self.focus_fn()

        if not current_focus.ok:
            raise AccessibilityFocusMismatch(
                "Keyboard focus could not be re-read before typing: "
                + current_focus.reason
            )

        if not current_focus.belongs_to_hwnd(
            binding.hwnd
        ):
            raise AccessibilityFocusMismatch(
                "Keyboard focus left the bound application before typing."
            )

        if not (
            current_focus.has_keyboard_focus
            and current_focus.is_keyboard_focusable
            and current_focus.is_enabled
        ):
            raise AccessibilityFocusMismatch(
                "Trusted UI target lost usable keyboard focus before typing."
            )

        if not _focus_matches(
            focus_run.target.element,
            current_focus,
        ):
            raise AccessibilityFocusMismatch(
                "A different UI element acquired focus before keyboard input."
            )

        authorization = BoundInputAuthorization.bind(
            grant,
            binding,
        )

        # Focus is done; Stop between focus and typing sends no text.
        self._checkpoint()

        keyboard = self.inputs.keyboard.type_text(
            binding,
            value,
            authorization=authorization,
        )

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        return AccessibilityFocusTypeRun(
            focus_run=focus_run,
            keyboard=keyboard,
        )

    def restore_text_value(
        self,
        binding: WindowBinding,
        target: AccessibilityTarget,
        value: str,
        *,
        grant: ActionGrant,
    ) -> AccessibilityValueRestoreRun:
        """
        Deterministically restore one exact writable text-input control.

        Recovery is bound to:
          exact application identity,
          exact HWND/layout,
          exact UIA RuntimeId,
          exact UI role/name/type.

        Escape is not treated as a data rollback primitive.
        """

        desired = str(value)

        BoundInputAuthorization.bind(grant, binding)

        self._acquire_foreground(binding)

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        expected = target.element

        if expected.role != "text_input":
            raise AccessibilityTargetStale(
                "Rollback target is not a trusted text input."
            )

        if not expected.runtime_id:
            raise AccessibilityTargetStale(
                "Rollback target has no UIA RuntimeId."
            )

        current = self.lookup_runtime_fn(
            binding.hwnd,
            expected.runtime_id,
        )

        if current is None:
            raise AccessibilityTargetStale(
                "Rollback target disappeared."
            )

        if current.role != "text_input":
            raise AccessibilityTargetStale(
                "Rollback target is no longer a text input."
            )

        if (
            current.name.strip().casefold()
            != expected.name.strip().casefold()
            or current.control_type.strip().casefold()
            != expected.control_type.strip().casefold()
        ):
            raise AccessibilityTargetStale(
                "Rollback target identity metadata changed."
            )

        authorization = BoundInputAuthorization.bind(
            grant,
            binding,
        )

        authorization.require_for(
            binding
        )

        before = current.value

        self._checkpoint()

        restored = self.set_value_fn(
            binding.hwnd,
            expected.runtime_id,
            desired,
            binding=binding,
            authorization=BoundGuiAuthorization.bind(
                grant, binding, allowed_runtime_ids=(expected.runtime_id,),
            ),
            input_authorization=authorization,
            binder=self.binder,
        )

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        if restored.runtime_id != expected.runtime_id:
            raise AccessibilityTargetStale(
                "Rollback verification resolved a different RuntimeId."
            )

        if restored.value != desired:
            raise AccessibilityActionError(
                "Rollback ValuePattern verification failed."
            )

        return AccessibilityValueRestoreRun(
            runtime_id=expected.runtime_id,
            previous_value=before,
            restored_value=restored.value,
            element=restored,
        )

    def invoke_target(
        self,
        binding: WindowBinding,
        description: str,
        *,
        role: str,
        grant: ActionGrant,
    ) -> AccessibilityInvokeRun:
        """
        Invoke one exact trusted UIA element without coordinate semantics.

        Foreground and layout are intentionally NOT requirements. The action
        remains fail-closed on application identity, HWND/process identity,
        RuntimeId, typed role and ActionGrant.
        """

        if not str(description or "").strip():
            raise ValueError(
                "description is required."
            )

        self._checkpoint()

        # Identity/visibility yes; foreground/layout no.
        self.binder.revalidate(
            binding,
            require_foreground=False,
            require_same_layout=False,
        )

        target = self.locator.locate(
            description,
            binding.hwnd,
            role=role,
        )

        # Re-read exact semantic identity immediately before action.
        current = self.lookup_runtime_fn(
            binding.hwnd,
            target.element.runtime_id,
        )

        if current is None:
            raise AccessibilityTargetStale(
                "Semantic target disappeared before Invoke."
            )

        if current.runtime_id != target.element.runtime_id:
            raise AccessibilityTargetStale(
                "Semantic target RuntimeId changed."
            )

        if current.role != target.element.role:
            raise AccessibilityTargetStale(
                "Semantic target role changed."
            )

        if (
            current.name.strip().casefold()
            != target.element.name.strip().casefold()
        ):
            raise AccessibilityTargetStale(
                "Semantic target name changed."
            )

        if (
            current.control_type.strip().casefold()
            != target.element.control_type.strip().casefold()
        ):
            raise AccessibilityTargetStale(
                "Semantic target control type changed."
            )

        if not current.enabled:
            raise AccessibilityActionError(
                "Trusted UIA target is disabled."
            )

        if not current.invoke_pattern:
            raise AccessibilityActionError(
                "Trusted UIA target does not expose InvokePattern."
            )

        authorization = BoundGuiAuthorization.bind(
            grant,
            binding,
            allowed_runtime_ids=(
                current.runtime_id,
            ),
        )

        authorization.require_for(
            binding,
            current.runtime_id,
        )

        # Re-check HWND/process identity immediately before action.
        self.binder.revalidate(
            binding,
            require_foreground=False,
            require_same_layout=False,
        )

        self._checkpoint()

        receipt = self.invoke_fn(
            binding.hwnd,
            current.runtime_id,
            binding=binding,
            authorization=authorization,
            binder=self.binder,
        )

        if (
            receipt.runtime_id
            != current.runtime_id
        ):
            raise AccessibilityTargetStale(
                "Invoke receipt belongs to a different RuntimeId."
            )

        # The actuator may legitimately disappear after Invoke.
        # Only the bound app/window identity is revalidated here.
        self.binder.revalidate(
            binding,
            require_foreground=False,
            require_same_layout=False,
        )

        authorization.require_for(
            binding,
            receipt.runtime_id,
        )

        fresh_target = AccessibilityTarget(
            candidate_id=target.candidate_id,
            confidence=target.confidence,
            element=current,
            selection_source=target.selection_source,
        )

        return AccessibilityInvokeRun(
            target=fresh_target,
            element=current,
        )

    # ------------------------------------------------ exact RuntimeId paths
    #
    # For app adapters that select one element by their own exact rules (an
    # exact contact name, the download control inside one verified message).
    # No locator and no model take part: the caller's element is re-read by
    # RuntimeId and must still have the same name, role and control type. All
    # other guards are the same as invoke_target.

    def _current_exact(
        self,
        binding: WindowBinding,
        expected: ActionableUiElement,
    ) -> ActionableUiElement:
        self._checkpoint()
        self.binder.revalidate(binding, require_foreground=False, require_same_layout=False)
        current = self.lookup_runtime_fn(binding.hwnd, expected.runtime_id)
        if current is None:
            raise AccessibilityTargetStale("The exact UIA element disappeared before acting.")
        if current.runtime_id != expected.runtime_id:
            raise AccessibilityTargetStale("The exact UIA element's RuntimeId changed.")
        if current.role != expected.role:
            raise AccessibilityTargetStale("The exact UIA element's role changed.")
        if current.name.strip().casefold() != expected.name.strip().casefold():
            raise AccessibilityTargetStale("The exact UIA element's name changed.")
        if current.control_type.strip().casefold() != expected.control_type.strip().casefold():
            raise AccessibilityTargetStale("The exact UIA element's control type changed.")
        if not current.enabled:
            raise AccessibilityActionError("The exact UIA element is disabled.")
        return current

    def invoke_exact_element(
        self,
        binding: WindowBinding,
        expected: ActionableUiElement,
        *,
        grant: ActionGrant,
    ) -> AccessibilityInvokeRun:
        """Invoke one caller-selected element by RuntimeId (no locator, no model)."""
        current = self._current_exact(binding, expected)
        if not current.invoke_pattern:
            raise AccessibilityActionError("The exact UIA element does not expose InvokePattern.")
        authorization = BoundGuiAuthorization.bind(
            grant, binding, allowed_runtime_ids=(current.runtime_id,),
        )
        authorization.require_for(binding, current.runtime_id)
        self.binder.revalidate(binding, require_foreground=False, require_same_layout=False)
        self._checkpoint()
        receipt = self.invoke_fn(
            binding.hwnd, current.runtime_id,
            binding=binding, authorization=authorization, binder=self.binder,
        )
        if receipt.runtime_id != current.runtime_id:
            raise AccessibilityTargetStale("Invoke receipt belongs to a different RuntimeId.")
        self.binder.revalidate(binding, require_foreground=False, require_same_layout=False)
        authorization.require_for(binding, receipt.runtime_id)
        return AccessibilityInvokeRun(
            target=AccessibilityTarget(
                candidate_id=current.runtime_id,
                confidence=1.0,
                element=current,
                selection_source="exact_runtime_id",
            ),
            element=current,
        )

    def set_exact_text_value(
        self,
        binding: WindowBinding,
        expected: ActionableUiElement,
        text: str,
        *,
        grant: ActionGrant,
    ) -> ActionableUiElement:
        """Set the ValuePattern text of one caller-selected editable element."""
        value = str(text)
        if not value:
            raise ValueError("text must not be empty.")
        # Typing changes what the window shows, so it follows the same
        # foreground + input-authorization rules as restore_text_value.
        BoundInputAuthorization.bind(grant, binding)
        self._acquire_foreground(binding)
        self.binder.revalidate(binding, require_foreground=True, require_same_layout=True)
        current = self._current_exact(binding, expected)
        if current.role != "text_input" or not current.value_pattern or current.value_read_only is not False:
            raise AccessibilityActionError("The exact UIA element is not an editable text field.")
        input_authorization = BoundInputAuthorization.bind(grant, binding)
        input_authorization.require_for(binding)
        self._checkpoint()
        return self.set_value_fn(
            binding.hwnd, current.runtime_id, value,
            binding=binding,
            authorization=BoundGuiAuthorization.bind(
                grant, binding, allowed_runtime_ids=(current.runtime_id,),
            ),
            input_authorization=input_authorization,
            binder=self.binder,
        )

    def click_target(
        self,
        binding: WindowBinding,
        description: str,
        *,
        role: str,
        grant: ActionGrant,
        radius: int = 8,
    ) -> AccessibilityClickRun:

        if not str(description or "").strip():
            raise ValueError(
                "description is required."
            )

        radius = int(radius)

        if radius < 1 or radius > 32:
            raise ValueError(
                "radius must be between 1 and 32."
            )

        # Exact foreground window before target discovery.
        BoundInputAuthorization.bind(grant, binding)

        self._acquire_foreground(binding)

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        target = self.locator.locate(
            description,
            binding.hwnd,
            role=role,
        )

        # UIA enumeration and optional semantic selection take time.
        # Never act until the exact bound HWND is foreground again.
        self._acquire_foreground(binding)

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        element = self._revalidate_target(
            target,
            binding.hwnd,
            role=role,
        )

        x, y = element.center

        scope = _point_scope(
            binding.snapshot.client_rect,
            element,
            x,
            y,
            radius=radius,
        )

        authorization = (
            BoundInputAuthorization.bind(
                grant,
                binding,
            ).restrict(
                scope
            )
        )

        self._checkpoint()

        click = self.inputs.click.click_at(
            binding,
            x,
            y,
            authorization=authorization,
            allowed_rect=scope,
        )

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        fresh_target = AccessibilityTarget(
            candidate_id=target.candidate_id,
            confidence=target.confidence,
            element=element,
            selection_source=target.selection_source,
        )

        return AccessibilityClickRun(
            target=fresh_target,
            click=click,
            authorized_rect=scope,
        )

    def click_and_type_target(
        self,
        binding: WindowBinding,
        description: str,
        text: str,
        *,
        role: str,
        grant: ActionGrant,
        radius: int = 8,
        focus_timeout: float = 1.0,
    ) -> AccessibilityClickTypeRun:

        value = str(text)

        if not value:
            raise ValueError(
                "text must not be empty."
            )

        click_run = self.click_target(
            binding,
            description,
            role=role,
            grant=grant,
            radius=radius,
        )

        focus = self._wait_for_focus(
            binding,
            click_run.target.element,
            timeout=float(
                focus_timeout
            ),
        )

        # Focus has been proven on the intended control.
        # Reacquire/revalidate once more immediately before keyboard input.
        self._acquire_foreground(binding)

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        keyboard_auth = (
            BoundInputAuthorization.bind(
                grant,
                binding,
            )
        )

        self._checkpoint()

        typed = self.inputs.keyboard.type_text(
            binding,
            value,
            authorization=keyboard_auth,
        )

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        return AccessibilityClickTypeRun(
            click_run=click_run,
            focus=focus,
            keyboard=typed,
        )

    def press_key(
        self,
        binding: WindowBinding,
        key: str,
        *,
        grant: ActionGrant,
    ) -> KeyboardResult:

        self._acquire_foreground(binding)

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        authorization = (
            BoundInputAuthorization.bind(
                grant,
                binding,
            )
        )

        self._checkpoint()

        result = self.inputs.keyboard.press(
            binding,
            key,
            authorization=authorization,
        )

        self.binder.revalidate(
            binding,
            require_foreground=True,
            require_same_layout=True,
        )

        return result

    def _revalidate_target(
        self,
        target: AccessibilityTarget,
        hwnd: int,
        *,
        role: str,
    ) -> ActionableUiElement:

        runtime_id = (
            target.element.runtime_id
        )

        if not runtime_id:
            raise AccessibilityTargetStale(
                "Accessibility target has no runtime identity."
            )

        matches = [
            element
            for _, element
            in self.locator.candidates(
                hwnd,
                role=role,
            )
            if element.runtime_id
            == runtime_id
        ]

        if len(matches) != 1:
            raise AccessibilityTargetStale(
                "Accessibility target changed or disappeared "
                "before input."
            )

        fresh = matches[0]

        if fresh.role != target.element.role:
            raise AccessibilityTargetStale(
                "Accessibility target role changed before input."
            )

        return fresh

    def _wait_for_focus(
        self,
        binding: WindowBinding,
        target: ActionableUiElement,
        *,
        timeout: float,
    ) -> FocusedUiElement:

        deadline = (
            time.monotonic()
            + max(0.05, timeout)
        )

        last = None

        while time.monotonic() < deadline:

            self._checkpoint()

            self.binder.revalidate(
                binding,
                require_foreground=True,
                require_same_layout=True,
            )

            current = self.focus_fn()
            last = current

            if (
                current.ok
                and current.belongs_to_hwnd(
                    binding.hwnd
                )
                and current.has_keyboard_focus
                and current.is_keyboard_focusable
                and current.is_enabled
                and _focus_matches(
                    target,
                    current,
                )
            ):
                return current

            if self.cancel_token is not None:
                self.cancel_token.sleep(0.05)
            else:
                time.sleep(0.05)

        if last is None:
            detail = "no accessibility focus result"
        elif not last.ok:
            detail = last.reason
        else:
            detail = (
                f"name={last.name!r}, "
                f"type={last.control_type!r}"
            )

        raise AccessibilityFocusMismatch(
            "Keyboard focus did not land on the trusted UI target: "
            + detail
        )


def _focus_matches(
    target: ActionableUiElement,
    focus: FocusedUiElement,
) -> bool:

    target_name = target.name.strip().casefold()
    focus_name = focus.name.strip().casefold()

    if target_name and focus_name:
        return (
            target_name == focus_name
            and target.control_type.casefold()
            == focus.control_type.casefold()
        )

    return (
        target.automation_id.strip().casefold()
        == focus.automation_id.strip().casefold()
        and target.class_name.strip().casefold()
        == focus.class_name.strip().casefold()
        and target.control_type.strip().casefold()
        == focus.control_type.strip().casefold()
    )


def _point_scope(
    client: WindowRect,
    element: ActionableUiElement,
    x: int,
    y: int,
    *,
    radius: int,
) -> WindowRect:

    x = int(x)
    y = int(y)

    element_left = math.floor(
        element.left
    )

    element_top = math.floor(
        element.top
    )

    element_right = math.ceil(
        element.right
    )

    element_bottom = math.ceil(
        element.bottom
    )

    if not (
        element_left <= x < element_right
        and element_top <= y < element_bottom
    ):
        raise AccessibilityActionError(
            "Trusted target center is outside its UIA rectangle."
        )

    if not (
        client.left <= x < client.right
        and client.top <= y < client.bottom
    ):
        raise AccessibilityActionError(
            "Trusted target center is outside the verified client."
        )

    scope = WindowRect(
        max(
            client.left,
            element_left,
            x - radius,
        ),
        max(
            client.top,
            element_top,
            y - radius,
        ),
        min(
            client.right,
            element_right,
            x + radius + 1,
        ),
        min(
            client.bottom,
            element_bottom,
            y + radius + 1,
        ),
    )

    if not scope.usable:
        raise AccessibilityActionError(
            "Trusted target input scope is unusable."
        )

    return scope
