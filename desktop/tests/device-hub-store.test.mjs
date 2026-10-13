import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import nodeTest from "node:test";
import vm from "node:vm";
import ts from "typescript";

const test = (name, fn) => nodeTest(name, { timeout: 5000 }, fn);

// The real module, compiled exactly as shipped, run in an isolated context with no imports.
function load(relative) {
  const compiled = ts.transpileModule(readFileSync(new URL(relative, import.meta.url), "utf8"), {
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

const hub = load("../src/deviceHubStore.ts");
const DEVICE = "prn-0123456789abcdef0123";

function fakeTransport() {
  const sent = [];
  const timers = new Map();
  let handler = null;
  let nextTimer = 1;
  const transport = {
    send: async (actionId, data) => { sent.push({ actionId, data: JSON.parse(JSON.stringify(data)) }); },
    listen: async (h) => { handler = h; return () => { handler = null; }; },
    setTimer: (callback) => { const id = nextTimer++; timers.set(id, callback); return id; },
    clearTimer: (id) => { timers.delete(id); },
  };
  const deliver = (payload) => handler && handler({ eventType: "device_hub_result", status: "ready",
    message: JSON.stringify(payload) });
  const fire = () => { for (const [id, callback] of [...timers]) { timers.delete(id); callback(); } };
  return { transport, sent, deliver, fire };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
// Values built inside the vm context belong to another realm; compare them as plain JSON.
const plain = (value) => JSON.parse(JSON.stringify(value));

test("each operation is one typed qronos.device_hub action resolved by ITS answer only", async () => {
  const fake = fakeTransport();
  const client = hub.createDeviceHubClient(fake.transport);
  const answer = client.jobs(DEVICE);
  await tick();
  const [{ actionId, data }] = fake.sent;
  assert.equal(actionId, "qronos.device_hub");
  assert.deepEqual(Object.keys(data).sort(), ["deviceId", "op", "requestId"]);
  assert.equal(data.op, "device.jobs");
  fake.deliver({ requestId: "someone-else", op: "device.jobs", ok: true, jobs: [] });  // foreign: ignored
  fake.deliver({ requestId: data.requestId, op: "devices.list", ok: true });          // wrong op: ignored
  assert.equal(client.pending(), 1);
  fake.deliver({ requestId: data.requestId, op: "device.jobs", ok: true,
                 jobs: [{ jobId: 7, document: "a.pdf", own: true, status: ["printing"], pagesPrinted: 1, totalPages: 3 },
                        { jobId: "x" }] });
  const result = await answer;
  assert.deepEqual(plain(hub.jobsOf(result).map((job) => [job.jobId, job.own])), [[7, true]]);
});

test("malformed requests are refused locally and nothing is sent", async () => {
  const fake = fakeTransport();
  const client = hub.createDeviceHubClient(fake.transport);
  for (const result of await Promise.all([
    client.jobs("../../x"),
    client.controlJob(DEVICE, 0, "cancel"),
    client.controlJob(DEVICE, 7, "delete_all"),
    client.print({ deviceId: DEVICE, path: "C:\\a.pdf", copies: 1, color: "rainbow", pages: "all" }),
    client.print({ deviceId: DEVICE, path: "C:\\a.pdf", copies: 1, color: "default", pages: "1;2" }),
  ])) {
    assert.equal(result.ok, false);
    assert.equal(result.code, "bad_request");
  }
  assert.equal(fake.sent.length, 0);
});

test("the typed secret goes only to the one decide send and only when it is needed", async () => {
  const fake = fakeTransport();
  const client = hub.createDeviceHubClient(fake.transport);
  void client.decide({ approvalId: "abcdefgh1234", needsSecret: true, preview: {} }, "correct horse");
  void client.decide({ approvalId: "abcdefgh5678", needsSecret: false, preview: {} }, "not needed");
  void client.decline({ approvalId: "abcdefgh9999", needsSecret: true, preview: {} });
  await tick();
  assert.deepEqual(fake.sent.map(({ data }) => [data.op, data.secret]),
                   [["decide", "correct horse"], ["decide", undefined], ["decline", undefined]]);
});

test("no answer in time is a timeout and a reset makes pending answers stale", async () => {
  const fake = fakeTransport();
  const client = hub.createDeviceHubClient(fake.transport);
  const first = client.list();
  await tick();
  fake.fire();
  assert.equal((await first).code, "timeout");
  const second = client.list();
  await tick();
  client.reset();
  assert.equal((await second).code, "stale");
  fake.deliver({ requestId: fake.sent[1].data.requestId, op: "devices.list", ok: true, devices: [] });  // late
  assert.equal(client.pending(), 0);
});

test("devices are validated field by field and the print form follows the device's real limits", () => {
  const devices = hub.devicesOf({ requestId: "r", op: "devices.list", ok: true, devices: [
    { id: DEVICE, name: "Office Laser", kind: "printer", connection: "local", isDefault: true, driver: "HP",
      status: { problems: [], warnings: [], ready: true }, queuedJobs: 1,
      capabilities: { copiesMax: 9, color: false, duplex: true, paperSizes: ["A4"] },
      workflows: ["print_document", "jobs_view", "job_control_own"], unsupported: "" },
    { id: "prn-ffffffffffffffffffff", name: "Prusa 3D", kind: "3d_printer", connection: "local",
      status: { ready: true }, capabilities: {}, workflows: [], unsupported: "3d_workflow_pending" },
    { id: "not-a-device", name: "forged" },
  ] });
  assert.deepEqual(plain(devices.map((device) => [device.name, device.kind])), [["Office Laser", "printer"], ["Prusa 3D", "3d_printer"]]);
  const [laser, prusa] = devices;
  const fields = { deviceId: laser.id, path: "C:\\docs\\a.pdf", copies: 2, color: "default", pages: "1-3,5" };
  assert.equal(hub.printProblem(laser, fields), null);
  assert.match(hub.printProblem(laser, { ...fields, copies: 10 }), /حداکثر 9/);
  assert.match(hub.printProblem(laser, { ...fields, color: "color" }), /رنگی/);
  assert.match(hub.printProblem(laser, { ...fields, path: "relative.pdf" }), /مسیر کامل/);
  assert.equal(hub.printProblem(prusa, { ...fields, deviceId: prusa.id }), hub.DEVICE_REFUSAL_TEXT.unsupported);
  assert.equal(hub.approvalOf({ requestId: "r", op: "job.control", ok: true, status: "approval_required",
                                approvalId: "short" }), null);
  assert.equal(hub.refusalText("not_own_job"), hub.DEVICE_REFUSAL_TEXT.not_own_job);
});

test("a 3D device is driven only by its driver's proof, and success means only a spooler handoff", async () => {
  const proven = { id: DEVICE, name: "Workshop", kind: "3d_printer", workflows: ["print_3d", "jobs_view"],
    unsupported: "", capabilities: { buildVolumeMicrons: { width: 250000, depth: 210000, height: 220000 },
      threeMfVersion: "http://schemas.microsoft.com/3dmanufacturing/core/2015/02", threeMfExtensions: ["urn:m"] } };
  const unproven = { ...proven, id: "prn-ffffffffffffffffffff", name: "Prusa 3D", workflows: [],
    unsupported: "3d_capability_unproven", capabilities: { buildVolumeMicrons: { width: -1, depth: 2, height: 3 } } };
  const [ok, named] = hub.devicesOf({ requestId: "r", op: "devices.list", ok: true, devices: [proven, unproven] });
  assert.deepEqual(plain(ok.capabilities.buildVolumeMicrons), { width: 250000, depth: 210000, height: 220000 });
  assert.equal(named.capabilities.buildVolumeMicrons, null);  // malformed dimensions are dropped, not guessed
  assert.match(hub.limitText(named.unsupported), /PrintCapabilities/);
  assert.equal(hub.print3dProblem(named, { deviceId: named.id, path: "D:/m/a.3mf" }), hub.DEVICE_REFUSAL_TEXT.unsupported);
  assert.notEqual(hub.print3dProblem(ok, { deviceId: ok.id, path: "D:/m/a.stl" }), null);
  assert.notEqual(hub.print3dProblem(ok, { deviceId: ok.id, path: "a.3mf" }), null);
  assert.equal(hub.print3dProblem(ok, { deviceId: ok.id, path: " D:/m/a.3mf " }), null);
  const fake = fakeTransport();
  const client = hub.createDeviceHubClient(fake.transport);
  assert.equal((await client.print3d({ deviceId: DEVICE, path: "D:/m/a.stl" })).code, "bad_request");
  assert.equal(fake.sent.length, 0);
  const answer = client.print3d({ deviceId: DEVICE, path: " D:/m/a.3mf " });
  await tick();
  const [{ data }] = fake.sent;
  assert.deepEqual(Object.keys(data).sort(), ["deviceId", "op", "path", "requestId"]);
  assert.equal(data.op, "print3d.request");
  assert.equal(data.path, "D:/m/a.3mf");
  fake.deliver({ requestId: data.requestId, op: "print3d.request", ok: true, status: "approval_required",
                 approvalId: "approval-123456", needsSecret: true, preview: { jobName: "Qronos 3D ab" } });
  const approval = hub.approvalOf(await answer);
  assert.equal(approval.needsSecret, true);
  const text = hub.handoffText({ requestId: "r", op: "decide", ok: true, status: "handed_to_spooler", jobId: 41 });
  assert.match(text, /سپرده شد/);
  assert.match(text, /41/);
  assert.match(text, /نیست/);  // it says that a handoff is not a printed part
});

test("wake: exact local time with its offset, bounded fields, truthful words and no claim that the PC woke", async () => {
  assert.equal(hub.isoWithOffset("2026-10-14T06:30", 210), "2026-10-14T06:30:00+03:30");
  assert.equal(hub.isoWithOffset("2026-10-14T06:30", -300), "2026-10-14T06:30:00-05:00");
  assert.equal(hub.isoWithOffset("2026-10-14 06:30", 0), null);
  assert.equal(hub.isoWithOffset("2026-10-14T06:30", 15 * 60), null);
  const fake = fakeTransport();
  const client = hub.createDeviceHubClient(fake.transport);
  assert.equal((await client.wakeSchedule("2026-10-14T06:30", 10)).code, "bad_request");  // no offset: refused here
  assert.equal((await client.wakeSchedule("2026-10-14T06:30:00+03:30", 121)).code, "bad_request");
  assert.equal(fake.sent.length, 0);
  const answer = client.wakeSchedule("2026-10-14T06:30:00+03:30", 15);
  await tick();
  const [{ data }] = fake.sent;
  assert.deepEqual(Object.keys(data).sort(), ["at", "op", "requestId", "stayAwakeMinutes"]);
  assert.equal(data.op, "wake.schedule");
  fake.deliver({ requestId: data.requestId, op: "wake.schedule", ok: true, status: "approval_required",
                 approvalId: "approval-wake-1", needsSecret: true, preview: { action: "wake_schedule" } });
  assert.equal(hub.approvalOf(await answer).needsSecret, true);
  const status = { requestId: "r", op: "wake.status", ok: true,
    capability: { verdict: "blocked_by_power_plan", reason: "wake_timers_important_only", modernStandby: true,
                  wakeTimersAc: "important_only", wakeTimersDc: "disabled", powerSource: "battery" },
    armed: { wakeId: "wk-0123456789abcdef", dueUtc: "2026-10-14T03:00:00+00:00", stayAwakeMinutes: 15, state: "armed" } };
  const capability = hub.wakeCapabilityOf(status);
  assert.equal(capability.verdict, "blocked_by_power_plan");
  assert.match(hub.WAKE_VERDICT_TEXT[capability.reason], /زمان‌سنج‌های مهم/);
  assert.equal(hub.wakeCapabilityOf({ ...status, capability: { verdict: "always" } }), null);  // unknown verdict dropped
  const armed = hub.wakeArmedOf(status);
  const words = hub.wakeArmedText(armed, "۱۴۰۵/۷/۲۲");
  assert.match(words, /پذیرفت/);
  assert.match(words, /نه اینکه کامپیوتر حتماً بیدار می‌شود/);
  assert.match(hub.wakeArmedText({ ...plain(armed), state: "fired" }, "x"), /نه اینکه کامپیوتر از خواب بیدار شد/);
  assert.equal(Object.keys(hub.WAKE_LIMIT_TEXT).length, 6);
  assert.match(hub.WAKE_LIMIT_TEXT.no_voice_or_brain_while_asleep, /صدا/);
  for (const code of ["bad_time", "already_armed", "wake_not_supported", "wake_native_refused"]) {
    assert.ok(hub.DEVICE_REFUSAL_TEXT[code], code);
  }
});

test("scan: only WIA scanners qualify, exact new png/jpg, typed fields, truthful saved words", async () => {
  const listed = { requestId: "r", op: "imaging.list", ok: true, devices: [
    { id: "img-0123456789abcdef0123", name: "Office Scanner", vendor: "Acme", kind: "scanner",
      workflows: ["scan_to_file"], unsupported: "" },
    { id: "img-ffffffffffffffffffff", name: "Old Camera", vendor: "Cam", kind: "camera", workflows: ["scan_to_file"],
      unsupported: "wia_camera_not_supported" },
    { id: "../x", name: "bad", kind: "scanner" },
  ] };
  const [scannerDevice, camera, ...rest] = hub.imagingDevicesOf(listed);
  assert.equal(rest.length, 0);  // a malformed id is dropped
  assert.deepEqual(plain(camera.workflows), []);  // a camera never gets the scan workflow, whatever is sent
  assert.match(hub.IMAGING_UNSUPPORTED_TEXT[camera.unsupported], /WIA 2.0/);
  assert.equal(hub.scanProblem(camera, "D:/s/a.png"), "یک اسکنر پشتیبانی‌شده انتخاب کنید.");
  assert.notEqual(hub.scanProblem(scannerDevice, "D:/s/a.gif"), null);
  assert.notEqual(hub.scanProblem(scannerDevice, "a.png"), null);
  assert.equal(hub.scanProblem(scannerDevice, " D:/s/a.JPG "), null);
  const fake = fakeTransport();
  const client = hub.createDeviceHubClient(fake.transport);
  assert.equal((await client.scan("img-0123456789abcdef0123", "D:/s/a.tif")).code, "bad_request");
  assert.equal((await client.scan("../x", "D:/s/a.png")).code, "bad_request");
  assert.equal(fake.sent.length, 0);
  const answer = client.scan("img-0123456789abcdef0123", " D:/s/a.png ");
  await tick();
  const [{ data }] = fake.sent;
  assert.deepEqual(Object.keys(data).sort(), ["destination", "deviceId", "op", "requestId"]);
  assert.equal(data.op, "image.capture");
  assert.equal(data.destination, "D:/s/a.png");
  fake.deliver({ requestId: data.requestId, op: "image.capture", ok: true, status: "approval_required",
                 approvalId: "approval-scan-1", needsSecret: true, preview: { action: "scan" } });
  assert.equal(hub.approvalOf(await answer).needsSecret, true);
  const saved = hub.scanSavedText({ requestId: "r", op: "decide", ok: true, status: "saved", path: "D:/s/a.png",
                                    width: 64, height: 48, format: "PNG" });
  assert.match(saved, /ذخیره شد/);
  assert.match(saved, /64×48/);
  assert.equal(Object.keys(hub.SCAN_LIMIT_TEXT).length, 6);
  for (const code of ["scan_cancelled", "not_one_image", "not_an_image", "destination_exists", "bad_destination"]) {
    assert.ok(hub.DEVICE_REFUSAL_TEXT[code], code);
  }
  assert.doesNotMatch(hub.DEVICE_REFUSAL_TEXT.too_large, /بسته/);  // neutral now that scans use it too
});
