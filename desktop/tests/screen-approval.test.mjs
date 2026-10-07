import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import nodeTest from "node:test";
import vm from "node:vm";
import ts from "typescript";

// A broken guard can leave an answer awaiting forever; fail instead of hang.
const test = (name, fn) => nodeTest(name, { timeout: 5000 }, fn);

// The real modules, compiled exactly as shipped, run in an isolated context.
function compile(relative, jsx = false) {
  const source = readFileSync(new URL(relative, import.meta.url), "utf8");
  return ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      ...(jsx ? { jsx: ts.JsxEmit.ReactJSX } : {}),
    },
  }).outputText;
}

function load(compiled, stubs = {}) {
  const exports = {};
  const errors = [];
  vm.runInNewContext(compiled, {
    exports,
    require(name) {
      assert.ok(Object.hasOwn(stubs, name), `Unexpected import: ${name}`);
      return stubs[name];
    },
    console: { ...console, error: (...args) => errors.push(args), info() {} },
    Promise,
    Set,
  }, { timeout: 1000 });
  return { exports, errors };
}

const approvalModule = load(compile("../src/screenApproval.ts"));
const approval = approvalModule.exports;

const id = (letter) => letter.repeat(24);
const promptEvent = (approvalId, question = "به این پنجره نگاه کن", kind = "screen_read") =>
  JSON.stringify({ approvalId, kind, question });
const discardEvent = (...approvalIds) =>
  JSON.stringify({ reason: "user_stop", discarded: approvalIds.length, approvalIds });

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/** The controller as App.tsx builds it, fed through the same event router. */
function harness() {
  const sent = [];
  const pending = [];
  const sendErrors = [];
  const controller = new approval.ScreenApprovalController(
    (prompt, approved) => {
      sent.push({ approvalId: prompt.approvalId, approved });
      const call = deferred();
      pending.push(call);
      return call.promise;
    },
    (error) => sendErrors.push(error),
  );
  let renders = 0;
  controller.subscribe(() => { renders += 1; });
  return {
    controller,
    sent,
    pending,
    sendErrors,
    get renders() { return renders; },
    emit(eventType, message = "") {
      return approval.applyScreenApprovalEvent(controller, eventType, message);
    },
    shown() { return controller.getSnapshot()?.prompt.approvalId ?? null; },
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("one prompt is displayed at a time and the rest wait in arrival order", async () => {
  const app = harness();
  app.emit("voice_needs_screen", promptEvent(id("a")));
  app.emit("voice_needs_screen", promptEvent(id("b"), "draw a cat", "paint_draw"));
  app.emit("voice_needs_screen", promptEvent(id("c")));

  assert.equal(app.shown(), id("a"));
  assert.equal(app.controller.waiting, 2);
  assert.match(app.controller.getSnapshot().text, /[؀-ۿ]/u);

  const answering = app.controller.answer(id("a"), true);
  app.pending[0].resolve();
  await answering;
  assert.equal(app.shown(), id("b"));
  assert.deepEqual(app.sent, [{ approvalId: id("a"), approved: true }]);
});

test("revocation while displayed dismisses the prompt at once and sends no decision", () => {
  const app = harness();
  app.emit("voice_needs_screen", promptEvent(id("a")));
  app.emit("voice_needs_screen", promptEvent(id("b")));

  app.emit("computer_approval_discarded", discardEvent(id("a")));

  assert.equal(app.shown(), id("b"));
  assert.deepEqual(app.sent, []);
});

test("revoking the displayed and every waiting prompt leaves nothing on screen", () => {
  const app = harness();
  app.emit("voice_needs_screen", promptEvent(id("a")));
  app.emit("voice_needs_screen", promptEvent(id("b")));

  app.emit("computer_approval_discarded", discardEvent(id("a"), id("b")));

  assert.equal(app.controller.getSnapshot(), null);
  assert.equal(app.controller.waiting, 0);
  assert.deepEqual(app.sent, []);
});

test("a revoked waiting prompt is never shown, even if its prompt event arrives late", () => {
  const app = harness();
  app.emit("voice_needs_screen", promptEvent(id("a")));
  app.emit("voice_needs_screen", promptEvent(id("b")));
  app.emit("computer_approval_discarded", discardEvent(id("b"), id("z")));
  app.emit("voice_needs_screen", promptEvent(id("z")));

  assert.equal(app.shown(), id("a"));
  assert.equal(app.controller.waiting, 0);
});

test("an unrelated discarded identity changes nothing on screen", () => {
  const app = harness();
  app.emit("voice_needs_screen", promptEvent(id("a")));
  app.emit("voice_needs_screen", promptEvent(id("b")));
  const before = app.controller.getSnapshot();
  const renders = app.renders;

  app.emit("computer_approval_discarded", discardEvent(id("q")));
  app.emit("computer_approval_discarded", JSON.stringify({ approvalIds: ["not an id"] }));
  app.emit("computer_approval_discarded", "not json");

  assert.equal(app.controller.getSnapshot(), before);
  assert.equal(app.controller.waiting, 1);
  assert.equal(app.renders, renders);
});

test("a delayed result for a revoked prompt cannot close a newer prompt with the same text", async () => {
  const app = harness();
  const same = "به این پنجره نگاه کن";
  app.emit("voice_needs_screen", promptEvent(id("a"), same));
  const oldAnswer = app.controller.answer(id("a"), true);
  // Stop lands while the old decision is in flight; the same question is
  // asked again with a new identity.
  app.emit("computer_approval_discarded", discardEvent(id("a")));
  app.emit("voice_needs_screen", promptEvent(id("b"), same));
  assert.equal(app.shown(), id("b"));

  app.pending[0].resolve();
  await oldAnswer;
  assert.equal(app.shown(), id("b"));
  assert.equal(app.controller.getSnapshot().submitting, false);

  // A stale click for the old identity is ignored outright.
  await app.controller.answer(id("a"), true);
  assert.deepEqual(app.sent, [{ approvalId: id("a"), approved: true }]);
});

test("a delayed rejection for a revoked prompt does not mark the newer one unconfirmed", async () => {
  const app = harness();
  app.emit("voice_needs_screen", promptEvent(id("a")));
  const oldAnswer = app.controller.answer(id("a"), false);
  app.emit("computer_approval_discarded", discardEvent(id("a")));
  app.emit("voice_needs_screen", promptEvent(id("b")));

  app.pending[0].reject(new Error("runtime gone"));
  await oldAnswer;

  assert.equal(app.shown(), id("b"));
  assert.equal(app.controller.getSnapshot().unconfirmed, false);
});

test("duplicate submissions while one is in flight send exactly one decision", async () => {
  const app = harness();
  app.emit("voice_needs_screen", promptEvent(id("a")));

  const first = app.controller.answer(id("a"), true);
  assert.equal(app.controller.getSnapshot().submitting, true);
  await app.controller.answer(id("a"), true);
  await app.controller.answer(id("a"), false);
  await app.controller.answer(id("x"), true);

  assert.equal(app.sent.length, 1);
  app.pending[0].resolve();
  await first;
  assert.equal(app.controller.getSnapshot(), null);
});

test("an unconfirmed decision keeps the prompt up, is not replayed, and can be re-decided", async () => {
  const app = harness();
  app.emit("voice_needs_screen", promptEvent(id("a")));

  const answering = app.controller.answer(id("a"), true);
  app.pending[0].reject(new Error("invoke failed"));
  await answering;
  await settle();

  const view = app.controller.getSnapshot();
  assert.equal(view.prompt.approvalId, id("a"));
  assert.equal(view.submitting, false);
  assert.equal(view.unconfirmed, true);
  assert.equal(app.sent.length, 1);
  assert.equal(app.sendErrors.length, 1);

  const decline = app.controller.answer(id("a"), false);
  app.pending[1].resolve();
  await decline;
  assert.deepEqual(app.sent.map((item) => item.approved), [true, false]);
  assert.equal(app.controller.getSnapshot(), null);
});

for (const ending of ["runtime_stopping", "runtime_ready"]) {
  test(`${ending} clears displayed and waiting prompts without answering`, async () => {
    const app = harness();
    app.emit("voice_needs_screen", promptEvent(id("a")));
    app.emit("voice_needs_screen", promptEvent(id("b")));
    const inFlight = app.controller.answer(id("a"), true);

    app.emit(ending);
    assert.equal(app.controller.getSnapshot(), null);
    assert.equal(app.controller.waiting, 0);

    app.pending[0].resolve();
    await inFlight;
    assert.equal(app.controller.getSnapshot(), null);
    assert.equal(app.sent.length, 1); // only the decision made before the end
  });
}

test("unmount cleanup clears everything and later events start fresh", () => {
  const app = harness();
  app.emit("voice_needs_screen", promptEvent(id("a")));
  app.emit("voice_needs_screen", promptEvent(id("b")));

  app.controller.clear(); // what App's unmount effect does

  assert.equal(app.controller.getSnapshot(), null);
  assert.deepEqual(app.sent, []);
  app.emit("voice_needs_screen", promptEvent(id("c")));
  assert.equal(app.shown(), id("c"));
});

test("events keep being processed while a prompt is displayed", () => {
  const app = harness();
  app.emit("voice_needs_screen", promptEvent(id("a")));
  // Each of these returns synchronously while "a" is on screen.
  assert.equal(app.emit("voice_needs_screen", promptEvent(id("b"))), true);
  assert.equal(app.emit("computer_approval_discarded", discardEvent(id("b"))), true);
  assert.equal(app.emit("voice_audio_spectrum", "{}"), false);
  assert.equal(app.emit("screen_approval_stale", "{}"), false);
  assert.equal(app.shown(), id("a"));
  assert.equal(app.controller.waiting, 0);
});

test("a prompt without a valid identity is never displayed", () => {
  const app = harness();
  app.emit("voice_needs_screen", JSON.stringify({ kind: "screen_read", question: "x" }));
  app.emit("voice_needs_screen", "draw a cat in Paint");
  assert.equal(app.controller.getSnapshot(), null);
  assert.equal(app.controller.waiting, 0);
});

test("re-announcing the displayed identity does not duplicate it", () => {
  const app = harness();
  app.emit("voice_needs_screen", promptEvent(id("a")));
  app.emit("voice_needs_screen", promptEvent(id("a")));
  assert.equal(app.controller.waiting, 0);
});

// ---------------------------------------------------------------- dialog

function dialogModule() {
  const jsx = (type, props, key) => ({ type, props: props ?? {}, key });
  const effects = [];
  const react = {
    useRef: (value) => ({ current: value }),
    useEffect: (fn) => { effects.push(fn); },
  };
  const { exports } = load(compile("../src/components/ScreenApprovalDialog.tsx", true), {
    react,
    "react/jsx-runtime": { jsx, jsxs: jsx, Fragment: "Fragment" },
    "../screenApproval": approval,
    "./ScreenApprovalDialog.css": {},
  });
  return { Dialog: exports.default, effects };
}

function render(view, onAnswer) {
  const { Dialog, effects } = dialogModule();
  const outer = Dialog({ view, onAnswer });
  if (outer === null) return { tree: null, effects };
  assert.equal(outer.key, view.prompt.approvalId); // fresh dialog per identity
  return { tree: outer.type(outer.props), effects };
}

function findAll(node, predicate, found = []) {
  if (node === null || typeof node !== "object") return found;
  if (Array.isArray(node)) {
    node.forEach((child) => findAll(child, predicate, found));
    return found;
  }
  if (predicate(node)) found.push(node);
  findAll(node.props?.children, predicate, found);
  return found;
}

const text = (node) =>
  typeof node === "string" ? node
    : Array.isArray(node) ? node.map(text).join("")
      : node && typeof node === "object" ? text(node.props?.children) : "";

function viewFor(approvalId, overrides = {}) {
  const controller = new approval.ScreenApprovalController(async () => {});
  controller.receive({ approvalId, kind: "paint_draw", question: "یک گربه" });
  return { ...controller.getSnapshot(), ...overrides };
}

test("dialog renders nothing when no prompt is displayed", () => {
  assert.equal(render(null, () => {}).tree, null);
});

test("dialog is an accessible modal alert dialog with Persian copy", () => {
  const view = viewFor(id("a"));
  const { tree } = render(view, () => {});
  const [dialog] = findAll(tree, (n) => n.props?.role === "alertdialog");
  assert.equal(dialog.props["aria-modal"], "true");
  const [title] = findAll(tree, (n) => n.props?.id === dialog.props["aria-labelledby"]);
  const [body] = findAll(tree, (n) => n.props?.id === dialog.props["aria-describedby"]);
  assert.match(text(title), /Paint/);
  assert.match(text(body), /یک گربه/);
  const buttons = findAll(tree, (n) => n.type === "button");
  assert.deepEqual(buttons.map(text), ["رد کردن", "اجازه می‌دهم"]);
  assert.ok(buttons.every((b) => b.props.disabled === false));
});

test("dialog buttons answer exactly the displayed identity", () => {
  const calls = [];
  const { tree } = render(viewFor(id("a")), (...args) => calls.push(args));
  const [decline, allow] = findAll(tree, (n) => n.type === "button");
  decline.props.onClick();
  allow.props.onClick();
  assert.deepEqual(calls, [[id("a"), false], [id("a"), true]]);
});

test("Escape declines; it is ignored while a decision is being sent", () => {
  const calls = [];
  const keyEvent = (key) => ({
    key,
    shiftKey: false,
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
  });

  const idle = render(viewFor(id("a")), (...args) => calls.push(args)).tree;
  const [dialog] = findAll(idle, (n) => n.props?.role === "alertdialog");
  const escape = keyEvent("Escape");
  dialog.props.onKeyDown(escape);
  assert.equal(escape.prevented, true);
  assert.equal(escape.stopped, true);
  const other = keyEvent("x");
  dialog.props.onKeyDown(other);
  assert.equal(other.stopped, undefined); // other keys still reach app hotkeys
  assert.deepEqual(calls, [[id("a"), false]]);

  const busy = render(viewFor(id("a"), { submitting: true }), (...args) => calls.push(args)).tree;
  const [busyDialog] = findAll(busy, (n) => n.props?.role === "alertdialog");
  busyDialog.props.onKeyDown(keyEvent("Escape"));
  assert.equal(calls.length, 1);
  assert.equal(busyDialog.props["aria-busy"], true);
});

test("buttons are disabled while sending and an unconfirmed outcome is announced honestly", () => {
  const sending = render(viewFor(id("a"), { submitting: true }), () => {}).tree;
  const buttons = findAll(sending, (n) => n.type === "button");
  assert.ok(buttons.every((b) => b.props.disabled === true));
  assert.equal(text(buttons[1]), "در حال ارسال...");

  const unconfirmed = render(viewFor(id("a"), { unconfirmed: true }), () => {}).tree;
  const [status] = findAll(unconfirmed, (n) => n.props?.role === "status");
  const copy = text(status);
  assert.match(copy, /[؀-ۿ]/u);
  // Honest: it may have arrived; check or Stop. Never "did not reach".
  assert.match(copy, /ممکن است/u);
  assert.match(copy, /Stop/);
  assert.ok(!copy.includes("نرسید"));
});

test("focus starts on the safe decline choice", () => {
  const { tree, effects } = render(viewFor(id("a")), () => {});
  const [decline] = findAll(tree, (n) => n.type === "button" && text(n) === "رد کردن");
  let focused = false;
  decline.props.ref.current = { focus() { focused = true; } };
  effects.forEach((fn) => fn());
  assert.equal(focused, true);
});

test("the approval flow never uses the blocking window.confirm", () => {
  for (const file of ["../src/App.tsx", "../src/screenApproval.ts", "../src/components/ScreenApprovalDialog.tsx"]) {
    const source = readFileSync(new URL(file, import.meta.url), "utf8");
    assert.ok(!/\bconfirm\s*\(/.test(source), file);
  }
});

// ------------------------------------------- App wiring: typed commands

/** Fake Tauri invoke: records every command, answers from a script. */
function fakeInvoke(script = {}) {
  const calls = [];
  const pending = [];
  const invoke = (command, args) => {
    calls.push({ command, args });
    const reply = script[command];
    if (reply === "defer") {
      const call = deferred();
      pending.push(call);
      return call.promise;
    }
    if (reply instanceof Error) return Promise.reject(reply);
    return Promise.resolve(typeof reply === "function" ? reply(args) : reply);
  };
  return {
    invoke,
    calls,
    pending,
    count: (command) => calls.filter((call) => call.command === command).length,
  };
}

function fakeTimers() {
  let next = 1;
  const scheduled = new Map();
  return {
    setTimeout(callback, ms) { const handle = next++; scheduled.set(handle, { callback, ms }); return handle; },
    clearTimeout(handle) { scheduled.delete(handle); },
    get pending() { return scheduled.size; },
    delays() { return [...scheduled.values()].map((item) => item.ms); },
    async fire() {
      const due = [...scheduled.values()];
      scheduled.clear();
      due.forEach((item) => item.callback());
      await settle();
    },
  };
}

/** The pieces App.tsx builds, wired exactly as App wires them. */
function wired(script) {
  const tauri = fakeInvoke(script);
  const errors = [];
  const controller = approval.createScreenApprovalController(tauri.invoke, (error) => errors.push(error));
  const timers = fakeTimers();
  const watch = approval.startScreenApprovalRuntimeWatch(controller, tauri.invoke, timers);
  const emit = (eventType, message = "") => approval.applyScreenApprovalEvent(controller, eventType, message);
  const shown = () => controller.getSnapshot()?.prompt.approvalId ?? null;
  return { tauri, errors, controller, timers, watch, emit, shown };
}

test("decisions use only the typed look_at_screen command with the exact identity", async () => {
  const app = wired({ look_at_screen: undefined, get_runtime_status: { running: true } });
  app.emit("voice_needs_screen", promptEvent(id("a"), "same words"));
  await app.controller.answer(id("a"), true);

  const sent = app.tauri.calls.filter((call) => call.command === "look_at_screen");
  assert.equal(sent.length, 1);
  assert.equal(JSON.stringify(sent[0].args), JSON.stringify({ approvalId: id("a"), approved: true, windowOnly: true }));
  assert.equal(app.tauri.count("start_runtime"), 0);
  app.watch.dispose();
});

test("a rejected decision is reported unconfirmed and is never invoked again automatically", async () => {
  const app = wired({ look_at_screen: new Error("flush failed after write"), get_runtime_status: { running: true } });
  app.emit("voice_needs_screen", promptEvent(id("a")));

  await app.controller.answer(id("a"), true);
  // Let every pending timer/status check run a few times.
  for (let round = 0; round < 3; round += 1) await app.timers.fire();

  const view = app.controller.getSnapshot();
  assert.equal(view.prompt.approvalId, id("a")); // same exact identity kept
  assert.equal(view.unconfirmed, true);
  assert.equal(view.submitting, false);
  assert.equal(app.tauri.count("look_at_screen"), 1);
  assert.equal(app.errors.length, 1);
  app.watch.dispose();
});

test("no runtime status polling happens without a visible prompt", async () => {
  const app = wired({ get_runtime_status: { running: true } });
  assert.equal(app.timers.pending, 0);
  app.emit("runtime_error", "Traceback: ordinary diagnostic");
  app.emit("voice_audio_spectrum", "{}");
  app.emit("computer_approval_discarded", discardEvent(id("q")));
  await app.timers.fire();
  assert.equal(app.timers.pending, 0);
  assert.equal(app.tauri.count("get_runtime_status"), 0);

  app.emit("voice_needs_screen", promptEvent(id("a")));
  assert.equal(app.timers.pending, 1);
  assert.deepEqual(app.timers.delays(), [approval.SCREEN_APPROVAL_STATUS_INTERVAL_MS]);
  app.emit("computer_approval_discarded", discardEvent(id("a")));
  assert.equal(app.timers.pending, 0); // stops as soon as nothing is visible
  await app.timers.fire();
  assert.equal(app.tauri.count("get_runtime_status"), 0);
  app.watch.dispose();
});

test("an unexpectedly stopped runtime clears the visible and waiting prompts without answering", async () => {
  const app = wired({ get_runtime_status: { running: false, status: "stopped" } });
  app.emit("voice_needs_screen", promptEvent(id("a")));
  app.emit("voice_needs_screen", promptEvent(id("b")));

  await app.timers.fire();

  assert.equal(app.tauri.count("get_runtime_status"), 1);
  assert.equal(app.controller.getSnapshot(), null);
  assert.equal(app.controller.waiting, 0);
  assert.equal(app.tauri.count("look_at_screen"), 0);
  assert.equal(app.tauri.count("start_runtime"), 0);
  assert.equal(app.timers.pending, 0); // nothing visible: polling stops
  app.watch.dispose();
});

test("a running runtime keeps the prompt and checks again, one check at a time", async () => {
  const app = wired({ get_runtime_status: "defer" });
  app.emit("voice_needs_screen", promptEvent(id("a")));
  await app.timers.fire();
  assert.equal(app.tauri.count("get_runtime_status"), 1);
  // While that check is in flight, new events never start a second one.
  app.emit("voice_needs_screen", promptEvent(id("b")));
  assert.equal(app.timers.pending, 0);

  app.tauri.pending[0].resolve({ running: true });
  await settle();
  assert.equal(app.shown(), id("a"));
  assert.equal(app.timers.pending, 1);
  app.watch.dispose();
});

test("ordinary diagnostics and unreadable status never count as a stopped runtime", async () => {
  const app = wired({ get_runtime_status: new Error("Could not inspect Qronos runtime process") });
  app.emit("voice_needs_screen", promptEvent(id("a")));
  app.emit("runtime_error", "Traceback (most recent call last): diagnostic");
  await app.timers.fire();
  assert.equal(app.shown(), id("a"));
  assert.equal(app.timers.pending, 1); // keeps checking at the same bounded rate

  for (const reply of [{}, { running: "false" }, null, "stopped"]) {
    assert.equal(approval.isStoppedRuntimeStatus(reply), false);
  }
  assert.equal(approval.isStoppedRuntimeStatus({ running: false }), true);
  app.watch.dispose();
});

test("a late stopped reply cannot clear a prompt from a newer runtime", async () => {
  const app = wired({ get_runtime_status: "defer" });
  app.emit("voice_needs_screen", promptEvent(id("a")));
  await app.timers.fire(); // check for runtime 1 is now in flight

  app.emit("runtime_ready"); // a new runtime replaced the old one
  app.emit("voice_needs_screen", promptEvent(id("b"), "به این پنجره نگاه کن"));
  app.tauri.pending[0].resolve({ running: false }); // the old runtime's answer
  await settle();

  assert.equal(app.shown(), id("b"));
  assert.equal(app.timers.pending, 1); // b gets its own fresh check
  app.watch.dispose();
});

test("a late stopped reply cannot clear a newer prompt with the same text", async () => {
  const app = wired({ get_runtime_status: "defer", look_at_screen: undefined });
  app.emit("voice_needs_screen", promptEvent(id("a"), "same words"));
  app.emit("voice_needs_screen", promptEvent(id("b"), "same words"));
  await app.timers.fire();
  await app.controller.answer(id("a"), false); // a decided; b now visible

  app.tauri.pending[0].resolve({ running: false });
  await settle();

  assert.equal(app.shown(), id("b"));
  app.watch.dispose();
});

test("unmount cancels the scheduled check and suppresses an in-flight result", async () => {
  const scheduled = wired({ get_runtime_status: { running: false } });
  scheduled.emit("voice_needs_screen", promptEvent(id("a")));
  assert.equal(scheduled.timers.pending, 1);
  scheduled.watch.dispose();
  assert.equal(scheduled.timers.pending, 0);
  await scheduled.timers.fire();
  assert.equal(scheduled.tauri.count("get_runtime_status"), 0);

  const inFlight = wired({ get_runtime_status: "defer" });
  inFlight.emit("voice_needs_screen", promptEvent(id("a")));
  await inFlight.timers.fire();
  inFlight.watch.dispose(); // App's unmount cleanup runs before clear()
  inFlight.tauri.pending[0].resolve({ running: false });
  await settle();
  assert.equal(inFlight.shown(), id("a")); // late result ignored
  inFlight.emit("voice_needs_screen", promptEvent(id("b")));
  assert.equal(inFlight.timers.pending, 0); // nothing rescheduled after unmount
  assert.equal(inFlight.tauri.count("look_at_screen"), 0);
});
