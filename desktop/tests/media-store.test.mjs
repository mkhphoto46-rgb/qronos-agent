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

test("visual hits are generated frame text with a frame SHA, joined to dialogue by time only", () => {
  const asset = "a_" + "3".repeat(32);
  const sha = "b".repeat(64);
  const block = media.visualBlockOf({ requestId: "r1", op: "search", ok: true, visual: { status: "complete", results: [
    { assetId: asset, timeMs: 5000, frameSha256: sha, caption: "A red bicycle.", visibleText: ["EXIT", 7],
      observations: ["bicycle"], verified: false, dialogue: [{ startMs: 4000, text: "Look at that red bicycle." }] },
    { assetId: asset, timeMs: 6000, frameSha256: "not-a-sha", caption: "x", verified: false },
    { assetId: asset, timeMs: 7000, frameSha256: sha, caption: "claims to be verified", verified: true },
  ] } });
  assert.equal(block.hits.length, 1);
  assert.deepEqual(plain(block.hits[0].visibleText), ["EXIT"]);
  assert.equal(block.hits[0].dialogue[0].text, "Look at that red bicycle.");
  const none = media.visualBlockOf({ requestId: "r2", op: "search", ok: true,
    visual: { status: "unavailable", reason: "no_visual_evidence", results: [] } });
  assert.match(none.note, /هنوز هیچ فریمی با مدل بینایی توصیف نشده است/);
  const panel = source("../src/components/MediaPanel.tsx");
  assert.match(panel, /OCR نیست/);
  assert.match(panel, /نه شباهت تصویر/);
  assert.match(panel, /تأییدنشده، نه واقعیت و نه شناسایی/);
  assert.match(panel, /"visual", "multimodal"\] as const/);
});

test("multimodal hits need a dialogue line AND an unverified frame; no-result outcomes and splits are explained", () => {
  const asset = "a_" + "5".repeat(32);
  const sha = "d".repeat(64);
  const line = { startMs: 60000, endMs: 64000, text: "My father died last winter.", channels: ["exact", "semantic", "guess"],
    track: { source: "embedded", language: "eng" } };
  const frame = { timeMs: 62000, frameSha256: sha, caption: "A small kitchen.", verified: false, channels: ["lexical"],
    relation: "during", gapMs: 0 };
  const block = media.multimodalBlockOf({ requestId: "r1", op: "search", ok: true, multimodal: { status: "complete",
    outcome: "matched", results: [
      { assetId: asset, startMs: 60000, endMs: 64000, dialogue: line, frames: [frame], framesInWindow: 1,
        alignment: { windowMs: 15000 } },
      { assetId: asset, startMs: 1, endMs: 2, dialogue: line, frames: [] },                                  // no frame
      { assetId: asset, startMs: 1, endMs: 2, frames: [frame] },                                            // no line
      { assetId: asset, startMs: 1, endMs: 2, dialogue: line, frames: [{ ...frame, verified: true }] },    // "verified"
      { assetId: asset, startMs: 1, endMs: 2, dialogue: line, frames: [{ ...frame, relation: "somewhere" }] },
    ] } });
  assert.equal(block.hits.length, 1);
  assert.deepEqual(plain(block.hits[0].dialogue.channels), ["exact", "semantic"]);
  assert.equal(block.hits[0].frames[0].relation, "during");
  assert.equal(block.hits[0].windowMs, 15000);
  assert.equal(block.note, null);
  const apart = media.multimodalBlockOf({ requestId: "r2", op: "search", ok: true, multimodal: {
    status: "complete", outcome: "same_asset_outside_window", results: [] } });
  assert.match(apart.note, /نه در فاصلهٔ زمانی خواسته‌شده/);
  const partial = media.multimodalBlockOf({ requestId: "r3", op: "search", ok: true, multimodal: {
    status: "partial", stoppedBy: "time_budget", outcome: "only_dialogue", results: [],
    dialogueChannel: { semantic: { requested: true, status: "unavailable", reason: "model_unavailable" } },
    visualChannel: { semantic: { requested: false, status: "not_requested" } } } });
  assert.match(partial.note, /فقط شرط گفتگو پیدا شد/);
  assert.match(partial.note, /ناقص ماند/);
  assert.equal(partial.channelNotes.length, 1);
  assert.match(partial.channelNotes[0], /فقط تطبیق واژه‌ای به کار رفت/);
  const none = media.multimodalBlockOf({ requestId: "r4", op: "search", ok: true,
    multimodal: { status: "unavailable", reason: "no_visual_evidence", results: [] } });
  assert.match(none.note, /جست‌وجوی ترکیبی در دسترس نیست/);
  const proposal = media.interpretationOf({ requestId: "r5", op: "search", ok: true,
    interpretation: { status: "proposed", dialogue: "father's death", visual: "kitchen", generated: true } });
  assert.deepEqual(plain(proposal), { status: "proposed", dialogue: "father's death", visual: "kitchen", generated: true, reason: null });
  assert.equal(media.interpretationOf({ requestId: "r6", op: "search", ok: true,
    interpretation: { status: "proposed", dialogue: "", visual: "kitchen" } }), null);
  assert.equal(media.interpretationOf({ requestId: "r7", op: "search", ok: true,
    interpretation: { status: "ambiguous", reason: "not_from_request" } }).reason, "not_from_request");
  assert.match(media.multimodalRequestProblem("father", " "), /هر دو بخش لازم است/);
  assert.equal(media.multimodalRequestProblem("father", "kitchen"), null);
  assert.deepEqual(plain(media.multimodalFields(" father ", "kitchen", 999, true, false)), {
    mode: "multimodal", dialogue: "father", visual: "kitchen", windowMs: 15000, dialogueMatch: "semantic",
    visualMatch: "lexical", limit: 20 });
  const panel = source("../src/components/MediaPanel.tsx");
  assert.match(panel, /نتیجه‌های ترکیبی \(هر کدام با هر دو شرط/);
  assert.match(panel, /پیشنهاد مغز سریع از واژه‌های خود شما \(تولیدشده، تأییدنشده\)/);
  assert.match(panel, /گوینده و OCR هنوز ساخته نشده‌اند/);
  assert.doesNotMatch(panel, /شباهت تصویر هنوز ساخته نشده/);  // image similarity exists now (round 79)
  assert.match(panel, /setResult\(null\);\s*if \(proposal\?\.status === "proposed"\)/);  // a split never searches
});

test("a search-result report names only the server-held resultId and its preview keeps frames unverified", () => {
  for (const op of ["search.report.preview", "search.report.generate", "search.report.export"]) assert.ok(media.MEDIA_OPS.has(op));
  const held = media.multimodalBlockOf({ requestId: "r1", op: "search", ok: true,
    multimodal: { status: "complete", outcome: "neither", results: [], resultId: "Ab_-0123456789abcdefXYZ" } });
  assert.equal(held.resultId, "Ab_-0123456789abcdefXYZ");
  const forged = media.multimodalBlockOf({ requestId: "r2", op: "search", ok: true,
    multimodal: { status: "complete", results: [], resultId: "../../x" } });
  assert.equal(forged.resultId, null);
  const preview = media.searchReportPreviewOf({ requestId: "r3", op: "search.report.preview", ok: true, preview: {
    kind: "search", status: "partial", searchedAt: "2026-10-14T00:00:00+00:00",
    query: { dialogue: "father died", visual: "kitchen", windowMs: 15000 },
    recheck: { kept: 1, outOfScope: 2, permission: 0, changed: 1 },
    results: [
      { title: "Harbour", dialogue: { startMs: 60000, text: "My father died." },
        frames: [{ timeMs: 62000, caption: "A kitchen.", relation: "during", gapMs: 0, verified: false },
                 { timeMs: 63000, caption: "claims verified", relation: "during", gapMs: 0, verified: true }] },
      { title: "NoFrame", dialogue: { startMs: 1, text: "x" }, frames: [] },
    ],
    resultsOmitted: 0, limitations: ["این گزارش از نتیجهٔ ذخیره‌شدهٔ همین جست‌وجو ساخته شده است", 7] } });
  assert.equal(preview.results.length, 1);
  assert.deepEqual(plain(preview.results[0].frames.map((f) => f.caption)), ["A kitchen."]);
  assert.deepEqual(plain(preview.recheck), { kept: 1, outOfScope: 2, permission: 0, changed: 1 });
  assert.equal(preview.limitations.length, 1);
  assert.equal(media.searchReportPreviewOf({ requestId: "r4", op: "report.preview", ok: true, preview: { kind: "full" } }), null);
  assert.match(media.refusalText("result_unavailable"), /دوباره جست‌وجو کنید/);
  const panel = source("../src/components/MediaPanel.tsx");
  assert.match(panel, /run\("search\.report\.preview", \{ resultId \}\)/);
  assert.match(panel, /run\("search\.report\.generate", \{ resultId, format: reportFormat \}\)/);
  assert.match(panel, /run\("search\.report\.export", \{ resultId, destination: destination\.trim\(\), format: reportFormat \}\)/);
  assert.match(panel, /پیش‌نمایش گزارش همین نتیجه‌ها/);
  assert.match(panel, /\(توصیف تولیدشده، تأییدنشده\)/);
});

test("semantic visual hits say their channel and an uncalibrated text similarity; a missing model is a note", () => {
  const asset = "a_" + "4".repeat(32);
  const sha = "c".repeat(64);
  const block = media.visualBlockOf({ requestId: "r1", op: "search", ok: true, visual: { status: "complete",
    semantic: { status: "unavailable", reason: "model_unavailable" }, results: [
      { assetId: asset, timeMs: 5000, frameSha256: sha, caption: "A red bicycle.", verified: false,
        channels: ["lexical", "semantic", "image"], similarity: 0.71 },
      { assetId: asset, timeMs: 9000, frameSha256: sha, caption: "A street.", verified: false, channels: ["semantic"],
        similarity: "high" },
    ] } });
  assert.deepEqual(plain(block.hits[0].channels), ["lexical", "semantic"]);
  assert.equal(block.hits[0].similarity, 0.71);
  assert.equal(block.hits[1].similarity, null);
  assert.match(block.semanticNote, /نتیجه‌های واژه‌ای همچنان معتبرند/);
  const panel = source("../src/components/MediaPanel.tsx");
  assert.match(panel, /نه شباهت تصویر/);
  assert.match(panel, /کالیبره‌نشده/);
});

test("image-similarity hits are their own channel with an uncalibrated cosine and a reason when unavailable", () => {
  const asset = "a_" + "6".repeat(32);
  const sha = "e".repeat(64);
  const image = { status: "complete", results: [
    { assetId: asset, timeMs: 5000, frameSha256: sha, channels: ["image"], imageSimilarity: 0.31, caption: null,
      generated: false, verified: false, dialogue: [{ startMs: 4000, text: "Look at that car." }] },
    { assetId: asset, timeMs: 6000, frameSha256: sha, channels: ["image"], imageSimilarity: 0.2, caption: "A red car.",
      generated: true, verified: false },
    { assetId: asset, timeMs: 7000, frameSha256: sha, channels: ["image"], imageSimilarity: 0.2, verified: true },
    { assetId: asset, timeMs: 8000, frameSha256: sha, channels: ["lexical"], imageSimilarity: 0.2, verified: false },
    { assetId: asset, timeMs: 9000, frameSha256: sha, channels: ["image"], imageSimilarity: "high", verified: false },
  ] };
  const block = media.visualBlockOf({ requestId: "r1", op: "search", ok: true,
    visual: { status: "unavailable", reason: "no_visual_evidence", results: [], image } });
  assert.equal(block.hits.length, 0);
  assert.equal(block.imageHits.length, 2);
  assert.equal(block.imageHits[0].caption, null);
  assert.equal(block.imageHits[0].dialogue[0].text, "Look at that car.");
  assert.equal(block.imageHits[1].caption, "A red car.");
  assert.equal(block.imageNote, null);
  const missing = media.visualBlockOf({ requestId: "r2", op: "search", ok: true, visual: { status: "complete",
    results: [], image: { status: "unavailable", reason: "image_encoder_not_provisioned", results: [] } } });
  assert.match(missing.imageNote, /SigLIP 2/);
  assert.match(missing.imageNote, /خودش دانلود نمی‌کند/);
  const frame = { timeMs: 62000, frameSha256: sha, caption: null, verified: false, channels: ["image"],
    imageSimilarity: 0.28, generated: false, relation: "during", gapMs: 0 };
  const line = { startMs: 60000, endMs: 64000, text: "The sea is calm today.", channels: ["exact"],
    track: { source: "embedded", language: "eng" } };
  const mm = media.multimodalBlockOf({ requestId: "r3", op: "search", ok: true, multimodal: { status: "complete",
    outcome: "matched", results: [
      { assetId: asset, startMs: 60000, endMs: 64000, dialogue: line, frames: [frame], alignment: { windowMs: 5000 } },
      { assetId: asset, startMs: 1, endMs: 2, dialogue: line, frames: [{ ...frame, channels: ["lexical"] }] },  // no text
    ] } });
  assert.equal(mm.hits.length, 1);
  assert.deepEqual(plain(mm.hits[0].frames[0].channels), ["image"]);
  assert.equal(mm.hits[0].frames[0].generated, false);
  assert.equal(mm.hits[0].frames[0].imageSimilarity, 0.28);
  const fallback = media.multimodalBlockOf({ requestId: "r4", op: "search", ok: true, multimodal: {
    status: "partial", outcome: "neither", results: [],
    visualChannel: { image: { requested: true, status: "unavailable", reason: "image_encoder_unavailable" } } } });
  assert.match(fallback.channelNotes[0], /فقط متن تولیدشدهٔ فریم‌ها به کار رفت/);
  assert.deepEqual(plain(media.multimodalFields("sea", "blue", 5000, false, "image")), {
    mode: "multimodal", dialogue: "sea", visual: "blue", windowMs: 5000, dialogueMatch: "exact",
    visualMatch: "image", limit: 20 });
  assert.equal(media.multimodalFields("sea", "blue", 5000, false, "anything").visualMatch, "lexical");
  const panel = source("../src/components/MediaPanel.tsx");
  assert.match(panel, /شباهت تصویری فریم‌ها \(بردار تصویر خود فریم با بردار متن پرسش؛ SigLIP 2، کالیبره‌نشده، نه شناسایی\)/);
  assert.match(panel, /<option value="image">شباهت تصویری خود فریم‌ها \(SigLIP 2، کالیبره‌نشده\)<\/option>/);
  assert.match(panel, /پیداشده با شباهت تصویری خود این فریم ذخیره‌شده \(نه از روی توصیف آن\)/);
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
