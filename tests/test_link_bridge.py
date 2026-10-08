"""The Device Link inside the real runtime bridge, on disposable state.

The runtime is the real QronosRuntime with an injected stand-in orchestrator
whose planner raises; the link is the real LinkRuntime on loopback port 0,
with its device, audit and relay files under a temporary data root. A paired
"phone" is the Link's own LinkClient over loopback. No model, microphone,
speaker, real phone or network beyond loopback.
"""

from __future__ import annotations

import io
import json
import math
from dataclasses import replace

import pytest

import core.runtime_bridge as runtime_bridge
from core.action_audit import ActionAuditLog
from core.link_client import LinkClient, LinkClientError
from core.runtime_bridge import QronosRuntime, handle_action
from tests.fixtures.link_harness import CLIENT_TIMEOUT


class Forbidden(AssertionError):
    pass


class StandInOrchestrator:
    """The orchestrator's chat-only Fast call (Link contract L4); everything else raises."""

    def __init__(self) -> None:
        self.prompts: list[list] = []
        self.refuse_admission = False

    def answer_chat_only(self, messages) -> str:
        if self.refuse_admission:
            raise RuntimeError("resources refused")
        self.prompts.append(list(messages))
        return "پاسخ کوتاه"

    def answer_web_prompt(self, prompt: str) -> str:
        raise Forbidden("a phone question used the web-research profile")

    def execute_plan(self, *args, **kwargs):
        raise Forbidden("a phone question reached the planner")


class StandInRouter:
    def route(self, *args, **kwargs):
        raise Forbidden("a phone question reached the task router")


NONCE = "n" * 24


class Bridge:
    def __init__(self, tmp_path, monkeypatch) -> None:
        self.root = tmp_path / "qronos-data"
        self.logs = tmp_path / "qronos-logs"
        monkeypatch.setattr(runtime_bridge, "CONFIG", replace(
            runtime_bridge.CONFIG,
            paths=replace(runtime_bridge.CONFIG.paths, data=self.root, logs=self.logs),
        ))
        real_settings = QronosRuntime._link_runtime_settings

        def loopback(runtime):
            # Only where it listens changes: loopback, any free port.
            return replace(real_settings(runtime), port=0, bind_host="127.0.0.1", pairing_host="127.0.0.1")

        monkeypatch.setattr(QronosRuntime, "_link_runtime_settings", loopback)
        self.events: list[tuple[str, str]] = []
        self.runtime = QronosRuntime(notify=lambda event, status, message: self.events.append((event, message)))
        self.runtime.action_audit = ActionAuditLog(path=None)
        self.runtime.orchestrator = StandInOrchestrator()
        self.runtime.task_router = StandInRouter()

        def no_wake(*args, **kwargs):
            raise Forbidden("the link needed the microphone or wake model")

        monkeypatch.setattr(self.runtime, "prepare_wake_capture", no_wake)
        monkeypatch.setattr(self.runtime, "prepare", no_wake)
        self.clients: list[LinkClient] = []

    def enable(self, value: bool = True) -> None:
        self.runtime.settings.set("link.enabled", value)

    def action(self, action_id: str, data=None) -> None:
        payload = {"command": "action", "actionId": action_id}
        if data is not None:
            payload["data"] = data
        handle_action(self.runtime, payload)

    def last(self, event: str):
        found = [message for name, message in self.events if name == event]
        return json.loads(found[-1]) if found else None

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def pair(self) -> tuple[LinkClient, str]:
        self.action("qronos.link_pair_open", {"requestNonce": NONCE, "name": "گوشی سارا"})
        pairing = self.last("link_pairing")
        assert pairing["requestNonce"] == NONCE
        client = LinkClient.from_uri(pairing["uri"], timeout=CLIENT_TIMEOUT)
        client.connect()
        self.clients.append(client)
        assert client.call("ping").ok
        return client, client.device_id

    def close(self) -> None:
        for client in self.clients:
            client.close()
        self.runtime.stop_device_link()


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    harness = Bridge(tmp_path, monkeypatch)
    yield harness
    harness.close()


def _files(root):
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


# ------------------------------------------------------------ lifecycle


def test_a_disabled_link_builds_nothing_at_startup(bridge):
    assert bridge.runtime.start_device_link() is False
    assert getattr(bridge.runtime, "_link_runtime", None) is None
    assert not [name for name in bridge.names() if name.startswith("link_")]
    assert _files(bridge.root) == [] and _files(bridge.logs) == []


def test_an_enabled_link_starts_in_text_only_mode_without_the_microphone(bridge):
    bridge.enable()
    assert bridge.runtime.start_device_link() is True
    status = bridge.last("link_status")
    assert status["enabled"] and status["running"] and status["port"] > 0
    # Remote access stays off until the WAN gate is accepted.
    assert status["remote_active"] is False and status["remote_unavailable"] == "remote_access_off"
    assert bridge.last("link_devices") == []


def test_a_phone_question_reaches_only_the_chat_only_fast_call(bridge):
    bridge.enable()
    bridge.runtime.start_device_link()
    client, _ = bridge.pair()
    response = client.call("ask", question="امروز چه روزی است؟")
    assert response.result["answer"] == "پاسخ کوتاه"
    assert response.result["performed_actions"] == []
    [messages] = bridge.runtime.orchestrator.prompts
    assert any("امروز چه روزی است؟" in message.content for message in messages)


def test_a_phone_cannot_ask_for_an_action_or_have_one_queued(bridge):
    """Open/delete/print requests from a phone are answered, never executed or queued."""
    bridge.enable()
    bridge.runtime.start_device_link()
    client, _ = bridge.pair()
    for question in ("فایل report.pdf را چاپ کن", "delete notes.txt", "open https://example.org"):
        response = client.call("ask", question=question)
        assert response.result["performed_actions"] == []
    assert not [name for name in bridge.names() if name.endswith("approval_required")]
    assert getattr(bridge.runtime, "_app_workflows", None) is None
    # A restart does not replay anything a phone asked for.
    asked = len(bridge.runtime.orchestrator.prompts)
    bridge.runtime.stop_device_link()
    bridge.runtime.start_device_link()
    assert len(bridge.runtime.orchestrator.prompts) == asked
    assert not [name for name in bridge.names() if name.endswith("approval_required")]


def test_a_refused_brain_admission_is_answered_busy(bridge):
    bridge.enable()
    bridge.runtime.start_device_link()
    client, _ = bridge.pair()
    bridge.runtime.orchestrator.refuse_admission = True
    refused = client.call("ask", question="سلام")
    assert not refused.ok and refused.code == "busy"


def test_a_busy_or_paused_runtime_refuses_phone_questions_instead_of_queueing(bridge, monkeypatch):
    bridge.enable()
    bridge.runtime.start_device_link()
    client, _ = bridge.pair()
    monkeypatch.setattr(QronosRuntime, "is_busy", property(lambda self: True))
    busy = client.call("ask", question="سلام")
    assert not busy.ok and busy.code == "busy"
    monkeypatch.setattr(QronosRuntime, "is_busy", property(lambda self: False))
    monkeypatch.setattr(QronosRuntime, "runtime_paused", property(lambda self: True))
    paused = client.call("ask", question="سلام")
    assert not paused.ok and paused.code == "busy"
    assert bridge.runtime.orchestrator.prompts == []


def test_the_pairing_uri_goes_only_to_its_request_and_is_never_written(bridge):
    bridge.enable()
    bridge.runtime.start_device_link()
    client, device_id = bridge.pair()
    uri = bridge.last("link_pairing")["uri"]
    others = [message for name, message in bridge.events if name != "link_pairing"]
    assert not any(uri in message for message in others)
    secret = client.secret.hex()
    for path in _files(bridge.root) + _files(bridge.logs):
        content = path.read_bytes()
        assert uri.encode() not in content, path.name
        if path.name != "link_devices.json":  # the registry is the key's one home
            assert secret.encode() not in content, path.name
    devices = bridge.last("link_devices")
    assert [d["device_id"] for d in devices] == [device_id]
    assert "secret" not in json.dumps(devices)


def test_revoking_from_the_desktop_ends_the_phone_session_at_once(bridge):
    bridge.enable()
    bridge.runtime.start_device_link()
    client, device_id = bridge.pair()
    bridge.action("qronos.link_revoke", {"deviceId": device_id})
    with pytest.raises(LinkClientError):
        client.call("ping")
    assert bridge.last("link_devices")[0]["status"] == "revoked"


def test_turning_the_link_off_in_settings_ends_sessions_and_an_unrelated_change_does_not(bridge):
    bridge.enable()
    bridge.runtime.start_device_link()
    client, _ = bridge.pair()
    server = bridge.runtime._link_runtime.server
    bridge.runtime.settings.set("appearance.ui_scale_percent", 110)
    bridge.runtime.apply_device_link_settings()
    assert bridge.runtime._link_runtime.server is server
    assert client.call("ping").ok
    bridge.enable(False)
    bridge.runtime.apply_device_link_settings()
    assert bridge.runtime._link_runtime.running is False
    with pytest.raises(LinkClientError):
        client.call("ping")
    assert bridge.last("link_status")["running"] is False


def test_turning_the_link_on_later_starts_it(bridge):
    bridge.runtime.apply_device_link_settings()
    assert getattr(bridge.runtime, "_link_runtime", None) is None
    bridge.enable()
    bridge.runtime.apply_device_link_settings()
    assert bridge.last("link_status")["running"] is True


def test_shutdown_stops_the_link_first(bridge):
    bridge.enable()
    bridge.runtime.start_device_link()
    client, _ = bridge.pair()
    bridge.runtime.close()
    assert bridge.runtime._link_runtime.running is False
    with pytest.raises(LinkClientError):
        client.call("ping")


def test_a_link_that_cannot_start_is_reported_and_the_runtime_carries_on(bridge, monkeypatch):
    from core.link_runtime import LinkRuntime

    def broken(self):
        raise OSError("address in use")

    monkeypatch.setattr(LinkRuntime, "start", broken)
    bridge.enable()
    assert bridge.runtime.start_device_link() is False
    assert bridge.last("link_error") == {"action": "start", "code": "failed", "detail": "OSError"}


def test_the_bridge_starts_the_link_after_runtime_ready_without_preparing_voice(monkeypatch):
    calls: list[str] = []

    class FakeRuntime:
        wake_word_enabled = False

        def start_device_link(self):
            calls.append("start_device_link")
            return False

        def close(self):
            calls.append("close")

    monkeypatch.setattr(runtime_bridge, "QronosRuntime", FakeRuntime)
    monkeypatch.setattr(runtime_bridge, "emit", lambda event, status, message: calls.append(event))
    monkeypatch.setattr(runtime_bridge.sys, "stdin", io.StringIO(""))
    assert runtime_bridge.main() == 0
    assert calls == ["runtime_ready", "start_device_link", "close"]


# ------------------------------------------------------------ requests


@pytest.mark.parametrize("action, data", [
    ("qronos.link_pair_open", {"requestNonce": "short", "name": "x"}),
    ("qronos.link_pair_open", {"requestNonce": NONCE, "name": "x" * 41}),
    ("qronos.link_pair_open", {"requestNonce": NONCE, "name": "bad\u0007name"}),
    ("qronos.link_pair_open", {"requestNonce": NONCE, "name": "evil" + chr(0x202E) + "gpj.exe"}),
    ("qronos.link_pair_open", {"requestNonce": NONCE, "name": "two" + chr(0x2028) + "lines"}),
    ("qronos.link_pair_open", {"requestNonce": NONCE, "name": 5}),
    ("qronos.link_pair_open", {"requestNonce": NONCE, "extra": True}),
    ("qronos.link_revoke", {"deviceId": "ABCDEF0123456789"}),
    ("qronos.link_revoke", {"deviceId": "0123"}),
    ("qronos.link_revoke", {"deviceId": 12345}),
    ("qronos.link_revoke", {}),
    ("qronos.link_remote", {"deviceId": "0123456789abcdef", "enabled": "false"}),
    ("qronos.link_remote", {"deviceId": "0123456789abcdef", "enabled": 0}),
    ("qronos.link_remote", {"deviceId": "0123456789abcdef", "enabled": False, "days": math.nan}),
    ("qronos.link_remote", {"deviceId": "0123456789abcdef", "enabled": False, "days": 30}),
    ("qronos.link_status", {"anything": 1}),
    ("qronos.link_devices", ["not", "an", "object"]),
])
def test_malformed_requests_are_refused_before_anything_happens(bridge, action, data):
    bridge.enable()
    bridge.runtime.start_device_link()
    bridge.pair()
    bridge.action("qronos.link_devices", {})
    before = bridge.last("link_devices")
    bridge.events.clear()
    bridge.action(action, data)
    assert bridge.names() == ["link_error"]
    assert bridge.last("link_error") == {"action": action, "code": "invalid_request"}
    bridge.action("qronos.link_devices", {})
    assert bridge.last("link_devices") == before


def test_a_persian_name_with_a_zero_width_non_joiner_is_accepted(bridge):
    bridge.enable()
    bridge.runtime.start_device_link()
    name = "گوشی" + chr(0x200C) + "ها"
    bridge.action("qronos.link_pair_open", {"requestNonce": NONCE, "name": name})
    assert bridge.last("link_error") is None
    assert bridge.last("link_devices")[0]["name"] == name


def test_remote_access_cannot_be_switched_on_or_a_relay_set_before_the_wan_gate(bridge, tmp_path):
    bridge.enable()
    bridge.runtime.start_device_link()
    _, device_id = bridge.pair()
    bridge.action("qronos.link_remote", {"deviceId": device_id, "enabled": True})
    assert bridge.last("link_error") == {"action": "qronos.link_remote", "code": "remote_unavailable"}
    bridge.action("qronos.link_relay_set", {"host": "relay.example", "port": 443})
    assert bridge.last("link_error") == {"action": "qronos.link_relay_set", "code": "remote_unavailable"}
    assert not (bridge.root / "link_relay.json").exists()
    bridge.action("qronos.link_remote", {"deviceId": device_id, "enabled": False})
    assert bridge.last("link_devices")[0]["remote_enabled"] is False


def test_pairing_needs_a_running_link_and_unknown_devices_are_typed(bridge):
    bridge.action("qronos.link_pair_open", {"requestNonce": NONCE, "name": "x"})
    assert bridge.last("link_error") == {"action": "qronos.link_pair_open", "code": "not_running"}
    assert bridge.last("link_pairing") is None
    bridge.action("qronos.link_revoke", {"deviceId": "0123456789abcdef"})
    assert bridge.last("link_error") == {"action": "qronos.link_revoke", "code": "unknown_device"}


def test_status_and_devices_are_available_with_the_link_off(bridge):
    bridge.action("qronos.link_status", {})
    assert bridge.last("link_status")["running"] is False
    assert bridge.last("link_devices") == []
