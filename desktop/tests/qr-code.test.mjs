import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/qrCode.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const qr = {};
vm.runInNewContext(compiled, {
  exports: qr, TextEncoder, Uint8Array, Array, Math, Error, RangeError, Infinity, Object,
}, { timeout: 2000 });

const plain = (value) => JSON.parse(JSON.stringify(value));

// ------------------------------------------------ published known answers

test("Reed-Solomon matches the published HELLO WORLD 1-M codewords", () => {
  const data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17];
  assert.deepEqual(plain(qr.reedSolomon(data, 10)), [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]);
});

test("format bits match the standard's table for level M", () => {
  // ISO/IEC 18004 Table C.1, error correction level M, masks 0-7.
  const expected = [
    "101010000010010", "101000100100101", "101111001111100", "101101101001011",
    "100010111111001", "100000011001110", "100111110010111", "100101010100000",
  ];
  for (let mask = 0; mask < 8; mask++) {
    assert.equal(qr.formatBits(mask).toString(2).padStart(15, "0"), expected[mask], `mask ${mask}`);
  }
});

test("version bits match the standard's table", () => {
  // ISO/IEC 18004 Table D.1, versions 7-15.
  const expected = {
    7: 0x07c94, 8: 0x085bc, 9: 0x09a99, 10: 0x0a4d3, 11: 0x0bbf6,
    12: 0x0c762, 13: 0x0d847, 14: 0x0e60d, 15: 0x0f928,
  };
  for (const [version, bits] of Object.entries(expected)) {
    assert.equal(qr.versionBits(Number(version)), bits, `version ${version}`);
  }
});

const ALIGNMENT = {
  2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
  9: [6, 26, 46], 10: [6, 28, 50], 11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62],
  14: [6, 26, 46, 66], 15: [6, 26, 48, 70],
};

test("alignment positions match the standard's table", () => {
  assert.deepEqual(plain(qr.alignmentPositions(1)), []);
  for (const [version, positions] of Object.entries(ALIGNMENT)) {
    assert.deepEqual(plain(qr.alignmentPositions(Number(version))), positions, `version ${version}`);
  }
});

test("every version's block table fills exactly the symbol's codewords", () => {
  const published = [26, 44, 70, 100, 134, 172, 196, 242, 292, 346, 404, 466, 532, 581, 655];
  for (let version = 1; version <= qr.MAX_VERSION; version++) {
    assert.equal(qr.totalCodewords(version), published[version - 1], `version ${version}`);
    assert.equal(qr.totalCodewords(version), Math.floor(qr.rawDataModules(version) / 8));
  }
});

// ------------------------------------------------ independent read-back

// Level M blocks per version: [ec, [data length of each block...]].
const BLOCKS = {
  1: [10, [16]], 2: [16, [28]], 3: [26, [44]], 4: [18, [32, 32]], 5: [24, [43, 43]],
  6: [16, [27, 27, 27, 27]], 7: [18, [31, 31, 31, 31]], 8: [22, [38, 38, 39, 39]],
  9: [22, [36, 36, 36, 37, 37]], 10: [26, [43, 43, 43, 43, 44]],
};

function reserved(version) {
  const size = version * 4 + 17;
  const r = Array.from({ length: size }, () => new Array(size).fill(false));
  const mark = (x0, y0, w, h) => {
    for (let y = y0; y < y0 + h; y++) for (let x = x0; x < x0 + w; x++) if (x >= 0 && y >= 0 && x < size && y < size) r[y][x] = true;
  };
  mark(0, 0, 9, 9); // finder, separator, format
  mark(size - 8, 0, 8, 9);
  mark(0, size - 8, 9, 8);
  mark(6, 0, 1, size);
  mark(0, 6, size, 1);
  const positions = ALIGNMENT[version] ?? [];
  for (const cy of positions) for (const cx of positions) {
    const corner = (cx === 6 && cy === 6) || (cx === 6 && cy === size - 7) || (cx === size - 7 && cy === 6);
    if (!corner) mark(cx - 2, cy - 2, 5, 5);
  }
  if (version >= 7) {
    mark(size - 11, 0, 3, 6);
    mark(0, size - 11, 6, 3);
  }
  return r;
}

function maskAt(mask, x, y) {
  return [
    (x + y) % 2 === 0, y % 2 === 0, x % 3 === 0, (x + y) % 3 === 0,
    (Math.floor(y / 2) + Math.floor(x / 3)) % 2 === 0, ((x * y) % 2) + ((x * y) % 3) === 0,
    (((x * y) % 2) + ((x * y) % 3)) % 2 === 0, (((x + y) % 2) + ((x * y) % 3)) % 2 === 0,
  ][mask];
}

function decode(matrix) {
  const { modules, size } = matrix;
  const version = (size - 17) / 4;
  // Format copy 1, read from the top-left corner as the standard lays it out.
  const order = [[8, 0], [8, 1], [8, 2], [8, 3], [8, 4], [8, 5], [8, 7], [8, 8], [7, 8], [5, 8], [4, 8], [3, 8], [2, 8], [1, 8], [0, 8]];
  let format = 0;
  order.forEach(([x, y], i) => { if (modules[y][x]) format |= 1 << i; });
  let copy2 = 0;
  for (let i = 0; i < 8; i++) if (modules[8][size - 1 - i]) copy2 |= 1 << i;
  for (let i = 8; i < 15; i++) if (modules[size - 15 + i][8]) copy2 |= 1 << i;
  assert.equal(format, copy2, "both format copies agree");
  const raw = format ^ 0x5412;
  assert.equal(raw >>> 13, 0b00, "level M");
  const mask = (raw >>> 10) & 7;
  assert.equal(modules[size - 8][8], true, "dark module");
  const r = reserved(version);
  const bits = [];
  let upward = true;
  for (let right = size - 1; right > 0; right -= 2) {
    if (right === 6) right--;
    for (let step = 0; step < size; step++) {
      const y = upward ? size - 1 - step : step;
      for (const x of [right, right - 1]) {
        if (!r[y][x]) bits.push(modules[y][x] !== maskAt(mask, x, y) ? 1 : 0);
      }
    }
    upward = !upward;
  }
  const codewords = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) codewords.push(bits.slice(i, i + 8).reduce((a, b) => (a << 1) | b, 0));
  const [ec, lengths] = BLOCKS[version];
  const blocks = lengths.map(() => []);
  let k = 0;
  for (let i = 0; i < Math.max(...lengths); i++) lengths.forEach((n, b) => { if (i < n) blocks[b].push(codewords[k++]); });
  const ecBlocks = lengths.map(() => []);
  for (let i = 0; i < ec; i++) lengths.forEach((_, b) => ecBlocks[b].push(codewords[k++]));
  blocks.forEach((block, b) => assert.deepEqual(plain(qr.reedSolomon(block, ec)), ecBlocks[b], `block ${b} RS`));
  const data = blocks.flat();
  const dataBits = data.flatMap((byte) => Array.from({ length: 8 }, (_, i) => (byte >>> (7 - i)) & 1));
  const read = (from, n) => dataBits.slice(from, from + n).reduce((a, b) => (a << 1) | b, 0);
  assert.equal(read(0, 4), 0b0100, "byte mode");
  const countBits = version < 10 ? 8 : 16;
  const length = read(4, countBits);
  const bytes = Array.from({ length }, (_, i) => read(4 + countBits + i * 8, 8));
  return { version, mask, text: new TextDecoder().decode(new Uint8Array(bytes)) };
}

const URI = "qronos://pair?v=1&h=192.168.100.200&p=47711&d=0123456789abcdef&k=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8&e=1791480123";

test("a pairing URI reads back exactly, for every mask", () => {
  for (let mask = 0; mask < 8; mask++) {
    const matrix = qr.encodeQr(URI, mask);
    const decoded = decode(matrix);
    assert.equal(decoded.text, URI);
    assert.equal(decoded.mask, mask);
  }
});

test("short, Persian and version-10 texts read back exactly", () => {
  for (const text of ["Q", "گوشی سارا", "x".repeat(150), "y".repeat(210)]) {
    const matrix = qr.encodeQr(text);
    assert.ok(matrix.version <= 10);
    assert.equal(decode(matrix).text, text);
  }
  assert.equal(qr.encodeQr("y".repeat(210)).version, 10, "16-bit length field path");
});

test("finder patterns and timing are in place", () => {
  const { modules, size } = qr.encodeQr(URI);
  const finder = ["1111111", "1000001", "1011101", "1011101", "1011101", "1000001", "1111111"];
  for (const [x0, y0] of [[0, 0], [size - 7, 0], [0, size - 7]]) {
    finder.forEach((row, dy) => [...row].forEach((c, dx) => assert.equal(modules[y0 + dy][x0 + dx], c === "1")));
  }
  for (let i = 8; i < size - 8; i++) {
    assert.equal(modules[6][i], i % 2 === 0);
    assert.equal(modules[i][6], i % 2 === 0);
  }
});

test("the automatic mask is the lowest-penalty one", () => {
  const chosen = qr.encodeQr(URI);
  for (let mask = 0; mask < 8; mask++) {
    assert.ok(qr.penalty(chosen.modules) <= qr.penalty(qr.encodeQr(URI, mask).modules));
  }
});

test("text beyond the supported capacity is refused, not truncated", () => {
  assert.throws(() => qr.encodeQr("z".repeat(413)), qr.QrCapacityError);
  assert.equal(qr.encodeQr("z".repeat(412)).version, 15);
});

test("the SVG path draws exactly the dark modules", () => {
  const matrix = qr.encodeQr("Q");
  const dark = matrix.modules.flat().filter(Boolean).length;
  assert.equal((qr.qrSvgPath(matrix).match(/M/g) ?? []).length, dark);
});
