"""Cross-check desktop/src/qrCode.ts against OpenCV's QR decoders and encoder.

Not a product dependency and not collected by pytest: run it deliberately
with any Python that already has OpenCV and NumPy, from the repository root:

    python -I desktop/tests/qr_opencv_crosscheck.py

Everything is synthetic and in memory: fixture strings are encoded by the
desktop encoder (through Node and the checkout's own TypeScript compiler),
drawn as bitmaps with a 4-module quiet zone, and decoded by OpenCV's
QRCodeDetector and QRCodeDetectorAruco. No camera, screen, file or network.
Exit status 0 only when every check passes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

DESKTOP = Path(__file__).resolve().parent.parent

NODE_HELPER = r"""
const fs = require("fs");
const vm = require("vm");
const ts = require("typescript");
const source = fs.readFileSync("src/qrCode.ts", "utf8");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const qr = {};
vm.runInNewContext(js, { exports: qr, TextEncoder, Uint8Array, Array, Math, Error, RangeError, Infinity, Object });
const requests = JSON.parse(fs.readFileSync(0, "utf8"));
const out = requests.map(([text, mask]) => {
  const m = qr.encodeQr(text, mask === null ? undefined : mask);
  return { version: m.version, mask: m.mask, modules: m.modules.map((row) => row.map((d) => (d ? 1 : 0))) };
});
process.stdout.write(JSON.stringify(out));
"""

# Byte capacity at level M per version (ISO/IEC 18004 Table 7).
CAPACITY_M = [14, 26, 42, 62, 84, 106, 122, 152, 180, 213, 251, 287, 331, 362, 412]

PAIRING_URI = (
    "qronos://pair?v=1&h=192.168.100.200&p=47711&d=0123456789abcdef"
    "&k=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8&e=1791480123"
)


def encode(requests: list[tuple[str, int | None]]) -> list[dict]:
    result = subprocess.run(
        ["node", "-e", NODE_HELPER], cwd=DESKTOP, input=json.dumps(requests),
        capture_output=True, text=True, encoding="utf-8", timeout=120, check=True,
    )
    return json.loads(result.stdout)


def bitmap(modules, scale: int = 8, quiet: int = 4) -> np.ndarray:
    matrix = np.array(modules, dtype=np.uint8)
    padded = np.pad(matrix, quiet, constant_values=0)
    image = np.where(padded == 1, 0, 255).astype(np.uint8)
    return np.kron(image, np.ones((scale, scale), dtype=np.uint8))


def decoders():
    return [("QRCodeDetector", cv2.QRCodeDetector()), ("QRCodeDetectorAruco", cv2.QRCodeDetectorAruco())]


def opencv_matrix(text: str, version: int) -> np.ndarray:
    params = cv2.QRCodeEncoder_Params()
    params.correction_level = cv2.QRCodeEncoder_CORRECT_LEVEL_M
    params.mode = cv2.QRCodeEncoder_MODE_BYTE
    params.version = version
    image = cv2.QRCodeEncoder_create(params).encode(text)
    # One pixel per module inside a quiet zone of equal width on every side.
    size = version * 4 + 17
    margin = (image.shape[0] - size) // 2
    assert image.shape == (size + 2 * margin, size + 2 * margin), image.shape
    return (image[margin:margin + size, margin:margin + size] < 128).astype(np.uint8)


def mask_of(modules: np.ndarray) -> int:
    order = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8), (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    bits = sum(int(modules[y][x]) << i for i, (x, y) in enumerate(order)) ^ 0x5412
    return (bits >> 10) & 7


def main() -> int:
    failures: list[str] = []
    checks = 0

    texts: list[tuple[str, str]] = [("pairing URI", PAIRING_URI)]
    for version, capacity in enumerate(CAPACITY_M, start=1):
        texts.append((f"v{version} full", "Q" * capacity))
        if version > 1:
            texts.append((f"v{version} first", "Q" * (CAPACITY_M[version - 2] + 1)))
    persian = "گوشی‌های جفت‌شده در همین شبکهٔ خانگی "
    texts.append(("long UTF-8", (persian * 20).encode("utf-8")[:400].decode("utf-8", "ignore")))

    requests = [(text, None) for _, text in texts] + [(PAIRING_URI, mask) for mask in range(8)]
    matrices = encode(requests)

    labels = [label for label, _ in texts] + [f"pairing URI mask {m}" for m in range(8)]
    strings = [text for _, text in texts] + [PAIRING_URI] * 8
    limitations: list[str] = []
    for label, text, matrix in zip(labels, strings, matrices):
        image = bitmap(matrix["modules"])
        decoded_by = []
        for name, detector in decoders():
            checks += 1
            decoded, _, _ = detector.detectAndDecode(image)
            if decoded == text:
                decoded_by.append(name)
                continue
            # A miss is the encoder's fault unless OpenCV's own symbol for the
            # same text, version and mask is identical and is missed as well.
            theirs = opencv_matrix(text, matrix["version"])
            identical = np.array_equal(theirs, np.array(matrix["modules"], dtype=np.uint8))
            own, _, _ = detector.__class__().detectAndDecode(bitmap(theirs.tolist()))
            if identical and own != text:
                limitations.append(f"{name} {label} (v{matrix['version']} mask {matrix['mask']}): "
                                   "also misses OpenCV's own identical symbol")
            else:
                failures.append(f"{name} {label} (v{matrix['version']} mask {matrix['mask']}): {decoded[:40]!r}")
        if not decoded_by:
            failures.append(f"no decoder read {label}")

    # Error correction: damage a block of data modules; level M must recover it.
    damaged = np.array(matrices[0]["modules"], dtype=np.uint8)
    size = damaged.shape[0]
    damaged[size - 12:size - 9, size - 6:size - 3] ^= 1
    for name, detector in decoders():
        checks += 1
        decoded, _, _ = detector.detectAndDecode(bitmap(damaged))
        if decoded != PAIRING_URI:
            failures.append(f"{name} damaged pairing URI: {decoded[:40]!r}")

    # Module-for-module agreement with OpenCV's own encoder, same version and mask.
    for label, text in [("pairing URI", PAIRING_URI), ("v10 full", "Q" * 213), ("v15 full", "Q" * 412)]:
        ours = encode([(text, None)])[0]
        theirs = opencv_matrix(text, ours["version"])
        same_mask = np.array(encode([(text, mask_of(theirs))])[0]["modules"], dtype=np.uint8)
        checks += 1
        if theirs.shape != same_mask.shape or not np.array_equal(theirs, same_mask):
            diff = int(np.sum(theirs != same_mask)) if theirs.shape == same_mask.shape else -1
            failures.append(f"OpenCV encoder differs for {label}: {diff} modules")

    print(json.dumps({
        "opencv": cv2.__version__, "numpy": np.__version__, "checks": checks,
        "failures": failures, "detectorLimitations": limitations,
    }, ensure_ascii=False, indent=1))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
