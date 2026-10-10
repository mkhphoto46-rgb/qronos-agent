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
  const { calls, invoke } = backend({ approve: { ok: true, launchToken: TOKEN }, status: { ok: true, status: STATUS } });
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

// ---- round 5: review U-1..U-3 and L-2 as required behaviour

test("an ambiguous launch failure never restarts the runtime and never exits on its own", async () => {
  const order = [];
  for (const failure of [new Error("IPC pipe closed"), { ok: false, code: "store" }, { ok: true }]) {
    order.length = 0;
    const client = update.createUpdateClient(async () => {
      order.push("launch");
      if (failure instanceof Error) throw failure;
      if (failure.ok === false) return failure;
      throw new Error("unreadable answer");
    });
    await assert.rejects(update.launchUpdate(client, TOKEN, {
      stopRuntime: async () => { order.push("stop"); return true; },
      restartRuntime: async () => { order.push("restart"); },
      exitApp: async () => { order.push("exit"); },
    }), (error) => error.code === "outcome_unknown");
    assert.deepEqual(order, ["stop", "launch"]);
  }
});

test("only typed refusals that prove nothing started bring the runtime back", async () => {
  for (const code of ["launch_unavailable", "writers_active", "not_staged", "permission_denied"]) {
    const order = [];
    const client = update.createUpdateClient(async () => ({ ok: false, code }));
    await assert.rejects(update.launchUpdate(client, TOKEN, {
      stopRuntime: async () => true,
      restartRuntime: async () => { order.push("restart"); },
      exitApp: async () => { order.push("exit"); },
    }), (error) => error.code === code);
    assert.deepEqual(order, ["restart"]);
  }
  assert.equal(update.LAUNCH_TOKEN_KEPT.has("launch_unavailable"), true);
  assert.equal(update.LAUNCH_TOKEN_KEPT.has("not_approved"), false); // the token was spent or unknown
});

const STATUS = {
  runningVersion: "0.1.0", trust: { configured: false, activeKeys: 0, error: null }, launchAvailable: false,
  phase: "handed_off", staged: { version: "0.1.0", kind: "repair", packageSha256: SHA }, launchReady: null,
  updateInProgress: false, launching: false, recoveryPending: false,
  outcome: { result: "unconfirmed", kind: "repair", version: "0.1.0", dataMismatch: [], dataRestored: false },
};

test("status is validated and carries the honest unconfirmed outcome", () => {
  const status = update.parseStatus(STATUS);
  assert.equal(status.outcome.result, "unconfirmed");
  assert.equal(status.launchAvailable, false);
  for (const broken of [{ ...STATUS, phase: "installed" }, { ...STATUS, launching: "yes" },
                        { ...STATUS, outcome: { ...STATUS.outcome, result: "probably" } },
                        { ...STATUS, staged: { version: "0.1.0", kind: "repair", packageSha256: "x" } }]) {
    assert.throws(() => update.parseStatus(broken), (error) => error.code === "bad_response");
  }
});

test("discovered packages are validated", () => {
  const good = { installer: "C:\\u\\qronos-desktop_0.2.0_x64-setup.exe",
                 manifest: "C:\\u\\qronos-desktop_0.2.0_x64-setup.exe.qronos-update.json" };
  assert.equal(update.parseCandidates([good]).length, 1);
  for (const bad of [[{ ...good, installer: "relative.exe", manifest: "relative.exe.qronos-update.json" }],
                     [{ ...good, manifest: "C:\\other.json" }], Array(17).fill(good), "list"]) {
    assert.throws(() => update.parseCandidates(bad), (error) => error.code === "bad_response");
  }
});

test("the panel keeps the token after a not-yet-spent refusal and shows the new states", () => {
  const panel = source("../src/components/UpdatePanel.tsx");
  assert.match(panel, /LAUNCH_TOKEN_KEPT\.has\(error\.code\)\) \{\s*setLaunchToken\(token\)/);
  assert.match(panel, /UpdateOutcomeUnknown/);
  for (const state of ["unconfirmed", "updateInProgress", "recoveryPending", "launchAvailable"]) {
    assert.match(panel, new RegExp(state));
  }
});

// ---- round 10: N6 complete bounded validation, N5 resolved unconfirmed repair

test("every rendered or bound offer field is validated and bounded; only checked fields are returned", () => {
  const parsed = update.parseOffer({ ...OFFER, changedStores: ["settings"], snapshotStores: ["settings"],
                                     restoreSnapshot: "1800000000-abcdef012345", notes: "line one\r\nline two",
                                     extraField: "never rendered" });
  assert.equal(parsed.extraField, undefined);
  assert.deepEqual(JSON.parse(JSON.stringify(parsed.changedStores)), ["settings"]);
  const broken = [
    { fromVersion: { major: 0 } }, { fromVersion: "0.1" }, { version: "1".repeat(129) }, { version: "v0.2.0" },
    { changedStores: "settings" }, { changedStores: Array(65).fill("settings") }, { changedStores: ["Settings!"] },
    { changedStores: ["x".repeat(49)] }, { snapshotStores: [7] }, { restoreSnapshot: "latest" },
    { restoreSnapshot: 5 }, { notes: 42 }, { notes: "n".repeat(2001) }, { notes: "bell\u0007" },
    { owner: "" }, { owner: "o".repeat(129) }, { owner: "line\nbreak" }, { keyId: "c".repeat(31) },
    { keyId: "C".repeat(32) }, { packageSha256: "AB".repeat(32) }, { packageBytes: -1 }, { packageBytes: 1.5 },
    { packageBytes: 3 * 1024 ** 3 }, { packageName: "evil.exe" }, { installer: "relative.exe" },
    { manifest: "C:\elsewhere.json" }, { kind: "downgrade" },
  ];
  for (const change of broken) {
    assert.throws(() => update.parseOffer({ ...OFFER, ...change }), (error) => error.code === "bad_response",
      JSON.stringify(change).slice(0, 80));
  }
});

test("nested status shapes are validated, including the resolved unconfirmed repair", () => {
  const resolved = { ...STATUS, recoveryPending: false,
    unconfirmed: { version: "0.1.0", kind: "repair", receipt: "absent", actions: ["discard", "retry"] } };
  const status = update.parseStatus(resolved);
  assert.deepEqual(JSON.parse(JSON.stringify(status.unconfirmed)),
    { version: "0.1.0", kind: "repair", receipt: "absent", actions: ["discard", "retry"] });
  assert.equal(status.recoveryPending, false);
  assert.equal(update.parseStatus({ ...STATUS, unconfirmed: null }).unconfirmed, null);
  for (const broken of [
    { unconfirmed: { ...resolved.unconfirmed, kind: "upgrade" } },
    { unconfirmed: { ...resolved.unconfirmed, receipt: "verified" } },
    { unconfirmed: { ...resolved.unconfirmed, actions: ["replay"] } },
    { unconfirmed: "yes" },
    { runningVersion: "latest" }, { trust: { configured: true, activeKeys: -1, error: null } },
    { trust: { configured: true, activeKeys: 1.5, error: null } },
    { trust: { configured: true, activeKeys: 1, error: "<b>html</b>" } },
    { outcome: { ...STATUS.outcome, dataMismatch: ["Settings With Spaces"] } },
    { outcome: { ...STATUS.outcome, version: { v: 1 } } },
    { staged: { version: "0.1", kind: "repair", packageSha256: SHA } },
  ]) {
    assert.throws(() => update.parseStatus({ ...resolved, ...broken }), (error) => error.code === "bad_response",
      JSON.stringify(broken).slice(0, 80));
  }
});

test("retry is a discard followed by a new check; the old launch is never replayed", async () => {
  const { calls, invoke } = backend({ discard: { ok: true }, request: { ok: true, offer: OFFER, approvalToken: TOKEN } });
  const client = update.createUpdateClient(invoke);
  await client.retry();
  await client.request(OFFER.installer, OFFER.manifest);
  assert.deepEqual(calls.map((call) => call.operation), ["discard", "request"]);
  assert.equal(calls.some((call) => call.operation === "launch"), false);
});

test("launch_failed is a proved non-start; launch_outcome_unknown is not", async () => {
  assert.equal(update.LAUNCH_NOT_STARTED.has("launch_failed"), true);
  assert.equal(update.LAUNCH_NOT_STARTED.has("launch_outcome_unknown"), false);
  const order = [];
  const client = update.createUpdateClient(async () => ({ ok: false, code: "launch_outcome_unknown" }));
  await assert.rejects(update.launchUpdate(client, TOKEN, {
    stopRuntime: async () => true,
    restartRuntime: async () => { order.push("restart"); },
    exitApp: async () => { order.push("exit"); },
  }), (error) => error.code === "outcome_unknown");
  assert.deepEqual(order, []);
});

test("the panel offers discard and retry for an unconfirmed repair and never promises recovery for it", () => {
  const panel = source("../src/components/UpdatePanel.tsx");
  assert.match(panel, /status\?\.unconfirmed/);
  assert.match(panel, /client\.retry\(\)/);
  assert.match(panel, /شروع دوبارهٔ Qronos این وضعیت را تغییر نمی‌دهد/);
  assert.match(panel, /status\?\.outcome && !status\.unconfirmed/);
  assert.doesNotMatch(panel, /nonce|launchToken\}/);
});
