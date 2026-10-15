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


def test_remote_access_stays_refused_while_the_explicit_remote_setting_is_off(bridge, tmp_path):
    # The explicit, default-off link.remote_access_enabled replaces the hardcoded gate; the refusals stay.
    bridge.enable()
    bridge.runtime.start_device_link()
    _, device_id = bridge.pair()
    assert bridge.runtime.settings.get("link.remote_access_enabled") is False
    bridge.action("qronos.link_remote", {"deviceId": device_id, "enabled": True})  # no days, no nonce
    assert bridge.last("link_error") == {"action": "qronos.link_remote", "code": "invalid_request"}
    bridge.action("qronos.link_remote", {"deviceId": device_id, "enabled": True, "days": 30, "requestNonce": NONCE})
    assert bridge.last("link_error") == {"action": "qronos.link_remote", "code": "remote_access_off"}
    bridge.action("qronos.link_relay_set", {"host": "relay.example", "port": 443})  # no nonce: not a WAN request
    assert bridge.last("link_error") == {"action": "qronos.link_relay_set", "code": "invalid_request"}
    assert not (bridge.root / "link_relay.json").exists()
    assert bridge.last("link_devices")[0]["remote_enabled"] is False
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


# ------------------------------------------------------------ security monitor (Link D9)


def _latch(bridge, **state) -> None:
    payload = {"version": 1, "contained": False, "contained_reason": "", "quarantined": False,
               "quarantine_reason": "", "contain_history": []}
    payload.update(state)
    bridge.root.mkdir(parents=True, exist_ok=True)
    (bridge.root / "link_security.json").write_text(json.dumps(payload), encoding="utf-8")


def test_a_quarantined_link_stays_off_even_on_the_lan_until_reset_at_this_pc(bridge):
    _latch(bridge, quarantined=True, quarantine_reason="repeated_containment")
    bridge.enable()
    assert bridge.runtime.start_device_link() is False
    status = bridge.last("link_status")
    assert status["running"] is False
    assert status["security"]["quarantined"] is True
    bridge.action("qronos.link_pair_open", {"requestNonce": NONCE, "name": "x"})
    assert bridge.last("link_error")["code"] == "not_running"

    bridge.action("qronos.link_security_reset", {})
    status = bridge.last("link_status")
    assert status["running"] is True and status["security"]["quarantined"] is False
    client, _ = bridge.pair()
    assert client.call("ask", question="سلام").result["answer"] == "پاسخ کوتاه"


def test_the_security_reset_takes_no_parameters(bridge):
    bridge.action("qronos.link_security_reset", {"by": "phone"})
    assert bridge.last("link_error") == {"action": "qronos.link_security_reset", "code": "invalid_request"}


def test_the_monitor_keeps_its_state_in_this_profile_and_status_reports_it(bridge):
    bridge.enable()
    bridge.runtime.start_device_link()
    security = bridge.last("link_status")["security"]
    assert security["ready"] is True and security["quarantined"] is False
    link = bridge.runtime._link_runtime
    assert link.settings.security_path == bridge.root / "link_security.json"
    assert link.settings.incident_path == bridge.logs / "link_incidents.jsonl"
    # Remote stays off: the WAN path is not enabled from this runtime yet.
    assert bridge.last("link_status")["remote_active"] is False


# ------------------------------------------------------------ WAN wiring (A90; fakes only, no socket/endpoint/keys)


DEVICE = "0123456789abcdef"
REVOKED = "fedcba9876543210"
WAN_NONCE = "w" * 32
RELAY_SET = "qronos.link_relay_set"
REMOTE = "qronos.link_remote"
REVOKE = "qronos.link_revoke"


class FakeMonitor:
    def __init__(self) -> None:
        self.remote_allowed = True
        self.quarantined = False

    def remote_admission(self):
        from types import SimpleNamespace

        return SimpleNamespace(allowed=self.remote_allowed, reason=None if self.remote_allowed else "quarantined")

    def status(self) -> dict:
        return {"quarantined": self.quarantined}


class FakeLink:
    """Stands in for C's LinkRuntime: records every mutation; never dials, listens or keeps keys."""

    running = False

    def __init__(self) -> None:
        self.monitor = FakeMonitor()
        self.relay = None
        self.calls: list[tuple] = []

    def status(self) -> dict:
        return {"running": False, "remote_active": False}

    def devices(self) -> list:
        return []

    def relay_endpoint(self):
        return self.relay

    def set_relay_endpoint(self, endpoint) -> bool:
        self.calls.append(("relay", endpoint))
        self.relay = endpoint
        return False

    def set_remote_access(self, device_id, enabled, days=None):
        from core.link_devices import UnknownDevice

        if device_id not in (DEVICE, REVOKED):
            raise UnknownDevice(device_id)
        if device_id == REVOKED and enabled:
            raise ValueError("revoked device")
        self.calls.append(("remote", device_id, enabled, days))

    def revoke(self, device_id) -> None:
        from core.link_devices import UnknownDevice

        if device_id not in (DEVICE, REVOKED):
            raise UnknownDevice(device_id)
        self.calls.append(("revoke", device_id))

    def stop(self) -> None:
        return None


@pytest.fixture
def wan(bridge, monkeypatch):
    """The real bridge with a fake link and a stand-in for C's relay_endpoint_from_request."""
    import core.link_runtime as link_runtime

    seen: list = []

    def endpoint_from_request(data, data_dir):
        seen.append((dict(data), data_dir))
        if data.get("host") != "relay.example":
            raise ValueError("relay_invalid")
        return ("endpoint", data["host"], data["port"])

    monkeypatch.setattr(link_runtime, "relay_endpoint_from_request", endpoint_from_request, raising=False)
    link = FakeLink()
    bridge.runtime._link_runtime = link
    bridge.link = link
    bridge.helper_calls = seen
    return bridge


def _acks(bridge) -> list[dict]:
    return [json.loads(message) for name, message in bridge.events if name == "link_operation_result"]


def _remote_on(bridge, device=DEVICE, days=30, nonce=WAN_NONCE) -> None:
    bridge.action(REMOTE, {"deviceId": device, "enabled": True, "days": days, "requestNonce": nonce})


@pytest.mark.parametrize("data, code", [
    ({"host": "https://relay.example", "port": 443, "requestNonce": WAN_NONCE}, "relay_invalid"),
    ({"host": "relay.example/path", "port": 443, "requestNonce": WAN_NONCE}, "relay_invalid"),
    ({"host": "relay.example", "port": True, "requestNonce": WAN_NONCE}, "invalid_request"),
    ({"host": "relay.example", "port": "443", "requestNonce": WAN_NONCE}, "invalid_request"),
    ({"host": "relay.example", "port": 0, "requestNonce": WAN_NONCE}, "invalid_request"),
    ({"host": "relay.example", "port": 70000, "requestNonce": WAN_NONCE}, "invalid_request"),
    ({"host": "relay.example", "port": 443, "caFile": "C:/ca.pem", "requestNonce": WAN_NONCE}, "invalid_request"),
    ({"host": "relay.example", "port": 443, "caPem": 5, "requestNonce": WAN_NONCE}, "invalid_request"),
    ({"host": "relay.example", "port": 443, "caPem": "x" * (16 * 1024 + 1), "requestNonce": WAN_NONCE},
     "invalid_request"),
    ({"host": "relay.example", "port": 443, "serverName": 7, "requestNonce": WAN_NONCE}, "invalid_request"),
    ({"host": "relay.example", "port": 443}, "invalid_request"),
    ({"host": "relay.example", "port": 443, "requestNonce": "short"}, "invalid_request"),
    ({"clear": True}, "invalid_request"),
    ({"clear": False, "requestNonce": WAN_NONCE}, "invalid_request"),
    ({"clear": True, "host": "relay.example", "requestNonce": WAN_NONCE}, "invalid_request"),
])
def test_wan_relay_saved_while_off_and_rejected_inputs_do_not_mutate(wan, data, code):
    # Saved while the global remote setting is OFF: stored through C's helper, never dialled.
    wan.action(RELAY_SET, {"host": "relay.example", "port": 443, "serverName": "relay.example",
                           "requestNonce": WAN_NONCE})
    assert wan.link.calls == [("relay", ("endpoint", "relay.example", 443))]
    assert wan.helper_calls[0][0] == {"host": "relay.example", "port": 443, "serverName": "relay.example"}
    assert _acks(wan)[-1] == {"requestNonce": WAN_NONCE, "action": RELAY_SET, "status": "applied"}
    assert wan.runtime._link_runtime_settings().remote_access_enabled is False
    assert wan.runtime.settings.get("link.remote_access_enabled") is False

    # A rejected request changes nothing: the saved endpoint stays.
    wan.events.clear()
    wan.action(RELAY_SET, data)
    assert wan.link.calls == [("relay", ("endpoint", "relay.example", 443))]
    assert wan.last("link_error") == {"action": RELAY_SET, "code": code}
    if data.get("requestNonce") == WAN_NONCE:
        assert _acks(wan) == [{"requestNonce": WAN_NONCE, "action": RELAY_SET, "status": "refused", "code": code}]
    else:
        assert _acks(wan) == []

    # A quarantined link refuses a new endpoint; an explicit clear is allowed even with remote off.
    wan.link.monitor.quarantined = True
    wan.action(RELAY_SET, {"host": "relay.example", "port": 444, "requestNonce": WAN_NONCE})
    assert wan.last("link_error") == {"action": RELAY_SET, "code": "quarantined"}
    wan.action(RELAY_SET, {"clear": True, "requestNonce": WAN_NONCE})
    assert wan.link.calls[-1] == ("relay", None)
    assert _acks(wan)[-1] == {"requestNonce": WAN_NONCE, "action": RELAY_SET, "status": "applied"}


def test_wan_remote_grant_requires_opt_in_relay_and_security_and_nonrevoked_device(wan):
    def refused(code: str) -> None:
        assert wan.last("link_error") == {"action": REMOTE, "code": code}
        assert _acks(wan)[-1] == {"requestNonce": WAN_NONCE, "action": REMOTE, "status": "refused", "code": code}
        assert not [call for call in wan.link.calls if call[0] == "remote"]

    _remote_on(wan)
    refused("remote_access_off")  # global setting off (default)
    wan.runtime.settings.set("link.remote_access_enabled", True)
    _remote_on(wan)
    refused("remote_access_off")  # still off: the local link itself is off
    wan.enable()
    _remote_on(wan)
    refused("relay_not_configured")
    wan.link.relay = ("endpoint", "relay.example", 443)
    wan.link.monitor.remote_allowed = False
    _remote_on(wan)
    refused("quarantined")
    wan.link.monitor.remote_allowed = True
    _remote_on(wan, device=REVOKED)
    refused("device_revoked")
    _remote_on(wan, device="aaaaaaaaaaaaaaaa")
    refused("unknown_device")
    for days in (0, 366, True, 30.0, None, "30"):
        _remote_on(wan, days=days)
        refused("invalid_request")

    _remote_on(wan, days=30)
    assert wan.link.calls == [("remote", DEVICE, True, 30)]
    assert _acks(wan)[-1] == {"requestNonce": WAN_NONCE, "action": REMOTE, "status": "applied"}
    assert wan.last("link_status") is not None  # the existing view refresh still follows


def test_wan_operation_ack_correlates_and_revoke_off_remain_available(wan):
    # Remote off and quarantined: switching a phone off and revoking it still work, old callers included.
    wan.link.monitor.remote_allowed = False
    wan.link.monitor.quarantined = True
    wan.action(REMOTE, {"deviceId": DEVICE, "enabled": False})
    wan.action(REVOKE, {"deviceId": DEVICE})
    assert wan.link.calls == [("remote", DEVICE, False, None), ("revoke", DEVICE)]
    assert _acks(wan) == []  # no nonce: legacy behaviour, view events only
    assert wan.last("link_error") is None

    other = "x" * 32
    wan.action(REMOTE, {"deviceId": DEVICE, "enabled": False, "requestNonce": WAN_NONCE})
    wan.action(REVOKE, {"deviceId": DEVICE, "requestNonce": other})
    assert _acks(wan) == [{"requestNonce": WAN_NONCE, "action": REMOTE, "status": "applied"},
                          {"requestNonce": other, "action": REVOKE, "status": "applied"}]
    wan.action(REVOKE, {"deviceId": "aaaaaaaaaaaaaaaa", "requestNonce": other})
    assert _acks(wan)[-1] == {"requestNonce": other, "action": REVOKE, "status": "refused", "code": "unknown_device"}
    assert wan.last("link_error") == {"action": REVOKE, "code": "unknown_device"}

    # An unsafe nonce is never echoed; a mutation with one is refused before acting.
    before = list(wan.link.calls)
    wan.events.clear()
    wan.action(REVOKE, {"deviceId": DEVICE, "requestNonce": "<script>"})
    assert wan.link.calls == before and _acks(wan) == []
    assert wan.last("link_error") == {"action": REVOKE, "code": "invalid_request"}
    # Every acknowledgement holds only the tracking fields: no endpoint, PEM, key or exception text.
    for ack in _acks(wan):
        assert set(ack) <= {"requestNonce", "action", "status", "code"}


def test_wan_opt_in_defaults_off_and_reloads_local_link(bridge):
    from core.settings_service import InvalidSettingValue

    runtime = bridge.runtime
    assert runtime.settings.get("link.remote_access_enabled") is False
    assert runtime._link_runtime_settings().remote_access_enabled is False
    with pytest.raises(InvalidSettingValue):
        runtime.settings.set("link.remote_access_enabled", "yes")
    runtime.settings.set("link.remote_access_enabled", True)
    assert runtime._link_runtime_settings().remote_access_enabled is False  # the local link is off
    bridge.enable()
    assert runtime._link_runtime_settings().remote_access_enabled is True

    class Restartable(FakeLink):
        def __init__(self, settings) -> None:
            super().__init__()
            self.settings = settings
            self.stops = 0

        def stop(self) -> None:
            self.stops += 1

        def start(self) -> bool:
            return False

    runtime.settings.set("link.remote_access_enabled", False)
    link = Restartable(runtime._link_runtime_settings())
    runtime._link_runtime = link
    runtime.settings.set("link.remote_access_enabled", True)
    runtime.apply_device_link_settings()  # the existing reload path restarts the link on this change
    assert link.stops == 1 and link.settings.remote_access_enabled is True
    runtime.apply_device_link_settings()  # unchanged: no restart
    assert link.stops == 1
    # No link action can switch the global setting.
    bridge.action("qronos.link_relay_set", {"clear": True, "requestNonce": WAN_NONCE})
    bridge.action("qronos.link_remote", {"deviceId": DEVICE, "enabled": False})
    assert runtime.settings.get("link.remote_access_enabled") is True
