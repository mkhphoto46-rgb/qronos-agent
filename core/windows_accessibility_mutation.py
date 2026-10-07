from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass

from core.gui_authorization import (
    BoundGuiAuthorization,
    GuiAuthorizationMismatch,
    GuiPermissionRequired,
)
from core.input_safety import BoundInputAuthorization, BoundInputGuard
from core.window_binding import WindowBinder, WindowBinding
from core.windows_accessibility import (
    AccessibilityUnavailable,
    ActionableUiElement,
    FocusedUiElement,
    _parse,
    _run_uia_script,
    actionable_element_by_runtime_id,
)


def _require_mutation(
    hwnd: int,
    runtime_id: str,
    *,
    binding: WindowBinding,
    authorization: BoundGuiAuthorization,
    input_authorization: BoundInputAuthorization | None,
    binder: WindowBinder,
    require_foreground: bool,
) -> None:
    """Validate a gate-issued exact-element capability before native work."""
    if not isinstance(authorization, BoundGuiAuthorization):
        raise GuiPermissionRequired("UIA mutation requires a semantic GUI authorization.")
    if not isinstance(binding, WindowBinding) or int(hwnd) != binding.hwnd:
        raise GuiAuthorizationMismatch("UIA mutation HWND does not match its binding.")
    authorization.require_for(binding, runtime_id)
    if require_foreground:
        input_auth = BoundInputGuard.require_authorization(input_authorization, binding)
        if input_auth.action_id != authorization.action_id:
            raise GuiAuthorizationMismatch("UIA and input authorizations belong to different actions.")
    binder.revalidate(
        binding,
        require_foreground=require_foreground,
        require_same_layout=require_foreground,
    )
    # Revalidation may take time; an expired capability must not reach native code.
    authorization.require_for(binding, runtime_id)
    if require_foreground:
        BoundInputGuard.require_authorization(input_authorization, binding)


def _powershell_string(value: str) -> str:
    """Single-quoted PowerShell literal; JSON strings still expand $()/variables."""
    return "'" + value.replace("'", "''") + "'"


def _native_guard_script(
    binding: WindowBinding,
    authorization: BoundGuiAuthorization,
    input_authorization: BoundInputAuthorization | None,
) -> str:
    """Recheck process ownership and expiry inside the UIA worker before acting."""
    expiry = authorization.expires_at
    if input_authorization is not None:
        expiry = min(expiry, input_authorization.expires_at)
    deadline_ms = int((time.time() + expiry - time.monotonic()) * 1000)
    return f"""
$process = [System.Diagnostics.Process]::GetProcessById({binding.snapshot.pid})
if (
    $root.Current.ProcessId -ne {binding.snapshot.pid} -or
    $process.StartTime.ToUniversalTime().ToFileTimeUtc() -ne {binding.snapshot.process_start_time_100ns} -or
    [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -ge {deadline_ms}
) {{
    throw 'The bound UI Automation authorization or process identity changed.'
}}
"""


def _run_mutation_script(script: str, *, timeout: float) -> tuple[bool, str]:
    # PowerShell errors and TimeoutExpired can contain the entire command,
    # including the rollback value. Never forward them to callers or audit.
    ok, raw = _run_uia_script(script, timeout=timeout)
    if ok:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("ok") is True:
            return True, raw
    return False, "Windows UI Automation mutation could not complete."


def focus_runtime_id(
    hwnd: int,
    runtime_id: str,
    *,
    binding: WindowBinding,
    authorization: BoundGuiAuthorization,
    binder: WindowBinder,
    input_authorization: BoundInputAuthorization | None = None,
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

    wanted_literal = _powershell_string(wanted)

    _require_mutation(
        handle, wanted, binding=binding, authorization=authorization,
        input_authorization=input_authorization, binder=binder,
        require_foreground=True,
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

$process = [System.Diagnostics.Process]::GetProcessById({binding.snapshot.pid})
if (
    $root.Current.ProcessId -ne {binding.snapshot.pid} -or
    $process.StartTime.ToUniversalTime().ToFileTimeUtc() -ne {binding.snapshot.process_start_time_100ns}
) {{
    throw 'The bound UI Automation process identity changed.'
}}

$wanted = {wanted_literal}

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
    {_native_guard_script(binding, authorization, input_authorization)}
    $target.SetFocus()
}}
catch {{
    [ordered]@{{
        ok = $false
        reason = 'UI Automation SetFocus failed.'
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

    _require_mutation(
        handle, wanted, binding=binding, authorization=authorization,
        input_authorization=input_authorization, binder=binder,
        require_foreground=True,
    )

    ok, raw = _run_mutation_script(
        script,
        timeout=timeout,
    )

    if not ok:
        return FocusedUiElement(
            ok=False,
            reason=raw,
        )

    _require_mutation(
        handle, wanted, binding=binding, authorization=authorization,
        input_authorization=input_authorization, binder=binder,
        require_foreground=True,
    )

    return _parse(raw)



def set_value_runtime_id(
    hwnd: int,
    runtime_id: str,
    value: str,
    *,
    binding: WindowBinding,
    authorization: BoundGuiAuthorization,
    binder: WindowBinder,
    input_authorization: BoundInputAuthorization | None = None,
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

    _require_mutation(
        handle, wanted, binding=binding, authorization=authorization,
        input_authorization=input_authorization, binder=binder,
        require_foreground=True,
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

$process = [System.Diagnostics.Process]::GetProcessById({binding.snapshot.pid})
if (
    $root.Current.ProcessId -ne {binding.snapshot.pid} -or
    $process.StartTime.ToUniversalTime().ToFileTimeUtc() -ne {binding.snapshot.process_start_time_100ns}
) {{
    throw 'The bound UI Automation process identity changed.'
}}

$wanted = {_powershell_string(wanted)}

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
    {_native_guard_script(binding, authorization, input_authorization)}
    $pattern.SetValue(
        $desired
    )
}}
catch {{
    [ordered]@{{
        ok = $false
        reason = 'ValuePattern.SetValue failed.'
    }} | ConvertTo-Json -Compress

    exit 0
}}

Start-Sleep -Milliseconds 120

[ordered]@{{
    ok = $true
}} | ConvertTo-Json -Compress
"""

    _require_mutation(
        handle, wanted, binding=binding, authorization=authorization,
        input_authorization=input_authorization, binder=binder,
        require_foreground=True,
    )

    ok, raw = _run_mutation_script(
        script,
        timeout=timeout,
    )

    if not ok:
        raise AccessibilityUnavailable(
            raw
        )

    _require_mutation(
        handle, wanted, binding=binding, authorization=authorization,
        input_authorization=input_authorization, binder=binder,
        require_foreground=True,
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
    binding: WindowBinding,
    authorization: BoundGuiAuthorization,
    binder: WindowBinder,
    input_authorization: BoundInputAuthorization | None = None,
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

    wanted_literal = _powershell_string(wanted)

    _require_mutation(
        handle, wanted, binding=binding, authorization=authorization,
        input_authorization=input_authorization, binder=binder,
        require_foreground=False,
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

$process = [System.Diagnostics.Process]::GetProcessById({binding.snapshot.pid})
if (
    $root.Current.ProcessId -ne {binding.snapshot.pid} -or
    $process.StartTime.ToUniversalTime().ToFileTimeUtc() -ne {binding.snapshot.process_start_time_100ns}
) {{
    throw 'The bound UI Automation process identity changed.'
}}

$wanted = {wanted_literal}

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

    {_native_guard_script(binding, authorization, input_authorization)}
    $pattern.Invoke()
}}
catch {{

    [ordered]@{{
        ok = $false
        reason = 'InvokePattern.Invoke failed.'
    }} | ConvertTo-Json -Compress

    exit 0
}}

[ordered]@{{
    ok = $true
    runtime_id = $wanted
}} | ConvertTo-Json -Compress
"""

    _require_mutation(
        handle, wanted, binding=binding, authorization=authorization,
        input_authorization=input_authorization, binder=binder,
        require_foreground=False,
    )

    ok, raw = _run_mutation_script(
        script,
        timeout=timeout,
    )

    if not ok:
        raise AccessibilityUnavailable(
            raw
        )

    _require_mutation(
        handle, wanted, binding=binding, authorization=authorization,
        input_authorization=input_authorization, binder=binder,
        require_foreground=False,
    )

    payload = json.loads(raw)

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
