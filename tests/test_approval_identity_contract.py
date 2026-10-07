"""
Single-use approval identities across backend, bridge, desktop and Rust.

The ledger is exercised directly; the bridge contract through handle_action;
the desktop helper by transpiling desktop/src/screenApproval.ts and running
it in Node when a local TypeScript install is present; and the App/Rust
wiring by reading the sources that must stay in agreement with the backend.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import threading

import pytest

from core.approval_ledger import (
    APPROVAL_ID_PATTERN,
    STALE_CLOSED,
    STALE_EXPIRED,
    STALE_MALFORMED,
    STALE_SUPERSEDED,
    STALE_UNKNOWN,
    STALE_USED,
    ApprovalKind,
    ApprovalLedger,
    StaleApproval,
)
from core.runtime_bridge import LOOK_AT_SCREEN_ACTION, handle_action

from test_runtime_bridge import FakeSpeechRuntime, captured_events, prepared_runtime


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "desktop" / "src" / "App.tsx"
SCREEN_TS = ROOT / "desktop" / "src" / "screenApproval.ts"
RUST_RUNTIME = ROOT / "desktop" / "src-tauri" / "src" / "runtime" / "mod.rs"
RUST_LIB = ROOT / "desktop" / "src-tauri" / "src" / "lib.rs"


# ------------------------------------------------------------------ ledger


def _stale(ledger: ApprovalLedger, approval_id: str) -> str:
    with pytest.raises(StaleApproval) as raised:
        ledger.take(approval_id)
    return raised.value.reason


def test_identity_is_single_use_and_bound_to_its_payload():
    ledger = ApprovalLedger()
    payload = object()
    ticket = ledger.issue(ApprovalKind.PAINT_DRAW, "draw a cat in Paint", payload=payload, window=7)

    taken = ledger.take(ticket.approval_id)

    assert taken.payload is payload and taken.window == 7
    assert _stale(ledger, ticket.approval_id) == STALE_USED


def test_same_question_gets_a_new_identity_and_revoked_one_stays_dead():
    ledger = ApprovalLedger()
    first = ledger.issue(ApprovalKind.SCREEN_READ, "what is this?")
    ledger.revoke_all("user_stop")
    second = ledger.issue(ApprovalKind.SCREEN_READ, "what is this?")

    assert first.approval_id != second.approval_id
    assert _stale(ledger, first.approval_id) == "user_stop"
    assert ledger.take(second.approval_id).question == "what is this?"
    assert _stale(ledger, first.approval_id) == "user_stop"


def test_identities_expire():
    now = {"t": 0.0}
    ledger = ApprovalLedger(clock=lambda: now["t"], ttl_seconds=5.0)
    ticket = ledger.issue(ApprovalKind.PAINT_UNDO, "undo")
    now["t"] = 5.0

    assert ledger.open_ticket(ApprovalKind.PAINT_UNDO) is None
    assert _stale(ledger, ticket.approval_id) == STALE_EXPIRED


def test_malformed_unknown_and_closed():
    ledger = ApprovalLedger()
    assert _stale(ledger, "x") == STALE_MALFORMED
    assert _stale(ledger, "../" * 10) == STALE_MALFORMED
    assert _stale(ledger, "A" * 30) == STALE_UNKNOWN

    ticket = ledger.issue(ApprovalKind.SCREEN_READ, "q")
    ledger.close()
    ledger.close()
    assert _stale(ledger, ticket.approval_id) == STALE_CLOSED
    # After shutdown even a never-issued identity is answered as shutdown.
    assert _stale(ledger, "Z" * 30) == STALE_CLOSED
    with pytest.raises(StaleApproval):
        ledger.issue(ApprovalKind.SCREEN_READ, "q")


def test_revoke_kinds_only_touches_those_kinds():
    ledger = ApprovalLedger()
    screen = ledger.issue(ApprovalKind.SCREEN_READ, "q")
    paint = ledger.issue(ApprovalKind.PAINT_DRAW, "p")

    revoked = ledger.revoke_kinds((ApprovalKind.SCREEN_READ,), STALE_SUPERSEDED)

    assert [t.approval_id for t in revoked] == [screen.approval_id]
    assert ledger.take(paint.approval_id).kind is ApprovalKind.PAINT_DRAW


def test_close_cannot_leave_a_concurrently_issued_ticket_open():
    ledger = ApprovalLedger()
    first = ledger.issue(ApprovalKind.SCREEN_READ, "before close")
    lock_released = threading.Event()
    issuer_finished = threading.Event()
    outcomes = []

    class WaitingIssuerLock:
        """Run a waiting issuer immediately after close releases its lock."""

        def __init__(self):
            self.lock = threading.Lock()
            self.scheduled = False

        def __enter__(self):
            self.lock.acquire()

        def __exit__(self, *_args):
            self.lock.release()
            if threading.current_thread().name == "ledger-closer" and not self.scheduled:
                self.scheduled = True
                lock_released.set()
                assert issuer_finished.wait(5)

    ledger._lock = WaitingIssuerLock()

    def issue_while_closing():
        assert lock_released.wait(5)
        try:
            outcomes.append(ledger.issue(ApprovalKind.SCREEN_READ, "during close"))
        except StaleApproval as exc:
            outcomes.append(exc.reason)
        finally:
            issuer_finished.set()

    issuer = threading.Thread(target=issue_while_closing)
    revoked = []
    closer = threading.Thread(target=lambda: revoked.extend(ledger.close()), name="ledger-closer")
    issuer.start()
    closer.start()
    closer.join(10)
    issuer.join(10)
    assert not closer.is_alive() and not issuer.is_alive()
    assert outcomes == [STALE_CLOSED]
    assert revoked == [first]
    assert ledger.open_tickets() == ()


def test_concurrent_answers_cannot_both_win():
    ledger = ApprovalLedger()
    ticket = ledger.issue(ApprovalKind.PAINT_DRAW, "p")
    winners: list = []
    barrier = threading.Barrier(8)

    def answer():
        barrier.wait()
        try:
            winners.append(ledger.take(ticket.approval_id))
        except StaleApproval:
            pass

    threads = [threading.Thread(target=answer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert len(winners) == 1


def test_generator_collision_or_bad_token_is_refused():
    ledger = ApprovalLedger(token_fn=lambda: "short")
    with pytest.raises(RuntimeError):
        ledger.issue(ApprovalKind.SCREEN_READ, "q")


# ------------------------------------------------------------ bridge contract


def _dispatch(runtime, data):
    seen: list = []
    done = threading.Event()

    def record(**kwargs):
        seen.append(kwargs)
        done.set()

    runtime.look_at_screen = record
    captured_events(lambda: handle_action(runtime, {"actionId": LOOK_AT_SCREEN_ACTION, "data": data}))
    assert done.wait(5)
    return seen[0]


def test_bridge_forwards_the_identity_and_never_invents_direct():
    runtime = prepared_runtime()
    approval_id = "B" * 24

    call = _dispatch(runtime, {"approvalId": approval_id, "approved": True, "windowOnly": True})

    assert call["approval_id"] == approval_id
    assert call["direct"] is False
    assert call["approved"] is True


@pytest.mark.parametrize(
    "data",
    [
        {"approvalId": 12345, "approved": True},
        {"approvalId": "B" * 24, "approved": True, "direct": True},
        {"approved": True, "direct": "yes"},
    ],
)
def test_bridge_rejects_ambiguous_or_mistyped_decisions(data):
    runtime = prepared_runtime()
    called: list = []
    runtime.look_at_screen = lambda **kwargs: called.append(kwargs)

    with pytest.raises(ValueError):
        handle_action(runtime, {"actionId": LOOK_AT_SCREEN_ACTION, "data": data})
    assert called == []


def test_prompt_payload_carries_a_wellformed_identity():
    runtime = prepared_runtime(speech=FakeSpeechRuntime(transcript="به این پنجره نگاه کن"))
    prompt = next(e for e in captured_events(runtime.push_to_talk) if e["eventType"] == "voice_needs_screen")
    body = json.loads(prompt["message"])

    assert body["kind"] == "screen_read"
    assert body["question"] == "به این پنجره نگاه کن"
    assert APPROVAL_ID_PATTERN.match(body["approvalId"])


# ------------------------------------------------ desktop and Rust agreement


def test_identity_format_is_identical_in_python_typescript_and_rust():
    ts = SCREEN_TS.read_text(encoding="utf-8")
    rust = RUST_RUNTIME.read_text(encoding="utf-8")

    assert APPROVAL_ID_PATTERN.pattern == "^[A-Za-z0-9_-]{22,64}$"
    assert "/^[A-Za-z0-9_-]{22,64}$/" in ts
    assert "const APPROVAL_ID_MIN_CHARS: usize = 22;" in rust
    assert "const APPROVAL_ID_MAX_CHARS: usize = 64;" in rust


def test_app_answers_prompts_only_by_identity_through_the_typed_command():
    app = APP.read_text(encoding="utf-8")

    helper = SCREEN_TS.read_text(encoding="utf-8")

    assert '"qronos.look_at_screen"' not in app
    assert '"qronos.look_at_screen"' not in helper
    # App builds the controller and runtime watch from the real Tauri invoke;
    # the factories own the typed commands (behaviour: desktop/tests/*.mjs).
    assert "createScreenApprovalController(\n      invoke," in app
    assert "startScreenApprovalRuntimeWatch(\n      screenApproval,\n      invoke," in app
    assert 'invoke("look_at_screen", {' in helper
    assert "approvalId: prompt.approvalId" in helper
    assert 'invoke("get_runtime_status")' in helper
    assert "start_runtime" not in helper
    # Every runtime event goes through the shared router, which parses
    # prompts and applies revocations.
    assert "applyScreenApprovalEvent(" in app
    assert "<ScreenApprovalDialog" in app
    assert "parseScreenPrompt(message)" in helper
    assert 'case "computer_approval_discarded":' in helper
    assert 'case "screen_approval_stale"' in app
    assert not re.search(r"\bconfirm\s*\(", app)
    # The question text is never sent back as the key of an answer.
    block = helper[helper.index('invoke("look_at_screen", {') :][:200]
    assert "question" not in block


def test_rust_registers_typed_command_and_blocks_generic_bypass():
    rust = RUST_RUNTIME.read_text(encoding="utf-8")
    lib = RUST_LIB.read_text(encoding="utf-8")

    assert "runtime::look_at_screen," in lib
    assert "pub fn look_at_screen(" in rust
    generic = rust[rust.index("pub fn send_runtime_action(") :]
    generic = generic[: generic.index("send_to_runtime(&state, payload)")]
    assert "reject_typed_only_action(cleaned)?;" in generic


_TS_DRIVER = r"""
const fs = require("fs");
const ts = require(process.argv[2]);
const source = fs.readFileSync(process.argv[3], "utf8");
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const mod = { exports: {} };
new Function("module", "exports", js)(mod, mod.exports);
const m = mod.exports;
const id = (c) => c.repeat(24);
const out = {};
out.valid = m.parseScreenPrompt(JSON.stringify({ approvalId: id("a"), kind: "paint_draw", question: "q" }));
out.noId = m.parseScreenPrompt(JSON.stringify({ kind: "paint_draw", question: "q" }));
out.badId = m.parseScreenPrompt(JSON.stringify({ approvalId: "../x", kind: "screen_read" }));
out.badKind = m.parseScreenPrompt(JSON.stringify({ approvalId: id("b"), kind: "file_action" }));
out.plainText = m.parseScreenPrompt("draw a cat in Paint");
out.discarded = m.parseDiscardedApprovalIds(JSON.stringify({ approvalIds: [id("c"), "bad", 7] }));
const q = new m.ScreenPromptQueue();
out.first = q.enqueue({ approvalId: id("d"), kind: "screen_read", question: "x" });
out.dupe = q.enqueue({ approvalId: id("d"), kind: "screen_read", question: "x" });
q.enqueue({ approvalId: id("e"), kind: "paint_draw", question: "y" });
q.enqueue({ approvalId: id("f"), kind: "paint_undo", question: "z" });
q.discard([id("e")]);
out.order = [q.next(), q.next(), q.next()].map((p) => (p ? p.approvalId : null));
out.reEnqueueShown = q.enqueue({ approvalId: id("d"), kind: "screen_read", question: "x" });
out.text = m.screenPromptText({ approvalId: id("g"), kind: "paint_undo", question: "undo" });
console.log(JSON.stringify(out));
"""


def _typescript_module() -> Path | None:
    for base in (ROOT / "desktop" / "node_modules",):
        candidate = base / "typescript" / "lib" / "typescript.js"
        if candidate.is_file():
            return candidate
    return None


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_desktop_prompt_helper_behaviour(tmp_path):
    typescript = _typescript_module()
    if typescript is None:
        pytest.skip("desktop/node_modules/typescript is not installed")
    driver = tmp_path / "driver.cjs"
    driver.write_text(_TS_DRIVER, encoding="utf-8")

    result = subprocess.run(
        ["node", str(driver), str(typescript), str(SCREEN_TS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=True,
    )
    out = json.loads(result.stdout)

    assert out["valid"] == {"approvalId": "a" * 24, "kind": "paint_draw", "question": "q"}
    assert out["noId"] is None and out["badId"] is None
    assert out["badKind"] is None and out["plainText"] is None
    assert out["discarded"] == ["c" * 24]
    assert out["first"] is True and out["dupe"] is False
    # The revoked prompt was never shown; the rest keep arrival order.
    assert out["order"] == ["d" * 24, "f" * 24, None]
    assert out["reEnqueueShown"] is False
    assert "Paint" in out["text"] and re.search("[؀-ۿ]", out["text"])


def test_take_with_kinds_leaves_other_kinds_untouched():
    ledger = ApprovalLedger()
    screen = ledger.issue(ApprovalKind.SCREEN_READ, "q")

    with pytest.raises(StaleApproval) as raised:
        ledger.take(screen.approval_id, kinds=(ApprovalKind.FILE_ACTION,))

    assert raised.value.reason == "wrong_kind"
    assert ledger.take(screen.approval_id, kinds=(ApprovalKind.SCREEN_READ,)) == screen


def test_payload_fn_receives_the_issued_identity():
    ledger = ApprovalLedger()
    ticket = ledger.issue(ApprovalKind.FILE_ACTION, "req", payload_fn=lambda approval_id: {"token": approval_id})
    assert ticket.payload == {"token": ticket.approval_id}
