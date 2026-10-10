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

const LAUNCHED = { ok: true, outcome: "launched", handoff: { version: "0.2.0", handedOver: true }, exiting: true };
const notStarted = (code, runtimeRestarted) => ({ ok: false, outcome: "not_started", code, error: "fixture",
                                                  runtimeRestarted });

test("a launch sends only the token, once, through update_command; the desktop does the rest", async () => {
  const { calls, invoke } = backend({ launch: LAUNCHED });
  const client = update.createUpdateClient(invoke);
  const result = await update.launchUpdate(client, TOKEN);
  assert.equal(result.handedOver, true);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)),
    [{ command: "update_command", operation: "launch", payload: { launchToken: TOKEN } }]);
  // No hook exists to stop, restart or close anything from here.
  assert.equal(update.launchUpdate.length, 2);
});

test("a stop the desktop could not prove is a typed refusal and the token is kept", async () => {
  const { calls, invoke } = backend({ launch: notStarted("runtime_stop_unproved", false) });
  const client = update.createUpdateClient(invoke);
  await assert.rejects(update.launchUpdate(client, TOKEN), (error) => error.code === "runtime_stop_unproved"
    && /تأیید نشد/.test(error.message));
  assert.equal(calls.length, 1);
  assert.equal(update.LAUNCH_TOKEN_KEPT.has("runtime_stop_unproved"), true);
});

test("a refused launch reports whether the desktop brought the runtime back and never exits from here", async () => {
  for (const restarted of [true, false]) {
    const client = update.createUpdateClient(async () => notStarted("runtime_running", restarted));
    const result = await client.launch(TOKEN);
    assert.deepEqual(JSON.parse(JSON.stringify(result)),
      { outcome: "not_started", code: "runtime_running", runtimeRestarted: restarted });
    await assert.rejects(update.launchUpdate(client, TOKEN), (error) => error.code === "runtime_running");
  }
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

test("an ambiguous launch answer is outcome unknown, never a refusal that could invite a restart", async () => {
  const failures = [
    new Error("IPC pipe closed"),
    { ok: false, code: "store" },                                   // a backend code without the desktop's verdict
    { ok: true },                                                   // no outcome
    { ok: true, outcome: "launched" },                              // no handoff
    { ok: false, outcome: "unknown", code: "launch_outcome_unknown", exiting: true },
    { ok: false, outcome: "not_started" },                          // no code
    { ok: false, outcome: "maybe", code: "launch_failed" },
    "launched",
  ];
  for (const failure of failures) {
    const client = update.createUpdateClient(async () => {
      if (failure instanceof Error) throw failure;
      return failure;
    });
    await assert.rejects(update.launchUpdate(client, TOKEN), (error) => error.code === "outcome_unknown",
      JSON.stringify(String(failure)));
  }
});

test("the desktop's typed non-start verdicts surface with their own code", async () => {
  for (const code of ["launch_unavailable", "writers_active", "not_staged", "permission_denied", "package_mismatch",
                      "desktop_busy", "backend_not_started"]) {
    const client = update.createUpdateClient(async () => notStarted(code, true));
    await assert.rejects(update.launchUpdate(client, TOKEN), (error) => error.code === code);
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
  // Media14 C1: a launch recorded before receipts were requested resolves with receipt "not_requested".
  const legacy = update.parseStatus({ ...resolved, unconfirmed: { ...resolved.unconfirmed, receipt: "not_requested" } });
  assert.equal(legacy.unconfirmed.receipt, "not_requested");
  assert.deepEqual(JSON.parse(JSON.stringify(legacy.unconfirmed.actions)), ["discard", "retry"]);
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
  const client = update.createUpdateClient(async () => ({ ok: false, code: "launch_outcome_unknown" }));
  await assert.rejects(update.launchUpdate(client, TOKEN), (error) => error.code === "outcome_unknown");
});

// ---- round 22: the desktop lifecycle owns the launch; the panel is mounted with a closed launch

test("the desktop's proved-no-start list and this module's are the same round-5 C3 set", () => {
  const rust = source("../src-tauri/src/runtime/lifecycle.rs");
  const block = rust.match(/pub\(super\) const PROVED_NO_START: &\[&str\] = &\[([^\]]*)\]/);
  assert.ok(block, "PROVED_NO_START not found in lifecycle.rs");
  const codes = [...block[1].matchAll(/"([a-z_]+)"/g)].map((match) => match[1]).sort();
  assert.deepEqual(codes, [...update.LAUNCH_NOT_STARTED].sort());
  assert.equal(codes.includes("launch_outcome_unknown"), false);
});

test("the pickers return only validated paths and a cancel is null", async () => {
  const installer = "C:\\u\\qronos-desktop_0.2.0_x64-setup.exe";
  const { calls, invoke } = backend({
    pick_package: { ok: true, cancelled: false, installer, manifest: `${installer}.qronos-update.json` },
    pick_folder: { ok: true, cancelled: true },
  });
  const client = update.createUpdateClient(invoke);
  assert.deepEqual(JSON.parse(JSON.stringify(await client.pickPackage())),
    { installer, manifest: `${installer}.qronos-update.json` });
  assert.equal(await client.pickFolder(), null);
  assert.deepEqual(calls.map((call) => [call.operation, call.payload]).map(JSON.stringify),
    [["pick_package", {}], ["pick_folder", {}]].map(JSON.stringify));
  for (const answer of [{ ok: true, cancelled: false, installer: "relative.exe", manifest: "relative.exe.qronos-update.json" },
                        { ok: true, cancelled: false, installer, manifest: "C:\\other.json" }]) {
    await assert.rejects(update.createUpdateClient(async () => answer).pickPackage(),
      (error) => error.code === "bad_response");
  }
  await assert.rejects(update.createUpdateClient(async () => ({ ok: true, cancelled: false, folder: "x" })).pickFolder(),
    (error) => error.code === "bad_response");
  await assert.rejects(update.createUpdateClient(async () => ({ ok: false, code: "picker_unavailable" })).pickFolder(),
    (error) => error.code === "picker_unavailable" && /انتخاب فایل/.test(error.message));
});

function loadPanel() {
  const compiled = ts.transpileModule(source("../src/components/UpdatePanel.tsx"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, {
    exports,
    require(name) {
      if (name === "react") return { useEffect() {}, useRef() { return { current: 0 }; }, useState(v) { return [v, () => {}]; } };
      if (name === "react/jsx-runtime") return { jsx() {}, jsxs() {}, Fragment: {} };
      if (name === "../updateStore") return update;
      throw new Error(`Unexpected import: ${name}`);
    },
    Boolean, Promise, Set, Error,
  }, { timeout: 1000 });
  return exports;
}

test("the launch is structurally closed unless the backend says it is available and approved", () => {
  const { launchOpen } = loadPanel();
  const open = { ...update.parseStatus({ ...STATUS, phase: "staged", launchAvailable: true,
                                         launchReady: { version: "0.2.0" }, outcome: null }) };
  assert.equal(launchOpen(open, TOKEN, false), true);
  assert.equal(launchOpen(null, TOKEN, false), false);
  assert.equal(launchOpen(open, null, false), false);
  assert.equal(launchOpen(open, TOKEN, true), false, "an unknown or handed-off launch latches it closed");
  for (const change of [{ launchAvailable: false }, { launchReady: false }, { updateInProgress: true },
                        { recoveryPending: true }]) {
    assert.equal(launchOpen({ ...open, ...change }, TOKEN, false), false, JSON.stringify(change));
  }
  // Today's product status: the backend's one enablement point is off, so it is closed.
  assert.equal(launchOpen(update.parseStatus(STATUS), TOKEN, false), false);
});

test("the panel is mounted in Settings without any stop, restart or exit hook, and drops stale answers", () => {
  const settings = source("../src/components/SettingsView.tsx");
  assert.match(settings, /<UpdatePanel client=\{updateClient\} visible=\{phase === "settings" && activeSection === "update"\} \/>/);
  assert.match(settings, /createUpdateClient\(\(command, args\) => invoke\(command, args\)\)/);
  const panel = source("../src/components/UpdatePanel.tsx");
  assert.doesNotMatch(panel, /stopRuntime|restartRuntime|exitApp|start_runtime|stop_runtime/);
  assert.doesNotMatch(settings.slice(settings.indexOf("renderUpdate")), /stopRuntime|restartRuntime|exitApp/);
  assert.match(panel, /generation\.current \+= 1/);
  assert.match(panel, /if \(asked !== generation\.current\) return;/);
  assert.match(panel, /disabled=\{busy \|\| !launchOpen\(status, launchToken, outcomeUnknown \|\| handedOff\)\}/);
});

test("the panel offers discard and retry for an unconfirmed repair and never promises recovery for it", () => {
  const panel = source("../src/components/UpdatePanel.tsx");
  assert.match(panel, /status\?\.unconfirmed/);
  assert.match(panel, /client\.retry\(\)/);
  assert.match(panel, /شروع دوبارهٔ Qronos این وضعیت را تغییر نمی‌دهد/);
  assert.match(panel, /status\?\.outcome && !status\.unconfirmed/);
  assert.doesNotMatch(panel, /nonce|launchToken\}/);
});
