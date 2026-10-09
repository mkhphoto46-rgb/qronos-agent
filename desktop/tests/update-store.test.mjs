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
    Promise, Set, Error, RegExp, Array, Object, JSON,
  }, { timeout: 1000 });
  return exports;
}

const update = load("../src/updateStore.ts");
const TOKEN = "A".repeat(43);
const SHA = "ab".repeat(32);
const OFFER = {
  version: "0.2.0", fromVersion: "0.1.0", kind: "upgrade", keyId: "c".repeat(32), owner: "Owner",
  packageSha256: SHA, packageBytes: 10, packageName: "qronos-desktop_0.2.0_x64-setup.exe", notes: "",
  installer: "C:\\u\\i.exe", manifest: "C:\\u\\i.exe.qronos-update.json", changedStores: [], snapshotStores: [],
  restoreSnapshot: null,
};

function backend(answers) {
  const calls = [];
  const invoke = async (command, args) => {
    calls.push({ command, ...args });
    const answer = answers[args.operation];
    if (answer instanceof Error) throw answer;
    return answer;
  };
  return { calls, invoke };
}

test("calls go through update_command only and never claim a running version", async () => {
  const { calls, invoke } = backend({ request: { ok: true, offer: OFFER, approvalToken: TOKEN } });
  const client = update.createUpdateClient(invoke);
  const answer = await client.request(OFFER.installer, OFFER.manifest);
  assert.equal(answer.approvalToken, TOKEN);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [{ command: "update_command", operation: "request",
    payload: { installer: OFFER.installer, manifest: OFFER.manifest } }]);
});

test("a backend refusal becomes a typed refusal with Persian text", async () => {
  const { invoke } = backend({ request: { ok: false, code: "no_trusted_key", error: "No owner key." } });
  const client = update.createUpdateClient(invoke);
  await assert.rejects(client.request("C:\\a.exe", "C:\\a.json"), (error) => {
    assert.equal(error.code, "no_trusted_key");
    assert.match(error.message, /کلید امضای مالک/);
    return true;
  });
  assert.equal(update.refusalText("something new"), "به‌روزرسانی انجام نشد.");
});

test("malformed offers and tokens from the backend are refused, not shown", async () => {
  for (const bad of [{ ...OFFER, packageSha256: "short" }, { ...OFFER, kind: "format" }, null]) {
    const { invoke } = backend({ request: { ok: true, offer: bad, approvalToken: TOKEN } });
    await assert.rejects(update.createUpdateClient(invoke).request("C:\\a", "C:\\b"),
      (error) => error.code === "bad_response");
  }
  const { invoke } = backend({ approve: { ok: true, launchToken: "not a token" } });
  await assert.rejects(update.createUpdateClient(invoke).approve(TOKEN, "secret"),
    (error) => error.code === "bad_response");
});

test("the secret is sent once in the approve payload and the client keeps nothing", async () => {
  const { calls, invoke } = backend({ approve: { ok: true, launchToken: TOKEN }, status: { ok: true, status: {} } });
  const client = update.createUpdateClient(invoke);
  await client.approve(TOKEN, "my secret");
  await client.status();
  assert.equal(calls[0].payload.secret, "my secret");
  assert.equal(JSON.stringify(calls[1]).includes("my secret"), false);
  assert.equal(JSON.stringify(client).includes("my secret"), false);
});

test("launch happens only after the runtime really stopped, then the app exits", async () => {
  const order = [];
  const { invoke } = backend({ launch: { ok: true, handoff: {} } });
  const client = update.createUpdateClient(async (c, a) => { order.push(a.operation); return invoke(c, a); });
  await update.launchUpdate(client, TOKEN, {
    stopRuntime: async () => { order.push("stop"); return true; },
    restartRuntime: async () => { order.push("restart"); },
    exitApp: async () => { order.push("exit"); },
  });
  assert.deepEqual(order, ["stop", "launch", "exit"]);
});

test("a runtime that did not stop means no launch request at all", async () => {
  const { calls, invoke } = backend({});
  const client = update.createUpdateClient(invoke);
  await assert.rejects(update.launchUpdate(client, TOKEN, {
    stopRuntime: async () => false, restartRuntime: async () => {}, exitApp: async () => { throw new Error("exit"); },
  }), (error) => error.code === "runtime_running");
  assert.equal(calls.length, 0);
});

test("a refused launch brings the runtime back and never exits", async () => {
  const order = [];
  const { invoke } = backend({ launch: { ok: false, code: "runtime_running" } });
  const client = update.createUpdateClient(invoke);
  await assert.rejects(update.launchUpdate(client, TOKEN, {
    stopRuntime: async () => true,
    restartRuntime: async () => { order.push("restart"); },
    exitApp: async () => { order.push("exit"); },
  }), (error) => error.code === "runtime_running");
  assert.deepEqual(order, ["restart"]);
});

test("digests are shown grouped for reading", () => {
  assert.equal(update.shortDigest(SHA), "abab abab abab abab");
});

test("the panel compiles, keeps the secret out of storage and uses a password field", () => {
  const panel = source("../src/components/UpdatePanel.tsx");
  const output = ts.transpileModule(panel, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    reportDiagnostics: true,
  });
  assert.equal(output.diagnostics.length, 0);
  for (const text of [panel, source("../src/updateStore.ts")]) {
    assert.doesNotMatch(text, /localStorage|sessionStorage|indexedDB|console\.(log|info|debug)/);
    assert.doesNotMatch(text, /https?:\/\//);
  }
  assert.match(panel, /type="password"/);
  assert.match(panel, /autoComplete="off"/);
  assert.match(panel, /dir="rtl"/);
});
