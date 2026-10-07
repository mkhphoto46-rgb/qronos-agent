from __future__ import annotations

import base64
import json
import subprocess
import sys
from dataclasses import dataclass


class AccessibilityUnavailable(RuntimeError):
    """Windows UI Automation focus information could not be obtained."""


@dataclass(frozen=True)
class FocusedUiElement:
    ok: bool
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    control_type: str = ""
    native_hwnd: int = 0
    ancestor_hwnds: tuple[int, ...] = ()
    has_keyboard_focus: bool = False
    is_keyboard_focusable: bool = False
    is_enabled: bool = False
    left: float = 0.0
    top: float = 0.0
    width: float = 0.0
    height: float = 0.0
    reason: str = ""

    def belongs_to_hwnd(self, hwnd: int) -> bool:
        wanted = int(hwnd)

        return (
            self.native_hwnd == wanted
            or wanted in self.ancestor_hwnds
        )

    @property
    def likely_text_input(self) -> bool:
        value = self.control_type.casefold()

        return (
            self.has_keyboard_focus
            and self.is_keyboard_focusable
            and self.is_enabled
            and (
                value.endswith(".edit")
                or value.endswith(".document")
                or value.endswith(".combobox")
            )
        )


_SCRIPT = r"""
$ErrorActionPreference = 'Stop'

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$element = [System.Windows.Automation.AutomationElement]::FocusedElement

if ($null -eq $element) {
    [ordered]@{
        ok = $false
        reason = 'Windows UI Automation returned no focused element.'
    } | ConvertTo-Json -Compress

    exit 0
}

$current = $element.Current
$rect = $current.BoundingRectangle

$ancestors = @()

$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$cursor = $element

for ($i = 0; $i -lt 32 -and $null -ne $cursor; $i++) {

    try {
        $handle = [int]$cursor.Current.NativeWindowHandle

        if ($handle -gt 0 -and -not ($ancestors -contains $handle)) {
            $ancestors += $handle
        }
    }
    catch {
    }

    try {
        $cursor = $walker.GetParent($cursor)
    }
    catch {
        $cursor = $null
    }
}

[ordered]@{
    ok = $true
    name = [string]$current.Name
    automation_id = [string]$current.AutomationId
    class_name = [string]$current.ClassName
    control_type = [string]$current.ControlType.ProgrammaticName
    native_hwnd = [int]$current.NativeWindowHandle
    ancestor_hwnds = @($ancestors)
    has_keyboard_focus = [bool]$current.HasKeyboardFocus
    is_keyboard_focusable = [bool]$current.IsKeyboardFocusable
    is_enabled = [bool]$current.IsEnabled
    left = [double]$rect.Left
    top = [double]$rect.Top
    width = [double]$rect.Width
    height = [double]$rect.Height
} | ConvertTo-Json -Depth 5 -Compress
"""


def available() -> bool:
    return sys.platform == "win32"


def focused_element(
    *,
    timeout: float = 8.0,
) -> FocusedUiElement:
    if not available():
        return FocusedUiElement(
            ok=False,
            reason="Windows UI Automation requires Windows.",
        )

    try:
        finished = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=float(timeout),
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return FocusedUiElement(
            ok=False,
            reason=str(exc),
        )

    if finished.returncode != 0:
        return FocusedUiElement(
            ok=False,
            reason=(
                finished.stderr
                or "Windows UI Automation PowerShell failed."
            ).strip()[:500],
        )

    return _parse(
        (finished.stdout or "").strip()
    )


def _parse(raw: str) -> FocusedUiElement:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return FocusedUiElement(
            ok=False,
            reason=f"Unreadable UI Automation response: {raw[:300]}",
        )

    if not isinstance(payload, dict):
        return FocusedUiElement(
            ok=False,
            reason="UI Automation response was not an object.",
        )

    if not payload.get("ok"):
        return FocusedUiElement(
            ok=False,
            reason=str(
                payload.get(
                    "reason",
                    "UI Automation focus lookup failed.",
                )
            ),
        )

    raw_ancestors = payload.get(
        "ancestor_hwnds",
        [],
    )

    if isinstance(raw_ancestors, int):
        raw_ancestors = [raw_ancestors]

    if not isinstance(raw_ancestors, list):
        raw_ancestors = []

    ancestors = tuple(
        int(value)
        for value in raw_ancestors
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and int(value) > 0
    )

    return FocusedUiElement(
        ok=True,
        name=str(payload.get("name", "")),
        automation_id=str(
            payload.get("automation_id", "")
        ),
        class_name=str(
            payload.get("class_name", "")
        ),
        control_type=str(
            payload.get("control_type", "")
        ),
        native_hwnd=int(
            payload.get("native_hwnd", 0)
            or 0
        ),
        ancestor_hwnds=ancestors,
        has_keyboard_focus=bool(
            payload.get(
                "has_keyboard_focus",
                False,
            )
        ),
        is_keyboard_focusable=bool(
            payload.get(
                "is_keyboard_focusable",
                False,
            )
        ),
        is_enabled=bool(
            payload.get(
                "is_enabled",
                False,
            )
        ),
        left=float(
            payload.get("left", 0.0)
            or 0.0
        ),
        top=float(
            payload.get("top", 0.0)
            or 0.0
        ),
        width=float(
            payload.get("width", 0.0)
            or 0.0
        ),
        height=float(
            payload.get("height", 0.0)
            or 0.0
        ),
    )



@dataclass(frozen=True)
class UiElementCandidate:
    """One visible Windows UI Automation element with screen geometry."""

    name: str
    automation_id: str
    class_name: str
    control_type: str
    native_hwnd: int
    has_keyboard_focus: bool
    is_keyboard_focusable: bool
    is_enabled: bool
    left: float
    top: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    @property
    def usable(self) -> bool:
        return self.width > 1.0 and self.height > 1.0

    @property
    def likely_text_input(self) -> bool:
        value = self.control_type.casefold()

        return (
            self.is_enabled
            and (
                value.endswith(".edit")
                or value.endswith(".document")
                or value.endswith(".combobox")
            )
        )

    def contains(self, x: int, y: int) -> bool:
        return (
            self.left <= int(x) < self.right
            and self.top <= int(y) < self.bottom
        )


def _run_uia_script(
    script: str,
    *,
    timeout: float,
) -> tuple[bool, str]:
    try:
        finished = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=float(timeout),
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)

    if finished.returncode != 0:
        return False, (
            finished.stderr
            or "Windows UI Automation PowerShell failed."
        ).strip()[:1000]

    return True, (finished.stdout or "").strip()


def elements_in_window(
    hwnd: int,
    *,
    max_elements: int = 512,
    timeout: float = 12.0,
) -> tuple[UiElementCandidate, ...]:
    """
    Enumerate visible UI Automation descendants of one exact HWND.

    This is geometry/semantics discovery only. It performs no UI action.
    """
    handle = int(hwnd)
    limit = int(max_elements)

    if handle <= 0:
        raise ValueError("hwnd must be positive.")

    if limit < 1 or limit > 4096:
        raise ValueError(
            "max_elements must be between 1 and 4096."
        )

    script = rf"""
$ErrorActionPreference = 'Stop'

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName WindowsBase

$hwnd = [IntPtr]::new({handle})
$root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)

if ($null -eq $root) {{
    [ordered]@{{
        ok = $false
        reason = 'UI Automation could not resolve the requested HWND.'
    }} | ConvertTo-Json -Compress

    exit 0
}}

$collection = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
)

$items = @()

for ($i = 0; $i -lt $collection.Count; $i++) {{

    if ($items.Count -ge {limit}) {{
        break
    }}

    $element = $collection.Item($i)

    try {{
        $current = $element.Current
        $rect = $current.BoundingRectangle

        if ($current.IsOffscreen) {{
            continue
        }}

        if ($rect.Width -le 1 -or $rect.Height -le 1) {{
            continue
        }}

        $items += [ordered]@{{
            name = [string]$current.Name
            automation_id = [string]$current.AutomationId
            class_name = [string]$current.ClassName
            control_type = [string]$current.ControlType.ProgrammaticName
            native_hwnd = [int]$current.NativeWindowHandle
            has_keyboard_focus = [bool]$current.HasKeyboardFocus
            is_keyboard_focusable = [bool]$current.IsKeyboardFocusable
            is_enabled = [bool]$current.IsEnabled
            left = [double]$rect.Left
            top = [double]$rect.Top
            width = [double]$rect.Width
            height = [double]$rect.Height
        }}
    }}
    catch {{
    }}
}}

[ordered]@{{
    ok = $true
    elements = @($items)
}} | ConvertTo-Json -Depth 6 -Compress
"""

    ok, raw = _run_uia_script(
        script,
        timeout=timeout,
    )

    if not ok:
        raise AccessibilityUnavailable(raw)

    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise AccessibilityUnavailable(
            f"Unreadable UI Automation response: {raw[:500]}"
        ) from exc

    if not isinstance(payload, dict) or not payload.get("ok"):
        reason = (
            payload.get("reason", "unknown")
            if isinstance(payload, dict)
            else "unknown"
        )

        raise AccessibilityUnavailable(
            f"UI Automation enumeration failed: {reason}"
        )

    raw_elements = payload.get(
        "elements",
        [],
    )

    if isinstance(raw_elements, dict):
        raw_elements = [raw_elements]

    if not isinstance(raw_elements, list):
        return ()

    result = []

    for value in raw_elements:

        if not isinstance(value, dict):
            continue

        try:
            candidate = UiElementCandidate(
                name=str(value.get("name", "")),
                automation_id=str(
                    value.get("automation_id", "")
                ),
                class_name=str(
                    value.get("class_name", "")
                ),
                control_type=str(
                    value.get("control_type", "")
                ),
                native_hwnd=int(
                    value.get("native_hwnd", 0)
                    or 0
                ),
                has_keyboard_focus=bool(
                    value.get(
                        "has_keyboard_focus",
                        False,
                    )
                ),
                is_keyboard_focusable=bool(
                    value.get(
                        "is_keyboard_focusable",
                        False,
                    )
                ),
                is_enabled=bool(
                    value.get(
                        "is_enabled",
                        False,
                    )
                ),
                left=float(
                    value.get("left", 0.0)
                    or 0.0
                ),
                top=float(
                    value.get("top", 0.0)
                    or 0.0
                ),
                width=float(
                    value.get("width", 0.0)
                    or 0.0
                ),
                height=float(
                    value.get("height", 0.0)
                    or 0.0
                ),
            )
        except (TypeError, ValueError):
            continue

        if candidate.usable:
            result.append(candidate)

    return tuple(result)


def element_at_point(
    x: int,
    y: int,
    *,
    timeout: float = 8.0,
) -> UiElementCandidate | None:
    """
    Return the UI Automation element physically under one screen point.

    No click is performed.
    """
    px = int(x)
    py = int(y)

    script = rf"""
$ErrorActionPreference = 'Stop'

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName WindowsBase

$point = [System.Windows.Point]::new(
    [double]{px},
    [double]{py}
)

$element = [System.Windows.Automation.AutomationElement]::FromPoint($point)

if ($null -eq $element) {{
    [ordered]@{{
        ok = $false
        reason = 'No UI Automation element exists at the requested point.'
    }} | ConvertTo-Json -Compress

    exit 0
}}

$current = $element.Current
$rect = $current.BoundingRectangle

[ordered]@{{
    ok = $true
    element = [ordered]@{{
        name = [string]$current.Name
        automation_id = [string]$current.AutomationId
        class_name = [string]$current.ClassName
        control_type = [string]$current.ControlType.ProgrammaticName
        native_hwnd = [int]$current.NativeWindowHandle
        has_keyboard_focus = [bool]$current.HasKeyboardFocus
        is_keyboard_focusable = [bool]$current.IsKeyboardFocusable
        is_enabled = [bool]$current.IsEnabled
        left = [double]$rect.Left
        top = [double]$rect.Top
        width = [double]$rect.Width
        height = [double]$rect.Height
    }}
}} | ConvertTo-Json -Depth 5 -Compress
"""

    ok, raw = _run_uia_script(
        script,
        timeout=timeout,
    )

    if not ok:
        raise AccessibilityUnavailable(raw)

    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise AccessibilityUnavailable(
            f"Unreadable UI Automation response: {raw[:500]}"
        ) from exc

    if not isinstance(payload, dict) or not payload.get("ok"):
        return None

    value = payload.get("element")

    if not isinstance(value, dict):
        return None

    try:
        candidate = UiElementCandidate(
            name=str(value.get("name", "")),
            automation_id=str(
                value.get("automation_id", "")
            ),
            class_name=str(
                value.get("class_name", "")
            ),
            control_type=str(
                value.get("control_type", "")
            ),
            native_hwnd=int(
                value.get("native_hwnd", 0)
                or 0
            ),
            has_keyboard_focus=bool(
                value.get(
                    "has_keyboard_focus",
                    False,
                )
            ),
            is_keyboard_focusable=bool(
                value.get(
                    "is_keyboard_focusable",
                    False,
                )
            ),
            is_enabled=bool(
                value.get(
                    "is_enabled",
                    False,
                )
            ),
            left=float(
                value.get("left", 0.0)
                or 0.0
            ),
            top=float(
                value.get("top", 0.0)
                or 0.0
            ),
            width=float(
                value.get("width", 0.0)
                or 0.0
            ),
            height=float(
                value.get("height", 0.0)
                or 0.0
            ),
        )
    except (TypeError, ValueError):
        return None

    return candidate if candidate.usable else None



@dataclass(frozen=True)
class ActionableUiElement:
    """
    One visible UI Automation candidate with interaction semantics.

    This is raw platform evidence. It carries no application-specific policy.
    """

    runtime_id: str
    name: str
    automation_id: str
    class_name: str
    control_type: str
    value: str

    enabled: bool
    focusable: bool
    focused: bool

    value_pattern: bool
    value_read_only: bool | None
    text_pattern: bool
    invoke_pattern: bool
    expand_pattern: bool

    left: float
    top: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    @property
    def usable(self) -> bool:
        return self.width > 1.0 and self.height > 1.0

    @property
    def center(self) -> tuple[int, int]:
        return (
            round(self.left + self.width / 2.0),
            round(self.top + self.height / 2.0),
        )

    @property
    def role(self) -> str:
        """
        Conservative typed interaction role.

        A control being called Edit is not enough. Explorer exposes many
        property cells as Edit even though they are not ordinary text fields.
        """

        if not self.enabled or not self.usable:
            return "other"

        raw = self.control_type.casefold()

        if (
            self.focusable
            and (
                raw.endswith(".edit")
                or raw.endswith(".document")
                or raw.endswith(".combobox")
            )
            and (
                (
                    self.value_pattern
                    and self.value_read_only is False
                )
                or self.text_pattern
            )
        ):
            return "text_input"

        if (
            self.focusable
            and (
                raw.endswith(".button")
                or raw.endswith(".splitbutton")
            )
            and (
                self.invoke_pattern
                or self.expand_pattern
            )
        ):
            return "button"

        if (
            self.focusable
            and raw.endswith(".menuitem")
        ):
            return "menu_item"

        if (
            self.focusable
            and raw.endswith(".listitem")
        ):
            return "list_item"

        if (
            self.focusable
            and raw.endswith(".tabitem")
        ):
            return "tab"

        return "other"


def actionable_elements_in_window(
    hwnd: int,
    *,
    max_elements: int = 1024,
    timeout: float = 15.0,
) -> tuple[ActionableUiElement, ...]:
    """
    Return typed, visible UI Automation candidates for one exact HWND.

    No input event is generated.
    """

    handle = int(hwnd)
    limit = int(max_elements)

    if handle <= 0:
        raise ValueError("hwnd must be positive.")

    if limit < 1 or limit > 4096:
        raise ValueError(
            "max_elements must be between 1 and 4096."
        )

    script = rf"""
$ErrorActionPreference = 'Stop'

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName WindowsBase

$root = [System.Windows.Automation.AutomationElement]::FromHandle(
    [IntPtr]::new({handle})
)

if ($null -eq $root) {{
    [ordered]@{{
        ok = $false
        reason = 'Could not resolve requested HWND.'
    }} | ConvertTo-Json -Compress
    exit 0
}}

$items = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
)

$result = @()

for ($i = 0; $i -lt $items.Count; $i++) {{

    if ($result.Count -ge {limit}) {{
        break
    }}

    $element = $items.Item($i)

    try {{
        $c = $element.Current
        $r = $c.BoundingRectangle

        if ($c.IsOffscreen) {{
            continue
        }}

        if ($r.Width -le 1 -or $r.Height -le 1) {{
            continue
        }}

        $valueAvailable = [bool]$element.GetCurrentPropertyValue(
            [System.Windows.Automation.AutomationElement]::IsValuePatternAvailableProperty
        )

        $textAvailable = [bool]$element.GetCurrentPropertyValue(
            [System.Windows.Automation.AutomationElement]::IsTextPatternAvailableProperty
        )

        $invokeAvailable = [bool]$element.GetCurrentPropertyValue(
            [System.Windows.Automation.AutomationElement]::IsInvokePatternAvailableProperty
        )

        $expandAvailable = [bool]$element.GetCurrentPropertyValue(
            [System.Windows.Automation.AutomationElement]::IsExpandCollapsePatternAvailableProperty
        )

        $value = ""
        $readOnly = $null

        if ($valueAvailable) {{
            try {{
                $pattern = $element.GetCurrentPattern(
                    [System.Windows.Automation.ValuePattern]::Pattern
                )

                $value = [string]$pattern.Current.Value
                $readOnly = [bool]$pattern.Current.IsReadOnly
            }}
            catch {{
            }}
        }}

        $runtimeId = ""

        try {{
            $runtimeId = (
                $element.GetRuntimeId() |
                ForEach-Object {{ [string]$_ }}
            ) -join "."
        }}
        catch {{
        }}

        $result += [ordered]@{{
            runtime_id = $runtimeId

            name = [string]$c.Name
            automation_id = [string]$c.AutomationId
            class_name = [string]$c.ClassName
            control_type = [string]$c.ControlType.ProgrammaticName

            value = $value

            enabled = [bool]$c.IsEnabled
            focusable = [bool]$c.IsKeyboardFocusable
            focused = [bool]$c.HasKeyboardFocus

            value_pattern = $valueAvailable
            value_read_only = $readOnly
            text_pattern = $textAvailable
            invoke_pattern = $invokeAvailable
            expand_pattern = $expandAvailable

            left = [double]$r.Left
            top = [double]$r.Top
            width = [double]$r.Width
            height = [double]$r.Height
        }}
    }}
    catch {{
    }}
}}

[ordered]@{{
    ok = $true
    elements = @($result)
}} | ConvertTo-Json -Depth 6 -Compress
"""

    ok, raw = _run_uia_script(
        script,
        timeout=timeout,
    )

    if not ok:
        raise AccessibilityUnavailable(raw)

    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise AccessibilityUnavailable(
            f"Unreadable UIA candidate response: {raw[:500]}"
        ) from exc

    if not isinstance(payload, dict) or not payload.get("ok"):

        reason = (
            payload.get("reason", "unknown")
            if isinstance(payload, dict)
            else "unknown"
        )

        raise AccessibilityUnavailable(
            f"UIA candidate discovery failed: {reason}"
        )

    values = payload.get(
        "elements",
        [],
    )

    if isinstance(values, dict):
        values = [values]

    if not isinstance(values, list):
        return ()

    result = []

    for value in values:

        if not isinstance(value, dict):
            continue

        raw_read_only = value.get(
            "value_read_only"
        )

        read_only = (
            raw_read_only
            if isinstance(raw_read_only, bool)
            else None
        )

        try:
            item = ActionableUiElement(
                runtime_id=str(
                    value.get(
                        "runtime_id",
                        "",
                    )
                ),
                name=str(
                    value.get("name", "")
                ),
                automation_id=str(
                    value.get(
                        "automation_id",
                        "",
                    )
                ),
                class_name=str(
                    value.get(
                        "class_name",
                        "",
                    )
                ),
                control_type=str(
                    value.get(
                        "control_type",
                        "",
                    )
                ),
                value=str(
                    value.get("value", "")
                ),

                enabled=bool(
                    value.get(
                        "enabled",
                        False,
                    )
                ),
                focusable=bool(
                    value.get(
                        "focusable",
                        False,
                    )
                ),
                focused=bool(
                    value.get(
                        "focused",
                        False,
                    )
                ),

                value_pattern=bool(
                    value.get(
                        "value_pattern",
                        False,
                    )
                ),
                value_read_only=read_only,
                text_pattern=bool(
                    value.get(
                        "text_pattern",
                        False,
                    )
                ),
                invoke_pattern=bool(
                    value.get(
                        "invoke_pattern",
                        False,
                    )
                ),
                expand_pattern=bool(
                    value.get(
                        "expand_pattern",
                        False,
                    )
                ),

                left=float(
                    value.get(
                        "left",
                        0.0,
                    )
                    or 0.0
                ),
                top=float(
                    value.get(
                        "top",
                        0.0,
                    )
                    or 0.0
                ),
                width=float(
                    value.get(
                        "width",
                        0.0,
                    )
                    or 0.0
                ),
                height=float(
                    value.get(
                        "height",
                        0.0,
                    )
                    or 0.0
                ),
            )
        except (TypeError, ValueError):
            continue

        if item.usable:
            result.append(item)

    result.sort(
        key=lambda item: (
            item.top,
            item.left,
            item.control_type,
            item.name,
        )
    )

    return tuple(result)



def focus_runtime_id(
    hwnd: int,
    runtime_id: str,
    *,
    timeout: float = 8.0,
) -> FocusedUiElement:
    """
    Ask Windows UI Automation to focus one exact element.

    The element must still exist underneath the exact requested HWND and
    must still have the same UIA RuntimeId discovered earlier.

    No mouse coordinates are involved.
    """

    handle = int(hwnd)
    wanted = str(runtime_id or "").strip()

    if handle <= 0:
        raise ValueError(
            "hwnd must be positive."
        )

    if not wanted:
        raise ValueError(
            "runtime_id is required."
        )

    wanted_json = json.dumps(
        wanted
    )

    script = rf"""
$ErrorActionPreference = 'Stop'

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName WindowsBase

$root = [System.Windows.Automation.AutomationElement]::FromHandle(
    [IntPtr]::new({handle})
)

if ($null -eq $root) {{
    [ordered]@{{
        ok = $false
        reason = 'UI Automation could not resolve the requested HWND.'
    }} | ConvertTo-Json -Compress

    exit 0
}}

$wanted = {wanted_json}

$items = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
)

$target = $null

for ($i = 0; $i -lt $items.Count; $i++) {{

    $candidate = $items.Item($i)

    try {{
        $runtime = (
            $candidate.GetRuntimeId() |
            ForEach-Object {{ [string]$_ }}
        ) -join "."

        if ($runtime -eq $wanted) {{
            $target = $candidate
            break
        }}
    }}
    catch {{
    }}
}}

if ($null -eq $target) {{
    [ordered]@{{
        ok = $false
        reason = 'The requested UI Automation RuntimeId no longer exists.'
    }} | ConvertTo-Json -Compress

    exit 0
}}

try {{
    $target.SetFocus()
}}
catch {{
    [ordered]@{{
        ok = $false
        reason = 'UI Automation SetFocus failed: ' + $_.Exception.Message
    }} | ConvertTo-Json -Compress

    exit 0
}}

Start-Sleep -Milliseconds 120

$element = [System.Windows.Automation.AutomationElement]::FocusedElement

if ($null -eq $element) {{
    [ordered]@{{
        ok = $false
        reason = 'Windows reported no focused element after SetFocus.'
    }} | ConvertTo-Json -Compress

    exit 0
}}

$current = $element.Current
$rect = $current.BoundingRectangle

$ancestors = @()

$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$cursor = $element

for ($i = 0; $i -lt 32 -and $null -ne $cursor; $i++) {{

    try {{
        $ancestorHandle = [int]$cursor.Current.NativeWindowHandle

        if (
            $ancestorHandle -gt 0 -and
            -not ($ancestors -contains $ancestorHandle)
        ) {{
            $ancestors += $ancestorHandle
        }}
    }}
    catch {{
    }}

    try {{
        $cursor = $walker.GetParent($cursor)
    }}
    catch {{
        $cursor = $null
    }}
}}

[ordered]@{{
    ok = $true
    name = [string]$current.Name
    automation_id = [string]$current.AutomationId
    class_name = [string]$current.ClassName
    control_type = [string]$current.ControlType.ProgrammaticName
    native_hwnd = [int]$current.NativeWindowHandle
    ancestor_hwnds = @($ancestors)
    has_keyboard_focus = [bool]$current.HasKeyboardFocus
    is_keyboard_focusable = [bool]$current.IsKeyboardFocusable
    is_enabled = [bool]$current.IsEnabled
    left = [double]$rect.Left
    top = [double]$rect.Top
    width = [double]$rect.Width
    height = [double]$rect.Height
}} | ConvertTo-Json -Depth 5 -Compress
"""

    ok, raw = _run_uia_script(
        script,
        timeout=timeout,
    )

    if not ok:
        return FocusedUiElement(
            ok=False,
            reason=raw,
        )

    return _parse(raw)



def actionable_element_by_runtime_id(
    hwnd: int,
    runtime_id: str,
    *,
    timeout: float = 15.0,
) -> ActionableUiElement | None:
    """
    Re-read one exact actionable UI Automation element by RuntimeId.

    This is used for post-action verification so Qronos verifies the same
    platform control it acted on rather than merely finding another control
    with a similar name.
    """

    wanted = str(runtime_id or "").strip()

    if not wanted:
        raise ValueError(
            "runtime_id is required."
        )

    for element in actionable_elements_in_window(
        int(hwnd),
        timeout=timeout,
    ):
        if element.runtime_id == wanted:
            return element

    return None



def set_value_runtime_id(
    hwnd: int,
    runtime_id: str,
    value: str,
    *,
    timeout: float = 10.0,
) -> ActionableUiElement:
    """
    Set the ValuePattern value of one exact UI Automation element.

    The element must:
      - still exist below the exact requested HWND,
      - retain the exact RuntimeId,
      - expose ValuePattern,
      - not be read-only.

    Intended primarily for deterministic rollback of temporary text-input
    actions.
    """

    handle = int(hwnd)
    wanted = str(runtime_id or "").strip()
    desired = str(value)

    if handle <= 0:
        raise ValueError(
            "hwnd must be positive."
        )

    if not wanted:
        raise ValueError(
            "runtime_id is required."
        )

    encoded = base64.b64encode(
        desired.encode("utf-8")
    ).decode("ascii")

    script = rf"""
$ErrorActionPreference = 'Stop'

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName WindowsBase

$root = [System.Windows.Automation.AutomationElement]::FromHandle(
    [IntPtr]::new({handle})
)

if ($null -eq $root) {{
    [ordered]@{{
        ok = $false
        reason = 'UI Automation could not resolve the requested HWND.'
    }} | ConvertTo-Json -Compress

    exit 0
}}

$wanted = "{wanted}"

$desired = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String(
        "{encoded}"
    )
)

$items = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
)

$target = $null

for ($i = 0; $i -lt $items.Count; $i++) {{

    $candidate = $items.Item($i)

    try {{
        $runtime = (
            $candidate.GetRuntimeId() |
            ForEach-Object {{
                [string]$_
            }}
        ) -join "."

        if ($runtime -eq $wanted) {{
            $target = $candidate
            break
        }}
    }}
    catch {{
    }}
}}

if ($null -eq $target) {{
    [ordered]@{{
        ok = $false
        reason = 'The exact RuntimeId no longer exists.'
    }} | ConvertTo-Json -Compress

    exit 0
}}

$valueAvailable = [bool]$target.GetCurrentPropertyValue(
    [System.Windows.Automation.AutomationElement]::IsValuePatternAvailableProperty
)

if (-not $valueAvailable) {{
    [ordered]@{{
        ok = $false
        reason = 'The exact target does not expose ValuePattern.'
    }} | ConvertTo-Json -Compress

    exit 0
}}

try {{
    $pattern = $target.GetCurrentPattern(
        [System.Windows.Automation.ValuePattern]::Pattern
    )
}}
catch {{
    [ordered]@{{
        ok = $false
        reason = 'Could not obtain ValuePattern.'
    }} | ConvertTo-Json -Compress

    exit 0
}}

if ($pattern.Current.IsReadOnly) {{
    [ordered]@{{
        ok = $false
        reason = 'The exact target ValuePattern is read-only.'
    }} | ConvertTo-Json -Compress

    exit 0
}}

try {{
    $pattern.SetValue(
        $desired
    )
}}
catch {{
    [ordered]@{{
        ok = $false
        reason = 'ValuePattern.SetValue failed: ' + $_.Exception.Message
    }} | ConvertTo-Json -Compress

    exit 0
}}

Start-Sleep -Milliseconds 120

[ordered]@{{
    ok = $true
}} | ConvertTo-Json -Compress
"""

    ok, raw = _run_uia_script(
        script,
        timeout=timeout,
    )

    if not ok:
        raise AccessibilityUnavailable(
            raw
        )

    try:
        payload = json.loads(
            raw or "{}"
        )
    except json.JSONDecodeError as exc:
        raise AccessibilityUnavailable(
            f"Unreadable SetValue response: {raw[:500]}"
        ) from exc

    if (
        not isinstance(payload, dict)
        or not payload.get("ok")
    ):
        reason = (
            payload.get(
                "reason",
                "unknown",
            )
            if isinstance(payload, dict)
            else "unknown"
        )

        raise AccessibilityUnavailable(
            f"UIA SetValue failed: {reason}"
        )

    fresh = actionable_element_by_runtime_id(
        handle,
        wanted,
        timeout=timeout,
    )

    if fresh is None:
        raise AccessibilityUnavailable(
            "Exact target disappeared after ValuePattern.SetValue."
        )

    if fresh.runtime_id != wanted:
        raise AccessibilityUnavailable(
            "Target identity changed after ValuePattern.SetValue."
        )

    if fresh.value != desired:
        raise AccessibilityUnavailable(
            "Target value did not match requested rollback value."
        )

    return fresh



@dataclass(frozen=True)
class UiInvokeReceipt:
    runtime_id: str


def invoke_runtime_id(
    hwnd: int,
    runtime_id: str,
    *,
    timeout: float = 10.0,
) -> UiInvokeReceipt:
    """
    Invoke one exact UI Automation element by RuntimeId.

    Success means Windows accepted InvokePattern.Invoke() for that exact
    RuntimeId.

    The target is NOT required to remain present afterwards. Postconditions
    belong to the action-specific verifier, not to the actuator.
    """

    handle = int(hwnd)
    wanted = str(runtime_id or "").strip()

    if handle <= 0:
        raise ValueError(
            "hwnd must be positive."
        )

    if not wanted:
        raise ValueError(
            "runtime_id is required."
        )

    wanted_json = json.dumps(
        wanted
    )

    script = rf"""
$ErrorActionPreference = 'Stop'

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName WindowsBase

$root = [System.Windows.Automation.AutomationElement]::FromHandle(
    [IntPtr]::new({handle})
)

if ($null -eq $root) {{
    [ordered]@{{
        ok = $false
        reason = 'UI Automation could not resolve the requested HWND.'
    }} | ConvertTo-Json -Compress
    exit 0
}}

$wanted = {wanted_json}

$items = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
)

$target = $null

for ($i = 0; $i -lt $items.Count; $i++) {{

    $candidate = $items.Item($i)

    try {{

        $runtime = (
            $candidate.GetRuntimeId() |
            ForEach-Object {{
                [string]$_
            }}
        ) -join "."

        if ($runtime -eq $wanted) {{
            $target = $candidate
            break
        }}
    }}
    catch {{
    }}
}}

if ($null -eq $target) {{
    [ordered]@{{
        ok = $false
        reason = 'The exact RuntimeId no longer exists.'
    }} | ConvertTo-Json -Compress
    exit 0
}}

if (-not $target.Current.IsEnabled) {{
    [ordered]@{{
        ok = $false
        reason = 'The exact target is disabled.'
    }} | ConvertTo-Json -Compress
    exit 0
}}

$invokeAvailable = [bool]$target.GetCurrentPropertyValue(
    [System.Windows.Automation.AutomationElement]::IsInvokePatternAvailableProperty
)

if (-not $invokeAvailable) {{
    [ordered]@{{
        ok = $false
        reason = 'The exact target does not expose InvokePattern.'
    }} | ConvertTo-Json -Compress
    exit 0
}}

try {{

    $pattern = $target.GetCurrentPattern(
        [System.Windows.Automation.InvokePattern]::Pattern
    )

    $pattern.Invoke()
}}
catch {{

    [ordered]@{{
        ok = $false
        reason = 'InvokePattern.Invoke failed: ' + $_.Exception.Message
    }} | ConvertTo-Json -Compress

    exit 0
}}

[ordered]@{{
    ok = $true
    runtime_id = $wanted
}} | ConvertTo-Json -Compress
"""

    ok, raw = _run_uia_script(
        script,
        timeout=timeout,
    )

    if not ok:
        raise AccessibilityUnavailable(
            raw
        )

    try:
        payload = json.loads(
            raw or "{}"
        )

    except json.JSONDecodeError as exc:

        raise AccessibilityUnavailable(
            f"Unreadable InvokePattern response: {raw[:500]}"
        ) from exc

    if (
        not isinstance(payload, dict)
        or not payload.get("ok")
    ):

        reason = (
            payload.get(
                "reason",
                "unknown",
            )
            if isinstance(
                payload,
                dict,
            )
            else "unknown"
        )

        raise AccessibilityUnavailable(
            f"UIA Invoke failed: {reason}"
        )

    returned = str(
        payload.get(
            "runtime_id",
            "",
        )
        or ""
    ).strip()

    if returned != wanted:
        raise AccessibilityUnavailable(
            "Invoke receipt RuntimeId did not match requested target."
        )

    return UiInvokeReceipt(
        runtime_id=wanted,
    )
