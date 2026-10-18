import assert from "node:assert/strict";
import nodeTest from "node:test";

// The real runtime-connection controller (desktop/src/runtimeConnection.ts), loaded by Node's own type stripping.
// Behaviour only: what the person sees, when retry is offered, and which late results are ignored.
const { createRuntimeConnection, RUNTIME_CONNECTION_TEXT } = await import("../src/runtimeConnection.ts");

const test = (name, fn) => nodeTest(name, { timeout: 5000 }, fn);

function harness({ probe, ping } = {}) {
  const views = [];
  const timers = [];
  const calls = { probe: [], ping: 0 };
  const connection = createRuntimeConnection({
    probe: async (attempt) => {
      calls.probe.push(attempt);
      return probe ? probe(attempt) : "spawned";
    },
    ping: async () => {
      calls.ping += 1;
      if (ping) await ping();
    },
    onChange: (view) => views.push(view),
    setTimer: (callback, ms) => { timers.push({ callback, ms, cleared: false }); return timers.length - 1; },
    clearTimer: (handle) => { timers[handle].cleared = true; },
    readyTimeoutMs: 1000,
  });
  return { connection, views, timers, calls, last: () => views[views.length - 1] };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("a failed startup is shown to the person with a safe message and a retry", async () => {
  const secret = "C:\\qronos-fixture\\runtime\\qronos-runtime.exe: spawn failed (provider ollama, Traceback ...)";
  const h = harness({ probe: async () => { throw new Error(secret); } });
  await h.connection.start();
  const view = h.last();
  assert.equal(view.phase, "failed");
  assert.equal(view.canRetry, true);
  assert.equal(view.busy, false);
  assert.equal(view.label, RUNTIME_CONNECTION_TEXT.failedLabel);
  for (const text of [view.label, view.detail]) {
    assert.ok(!text.includes("C:\\") && !text.includes("Traceback") && !text.includes("ollama"), text);
  }
});

test("a spawned process is not ready: only runtime_ready makes the runtime ready", async () => {
  const h = harness();
  await h.connection.start();
  assert.equal(h.last().phase, "connecting");
  assert.equal(h.last().canRetry, false);
  h.connection.handleEvent("runtime_log");
  assert.equal(h.last().phase, "connecting");
  h.connection.handleEvent("runtime_ready");
  assert.equal(h.last().phase, "ready");
  assert.ok(h.timers.every((timer) => timer.cleared), "the readiness deadline is cancelled");
});

test("an already running runtime is asked once and becomes ready on the bridge's own answer", async () => {
  const h = harness({ probe: async () => "running" });
  await h.connection.start();
  assert.equal(h.calls.ping, 1);
  assert.equal(h.last().phase, "connecting");
  h.connection.handleEvent("runtime_pong");
  assert.equal(h.last().phase, "ready");
});

test("no answer within the deadline is a visible failure that offers retry", async () => {
  const h = harness();
  await h.connection.start();
  h.timers[0].callback();
  assert.equal(h.last().phase, "failed");
  assert.equal(h.last().canRetry, true);
});

test("starting twice or retrying while busy never starts a second attempt", async () => {
  let release;
  const h = harness({ probe: () => new Promise((resolve) => { release = resolve; }) });
  const first = h.connection.start();
  const second = h.connection.start();
  assert.equal(h.connection.retry(), false);
  await settle();
  assert.deepEqual(h.calls.probe, [1]);
  release("spawned");
  await Promise.all([first, second]);
  assert.deepEqual(h.calls.probe, [1]);
  assert.equal(h.connection.retry(), false, "connecting: no retry until it fails");
});

test("retry after a failure runs exactly one new attempt and can reach ready", async () => {
  let fail = true;
  const h = harness({ probe: async () => { if (fail) throw new Error("x"); return "spawned"; } });
  await h.connection.start();
  assert.equal(h.last().phase, "failed");
  fail = false;
  assert.equal(h.connection.retry(), true);
  assert.equal(h.connection.retry(), false);
  await settle();
  assert.deepEqual(h.calls.probe, [1, 2]);
  h.connection.handleEvent("runtime_ready");
  assert.equal(h.last().phase, "ready");
});

test("a late result of an older attempt, or any result after disposal, changes nothing", async () => {
  const pending = [];
  const h = harness({ probe: () => new Promise((resolve, reject) => pending.push({ resolve, reject })) });
  void h.connection.start();
  await settle();
  h.timers[0].callback(); // attempt 1 times out
  assert.equal(h.connection.retry(), true); // attempt 2 starts
  await settle();
  pending[0].reject(new Error("late failure of attempt 1"));
  await settle();
  assert.equal(h.last().phase, "connecting", "attempt 1's late failure must not fail attempt 2");
  h.connection.dispose();
  const seen = h.views.length;
  pending[1].reject(new Error("after disposal"));
  h.connection.handleEvent("runtime_ready");
  h.connection.handleEvent("runtime_error");
  await settle();
  assert.equal(h.views.length, seen);
  assert.equal(h.connection.retry(), false);
});

test("a runtime error after ready is reported without pretending the runtime is gone", async () => {
  const h = harness();
  await h.connection.start();
  h.connection.handleEvent("runtime_ready");
  h.connection.handleEvent("runtime_error");
  assert.equal(h.last().phase, "degraded");
  assert.equal(h.last().canRetry, true);
  h.connection.handleEvent("runtime_error");
  assert.equal(h.last().phase, "degraded");
  h.connection.handleEvent("runtime_ready");
  assert.equal(h.last().phase, "ready");
});

test("a runtime error during startup fails the attempt; a stopping bridge shows reconnecting", async () => {
  const h = harness();
  await h.connection.start();
  h.connection.handleEvent("runtime_error");
  assert.equal(h.last().phase, "failed");
  const g = harness();
  await g.connection.start();
  g.connection.handleEvent("runtime_ready");
  g.connection.handleEvent("runtime_stopping");
  assert.equal(g.last().phase, "connecting");
  assert.equal(g.last().label, RUNTIME_CONNECTION_TEXT.reconnectingLabel);
  assert.deepEqual(g.calls.probe, [1], "reconnecting waits for the supervisor; it spawns nothing itself");
});
