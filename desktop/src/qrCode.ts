/**
 * A small QR Code encoder (ISO/IEC 18004) for the Device Link pairing code.
 *
 * Byte mode, error correction level M, versions 1-15 (up to 412 bytes). The
 * pairing URI holds the new phone's key, so it is encoded here, in memory,
 * rather than sent to any service or kept anywhere. Nothing else uses this.
 */

export type QrMatrix = {
  version: number;
  size: number;
  mask: number;
  /** modules[y][x]; true is dark. */
  modules: boolean[][];
};

// Level M: [EC codewords per block, blocks in group 1, data codewords per
// group-1 block, blocks in group 2 (one more data codeword each)].
const LEVEL_M_BLOCKS: ReadonlyArray<readonly [number, number, number, number]> = [
  [10, 1, 16, 0],
  [16, 1, 28, 0],
  [26, 1, 44, 0],
  [18, 2, 32, 0],
  [24, 2, 43, 0],
  [16, 4, 27, 0],
  [18, 4, 31, 0],
  [22, 2, 38, 2],
  [22, 3, 36, 2],
  [26, 4, 43, 1],
  [30, 1, 50, 4],
  [22, 6, 36, 2],
  [22, 8, 37, 1],
  [24, 4, 40, 5],
  [24, 5, 41, 5],
];

export const MAX_VERSION = LEVEL_M_BLOCKS.length;
const FORMAT_LEVEL_M = 0b00;

export class QrCapacityError extends Error {}

// ------------------------------------------------------------ GF(256) / RS

const EXP = new Uint8Array(512);
const LOG = new Uint8Array(256);
{
  let value = 1;
  for (let i = 0; i < 255; i++) {
    EXP[i] = value;
    LOG[value] = i;
    value <<= 1;
    if (value & 0x100) value ^= 0x11d;
  }
  for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
}

function multiply(a: number, b: number): number {
  return a === 0 || b === 0 ? 0 : EXP[LOG[a] + LOG[b]];
}

/** Generator polynomial coefficients (highest degree first, leading 1 omitted). */
function generator(degree: number): number[] {
  let poly = [1];
  for (let i = 0; i < degree; i++) {
    const next = new Array<number>(poly.length + 1).fill(0);
    for (let j = 0; j < poly.length; j++) {
      next[j] ^= poly[j];
      next[j + 1] ^= multiply(poly[j], EXP[i]);
    }
    poly = next;
  }
  return poly.slice(1);
}

/** Reed-Solomon error-correction codewords for one block. */
export function reedSolomon(data: readonly number[], degree: number): number[] {
  const gen = generator(degree);
  const remainder = new Array<number>(degree).fill(0);
  for (const byte of data) {
    const factor = byte ^ (remainder.shift() as number);
    remainder.push(0);
    for (let i = 0; i < degree; i++) remainder[i] ^= multiply(gen[i], factor);
  }
  return remainder;
}

// ------------------------------------------------------------ layout facts

export function sizeOf(version: number): number {
  return version * 4 + 17;
}

export function alignmentPositions(version: number): number[] {
  if (version === 1) return [];
  const count = Math.floor(version / 7) + 2;
  const step = Math.ceil((version * 4 + 4) / (count * 2 - 2)) * 2;
  const positions = [6];
  for (let pos = sizeOf(version) - 7; positions.length < count; pos -= step) positions.splice(1, 0, pos);
  return positions;
}

/** Modules available for data and error correction (codewords * 8 + remainder bits). */
export function rawDataModules(version: number): number {
  let result = (16 * version + 128) * version + 64;
  if (version >= 2) {
    const count = Math.floor(version / 7) + 2;
    result -= (25 * count - 10) * count - 55;
    if (version >= 7) result -= 36;
  }
  return result;
}

export function totalCodewords(version: number): number {
  const [ec, group1, data1, group2] = LEVEL_M_BLOCKS[version - 1];
  return group1 * (data1 + ec) + group2 * (data1 + 1 + ec);
}

function dataCodewords(version: number): number {
  const [, group1, data1, group2] = LEVEL_M_BLOCKS[version - 1];
  return group1 * data1 + group2 * (data1 + 1);
}

/** The 15 format bits for level M and one mask, already masked with 0x5412. */
export function formatBits(mask: number): number {
  const data = (FORMAT_LEVEL_M << 3) | mask;
  let rem = data;
  for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
  return ((data << 10) | rem) ^ 0x5412;
}

/** The 18 version bits (versions 7 and up). */
export function versionBits(version: number): number {
  let rem = version;
  for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
  return (version << 12) | rem;
}

const bit = (value: number, index: number) => ((value >>> index) & 1) !== 0;

// ------------------------------------------------------------ encoding

function utf8(text: string): number[] {
  return Array.from(new TextEncoder().encode(text));
}

function chooseVersion(length: number): number {
  for (let version = 1; version <= MAX_VERSION; version++) {
    const countBits = version < 10 ? 8 : 16;
    if (4 + countBits + length * 8 <= dataCodewords(version) * 8) return version;
  }
  throw new QrCapacityError("The text is too long for this QR encoder.");
}

function dataStream(bytes: number[], version: number): number[] {
  const bits: number[] = [];
  const push = (value: number, length: number) => {
    for (let i = length - 1; i >= 0; i--) bits.push((value >>> i) & 1);
  };
  push(0b0100, 4);
  push(bytes.length, version < 10 ? 8 : 16);
  for (const byte of bytes) push(byte, 8);
  const capacity = dataCodewords(version) * 8;
  push(0, Math.min(4, capacity - bits.length));
  while (bits.length % 8 !== 0) bits.push(0);
  const codewords: number[] = [];
  for (let i = 0; i < bits.length; i += 8) {
    codewords.push(bits.slice(i, i + 8).reduce((acc, value) => (acc << 1) | value, 0));
  }
  for (let pad = 0xec; codewords.length < dataCodewords(version); pad ^= 0xec ^ 0x11) codewords.push(pad);
  return codewords;
}

function interleaved(data: number[], version: number): number[] {
  const [ec, group1, data1, group2] = LEVEL_M_BLOCKS[version - 1];
  const blocks: number[][] = [];
  let offset = 0;
  for (let b = 0; b < group1 + group2; b++) {
    const length = b < group1 ? data1 : data1 + 1;
    blocks.push(data.slice(offset, offset + length));
    offset += length;
  }
  const ecBlocks = blocks.map((block) => reedSolomon(block, ec));
  const result: number[] = [];
  for (let i = 0; i <= data1; i++) {
    for (const block of blocks) if (i < block.length) result.push(block[i]);
  }
  for (let i = 0; i < ec; i++) for (const block of ecBlocks) result.push(block[i]);
  return result;
}

class Grid {
  readonly modules: boolean[][];
  readonly reserved: boolean[][];

  constructor(readonly version: number, readonly size = sizeOf(version)) {
    this.modules = Array.from({ length: size }, () => new Array<boolean>(size).fill(false));
    this.reserved = Array.from({ length: size }, () => new Array<boolean>(size).fill(false));
  }

  fn(x: number, y: number, dark: boolean): void {
    this.modules[y][x] = dark;
    this.reserved[y][x] = true;
  }

  drawFunctionPatterns(): void {
    const size = this.size;
    for (let i = 0; i < size; i++) {
      this.fn(6, i, i % 2 === 0);
      this.fn(i, 6, i % 2 === 0);
    }
    for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]]) {
      for (let dy = -4; dy <= 4; dy++) {
        for (let dx = -4; dx <= 4; dx++) {
          const x = cx + dx;
          const y = cy + dy;
          if (x < 0 || y < 0 || x >= size || y >= size) continue;
          const distance = Math.max(Math.abs(dx), Math.abs(dy));
          this.fn(x, y, distance !== 2 && distance !== 4);
        }
      }
    }
    const positions = alignmentPositions(this.version);
    const last = positions.length - 1;
    positions.forEach((cy, i) => positions.forEach((cx, j) => {
      if ((i === 0 && j === 0) || (i === 0 && j === last) || (i === last && j === 0)) return;
      for (let dy = -2; dy <= 2; dy++) {
        for (let dx = -2; dx <= 2; dx++) this.fn(cx + dx, cy + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
      }
    }));
    this.drawFormat(0); // reserves the areas; redrawn with the chosen mask
    if (this.version >= 7) {
      const bits = versionBits(this.version);
      for (let i = 0; i < 18; i++) {
        const a = size - 11 + (i % 3);
        const b = Math.floor(i / 3);
        this.fn(a, b, bit(bits, i));
        this.fn(b, a, bit(bits, i));
      }
    }
  }

  drawFormat(mask: number): void {
    const bits = formatBits(mask);
    const size = this.size;
    for (let i = 0; i <= 5; i++) this.fn(8, i, bit(bits, i));
    this.fn(8, 7, bit(bits, 6));
    this.fn(8, 8, bit(bits, 7));
    this.fn(7, 8, bit(bits, 8));
    for (let i = 9; i < 15; i++) this.fn(14 - i, 8, bit(bits, i));
    for (let i = 0; i < 8; i++) this.fn(size - 1 - i, 8, bit(bits, i));
    for (let i = 8; i < 15; i++) this.fn(8, size - 15 + i, bit(bits, i));
    this.fn(8, size - 8, true); // the dark module
  }

  placeCodewords(codewords: number[]): void {
    let index = 0;
    const total = codewords.length * 8;
    for (let right = this.size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let vertical = 0; vertical < this.size; vertical++) {
        for (let j = 0; j < 2; j++) {
          const x = right - j;
          const upward = ((right + 1) & 2) === 0;
          const y = upward ? this.size - 1 - vertical : vertical;
          if (this.reserved[y][x] || index >= total) continue;
          this.modules[y][x] = bit(codewords[index >>> 3], 7 - (index & 7));
          index++;
        }
      }
    }
  }

  applyMask(mask: number): void {
    for (let y = 0; y < this.size; y++) {
      for (let x = 0; x < this.size; x++) {
        if (!this.reserved[y][x] && maskApplies(mask, x, y)) this.modules[y][x] = !this.modules[y][x];
      }
    }
  }
}

export function maskApplies(mask: number, x: number, y: number): boolean {
  switch (mask) {
    case 0: return (x + y) % 2 === 0;
    case 1: return y % 2 === 0;
    case 2: return x % 3 === 0;
    case 3: return (x + y) % 3 === 0;
    case 4: return (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0;
    case 5: return ((x * y) % 2) + ((x * y) % 3) === 0;
    case 6: return (((x * y) % 2) + ((x * y) % 3)) % 2 === 0;
    case 7: return (((x + y) % 2) + ((x * y) % 3)) % 2 === 0;
    default: throw new RangeError("mask");
  }
}

/** The standard's penalty score; the encoder keeps the mask with the lowest. */
export function penalty(modules: boolean[][]): number {
  const size = modules.length;
  let score = 0;
  const line = (get: (i: number) => boolean) => {
    let run = 1;
    for (let i = 1; i <= size; i++) {
      if (i < size && get(i) === get(i - 1)) {
        run++;
        continue;
      }
      if (run >= 5) score += 3 + (run - 5);
      run = 1;
    }
    const pattern = [true, false, true, true, true, false, true];
    for (let i = 0; i + 7 <= size; i++) {
      if (!pattern.every((dark, k) => get(i + k) === dark)) continue;
      const lightBefore = i >= 4 && [1, 2, 3, 4].every((k) => !get(i - k));
      const lightAfter = i + 11 <= size && [7, 8, 9, 10].every((k) => !get(i + k));
      if (lightBefore || lightAfter) score += 40;
    }
  };
  for (let y = 0; y < size; y++) line((x) => modules[y][x]);
  for (let x = 0; x < size; x++) line((y) => modules[y][x]);
  let dark = 0;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      if (modules[y][x]) dark++;
      if (x + 1 < size && y + 1 < size) {
        const c = modules[y][x];
        if (modules[y][x + 1] === c && modules[y + 1][x] === c && modules[y + 1][x + 1] === c) score += 3;
      }
    }
  }
  const total = size * size;
  score += (Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1) * 10;
  return score;
}

/** Encode text (UTF-8, byte mode, level M) into a QR matrix. */
export function encodeQr(text: string, forcedMask?: number): QrMatrix {
  const bytes = utf8(text);
  const version = chooseVersion(bytes.length);
  const codewords = interleaved(dataStream(bytes, version), version);
  let best: QrMatrix | null = null;
  let bestScore = Infinity;
  for (const mask of forcedMask === undefined ? [0, 1, 2, 3, 4, 5, 6, 7] : [forcedMask]) {
    const grid = new Grid(version);
    grid.drawFunctionPatterns();
    grid.placeCodewords(codewords);
    grid.applyMask(mask);
    grid.drawFormat(mask);
    const score = penalty(grid.modules);
    if (score < bestScore) {
      bestScore = score;
      best = { version, size: grid.size, mask, modules: grid.modules };
    }
  }
  return best as QrMatrix;
}

/** An SVG path drawing every dark module, inside a 4-module quiet zone. */
export function qrSvgPath(matrix: QrMatrix, quiet = 4): string {
  const parts: string[] = [];
  matrix.modules.forEach((row, y) => row.forEach((dark, x) => {
    if (dark) parts.push(`M${x + quiet},${y + quiet}h1v1h-1z`);
  }));
  return parts.join("");
}
