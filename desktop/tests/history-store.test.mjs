import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import nodeTest from "node:test";
import vm from "node:vm";
import ts from "typescript";

const test = (name, fn) => nodeTest(name, { timeout: 5000 }, fn);

function source(relative) {
  return readFileSync(new URL(relative, import.meta.url), "utf8");
}

// The real module, compiled exactly as shipped, run in an isolated context with no imports.
function load(relative) {
  const compiled = ts.transpileModule(source(relative), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, {
    exports,
    require(name) { throw new Error(`Unexpected import: ${name}`); },
    JSON, Number, Array, Set, String, Boolean, Object, RegExp,
  }, { timeout: 1000 });
  return exports;
}

const model = load("../src/historyModel.ts");
const TOKEN = "AbCdEfGhIjKlMnOpQrStUv_-";

function action(overrides = {}) {
  return {
    actionId: "act-1", category: "create_or_edit_file", kind: "action", name: "notes.txt",
    firstAt: 1_700_000_000_000, lastAt: 1_700_000_000_500,
    events: [{ event: "decided", at: 1_700_000_000_000 }, { event: "completed", at: 1_700_000_000_500 }],
    outcome: "succeeded", decision: "require_ui_confirmation",
    undo: { state: "available", reversal: "delete_created", reason: "" }, ...overrides,
  };
}

const snapshot = (actions, truncated = false) => JSON.stringify({ schemaVersion: 1, actions, truncated });
const plain = (value) => JSON.parse(JSON.stringify(value));

test("a snapshot keeps only checked fields and drops a row that would show a path or an unknown state", () => {
  const parsed = model.parseSnapshot(snapshot([
    action(),
    action({ actionId: "act-2", name: "C:\\Users\\someone\\secret.txt" }),
    action({ actionId: "act-3", name: "folder/inner.txt" }),
    action({ actionId: "act-4", undo: { state: "probably_fine", reversal: "", reason: "" } }),
    action({ actionId: "act-5", outcome: "Moved C:/x to C:/y" }),
    action({ actionId: "act-6", extra: "never shown" }),
  ]));
  assert.deepEqual(plain(parsed.actions.map((item) => item.actionId)), ["act-1", "act-6"]);
  assert.equal(parsed.actions[1].extra, undefined);
  assert.equal(model.parseSnapshot(snapshot(Array(201).fill(action()))), null);
  assert.equal(model.parseSnapshot("{\"schemaVersion\":2,\"actions\":[],\"truncated\":false}"), null);
  assert.equal(model.parseSnapshot("not json"), null);
});

test("an undo prompt is accepted only for the undo this panel asked for", () => {
  const prompt = JSON.stringify({ actionToken: TOKEN, actionId: "act-1", reversal: "delete_created",
                                  name: "notes.txt", needsSecret: false });
  let state = model.reduceHistory(model.INITIAL_HISTORY, "history_undo_approval_required", prompt);
  assert.equal(state, model.INITIAL_HISTORY, "an unsolicited prompt is ignored");
  state = model.reduceHistory({ ...model.INITIAL_HISTORY, requested: "act-9" }, "history_undo_approval_required",
                              prompt);
  assert.equal(state.prompt, null, "a prompt for another action is ignored");
  state = model.reduceHistory({ ...model.INITIAL_HISTORY, requested: "act-1" }, "history_undo_approval_required",
                              prompt);
  assert.deepEqual(plain(state.prompt), JSON.parse(prompt));
  assert.equal(state.requested, null);
});

test("answers count only for the open prompt; late, unknown or other answers change nothing", () => {
  const open = { ...model.INITIAL_HISTORY, prompt: { actionToken: TOKEN, actionId: "act-1",
                                                      reversal: "move_back", name: "a.txt", needsSecret: true } };
  for (const type of ["history_undo_result", "history_undo_stale", "history_undo_declined",
                      "history_undo_secret_rejected"]) {
    const other = model.reduceHistory(open, type, JSON.stringify({ actionToken: "Zz" + TOKEN.slice(2),
                                                                   actionId: "act-1", outcome: "undone" }));
    assert.equal(other, open, `${type} for another prompt is ignored`);
    const mine = model.reduceHistory(open, type, JSON.stringify({ actionToken: TOKEN, actionId: "act-1",
                                                                  outcome: "undone", reason: "already_answered" }));
    assert.equal(mine.prompt, null, `${type} closes the open prompt`);
    assert.ok(mine.notice);
    assert.equal(model.reduceHistory(model.INITIAL_HISTORY, type, JSON.stringify({ actionToken: TOKEN })),
                 model.INITIAL_HISTORY, `${type} with no open prompt is ignored`);
  }
  const refused = model.reduceHistory({ ...model.INITIAL_HISTORY, requested: "act-1" }, "history_undo_refused",
                                      JSON.stringify({ actionId: "act-1", reason: "changed_since" }));
  assert.equal(refused.requested, null);
  assert.match(refused.notice, /تغییر کرده/);
});

test("the secret travels only with an approval that needs it and is never part of the state", () => {
  const prompt = { actionToken: TOKEN, actionId: "act-1", reversal: "move_back", name: "a.txt", needsSecret: true };
  assert.deepEqual(plain(model.undoDecision(prompt, true, "my secret")),
    { actionId: "qronos.history_undo_decision", data: { actionToken: TOKEN, approved: true, secret: "my secret" } });
  assert.deepEqual(plain(model.undoDecision(prompt, false, "my secret")).data, { actionToken: TOKEN, approved: false });
  assert.deepEqual(plain(model.undoDecision({ ...prompt, needsSecret: false }, true, "x")).data,
    { actionToken: TOKEN, approved: true });
  const store = source("../src/historyStore.ts");
  assert.doesNotMatch(store, /secret:\s*secret|state\.secret|localStorage|sessionStorage/);
});

test("every undo state has an honest Persian text and only a reversible one offers a reversal sentence", () => {
  for (const state of ["available", "done", "failed", "expired", "irreversible", "nothing_to_undo", "not_latest",
                       "paint_flow", "earlier_session", "changed_since", "unverifiable", "unsupported"]) {
    assert.ok(model.UNDO_TEXT[state], state);
  }
  assert.match(model.reversalText("delete_created", "a.txt"), /اصل فایل دست نمی‌خورد/);
  assert.equal(model.reversalText("irreversible", "a.txt"), "");
});

test("the drawer is mounted beside the queue and handles secrets and stale prompts", () => {
  const app = source("../src/App.tsx");
  assert.match(app, /import HistoryDrawer from "\.\/components\/HistoryDrawer";/);
  assert.match(app, /<HistoryDrawer\s+hidden=\{\s*viewPhase !== "home" &&\s*viewPhase !== "conversations"\s*\}\s*\/>/);
  const drawer = source("../src/components/HistoryDrawer.tsx");
  assert.match(drawer, /type="password"/);
  assert.match(drawer, /autoComplete="off"/);
  assert.match(drawer, /if \(!prompt\) setSecret\(""\)/);
  assert.match(drawer, /void closeUndoConversation\(\)/);
  assert.match(drawer, /action\.undo\.state === "available" && \(/);
  assert.doesNotMatch(drawer, /console\.(log|info|debug)|localStorage/);
  const output = ts.transpileModule(drawer, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    reportDiagnostics: true,
  });
  assert.equal(output.diagnostics.length, 0);
});
