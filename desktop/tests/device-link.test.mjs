import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import nodeTest from "node:test";
import vm from "node:vm";
import ts from "typescript";

const test = (name, fn) => nodeTest(name, { timeout: 5000 }, fn);

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
  vm.runInNewContext(compiled, {
    exports,
    require(name) {
      assert.ok(Object.hasOwn(stubs, name), `Unexpected import: ${name}`);
      return stubs[name];
    },
    console, Promise, Set, JSON, Number, Object, Array, Math, Date, Error, URLSearchParams,
    TextEncoder, Uint8Array, Infinity, RangeError, String,
  }, { timeout: 2000 });
  return exports;
}

const link = load(compile("../src/deviceLink.ts"));
const qr = load(compile("../src/qrCode.ts"));
const plain = (value) => JSON.parse(JSON.stringify(value));

const ID_A = "0123456789abcdef";
const ID_B = "fedcba9876543210";
const NONCE = "a".repeat(32);
const URI = `qronos://pair?v=1&h=192.168.1.20&p=47711&d=${ID_A}&k=${"K".repeat(43)}&e=1000120`;

const statusMessage = (overrides = {}) => JSON.stringify({
  enabled: true, running: true, port: 47711, remote_active: false,
  remote_unavailable: "remote_access_off", parked_devices: 0, pairing_open: false, ...overrides,
});
const device = (id, overrides = {}) => ({
  device_id: id, name: "گوشی سارا", status: "active", created_at: 1000, last_seen_at: 1001,
  paired_from: "", remote_enabled: false, remote_expires_at: null, remote_allowed: false, ...overrides,
});

function harness({ failSend = false, now = 1000 } = {}) {
  const sent = [];
  const saved = [];
  const errors = [];
  const clock = { now };
  const controller = new link.DeviceLinkController({
    send: async (actionId, data) => {
      sent.push({ actionId, data });
      if (failSend) throw new Error("bridge closed");
    },
    saveEnabled: async (enabled) => { saved.push(enabled); },
    nonce: () => NONCE,
    now: () => clock.now,
    onError: (error) => errors.push(error),
  });
  return {
    controller, sent, saved, errors, clock,
    emit: (type, message) => link.applyDeviceLinkEvent(controller, type, message),
    view: () => controller.getSnapshot(),
  };
}

// ------------------------------------------------------------ parsing

test("status, devices and errors are accepted only in their exact shape", () => {
  assert.equal(link.parseLinkStatus(statusMessage()).running, true);
  for (const bad of [
    statusMessage({ running: "true" }), statusMessage({ port: 0 }), statusMessage({ port: 1.5 }),
    statusMessage({ pairing_open: 1 }), "garbage", "[]",
  ]) {
    assert.equal(link.parseLinkStatus(bad), null, bad);
  }
  assert.equal(link.parseLinkDevices(JSON.stringify([device(ID_A)])).length, 1);
  for (const bad of [
    [device("ABCDEF0123456789")], [device(ID_A, { status: "lost" })], [device(ID_A, { created_at: "x" })],
    [device(ID_A, { remote_enabled: "false" })], [device(ID_A, { secret: "k" })], { not: "a list" },
  ]) {
    assert.equal(link.parseLinkDevices(JSON.stringify(bad)), null, JSON.stringify(bad).slice(0, 80));
  }
  assert.equal(link.parseLinkPairing(JSON.stringify({ requestNonce: NONCE, uri: "https://evil.example" })), null);
  assert.equal(link.parseLinkPairing(JSON.stringify({ requestNonce: "short", uri: URI })), null);
  assert.equal(link.parseLinkPairing(JSON.stringify({ requestNonce: NONCE, uri: URI })).expiresAt, 1000120);
});

test("device names follow the runtime's rules, Persian joiners included", () => {
  assert.equal(link.cleanDeviceName("  گوشی" + String.fromCharCode(0x200c) + "ها  "), "گوشی" + String.fromCharCode(0x200c) + "ها");
  for (const bad of ["x".repeat(41), "a" + String.fromCharCode(7), "evil" + String.fromCharCode(0x202e) + "gpj", "a" + String.fromCharCode(0x2028) + "b"]) {
    assert.equal(link.cleanDeviceName(bad), null);
  }
});

// ------------------------------------------------------------ pairing

test("a pairing code is shown only for this window's own request", async () => {
  const h = harness();
  await h.controller.openPairing("گوشی سارا");
  assert.deepEqual(plain(h.sent), [{ actionId: "qronos.link_pair_open", data: { requestNonce: NONCE, name: "گوشی سارا" } }]);
  assert.equal(h.view().waitingForPairing, true);
  h.emit("link_pairing", JSON.stringify({ requestNonce: "b".repeat(32), uri: URI }));
  assert.equal(h.view().pairing, null, "another window's code is ignored");
  h.emit("link_pairing", JSON.stringify({ requestNonce: NONCE, uri: URI }));
  assert.equal(h.view().pairing.uri, URI);
  assert.equal(h.view().waitingForPairing, false);
  h.emit("link_pairing", JSON.stringify({ requestNonce: NONCE, uri: URI.replace("47711", "1") }));
  assert.equal(h.view().pairing.uri, URI, "a replayed answer cannot replace the code");
});

test("a second pairing request while one is open sends nothing", async () => {
  const h = harness();
  await h.controller.openPairing("a");
  await h.controller.openPairing("b");
  assert.equal(h.sent.length, 1);
});

test("the code disappears at its expiry, on cancel and when the window closes", async () => {
  const h = harness({ now: 1000000 });
  await h.controller.openPairing("a");
  h.emit("link_pairing", JSON.stringify({ requestNonce: NONCE, uri: URI }));
  h.clock.now = 1000119;
  h.controller.tick();
  assert.notEqual(h.view().pairing, null);
  h.clock.now = 1000121;
  h.controller.tick();
  assert.equal(h.view().pairing, null);

  const c = harness();
  await c.controller.openPairing("a");
  c.emit("link_pairing", JSON.stringify({ requestNonce: NONCE, uri: URI }));
  await c.controller.cancelPairing();
  assert.equal(c.view().pairing, null);
  assert.equal(c.sent.at(-1).actionId, "qronos.link_pair_cancel");

  const s = harness();
  await s.controller.openPairing("a");
  s.emit("link_pairing", JSON.stringify({ requestNonce: NONCE, uri: URI }));
  s.emit("link_status", statusMessage({ pairing_open: true }));
  assert.notEqual(s.view().pairing, null);
  s.emit("link_status", statusMessage({ pairing_open: false }));
  assert.equal(s.view().pairing, null, "paired or closed: the code goes");
});

test("a local two-minute bound applies even when the URI says later", async () => {
  const h = harness({ now: 1000 });
  await h.controller.openPairing("a");
  h.emit("link_pairing", JSON.stringify({ requestNonce: NONCE, uri: URI.replace("e=1000120", "e=9999999") }));
  assert.equal(h.view().pairing.expiresAt, 1000 + link.PAIRING_SECONDS);
});

test("turning the link off drops a shown code and saves the switch", async () => {
  const h = harness();
  await h.controller.openPairing("a");
  h.emit("link_pairing", JSON.stringify({ requestNonce: NONCE, uri: URI }));
  await h.controller.setEnabled(false);
  assert.equal(h.view().pairing, null);
  assert.deepEqual(h.saved, [false]);
  assert.equal(h.sent.at(-1).actionId, "qronos.link_status");
});

test("a refused pairing request is explained in Persian and can be retried", async () => {
  const h = harness();
  await h.controller.openPairing("a");
  h.emit("link_error", JSON.stringify({ action: "qronos.link_pair_open", code: "not_running" }));
  assert.equal(h.view().waitingForPairing, false);
  assert.ok(h.view().error.includes("روشن نیست"));
  await h.controller.openPairing("a");
  assert.equal(h.sent.length, 2);
});

test("an unconfirmed send is reported and not replayed", async () => {
  const h = harness({ failSend: true });
  await h.controller.openPairing("a");
  assert.equal(h.view().waitingForPairing, false);
  assert.ok(h.view().error);
  assert.equal(h.sent.length, 1);
  assert.equal(h.errors.length, 1);
});

// ------------------------------------------------------------ devices

test("revoke and remote-off are sent only for listed devices", async () => {
  const h = harness();
  h.emit("link_devices", JSON.stringify([device(ID_A, { remote_enabled: true }), device(ID_B, { status: "revoked" })]));
  assert.equal(await h.controller.revoke("9999999999999999"), false);
  assert.equal(await h.controller.revoke(ID_B), false, "already revoked");
  assert.equal(await h.controller.remoteOff(ID_B), false);
  await h.controller.revoke(ID_A);
  await h.controller.remoteOff(ID_A);
  assert.deepEqual(plain(h.sent), [
    { actionId: "qronos.link_revoke", data: { deviceId: ID_A } },
    { actionId: "qronos.link_remote", data: { deviceId: ID_A, enabled: false } },
  ]);
  assert.ok(!h.sent.some((item) => item.data.enabled === true), "remote access is never switched on");
});

test("the runtime action carries exactly the action id and data", async () => {
  const calls = [];
  const controller = link.createDeviceLinkController(
    async (command, args) => { calls.push({ command, args }); },
    async () => {},
    () => {},
    (bytes) => bytes.fill(171),
  );
  await controller.openPairing("x");
  assert.deepEqual(plain(calls), [{
    command: "send_runtime_action",
    args: { actionId: "qronos.link_pair_open", data: { requestNonce: "ab".repeat(16), name: "x" } },
  }]);
});

// ------------------------------------------------------------ panel

function mountPanel() {
  const jsx = (type, props, key) => ({ type, props: props ?? {}, key });
  let slots = [];
  let index = 0;
  const react = {
    useState(initial) {
      const slot = index++;
      if (!slots[slot]) slots[slot] = { value: initial };
      return [slots[slot].value, (next) => { slots[slot].value = next; }];
    },
    useEffect() { index++; },
    useMemo(fn) { index++; return fn(); },
  };
  const exports = load(compile("../src/components/DeviceLinkPanel.tsx", true), {
    react,
    "react/jsx-runtime": { jsx, jsxs: jsx, Fragment: "Fragment" },
    "../qrCode": qr,
    "./DeviceLinkPanel.css": {},
  });
  return (view, enabled, handlers = {}) => {
    index = 0;
    const noop = () => {};
    return exports.default({
      view, enabled,
      onSetEnabled: handlers.setEnabled ?? noop, onRefresh: noop,
      onOpenPairing: handlers.open ?? noop, onCancelPairing: handlers.cancel ?? noop,
      onRevoke: handlers.revoke ?? noop, onRemoteOff: handlers.remoteOff ?? noop,
      onTick: noop, onDismissError: noop, onClose: noop,
    });
  };
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
  typeof node === "string" || typeof node === "number" ? String(node)
    : Array.isArray(node) ? node.map(text).join("")
      : node && typeof node === "object" ? text(node.props?.children) : "";

const baseView = (overrides = {}) => ({
  status: link.parseLinkStatus(statusMessage()), devices: [], pairing: null,
  waitingForPairing: false, savingEnabled: false, error: null, ...overrides,
});

test("the panel explains in Persian that phones can only ask, and that remote is off", () => {
  const tree = mountPanel()(baseView(), true);
  const all = text(tree);
  assert.ok(all.includes("اتصال گوشی"));
  assert.ok(all.includes("هیچ فایلی باز، حذف یا چاپ نمی‌شود"));
  assert.ok(all.includes("دسترسی از بیرون خانه در این نسخه فعال نیست"));
  const off = text(mountPanel()(baseView({ status: null }), false));
  assert.ok(off.includes("خاموش است"));
});

test("the pairing code is drawn as a QR image, never shown as text", () => {
  const view = baseView({ pairing: { uri: URI, name: "گوشی سارا", expiresAt: 1000120 } });
  const tree = mountPanel()(view, true);
  const [svg] = findAll(tree, (n) => n.type === "svg");
  assert.equal(svg.props.role, "img");
  const [path] = findAll(svg, (n) => n.type === "path");
  assert.equal(path.props.d, qr.qrSvgPath(qr.encodeQr(URI)));
  assert.ok(!text(tree).includes(URI) && !text(tree).includes("K".repeat(43)));
});

test("pairing needs a running link; revoking asks once more", () => {
  const render = mountPanel();
  const stopped = render(baseView({ status: link.parseLinkStatus(statusMessage({ running: false })) }), true);
  const submit = findAll(stopped, (n) => n.type === "button" && n.props.type === "submit")[0];
  assert.equal(submit.props.disabled, true);

  const revoked = [];
  const view = baseView({ devices: link.parseLinkDevices(JSON.stringify([device(ID_A)])) });
  let tree = render(view, true, { revoke: (id) => revoked.push(id) });
  findAll(tree, (n) => n.type === "button" && text(n) === "لغو دسترسی")[0].props.onClick();
  assert.deepEqual(revoked, [], "the first click only asks");
  tree = render(view, true, { revoke: (id) => revoked.push(id) });
  findAll(tree, (n) => n.type === "button" && text(n).includes("بله"))[0].props.onClick();
  assert.deepEqual(revoked, [ID_A]);
});

// ------------------------------------------------------------ App wiring

test("App routes link events, never logs the pairing event, and the Settings page hosts the panel", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.ok(app.includes("applyDeviceLinkEvent("));
  assert.match(app, /eventType !==\s*"link_pairing"/);
  assert.ok(app.includes('setSetting("link.enabled", enabled)'));
  const settings = readFileSync(new URL("../src/components/SettingsView.tsx", import.meta.url), "utf8");
  assert.ok(settings.includes("<DeviceLinkPanel"));
  for (const file of ["../src/deviceLink.ts", "../src/components/DeviceLinkPanel.tsx", "../src/qrCode.ts"]) {
    const source = readFileSync(new URL(file, import.meta.url), "utf8");
    assert.ok(!/localStorage|sessionStorage|console\./.test(source), `${file} never stores or logs a code`);
    assert.ok(!/fetch\(|XMLHttpRequest|WebSocket/.test(source), `${file} makes no network request`);
  }
});
