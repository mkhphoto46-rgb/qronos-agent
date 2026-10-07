from dataclasses import replace
import base64
import json
import os
import subprocess
from unittest.mock import Mock

import pytest

from core.actions import ActionRequest
from core.gui_authorization import BoundGuiAuthorization, GuiAuthorizationMismatch, GuiPermissionRequired
from core.input_safety import BoundInputAuthorization, InputPermissionRequired
from core.window_binding import WindowBinder, WindowRect, WindowSnapshot, WindowBindingStale
from core.windows_accessibility import AccessibilityUnavailable, ActionableUiElement
from core import windows_accessibility_mutation as native
from security.gate import ActionGrantUnavailable, evaluate, issue_grant
from security.permissions import ActionCategory


@pytest.fixture
def context(monkeypatch):
    snapshot = WindowSnapshot(
        hwnd=77, pid=500, thread_id=8, title="Example",
        process_path=r"C:\Example\example.exe", process_start_time_100ns=123456,
        window_rect=WindowRect(100, 50, 1100, 750),
        client_rect=WindowRect(108, 80, 1092, 742), dpi=96,
        visible=True, minimized=False, foreground=True,
    )
    state = {"snapshot": snapshot}
    monkeypatch.setattr("core.window_binding.window_is_foreground", lambda hwnd: False)
    binder = WindowBinder(
        enumerate_fn=lambda: (state["snapshot"],),
        snapshot_fn=lambda hwnd: state["snapshot"],
    )
    binding = binder.bind_snapshot(snapshot, app_id="example", display_name="Example")
    request = ActionRequest(
        category=ActionCategory.GUI_AUTOMATION, target="app:example", summary="UIA test",
    )
    verdict = evaluate(request)
    grant = issue_grant(request, verdict, approval=verdict.decision)
    authorization = BoundGuiAuthorization.bind(grant, binding, allowed_runtime_ids=("42.1",))
    input_authorization = BoundInputAuthorization.bind(grant, binding)
    runner = Mock(return_value=(True, json.dumps({"ok": True, "runtime_id": "42.1"})))
    monkeypatch.setattr(native, "_run_uia_script", runner)
    return state, binding, binder, authorization, input_authorization, runner, grant


def call_native(operation, context, *, hwnd=None, runtime_id="42.1", authorization=None, missing=False):
    _state, binding, binder, auth, input_auth, _runner, _grant = context
    kwargs = {
        "binding": binding, "binder": binder,
        "authorization": None if missing else (authorization or auth),
        "input_authorization": input_auth,
    }
    arguments = (binding.hwnd if hwnd is None else hwnd, runtime_id)
    if operation == "set_value_runtime_id":
        arguments += ("private rollback text",)
    return getattr(native, operation)(*arguments, **kwargs)


MUTATIONS = ("focus_runtime_id", "set_value_runtime_id", "invoke_runtime_id")


@pytest.mark.parametrize("operation", MUTATIONS)
def test_direct_native_mutation_without_authorization_is_refused(operation, context):
    with pytest.raises(GuiPermissionRequired):
        call_native(operation, context, missing=True)
    context[5].assert_not_called()


@pytest.mark.parametrize("operation", MUTATIONS)
def test_native_mutation_rejects_unreviewed_element(operation, context):
    with pytest.raises(GuiAuthorizationMismatch, match="RuntimeId"):
        call_native(operation, context, runtime_id="42.2")
    context[5].assert_not_called()


@pytest.mark.parametrize("operation", MUTATIONS)
def test_native_mutation_rejects_wrong_hwnd(operation, context):
    with pytest.raises(GuiAuthorizationMismatch, match="HWND"):
        call_native(operation, context, hwnd=78)
    context[5].assert_not_called()


@pytest.mark.parametrize("operation", MUTATIONS)
def test_native_mutation_rejects_expired_grant(operation, context, monkeypatch):
    monkeypatch.setattr("core.gui_authorization.time.monotonic", lambda: context[3].expires_at + 1)
    with pytest.raises(GuiPermissionRequired, match="expired"):
        call_native(operation, context)
    context[5].assert_not_called()


@pytest.mark.parametrize("operation", MUTATIONS)
@pytest.mark.parametrize("field", ("pid", "process_start_time_100ns"))
def test_native_mutation_refuses_reused_window_process(operation, field, context):
    state = context[0]
    state["snapshot"] = replace(state["snapshot"], **{field: getattr(state["snapshot"], field) + 1})
    with pytest.raises(WindowBindingStale, match="different process"):
        call_native(operation, context)
    context[5].assert_not_called()


@pytest.mark.parametrize("operation", MUTATIONS)
def test_grant_expiring_during_revalidation_cannot_reach_native_worker(operation, context, monkeypatch):
    binder = context[2]
    original = binder.revalidate

    def revalidate(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr("core.gui_authorization.time.monotonic", lambda: context[3].expires_at + 1)
        return result

    monkeypatch.setattr(binder, "revalidate", revalidate)
    with pytest.raises(GuiPermissionRequired, match="expired"):
        call_native(operation, context)
    context[5].assert_not_called()


@pytest.mark.parametrize("operation", MUTATIONS[:2])
@pytest.mark.parametrize("change", ("foreground", "layout"))
def test_focus_and_value_require_current_foreground_and_layout(operation, change, context):
    state = context[0]
    changes = {"foreground": False} if change == "foreground" else {"dpi": 144}
    state["snapshot"] = replace(state["snapshot"], **changes)
    with pytest.raises(WindowBindingStale):
        call_native(operation, context)
    context[5].assert_not_called()


@pytest.mark.parametrize("operation", MUTATIONS[:2])
def test_focus_and_value_require_input_authorization_too(operation, context):
    context = (*context[:4], None, *context[5:])
    with pytest.raises(InputPermissionRequired):
        call_native(operation, context)
    context[5].assert_not_called()


def test_semantic_invoke_survives_layout_and_foreground_changes(context):
    state = context[0]
    state["snapshot"] = replace(state["snapshot"], foreground=False, dpi=144)
    receipt = call_native("invoke_runtime_id", context)
    assert receipt.runtime_id == "42.1"
    context[5].assert_called_once()
    script = context[5].call_args.args[0]
    # The subprocess rechecks expiry/process ownership just before the primitive.
    prefix = script[:script.index("$pattern.Invoke()")]
    assert "ToUnixTimeMilliseconds()" in prefix
    assert prefix.count("$root.Current.ProcessId -ne 500") == 2
    assert "ToFileTimeUtc() -ne 123456" in prefix
    assert "QronosBoundWindowReader" not in script


@pytest.mark.parametrize("operation", MUTATIONS)
@pytest.mark.parametrize("raw", ("raw secret from a failed command", '{"ok":false,"reason":"private data"}', "not json"))
def test_mutation_errors_do_not_expose_worker_output(operation, raw, context):
    context[5].return_value = (raw != "raw secret from a failed command", raw)
    if operation == "focus_runtime_id":
        result = call_native(operation, context)
        assert not result.ok
        error = result.reason
    else:
        with pytest.raises(AccessibilityUnavailable) as caught:
            call_native(operation, context)
        error = str(caught.value)
    assert error == "Windows UI Automation mutation could not complete."


def test_gate_denial_cannot_issue_native_gui_capability(context):
    request = ActionRequest(category=ActionCategory.GUI_AUTOMATION, target="app:example", summary="denied")
    verdict = evaluate(request)
    assert verdict.needs_approval
    with pytest.raises(ActionGrantUnavailable):
        issue_grant(request, verdict)
    context[5].assert_not_called()


def test_runtime_id_is_a_literal_and_rollback_text_is_encoded(context, monkeypatch):
    _state, binding, _binder, _auth, _input, runner, grant = context
    wanted = "42.'; $(throw 'injected'); $env:SECRET"
    authorization = BoundGuiAuthorization.bind(grant, binding, allowed_runtime_ids=(wanted,))
    element = ActionableUiElement(
        runtime_id=wanted, name="Edit", automation_id="Edit", class_name="Edit",
        control_type="ControlType.Edit", value="private rollback text", enabled=True,
        focusable=True, focused=True, value_pattern=True, value_read_only=False,
        text_pattern=True, invoke_pattern=False, expand_pattern=False,
        left=200, top=100, width=300, height=30,
    )
    monkeypatch.setattr(native, "actionable_element_by_runtime_id", lambda *args, **kwargs: element)
    call_native("set_value_runtime_id", context, runtime_id=wanted, authorization=authorization)
    script = runner.call_args.args[0]
    assert "$wanted = '42.''; $(throw ''injected''); $env:SECRET'" in script
    assert "private rollback text" not in script
    assert base64.b64encode(b"private rollback text").decode() in script


@pytest.mark.parametrize("operation", MUTATIONS)
@pytest.mark.parametrize("field,value", (("app_id", "other"), ("identity_token", "other-process")))
def test_native_mutation_rejects_foreign_binding(operation, field, value, context):
    changed = (*context[:1], replace(context[1], **{field: value}), *context[2:])
    with pytest.raises(GuiAuthorizationMismatch):
        call_native(operation, changed)
    context[5].assert_not_called()


@pytest.mark.parametrize("operation", MUTATIONS[:2])
def test_uia_and_input_capabilities_cannot_be_spliced_from_different_actions(operation, context):
    request = ActionRequest(category=ActionCategory.GUI_AUTOMATION, target="app:example", summary="other action")
    verdict = evaluate(request)
    grant = issue_grant(request, verdict, approval=verdict.decision)
    other_input = BoundInputAuthorization.bind(grant, context[1])
    changed = (*context[:4], other_input, *context[5:])
    with pytest.raises(GuiAuthorizationMismatch, match="different actions"):
        call_native(operation, changed)
    context[5].assert_not_called()


@pytest.mark.parametrize("operation", MUTATIONS)
@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell parser is platform-specific")
def test_generated_native_scripts_parse_without_executing_uia(operation, context, monkeypatch):
    element = Mock(runtime_id="42.1", value="private rollback text")
    monkeypatch.setattr(native, "actionable_element_by_runtime_id", lambda *args, **kwargs: element)
    call_native(operation, context)
    source = context[5].call_args.args[0]
    encoded = base64.b64encode(source.encode()).decode()
    # ParseInput only constructs an AST. It does not load UIA or run the script.
    parser = (
        "$qronosSource = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "')); "
        "$qronosTokens = $null; $qronosErrors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseInput("
        "$qronosSource, [ref]$qronosTokens, [ref]$qronosErrors) > $null; "
        "if ($qronosErrors.Count -gt 0) { $qronosErrors | ForEach-Object { $_.Message }; exit 1 }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", parser],
        capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("operation,primitive", (
    ("focus_runtime_id", "$target.SetFocus()"),
    ("set_value_runtime_id", "$pattern.SetValue("),
))
def test_worker_rechecks_foreground_and_full_layout_after_enumeration(operation, primitive, context, monkeypatch):
    monkeypatch.setattr(native, "actionable_element_by_runtime_id", lambda *args, **kwargs: Mock(runtime_id="42.1", value="private rollback text"))
    call_native(operation, context)
    script = context[5].call_args.args[0]
    guard_start = script.rindex("$qronosHwnd =")
    action = script.index(primitive)
    assert script.index("$root.FindAll(") < guard_start < action
    guard = script[guard_start:action]
    for check in (
        "::GetForegroundWindow()", "::GetAncestor($qronosHwnd, 2)",
        "::IsWindowVisible($qronosHwnd)", "::IsIconic($qronosHwnd)",
        "::GetWindowRect(", "::GetClientRect(", "::ClientToScreen(",
        "::GetDpiForWindow($qronosHwnd) -ne 96",
        "$qronosOuter.Left -ne 100", "$qronosOuter.Top -ne 50",
        "$qronosOuter.Right -ne 1100", "$qronosOuter.Bottom -ne 750",
        "$qronosTopLeft.X -ne 108", "$qronosTopLeft.Y -ne 80",
        "$qronosBottomRight.X -ne 1092", "$qronosBottomRight.Y -ne 742",
    ):
        assert check in guard


@pytest.mark.skipif(os.name != "nt", reason="Executes only synthetic observations through the Windows PowerShell guard")
@pytest.mark.parametrize("changed_state,accepted", (
    ("", True),
    ("[QronosBoundWindowReader]::Hosted = $true; [QronosBoundWindowReader]::Foreground = 88", True),
    ("[QronosBoundWindowReader]::Foreground = 99", False),
    ("[QronosBoundWindowReader]::Foreground = 0", False),
    ("[QronosBoundWindowReader]::MoveX = 1", False),
    ("[QronosBoundWindowReader]::ExtraWidth = 1", False),
    ("[QronosBoundWindowReader]::ClientShift = 1", False),
    ("[QronosBoundWindowReader]::ClientExtra = 1", False),
    ("[QronosBoundWindowReader]::Dpi = 144", False),
    ("[QronosBoundWindowReader]::Visible = $false", False),
    ("[QronosBoundWindowReader]::Minimized = $true", False),
    ("[QronosBoundWindowReader]::GeometryOk = $false", False),
))
def test_worker_layout_guard_blocks_changes_during_setup_without_real_gui(changed_state, accepted, context):
    # These methods return synthetic observations. No user32 APIs or UIA
    # operations are loaded or executed by this behavior test.
    fake_reader = r"""
Add-Type -TypeDefinition @'
using System;
public static class QronosBoundWindowReader {
    public struct Rect { public int Left, Top, Right, Bottom; }
    public struct Point { public int X, Y; }
    public static long Foreground = 77;
    public static bool Hosted = false, Visible = true, Minimized = false, GeometryOk = true;
    public static int MoveX = 0, ExtraWidth = 0, ClientShift = 0, ClientExtra = 0;
    public static uint Dpi = 96;
    public static IntPtr GetForegroundWindow() { return new IntPtr(Foreground); }
    public static IntPtr GetAncestor(IntPtr hwnd, uint flags) {
        return Hosted && (hwnd.ToInt64() == 77 || hwnd.ToInt64() == 88) ? new IntPtr(88) : hwnd;
    }
    public static bool GetWindowRect(IntPtr hwnd, out Rect rect) {
        rect = new Rect { Left = 100 + MoveX, Top = 50, Right = 1100 + MoveX + ExtraWidth, Bottom = 750 };
        return GeometryOk;
    }
    public static bool GetClientRect(IntPtr hwnd, out Rect rect) {
        rect = new Rect { Left = 0, Top = 0, Right = 984 + ClientExtra, Bottom = 662 };
        return true;
    }
    public static bool ClientToScreen(IntPtr hwnd, ref Point point) {
        point.X += 108 + ClientShift; point.Y += 80; return true;
    }
    public static uint GetDpiForWindow(IntPtr hwnd) { return Dpi; }
    public static bool IsWindowVisible(IntPtr hwnd) { return Visible; }
    public static bool IsIconic(IntPtr hwnd) { return Minimized; }
}
'@
"""
    script = (
        "$ErrorActionPreference = 'Stop'\n" + fake_reader + "\n" + changed_state + "\ntry {\n"
        + native._native_layout_guard_script(context[1])
        + "\nWrite-Output 'synthetic-mutation-reached'\n} catch { Write-Output 'blocked' }"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        # Cold PowerShell/Add-Type startup can exceed 10 seconds on a busy
        # Windows CI runner. This budget belongs only to this synthetic test;
        # native action deadlines remain unchanged.
        capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("synthetic-mutation-reached" if accepted else "blocked")


@pytest.mark.skipif(os.name != "nt", reason="Compiles only the Windows worker's native reader declarations")
def test_native_window_reader_declarations_compile_without_querying_windows():
    encoded = base64.b64encode(native._WINDOW_STATE_READER_SCRIPT.encode("utf-16-le")).decode()
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr
