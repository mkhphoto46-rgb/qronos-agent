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
    Promise, Set, Map, Error, RegExp, Array, Object, JSON, String, Number,
  }, { timeout: 1000 });
  return exports;
}

const media = load("../src/mediaStore.ts");

// Values made in the vm realm, compared as plain JSON (no cross-realm prototype mismatch).
const plain = (value) => JSON.parse(JSON.stringify(value));

function fakeTransport() {
  const sent = [];
  const timers = new Map();
  let handler = null;
  let nextTimer = 1;
  const transport = {
    send: async (actionId, data) => { sent.push({ actionId, data: JSON.parse(JSON.stringify(data)) }); },
    listen: async (h) => { handler = h; return () => { handler = null; }; },
    setTimer: (callback, ms) => { const id = nextTimer++; timers.set(id, { callback, ms }); return id; },
    clearTimer: (id) => { timers.delete(id); },
  };
  const deliver = (eventType, payload) => handler && handler({ eventType, status: "ready",
    message: typeof payload === "string" ? payload : JSON.stringify(payload) });
  const fire = () => { for (const [id, t] of [...timers]) { timers.delete(id); t.callback(); } };
  return { transport, sent, deliver, fire, timers, listening: () => handler !== null };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

test("a request is one typed qronos.media action and resolves on ITS answer only", async () => {
  const fake = fakeTransport();
  const client = media.createMediaClient(fake.transport);
  const answer = client.request("search", { query: "key", mode: "semantic", limit: 5, op: "status", requestId: "forged" });
  await tick();
  assert.equal(fake.sent.length, 1);
  const { actionId, data } = fake.sent[0];
  assert.equal(actionId, "qronos.media");
  assert.equal(data.op, "search");  // fields can never override op or requestId
  assert.notEqual(data.requestId, "forged");
  assert.match(data.requestId, /^[A-Za-z0-9_-]{1,64}$/);
  fake.deliver("media_result", { requestId: "someone-else", op: "search", ok: true });  // foreign: ignored
  fake.deliver("media_result", { requestId: data.requestId, op: "status", ok: true });  // wrong op: ignored
  assert.equal(client.pending(), 1);
  fake.deliver("media_result", { requestId: data.requestId, op: "search", ok: true, results: { results: [] } });
  const result = await answer;
  assert.equal(result.ok, true);
  assert.equal(client.pending(), 0);
});

test("unknown operations and approval ops through request are refused before sending", async () => {
  const fake = fakeTransport();
  const client = media.createMediaClient(fake.transport);
  await assert.rejects(client.request("format_disk"));
  assert.throws(() => client.request("decide", { approvalId: "x".repeat(20), approved: true }));
  assert.equal(fake.sent.length, 0);
});

test("malformed, oversized or non-media events are ignored", () => {
  assert.equal(media.parseMediaEvent({ eventType: "media_result", status: "ready", message: "not json" }), null);
  assert.equal(media.parseMediaEvent({ eventType: "media_result", status: "ready",
    message: JSON.stringify({ requestId: "bad id!", op: "status", ok: true }) }), null);
  assert.equal(media.parseMediaEvent({ eventType: "media_result", status: "ready",
    message: JSON.stringify({ requestId: "r1", op: "drop_table", ok: true }) }), null);
  assert.equal(media.parseMediaEvent({ eventType: "media_result", status: "ready",
    message: JSON.stringify({ requestId: "r1", op: "status", ok: "yes" }) }), null);
  assert.equal(media.parseMediaEvent({ eventType: "media_result", status: "ready",
    message: "x".repeat(media.MAX_EVENT_CHARS + 1) }), null);
  assert.equal(media.parseMediaEvent({ eventType: "queue_snapshot", status: "ready", message: "{}" }), null);
  assert.ok(media.parseMediaEvent({ eventType: "media_progress", status: "ready", message: "{}" }));
});

test("reset drops pending answers as stale and ignores the late ones", async () => {
  const fake = fakeTransport();
  const client = media.createMediaClient(fake.transport);
  const answer = client.request("status");
  await tick();
  const { requestId } = fake.sent[0].data;
  client.reset();
  assert.equal((await answer).code, "stale");
  fake.deliver("media_result", { requestId, op: "status", ok: true });  // late: nobody waits for it
  assert.equal(client.pending(), 0);
});

test("no answer in time resolves as timeout; a failed send resolves as closed", async () => {
  const fake = fakeTransport();
  const client = media.createMediaClient(fake.transport, { timeoutMs: 10 });
  const answer = client.request("status");
  await tick();
  fake.fire();
  assert.equal((await answer).code, "timeout");
  const failing = fakeTransport();
  failing.transport.send = async () => { throw new Error("runtime not running"); };
  const closed = media.createMediaClient(failing.transport);
  assert.equal((await closed.request("status")).code, "closed");
});

test("an approval is validated and the secret goes to the one decide send only", async () => {
  const fake = fakeTransport();
  const client = media.createMediaClient(fake.transport);
  const asked = client.request("roots.add", { path: "D:\\Films", kind: "folder", maxDepth: 8 });
  await tick();
  const first = fake.sent[0].data;
  fake.deliver("media_result", { requestId: first.requestId, op: "roots.add", ok: true, status: "approval_required",
    approvalId: "approval-0123456789", needsSecret: true, preview: { path: "D:\\Films" } });
  const approval = media.approvalOf(await asked);
  assert.deepEqual(JSON.parse(JSON.stringify(approval)), { approvalId: "approval-0123456789", needsSecret: true,
    preview: { path: "D:\\Films" }, op: "roots.add" });
  assert.equal(media.approvalOf({ requestId: "r", op: "roots.add", ok: true, status: "approval_required",
    approvalId: "../x" }), null);
  const decided = client.decide(approval, "correct horse");
  await tick();
  const decide = fake.sent[1].data;
  assert.deepEqual(Object.keys(decide).sort(), ["approvalId", "approved", "op", "requestId", "secret"]);
  fake.deliver("media_result", { requestId: decide.requestId, op: "decide", ok: true, status: "added" });
  await decided;
  const declined = client.decline({ ...approval, needsSecret: false });
  await tick();
  assert.deepEqual(Object.keys(fake.sent[2].data).sort(), ["approvalId", "op", "requestId"]);
  fake.deliver("media_result", { requestId: fake.sent[2].data.requestId, op: "decline", ok: true });
  await declined;
  // No retained copy of the secret anywhere the client exposes.
  assert.ok(!JSON.stringify(Object.entries(client)).includes("correct horse"));
});

test("refusal codes have Persian text and unknown codes are shown as codes", () => {
  assert.match(media.refusalText("model_unavailable"), /bge-m3/);
  assert.match(media.refusalText("request_unresolved"), /مدل/);
  assert.match(media.refusalText("weird_code"), /weird_code/);
});

test("reports: PDF and every kind are offered, the preview op is allowed, and an export names its format", () => {
  assert.ok(media.MEDIA_OPS.has("report.preview") && media.MEDIA_OPS.has("report.export"));
  assert.deepEqual([...media.REPORT_FORMATS], ["pdf", "html", "json", "csv"]);
  assert.deepEqual([...media.REPORT_KINDS], ["short", "full", "transcript", "evidence"]);
  assert.equal(media.exportDestinationProblem("C:\\Reports\\film.PDF", "pdf"), null);
  assert.match(media.exportDestinationProblem("C:\\Reports\\film.html", "pdf"), /\.pdf/);
  assert.match(media.exportDestinationProblem("  ", "csv"), /مسیر/);
  assert.match(media.refusalText("report_unavailable"), /PDF/);
  const panel = source("../src/components/MediaPanel.tsx");
  // The PDF is made by the media host from the index; the panel never prints HTML or opens a print dialog.
  for (const forbidden of ["window.print", "print(", "iframe", "dangerouslySetInnerHTML"]) {
    assert.ok(!panel.includes(forbidden), forbidden);
  }
  assert.match(panel, /run\("report\.export", \{ assetId, destination: destination\.trim\(\), format: reportFormat, kind: reportKind \}\)/);
  assert.match(panel, /run\("report\.preview", \{ assetId, kind: reportKind \}\)/);
});

test("topic search: hits must carry indexed lines, hints are labelled hints, and the panel offers the mode", () => {
  const asset = "a_" + "1".repeat(32);
  const block = media.topicBlockOf({
    requestId: "r1", op: "search", ok: true,
    topic: {
      status: "complete",
      expansion: { applied: true, hints: ["trusted", 7, "brother"] },
      results: [
        { assetId: asset, title: "Film", startMs: 500, endMs: 34000, source: "embedded", score: 0.71,
          components: { segment: 0.8, evidence: 0.9, terms: 0, expansion: 0.6 },
          evidence: [{ startMs: 500, endMs: 4000, text: "You lied to me for three years." }] },
        { assetId: asset, startMs: 1, endMs: 2, evidence: [] },
        { assetId: "../x", startMs: 1, endMs: 2, evidence: [{ startMs: 1, text: "forged" }] },
      ],
    },
  });
  assert.equal(block.hits.length, 1);  // no lines or a bad asset id: dropped
  assert.equal(block.hits[0].evidence[0].text, "You lied to me for three years.");
  assert.deepEqual(plain(block.hints), ["trusted", "brother"]);
  assert.equal(block.expanded, true);
  const off = media.topicBlockOf({ requestId: "r2", op: "search", ok: true,
    topic: { status: "unavailable", reason: "model_unavailable", results: [] } });
  assert.match(off.note, /bge-m3/);
  const panel = source("../src/components/MediaPanel.tsx");
  assert.match(panel, /"exact", "fuzzy", "semantic", "topic"/);
  assert.match(panel, /راهنمای جست‌وجو، نه واقعیت/);  // hints are shown as hints, never as facts
  assert.match(panel, /امتیاز رتبه با وزن‌های ثابت؛ احتمال یا درصد نیست/);
});

test("generated labels and brain hints are shown only as generated text with the indexed lines they cite", () => {
  const asset = "a_" + "2".repeat(32);
  const line = { startMs: 500, endMs: 4000, text: "You lied to me for three years." };
  const block = media.topicBlockOf({ requestId: "r1", op: "search", ok: true, topic: {
    status: "complete", brainHints: { requested: true, applied: true, hints: ["you lied to me", 3] },
    results: [
      { assetId: asset, startMs: 500, endMs: 34000, evidence: [line],
        generated: { label: "اعتماد", summary: "دربارهٔ دروغ", cites: [{ lineId: "L7", startMs: 500 }] } },
      { assetId: asset, startMs: 1, endMs: 2, evidence: [line], generated: { label: "x", summary: "y", cites: [] } },
    ] } });
  assert.equal(block.hits[0].generated.label, "اعتماد");
  assert.equal(block.hits[1].generated, null);  // a label without cited lines is not shown
  assert.deepEqual(plain(block.brainHints), ["you lied to me"]);
  const declined = media.topicBlockOf({ requestId: "r2", op: "search", ok: true, topic: { status: "complete",
    brainHints: { requested: true, applied: false, reason: "model_unavailable" }, results: [] } });
  assert.equal(declined.brainHintsReason, "model_unavailable");
  const panel = source("../src/components/MediaPanel.tsx");
  assert.match(panel, /تأییدنشده، نقل‌قول نیست/);
  assert.match(panel, /راهنمای جست‌وجو، نه واقعیت و نه نتیجه/);
  assert.match(panel, /brainHints: true/);
});

test("the panel uses the registered action transport, clears the secret and starts no runtime itself", () => {
  const panel = source("../src/components/MediaPanel.tsx");
  assert.match(panel, /invoke\("send_runtime_action", \{ actionId, data \}\)/);
  assert.match(panel, /"qronos:\/\/runtime-event"/);
  assert.match(panel, /setSecret\(""\); \/\/ cleared before anything is sent/);
  assert.match(panel, /if \(!visible\) \{\s*generation\.current \+= 1;\s*client\.reset\(\);\s*setSecret\(""\);/);
  for (const forbidden of ["start_runtime", "stop_runtime", "update_command", "Command.create", "shell", "localStorage"]) {
    assert.ok(!panel.includes(forbidden), forbidden);
  }
  assert.match(panel, /شباهت کسینوسی؛ احتمال یا درصد نیست/);  // semantic scores are labelled uncalibrated
});
