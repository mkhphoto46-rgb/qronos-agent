"""
Queued work through the real model client, against a slow loopback server.

The queue, the real Orchestrator, the real OllamaController and the real
local model HTTP client run unchanged; only resource admission and telemetry
are faked, and the "model server" is a local HTTP listener on 127.0.0.1 that
answers on request or stalls mid-generation and watches for the client to
hang up. No model is loaded, no model server is started (``ensure_ready`` is
replaced so nothing can launch one), and nothing leaves loopback.

What is proved here, on the actual socket:

    a user's cancel, the user's own turn, Pause and closing Qronos each make
    the server see this request's connection close - promptly, not when
    generation would have finished;
    nothing the closed request could have produced is attached;
    an interrupted request runs again only on explicit resume, and answers
    exactly once;
    a stop aimed at an earlier request or run never reaches a later one.

Closing the connection is all the client can do. Whether and when the model
server stops generating and releases the GPU is its business; these tests
only observe that the request was closed.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.activity_guard import ActivityMode, ResourcePressure
from core.model_registry import get_model
from core.ollama_controller import OllamaController
from core.orchestrator import Orchestrator
from core.resource_policy import ResourceDecision
from core.runtime_bridge import handle_queue_cancel, handle_queue_resume_task
from tests.test_queue_durability import saved
from tests.test_queued_conversation import Harness


WAIT = 5.0
#: How soon the server must see the client hang up after a stop.
PROMPT_CLOSE = 0.75
ANSWER = "the slow server's answer"
PRIVATE = "PRIVATE-PROMPT-MARKER"


@pytest.fixture
def server(monkeypatch):
    state = {
        "mode": "stall",
        "chats": 0,
        "stalled": threading.Event(),
        "closed": threading.Event(),
    }

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # Residency checks: nothing is loaded on this "server".
            self._reply({"models": []})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))

            if self.path == "/api/show":
                self._reply({"model_info": {"general.architecture": "fixture"}})
                return

            if self.path != "/api/chat":
                self._reply({})
                return

            state["chats"] += 1

            if state["mode"] == "answer":
                self._reply({"message": {"content": ANSWER}})
                return

            # Mid-generation: say nothing, and notice the client hanging up.
            state["stalled"].set()
            self.connection.settimeout(WAIT)

            try:
                if self.connection.recv(1) == b"":
                    state["closed"].set()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                state["closed"].set()
            except OSError:
                pass

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=httpd.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    state["url"] = f"http://127.0.0.1:{httpd.server_port}"

    # Nothing may start a model server, whatever happens to a connection.
    monkeypatch.setattr(OllamaController, "ensure_ready", Mock(return_value=False))
    monkeypatch.setenv("QRONOS_OLLAMA_AUTO_START", "0")
    monkeypatch.setenv("QRONOS_MEMORY_RUNTIME", "0")

    yield state

    state["mode"] = "answer"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


class Admits:
    """Resource admission that always says yes and never swaps models."""

    def __init__(self) -> None:
        # Broad "unload every Qronos model" calls. The queue never makes one;
        # the runtime's own Pause may, for idle brains, as it always has.
        self.unload_everything = 0

    def prepare_model_slot(self, task_class) -> bool:
        return False

    def select_model(self, **kwargs):
        return SimpleNamespace(
            decision=ResourceDecision.ALLOW,
            model=get_model("fast"),
            already_resident=False,
        )

    def is_model_resident(self, name: str) -> bool:
        return False

    def unload_qronos_models(self, except_name=None) -> bool:
        self.unload_everything += 1
        return False


def a_real_brain(url: str) -> Orchestrator:
    state = SimpleNamespace(
        mode=ActivityMode.NORMAL,
        resource_pressure=ResourcePressure.NORMAL,
    )

    return Orchestrator(
        runtime=OllamaController(url),
        model_manager=Admits(),  # type: ignore[arg-type]
        activity_guard=SimpleNamespace(detect=lambda: state),  # type: ignore
        telemetry=SimpleNamespace(  # type: ignore[arg-type]
            current=lambda: SimpleNamespace(system=None, gpu=None),
            invalidate=lambda: None,
        ),
    )


def a_harness(tmp_path, monkeypatch, server) -> Harness:
    return Harness(
        tmp_path,
        monkeypatch,
        brain=a_real_brain(server["url"]),  # type: ignore[arg-type]
        threaded=True,
    )


def start_stalled(harness: Harness, server, request_id: str) -> str:
    server["stalled"].clear()
    server["closed"].clear()
    task_id = harness.submit(f"list or tuple? {PRIVATE}", request_id)
    harness.scheduler.tick()
    assert server["stalled"].wait(WAIT), "the request never reached the server"
    return task_id


def closed_promptly(server, since: float) -> None:
    assert server["closed"].wait(PROMPT_CLOSE), "the request was not closed"
    assert time.monotonic() - since < PROMPT_CLOSE + 0.25


def answers(harness: Harness, request_id: str) -> list[str]:
    return [t for role, t in harness.messages(request_id) if role == "assistant"]


def wait_journal(harness: Harness, state: str) -> None:
    journal = harness.runtime._queue_journal
    deadline = time.monotonic() + WAIT

    while time.monotonic() < deadline:
        if journal.records()[0].state.value == state:
            break

        time.sleep(0.01)

    assert journal.status().durable
    assert saved()["payload"]["records"][0]["state"] == state


def nothing_leaked(harness: Harness, *, queue_unloads: bool = False) -> None:
    """
    No stop or error detail, and no finished record, carries the request or
    transport diagnostics.

    The request itself is shown where it belongs by design: in the
    conversation, in the live queue row's summary, and in the saved record
    of a request that may still run. Those channels are not checked here.
    """
    details = []

    for _kind, _status, message in harness.events:
        try:
            payload = json.loads(message)
        except ValueError:
            continue

        if isinstance(payload, dict):
            details.extend(
                str(payload.get(key, ""))
                for key in ("detail", "error")
            )

            for task in payload.get("tasks", []) or []:
                details.append(str(task.get("detail", "")))

    finished = [
        record
        for record in saved()["payload"]["records"]
        if record["state"] in ("done", "failed", "cancelled")
    ]
    text = json.dumps(details) + json.dumps(finished)

    assert PRIVATE not in text
    assert "Local inference cancelled" not in json.dumps(harness.events)
    assert "127.0.0.1" not in json.dumps(harness.events)

    if not queue_unloads:
        assert harness.brain.model_manager.unload_everything == 0


# -------------------------------------------------------------------- tests


def test_a_users_cancel_closes_the_request_on_the_wire(
    tmp_path,
    monkeypatch,
    server,
) -> None:
    harness = a_harness(tmp_path, monkeypatch, server)
    task_id = start_stalled(harness, server, "req-wire-cancel")

    stopped_at = time.monotonic()
    handle_queue_cancel(harness.runtime, {"taskId": task_id})

    closed_promptly(server, stopped_at)
    harness.wait_settled()

    phases = harness.phases("req-wire-cancel")
    assert phases[-1] == "cancelled"
    assert "failed" not in phases
    assert answers(harness, "req-wire-cancel") == []
    wait_journal(harness, "cancelled")
    nothing_leaked(harness)


def test_the_users_turn_closes_it_and_it_answers_once_later(
    tmp_path,
    monkeypatch,
    server,
) -> None:
    harness = a_harness(tmp_path, monkeypatch, server)
    start_stalled(harness, server, "req-wire-preempt")

    stopped_at = time.monotonic()
    harness.runtime._set_busy(True)

    closed_promptly(server, stopped_at)
    harness.wait_settled()
    assert harness.task_for("req-wire-preempt")["state"] == "queued"
    assert harness.task_for("req-wire-preempt")["attempts"] == 0
    assert answers(harness, "req-wire-preempt") == []
    wait_journal(harness, "queued")

    server["mode"] = "answer"
    harness.runtime._set_busy(False)
    harness.clock.advance(31)
    harness.scheduler.tick()
    harness.wait_settled()

    assert answers(harness, "req-wire-preempt") == [ANSWER]
    assert server["chats"] == 2
    nothing_leaked(harness)


def test_pause_closes_it_and_resume_answers_once(
    tmp_path,
    monkeypatch,
    server,
) -> None:
    harness = a_harness(tmp_path, monkeypatch, server)
    start_stalled(harness, server, "req-wire-pause")

    stopped_at = time.monotonic()
    harness.runtime.set_runtime_paused(True)

    closed_promptly(server, stopped_at)
    harness.wait_settled()
    assert answers(harness, "req-wire-pause") == []
    assert "failed" not in harness.phases("req-wire-pause")

    server["mode"] = "answer"
    harness.runtime.set_runtime_paused(False)
    harness.clock.advance(31)
    harness.scheduler.tick()
    harness.wait_settled()

    assert answers(harness, "req-wire-pause") == [ANSWER]
    # Pause itself releases idle brains (runtime behaviour, not the queue's).
    nothing_leaked(harness, queue_unloads=True)


def test_closing_interrupts_it_and_only_explicit_resume_answers(
    tmp_path,
    monkeypatch,
    server,
) -> None:
    first = a_harness(tmp_path, monkeypatch, server)
    task_id = start_stalled(first, server, "req-wire-close")

    stopped_at = time.monotonic()
    first.runtime.close()

    closed_promptly(server, stopped_at)
    first.wait_settled()
    assert answers(first, "req-wire-close") == []
    wait_journal(first, "interrupted")

    server["mode"] = "answer"
    second = a_harness(tmp_path, monkeypatch, server)
    second.scheduler.tick()
    time.sleep(0.2)

    # Restored, interrupted, and not run again unasked.
    assert second.task_for("req-wire-close")["state"] == "interrupted"
    assert server["chats"] == 1

    handle_queue_resume_task(second.runtime, {"taskId": task_id})
    second.scheduler.tick()
    second.wait_settled()

    assert answers(second, "req-wire-close") == [ANSWER]
    assert server["chats"] == 2
    nothing_leaked(second)


def test_a_stop_for_an_earlier_request_never_reaches_a_later_one(
    tmp_path,
    monkeypatch,
    server,
) -> None:
    harness = a_harness(tmp_path, monkeypatch, server)
    old_id = start_stalled(harness, server, "req-wire-old")
    old_work = harness.scheduler.work_for(old_id)

    harness.runtime._set_busy(True)
    assert server["closed"].wait(PROMPT_CLOSE)
    harness.wait_settled()
    harness.runtime._set_busy(False)
    handle_queue_cancel(harness.runtime, {"taskId": old_id})

    # A new request runs on its own token. Stops aimed at the old one - its
    # work object, which still exists - change nothing for it.
    new_id = start_stalled(harness, server, "req-wire-new")
    assert old_work.preempt() is False
    assert old_work.pause() is False
    assert old_work.cancel() is True  # already cancelled; nothing to reach
    time.sleep(0.2)
    assert not server["closed"].is_set()
    assert harness.task_for("req-wire-new")["state"] == "running"

    # Let the new request finish normally.
    server["mode"] = "answer"
    handle_queue_cancel(harness.runtime, {"taskId": new_id})
    harness.wait_settled()

    assert answers(harness, "req-wire-old") == []
