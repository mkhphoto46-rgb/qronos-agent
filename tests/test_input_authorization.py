from __future__ import annotations

import pytest

from core.actions import ActionRequest
from core.input_safety import (
    BoundInputAuthorization,
    BoundInputGuard,
    InputAuthorizationMismatch,
    InputPermissionRequired,
)
from core.window_binding import WindowBinding, WindowRect, WindowSnapshot
from security.gate import ActionGrantUnavailable, evaluate, issue_grant
from security.permissions import ActionCategory


def _binding(*, app_id: str = "paint", layout_token: str = "layout") -> WindowBinding:
    snapshot = WindowSnapshot(
        hwnd=101,
        pid=202,
        thread_id=303,
        title="Paint",
        process_path=r"C:\Windows\mspaint.exe",
        process_start_time_100ns=404,
        window_rect=WindowRect(90, 90, 510, 410),
        client_rect=WindowRect(100, 100, 500, 400),
        dpi=96,
        visible=True,
        minimized=False,
        foreground=True,
    )
    return WindowBinding(
        app_id=app_id,
        display_name="Paint",
        snapshot=snapshot,
        identity_token="identity",
        layout_token=layout_token,
    )


def _grant(target: str = "app:paint", ttl_seconds: float = 180.0):
    request = ActionRequest(
        category=ActionCategory.GUI_AUTOMATION,
        target=target,
        summary="test GUI authorization",
    )
    verdict = evaluate(request)
    return request, verdict, issue_grant(
        request,
        verdict,
        approval=verdict.decision if verdict.needs_approval else None,
        ttl_seconds=ttl_seconds,
    )


def test_gui_grant_requires_the_exact_gate_approval():
    request = ActionRequest(
        category=ActionCategory.GUI_AUTOMATION,
        target="app:paint",
        summary="test GUI authorization",
    )
    verdict = evaluate(request)
    assert verdict.needs_approval
    with pytest.raises(ActionGrantUnavailable):
        issue_grant(request, verdict, approval=None)


def test_grant_is_bound_to_exact_action_window_layout_and_scope():
    request, _verdict, grant = _grant()
    binding = _binding()
    auth = BoundInputAuthorization.bind(grant, binding)
    assert auth.action_id == request.action_id
    assert auth.hwnd == binding.hwnd
    assert auth.identity_token == binding.identity_token
    assert auth.layout_token == binding.layout_token
    assert auth.allowed_rect == binding.snapshot.client_rect


def test_wrong_app_target_is_rejected_before_input():
    _request, _verdict, grant = _grant(target="app:notepad")
    with pytest.raises(InputAuthorizationMismatch):
        BoundInputAuthorization.bind(grant, _binding())


def test_layout_change_invalidates_authorization_before_binder_call():
    _request, _verdict, grant = _grant()
    original = _binding()
    auth = BoundInputAuthorization.bind(grant, original)
    changed = _binding(layout_token="new-layout")

    class Binder:
        calls = 0
        def revalidate(self, *args, **kwargs):
            self.calls += 1
            return changed.snapshot

    binder = Binder()
    guard = BoundInputGuard(binder)  # type: ignore[arg-type]
    with pytest.raises(InputAuthorizationMismatch):
        guard.revalidate(changed, authorization=auth)
    assert binder.calls == 0


def test_scope_can_only_be_narrowed():
    _request, _verdict, grant = _grant()
    binding = _binding()
    auth = BoundInputAuthorization.bind(grant, binding)
    narrow = WindowRect(150, 150, 300, 300)
    restricted = auth.restrict(narrow)
    assert restricted.allowed_rect == narrow
    with pytest.raises(InputAuthorizationMismatch):
        restricted.restrict(WindowRect(100, 100, 500, 400))


def test_expired_authorization_is_rejected_before_binder_call(monkeypatch):
    _request, _verdict, grant = _grant()
    binding = _binding()
    auth = BoundInputAuthorization.bind(grant, binding)

    class Binder:
        calls = 0
        def revalidate(self, *args, **kwargs):
            self.calls += 1
            return binding.snapshot

    binder = Binder()
    monkeypatch.setattr("core.input_safety.time.monotonic", lambda: auth.expires_at + 1.0)
    guard = BoundInputGuard(binder)  # type: ignore[arg-type]
    with pytest.raises(InputPermissionRequired):
        guard.revalidate(binding, authorization=auth)
    assert binder.calls == 0


def test_missing_authorization_is_rejected_before_binder_call():
    binding = _binding()

    class Binder:
        calls = 0
        def revalidate(self, *args, **kwargs):
            self.calls += 1
            return binding.snapshot

    binder = Binder()
    guard = BoundInputGuard(binder)  # type: ignore[arg-type]
    with pytest.raises(InputPermissionRequired):
        guard.require_authorization(None, binding)
    assert binder.calls == 0
