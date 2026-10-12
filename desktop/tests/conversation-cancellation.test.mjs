import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/conversationStore.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;

function fixture() {
  let listener;
  const exports = {};
  const stubs = {
    react: {
      useEffect() {},
      useSyncExternalStore(_subscribe, snapshot) { return snapshot(); },
    },
    "@tauri-apps/api/core": {
      async invoke(command) {
        return command === "get_runtime_status" ? { running: true } : undefined;
      },
    },
    "@tauri-apps/api/event": {
      async listen(_name, callback) { listener = callback; return () => {}; },
    },
  };
  vm.runInNewContext(compiled, {
    exports,
    require(name) {
      assert.ok(Object.hasOwn(stubs, name), `Unexpected import: ${name}`);
      return stubs[name];
    },
    console,
  }, { timeout: 1000 });
  return {
    send: exports.sendConversationMessage,
    snapshot: exports.useConversationStore,
    emit(eventType, payload, status = "ready") {
      assert.equal(typeof listener, "function");
      listener({ payload: { eventType, status, message: JSON.stringify(payload) } });
    },
    cancel(requestId) {
      this.emit("text_error", {
        requestId, status: "cancelled", reason: "user_stop", error: "private provider text",
      }, "cancelled");
    },
  };
}

test("Stop resolves the composer as a cancellation and admits another message", async () => {
  const app = fixture();
  const first = await app.send("synthetic first request");
  app.cancel(first);
  const stopped = app.snapshot();
  assert.equal(stopped.pendingRequestId, null);
  const bubble = stopped.messages.find((item) => item.role === "qronos");
  assert.equal(bubble.pending, false);
  assert.equal(bubble.error, false);
  assert.equal(bubble.cancelled, true);
  assert.ok(!bubble.text.includes("private provider text"));
  const second = await app.send("synthetic second request");
  assert.notEqual(second, first);
  assert.equal(app.snapshot().pendingRequestId, second);
});

test("an old or unidentified cancellation cannot clear a new request", async () => {
  const app = fixture();
  const first = await app.send("first");
  app.cancel(first);
  const second = await app.send("second");
  app.cancel(first);
  app.cancel("");
  app.cancel("unknown-request");
  assert.equal(app.snapshot().pendingRequestId, second);
  assert.equal(app.snapshot().messages.at(-1).pending, true);
});

test("a real error remains an error rather than a cancellation", async () => {
  const app = fixture();
  const requestId = await app.send("error fixture");
  app.emit("text_error", { requestId, error: "fixed synthetic failure" }, "error");
  const bubble = app.snapshot().messages.at(-1);
  assert.equal(bubble.error, true);
  assert.equal(bubble.cancelled, false);
  assert.ok(bubble.text.includes("fixed synthetic failure"));
});

test("a completed canonical answer is not replaced by a late Stop", async () => {
  const app = fixture();
  const requestId = await app.send("answer fixture");
  app.emit("conversation_message_added", {
    id: "canonical-answer", role: "qronos", text: "Actual answer", source: "text",
    timestamp: Date.now() / 1000, requestId,
  });
  app.cancel(requestId);
  const answer = app.snapshot().messages.find((item) => item.id === "canonical-answer");
  assert.equal(answer.text, "Actual answer");
  assert.equal(answer.cancelled, undefined);
});

test("reloading backend history drops the transient cancellation bubble", async () => {
  const app = fixture();
  const requestId = await app.send("history fixture");
  app.cancel(requestId);
  app.emit("conversation_history_snapshot", { messages: [{
    id: "canonical-user", role: "user", text: "history fixture", source: "text",
    timestamp: Date.now() / 1000, requestId,
  }] });
  assert.equal(app.snapshot().messages.length, 1);
  assert.equal(app.snapshot().messages[0].role, "user");
});

test("missing local setup errors have readable product text", async () => {
  for (const error of ["Local brain model is not installed", "Local brain is unavailable"]) {
    const app = fixture();
    const requestId = await app.send("setup fixture");
    app.emit("text_error", { requestId, error }, "error");
    const bubble = app.snapshot().messages.at(-1);
    assert.equal(bubble.error, true);
    assert.ok(!bubble.text.includes("???"));
    assert.match(bubble.text, /[\u0600-\u06ff]/u);
  }
});
