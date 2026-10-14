"""Real frame vectors: one image tower and one text tower in ONE space (handoff s16; the backend stays benchmark-gated, s48).

The declared encoder is SigLIP 2 base (google/siglip2-base-patch16-224, Apache-2.0) in the int8 ONNX export of
onnx-community/siglip2-base-patch16-224-ONNX at a pinned revision (qronos_media.resources.SIGLIP2_BASE_224_INT8). Its
image tower embeds the PIXELS of a stored keyframe and its text tower embeds a typed query, so a query is compared
with what the frame shows, not with a caption of it. This is a separate channel from ``visual_semantic`` (bge-m3
vectors of the generated caption TEXT), which is unchanged.

Boundaries:

* The three files (two ONNX graphs and the SentencePiece model) are provisioned separately under
  ``CONFIG.paths.runtime_assets`` and never downloaded or bundled. Each is read ONCE into memory, checked by size and
  SHA-256 against its pin, and only those verified bytes reach the runtime: no path is handed to it, so no other file
  (external data, a custom-op library) can be loaded beside it.
* onnxruntime runs on the CPUExecutionProvider only, with a bounded thread count, sequentially, in this process and
  only while the runtime-issued ModelLease for the declared model is held. Sessions are counted on that lease; the
  slot is freed only once every session this module opened is closed.
* The tokenizer is a small pure-Python reader of the SentencePiece model (BPE by piece score with byte fallback):
  no tokenizers, sentencepiece, torch or transformers package and no remote code.
* Vectors are float32, L2-normalised and exactly ``DIMENSION`` long; anything else from the runtime is refused, and a
  stored vector of the wrong length, a non-finite value or the wrong norm is never ranked.
* Every stored vector carries the model FINGERPRINT (model, revision, the three file SHA-256s, the preprocessing and
  tokenizer versions). Search reads only vectors of the current fingerprint; a changed declaration is a new stage
  version, so frames are embedded again rather than compared across spaces.
* Scores are raw cosine similarities between a text vector and frame vectors: uncalibrated, not a probability, not an
  identification of anyone or anything. Retrieval quality, including for Persian queries, is unmeasured.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import threading
from typing import Any, Callable, Iterator

from qronos_media.resources import SIGLIP2_BASE_224_INT8, ModelAsset, ModelSpec, ResourceRefused

#: Version of the "image_embeddings" stage (what is embedded and how it is stored).
STAGE_VERSION = 1
PREPROCESS_VERSION = 1
TOKENIZER_VERSION = 1
#: The pinned export (onnx-community/siglip2-base-patch16-224-ONNX) and its base model's revision.
ONNX_REPOSITORY = "onnx-community/siglip2-base-patch16-224-ONNX"
ONNX_REVISION = "ba1f3b0843f24bc5417d38e19c37b287d719b2f4"
BASE_MODEL = "google/siglip2-base-patch16-224"
BASE_REVISION = "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2"
LICENCE = "Apache-2.0 (base model); the ONNX conversion states no licence of its own (licence review)"

DIMENSION = SIGLIP2_BASE_224_INT8.dimension
IMAGE_SIZE = 224
#: preprocessor_config.json of the pinned revision: bilinear resize to 224x224, rescale 1/255, mean 0.5, std 0.5.
IMAGE_MEAN = 0.5
IMAGE_STD = 0.5
#: Text: lowercased, EOS appended, padded/truncated to 64 tokens (how the model was trained), pad 0, EOS 1.
MAX_TEXT_TOKENS = 64
MAX_QUERY_CHARS = 1_200
PAD_ID = 0
EOS_ID = 1
#: At most this many frames per stage batch (between checkpoints). The image tower itself runs ONE frame per call: the
#: pinned int8 graph quantises activations dynamically per call, so frames batched together changed each other's
#: vectors (min cosine 0.987 against the same frame alone in the real CPU smoke); one per call keeps a stored frame's
#: vector independent of its neighbours, so the cache, resume and incremental re-runs agree.
IMAGE_BATCH = 8
#: Bounded CPU use: intra-op threads of each session (inter-op 1, sequential execution).
INTRA_OP_THREADS = 2
MAX_FRAME_PIXELS = 4096 * 4096
FRAME_FORMATS = frozenset({"JPEG", "PNG"})
#: |norm - 1| above this marks a stored vector as corrupt.
NORM_TOLERANCE = 1e-3
MAX_SCAN_ROWS = 50_000
SCAN_CHUNK = 2_000

VISION_ASSET: ModelAsset = SIGLIP2_BASE_224_INT8.weights  # type: ignore[assignment]
TEXT_ASSET, TOKENIZER_ASSET = SIGLIP2_BASE_224_INT8.companions

SCORE_NOTE = ("cosine similarity between the query's SigLIP 2 TEXT vector and stored SigLIP 2 IMAGE vectors of the "
              "frames' pixels: uncalibrated, not a probability, not an identification; quality unmeasured")
METHOD = "image similarity: SigLIP 2 image vectors of the stored keyframes against one text vector of the query"

#: The contract of the two graphs: the input each needs and the output used.
VISION_INPUT = "pixel_values"
TEXT_INPUT = "input_ids"
TEXT_OPTIONAL_MASK = "attention_mask"
OUTPUT = "pooler_output"


class EncoderUnavailable(ResourceRefused):
    """The encoder cannot run here (not provisioned, not the pinned files, or an unexpected graph)."""


class EncoderOutputInvalid(RuntimeError):
    """The runtime answered something that is not a finite vector of the declared length."""


class FrameUnusable(ValueError):
    """The stored frame is not an image this encoder accepts."""


def fingerprint(spec: ModelSpec = SIGLIP2_BASE_224_INT8) -> str:
    """Which exact vector space a stored vector belongs to (no I/O; derived from the declaration)."""
    assets = [spec.weights, *spec.companions]
    identity = {"model": spec.name, "repository": ONNX_REPOSITORY, "revision": ONNX_REVISION,
                "files": sorted(f"{'/'.join(a.parts)}:{a.size}:{a.sha256}" for a in assets if a is not None),
                "dimension": spec.dimension, "preprocess": PREPROCESS_VERSION, "tokenizer": TOKENIZER_VERSION,
                "imageSize": IMAGE_SIZE, "maxTextTokens": MAX_TEXT_TOKENS}
    return "siglip2-b16-224-int8:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def model_provenance() -> dict[str, Any]:
    return {"model": SIGLIP2_BASE_224_INT8.name, "baseModel": BASE_MODEL, "baseRevision": BASE_REVISION,
            "repository": ONNX_REPOSITORY, "revision": ONNX_REVISION, "licence": LICENCE,
            "fingerprint": fingerprint(), "dimension": DIMENSION, "runtime": SIGLIP2_BASE_224_INT8.runtime,
            "embeds": "the pixels of stored keyframes (image tower) and the query text (text tower)"}


# ------------------------------------------------------------------ verified assets

_VERIFIED_LOCK = threading.Lock()


def _asset_path(root: Path, asset: ModelAsset) -> Path:
    return Path(root, *asset.parts)


def _stat_reason(root: Path, asset: ModelAsset) -> str | None:
    from core.path_safety import is_link_or_reparse_point

    path = _asset_path(root, asset)
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return "image_encoder_not_provisioned"
    try:
        linked = any(is_link_or_reparse_point(p) for p in (path, path.parent))
    except OSError:
        linked = True
    if linked or not os.path.isfile(path):
        return "image_encoder_asset_refused"
    if info.st_size != asset.size:
        return "image_encoder_mismatch"
    return None


def read_verified(root: Path, asset: ModelAsset) -> bytes:
    """The asset's bytes, read once and checked by size and SHA-256 (those exact bytes are what gets used)."""
    reason = _stat_reason(root, asset)
    if reason is not None:
        raise EncoderUnavailable(reason, "/".join(asset.parts))
    path = _asset_path(root, asset)
    with _VERIFIED_LOCK:
        try:
            with open(path, "rb") as stream:  # read-only; never executed, never handed on by path
                data = stream.read(asset.size + 1)
        except OSError:
            raise EncoderUnavailable("image_encoder_not_provisioned", "/".join(asset.parts)) from None
    if len(data) != asset.size or hashlib.sha256(data).hexdigest() != asset.sha256:
        raise EncoderUnavailable("image_encoder_mismatch", "/".join(asset.parts))
    return data


# ------------------------------------------------------------------ the SentencePiece tokenizer (pure Python)

_NORMAL, _UNKNOWN, _CONTROL, _USER_DEFINED, _UNUSED, _BYTE = 1, 2, 3, 4, 5, 6
_SPACE = "▁"


class TokenizerInvalid(ValueError):
    """The SentencePiece model is not one this reader supports."""


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise TokenizerInvalid("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _fields(data: bytes) -> Iterator[tuple[int, int, Any]]:
    """(field number, wire type, value) of one protobuf message; value is int, bytes or a 4/8-byte blob."""
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 1:
            value, pos = data[pos:pos + 8], pos + 8
        elif wire == 2:
            length, pos = _varint(data, pos)
            value, pos = data[pos:pos + length], pos + length
        elif wire == 5:
            value, pos = data[pos:pos + 4], pos + 4
        else:
            raise TokenizerInvalid(f"unsupported wire type {wire}")
        if pos > len(data):
            raise TokenizerInvalid("truncated field")
        yield number, wire, value


@dataclass(frozen=True)
class Piece:
    text: str
    score: float
    kind: int


class SentencePieceBpe:
    """Encodes text exactly as a SentencePiece BPE model with byte fallback does (identity normaliser, no dummy prefix).

    Built from the bytes of a ``tokenizer.model`` (ModelProto): pieces (field 1: piece, score, type), the trainer
    spec (field 2: model_type 3, byte_fallback 35) and the normaliser spec (field 3: name 1, add_dummy_prefix 3,
    remove_extra_whitespaces 4). Anything this reader would encode differently is refused, not approximated.
    """

    def __init__(self, model: bytes) -> None:
        pieces: list[Piece] = []
        model_type, byte_fallback = None, False
        normaliser: dict[int, Any] = {}
        for number, wire, value in _fields(model):
            if number == 1 and wire == 2:
                text, score, kind = "", 0.0, _NORMAL
                for n, w, v in _fields(value):
                    if n == 1 and w == 2:
                        text = v.decode("utf-8")
                    elif n == 2 and w == 5:
                        score = struct.unpack("<f", v)[0]
                    elif n == 3 and w == 0:
                        kind = v
                pieces.append(Piece(text, score, kind))
            elif number == 2 and wire == 2:
                for n, w, v in _fields(value):
                    if n == 3 and w == 0:
                        model_type = v
                    elif n == 35 and w == 0:
                        byte_fallback = bool(v)
            elif number == 3 and wire == 2:
                normaliser = {n: v for n, w, v in _fields(value) if n in (1, 3, 4)}
        if model_type != 2:
            raise TokenizerInvalid("not a BPE SentencePiece model")
        if not byte_fallback:
            raise TokenizerInvalid("byte fallback is required")
        name = normaliser.get(1, b"").decode("utf-8") if isinstance(normaliser.get(1), bytes) else ""
        if name not in ("identity", "") or normaliser.get(3, 1) or normaliser.get(4, 1):
            # Defaults of a proto2 NormalizerSpec are add_dummy_prefix=1 and remove_extra_whitespaces=1: the model
            # must say both are off (Gemma does) or its text would be changed in ways this reader does not repeat.
            raise TokenizerInvalid("only the identity normaliser without dummy prefix or whitespace folding is supported")
        if len(pieces) <= max(PAD_ID, EOS_ID) or pieces[PAD_ID].kind != _CONTROL or pieces[EOS_ID].kind != _CONTROL:
            raise TokenizerInvalid("pad/eos are not the expected control pieces")
        self.pieces = pieces
        self._ids: dict[str, int] = {}
        self._merge: dict[str, tuple[float, int]] = {}
        self._user: dict[str, int] = {}
        self._bytes: dict[int, int] = {}
        self.unk_id: int | None = None
        for index, piece in enumerate(pieces):
            if piece.kind == _BYTE:
                if len(piece.text) == 6 and piece.text.startswith("<0x") and piece.text.endswith(">"):
                    self._bytes[int(piece.text[3:5], 16)] = index
                continue
            if piece.kind == _UNKNOWN:
                self.unk_id = index if self.unk_id is None else self.unk_id
                continue
            self._ids.setdefault(piece.text, index)
            if piece.kind == _NORMAL:
                self._merge.setdefault(piece.text, (piece.score, index))
            elif piece.kind == _USER_DEFINED and piece.text:
                self._user.setdefault(piece.text, index)
        if len(self._bytes) != 256:
            raise TokenizerInvalid("byte fallback pieces are incomplete")
        self._user_lengths = sorted({len(t) for t in self._user}, reverse=True)

    def _symbols(self, text: str) -> list[str]:
        symbols: list[str] = []
        i = 0
        while i < len(text):
            for length in self._user_lengths:  # user-defined pieces are matched whole, longest first
                if text[i:i + length] in self._user:
                    symbols.append(text[i:i + length])
                    i += length
                    break
            else:
                symbols.append(text[i])
                i += 1
        return symbols

    def encode(self, text: str) -> list[int]:
        """Token ids of ``text`` (no special tokens added)."""
        normalised = text.replace(" ", _SPACE)
        symbols = self._symbols(normalised)
        fixed = [s in self._user for s in symbols]
        while len(symbols) > 1:
            best: tuple[float, int] | None = None
            where = -1
            for i in range(len(symbols) - 1):
                if fixed[i] or fixed[i + 1]:
                    continue
                found = self._merge.get(symbols[i] + symbols[i + 1])
                if found is not None and (best is None or found[0] > best[0]):
                    best, where = found, i  # ties keep the leftmost pair
            if best is None:
                break
            symbols[where:where + 2] = [symbols[where] + symbols[where + 1]]
            fixed[where:where + 2] = [False]
        ids: list[int] = []
        for symbol in symbols:
            index = self._ids.get(symbol)
            if index is not None and self.pieces[index].kind in (_NORMAL, _USER_DEFINED):
                ids.append(index)
                continue
            ids.extend(self._bytes[b] for b in symbol.encode("utf-8"))
        return ids


def query_ids(tokenizer: SentencePieceBpe, query: str) -> list[int]:
    """The text tower's input for one query: lowercased, at most 63 tokens + EOS, padded with PAD to 64."""
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be 1..{MAX_QUERY_CHARS} characters")
    ids = tokenizer.encode(query.strip().lower())[:MAX_TEXT_TOKENS - 1] + [EOS_ID]
    return ids + [PAD_ID] * (MAX_TEXT_TOKENS - len(ids))


# ------------------------------------------------------------------ frame preprocessing

def preprocess_frame(data: bytes):
    """A stored keyframe (JPEG/PNG bytes) as the image tower's float32 [3, 224, 224] input."""
    import numpy as np
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in FRAME_FORMATS:
                raise FrameUnusable(f"format {image.format!r} is not a stored keyframe format")
            width, height = image.size
            if width < 1 or height < 1 or width * height > MAX_FRAME_PIXELS:
                raise FrameUnusable("frame dimensions are out of bounds")
            rgb = image.convert("RGB").resize((IMAGE_SIZE, IMAGE_SIZE), Image.Resampling.BILINEAR)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        if isinstance(exc, FrameUnusable):
            raise
        raise FrameUnusable("frame cannot be decoded") from None
    pixels = np.asarray(rgb, dtype=np.float32) * np.float32(1.0 / 255.0)
    pixels = (pixels - np.float32(IMAGE_MEAN)) / np.float32(IMAGE_STD)
    return np.ascontiguousarray(pixels.transpose(2, 0, 1))


# ------------------------------------------------------------------ vectors

def _normalised_rows(output: Any, count: int) -> list[bytes]:
    import numpy as np

    array = np.asarray(output)
    if array.dtype != np.float32 or array.shape != (count, DIMENSION):
        raise EncoderOutputInvalid(f"expected float32 [{count}, {DIMENSION}], got {array.dtype} {list(array.shape)}")
    if not np.isfinite(array).all():
        raise EncoderOutputInvalid("non-finite values")
    norms = np.linalg.norm(array.astype(np.float64), axis=1)
    if not (norms > 1e-6).all():
        raise EncoderOutputInvalid("a zero vector")
    unit = (array / norms[:, None]).astype("<f4")
    return [row.tobytes() for row in unit]


def decode_vector(blob: object) -> Any | None:
    """A stored vector, or None when it is not exactly a finite unit float32 vector of DIMENSION values."""
    import numpy as np

    if not isinstance(blob, bytes) or len(blob) != DIMENSION * 4:
        return None
    vector = np.frombuffer(blob, dtype="<f4")
    if not np.isfinite(vector).all() or abs(float(np.linalg.norm(vector.astype(np.float64))) - 1.0) > NORM_TOLERANCE:
        return None
    return vector


# ------------------------------------------------------------------ the runtime boundary

def _shape_ok(declared: Any, wanted: tuple[Any, ...]) -> bool:
    """A declared ONNX shape matches ``wanted`` (None = any size). A symbolic declared dimension (a name such as
    "height", or unknown) is accepted, as the pinned exports declare them; a different FIXED size is refused. The
    actual sizes are enforced on every call: the input is built here, and the output must be exactly [n, DIMENSION]."""
    if not isinstance(declared, (list, tuple)) or len(declared) != len(wanted):
        return False
    return all(w is None or d == w or not isinstance(d, int) for d, w in zip(declared, wanted))


def default_session_factory(model: bytes, tower: str) -> Any:
    """An onnxruntime session over VERIFIED bytes: CPU provider only, bounded threads, sequential, no custom ops."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = INTRA_OP_THREADS
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.log_severity_level = 3
    return ort.InferenceSession(model, sess_options=options, providers=["CPUExecutionProvider"])


SessionFactory = Callable[[bytes, str], Any]


class FrameEncoder:
    """The open towers for ONE lease. Use only inside :meth:`ImageEncoderProvider.open`."""

    def __init__(self, vision: Any = None, text: Any = None, tokenizer: SentencePieceBpe | None = None) -> None:
        self._vision = vision
        self._text = text
        self._tokenizer = tokenizer
        self._text_feeds: tuple[str, ...] = ()
        if vision is not None:
            self._check(vision, "vision", {VISION_INPUT: (None, 3, IMAGE_SIZE, IMAGE_SIZE)})
        if text is not None:
            names = self._check(text, "text", {TEXT_INPUT: (None, None), TEXT_OPTIONAL_MASK: (None, None)},
                                optional={TEXT_OPTIONAL_MASK})
            self._text_feeds = tuple(names)

    def close(self) -> None:
        """Drop every reference to the sessions (nothing can run on them afterwards)."""
        self._vision = self._text = self._tokenizer = None

    @staticmethod
    def _check(session: Any, tower: str, inputs: dict[str, tuple], optional: set[str] = frozenset()) -> list[str]:
        declared = {item.name: item for item in session.get_inputs()}
        unknown = set(declared) - set(inputs)
        missing = {name for name in inputs if name not in optional} - set(declared)
        if unknown or missing:
            raise EncoderUnavailable("image_encoder_contract", f"{tower} inputs {sorted(declared)}")
        for name, item in declared.items():
            if not _shape_ok(list(item.shape), inputs[name]):
                raise EncoderUnavailable("image_encoder_contract", f"{tower} input {name} shape {item.shape}")
        outputs = {item.name: item for item in session.get_outputs()}
        if OUTPUT not in outputs or not _shape_ok(list(outputs[OUTPUT].shape), (None, DIMENSION)):
            raise EncoderUnavailable("image_encoder_contract", f"{tower} output {OUTPUT} missing or not {DIMENSION}")
        return sorted(declared)

    def encode_frames(self, frames: list[Any]) -> list[bytes]:
        """Unit float32 vectors of at most IMAGE_BATCH preprocessed frames, one runtime call per frame (see IMAGE_BATCH)."""
        import numpy as np

        if self._vision is None:
            raise EncoderUnavailable("image_encoder_contract", "the image tower is not open")
        if not 1 <= len(frames) <= IMAGE_BATCH:
            raise ValueError(f"1..{IMAGE_BATCH} frames per call")
        batch = np.stack(frames).astype(np.float32, copy=False)
        if batch.shape[1:] != (3, IMAGE_SIZE, IMAGE_SIZE):
            raise ValueError("frames must be preprocessed [3, 224, 224]")
        vectors: list[bytes] = []
        for index in range(len(frames)):
            output = self._vision.run([OUTPUT], {VISION_INPUT: batch[index:index + 1]})[0]
            vectors += _normalised_rows(output, 1)
        return vectors

    def encode_query(self, query: str) -> bytes:
        """ONE unit float32 vector of the query text in the same space as the frames."""
        import numpy as np

        if self._text is None or self._tokenizer is None:
            raise EncoderUnavailable("image_encoder_contract", "the text tower is not open")
        ids = np.asarray([query_ids(self._tokenizer, query)], dtype=np.int64)
        feeds = {TEXT_INPUT: ids}
        if TEXT_OPTIONAL_MASK in self._text_feeds:
            feeds[TEXT_OPTIONAL_MASK] = np.ones_like(ids)  # the model was trained without a mask: attend to all
        return _normalised_rows(self._text.run([OUTPUT], feeds)[0], 1)[0]


class ImageEncoderProvider:
    """The runtime's capability for the declared image encoder (bound to the provisioned asset folder)."""

    def __init__(self, runtime_assets: Path, *, session_factory: SessionFactory | None = None,
                 spec: ModelSpec = SIGLIP2_BASE_224_INT8,
                 reader: Callable[[Path, ModelAsset], bytes] = read_verified,
                 checker: Callable[[Path, ModelAsset], str | None] = _stat_reason) -> None:
        self.root = Path(runtime_assets)
        self.spec = spec
        self._factory = session_factory or default_session_factory
        # The verified read and the cheap presence check (fixtures substitute synthetic files of the pinned roles).
        self._read = reader
        self._check = checker

    def unavailable_reason(self) -> str | None:
        """Cheap: are the three pinned files present at their pinned sizes (the hash is checked at open)."""
        for asset in (self.spec.weights, *self.spec.companions):
            reason = self._check(self.root, asset)
            if reason is not None:
                return reason
        return None

    def fingerprint(self) -> str:
        return fingerprint(self.spec)

    @contextmanager
    def open(self, lease: Any, *, image: bool = False, text: bool = False) -> Iterator[FrameEncoder]:
        """The requested towers, opened under ``lease`` (the declared model's ModelLease) and closed on exit."""
        from qronos_media.model_authority import ModelLease

        if not isinstance(lease, ModelLease) or lease.spec is not self.spec or lease.closed:
            raise ResourceRefused("model_lease_required", "image_embeddings")
        lease.open_session()  # counted BEFORE anything is loaded: the slot cannot be freed meanwhile
        sessions: list[Any] = []
        encoder: FrameEncoder | None = None
        try:
            vision = text_session = tokenizer = None
            if image:
                vision = self._factory(self._read(self.root, self.spec.weights), "vision")
                sessions.append(vision)
            if text:
                text_asset, tokenizer_asset = self.spec.companions
                try:
                    tokenizer = SentencePieceBpe(self._read(self.root, tokenizer_asset))
                except TokenizerInvalid as exc:
                    raise EncoderUnavailable("image_encoder_contract", f"tokenizer: {exc}") from None
                text_session = self._factory(self._read(self.root, text_asset), "text")
                sessions.append(text_session)
            encoder = FrameEncoder(vision, text_session, tokenizer)
            del vision, text_session, tokenizer
            yield encoder
        finally:
            if encoder is not None:
                encoder.close()
            for session in sessions:
                close = getattr(session, "close", None)  # fakes may offer it; onnxruntime frees on release
                if callable(close):
                    close()
            sessions.clear()
            lease.close_session()


# ------------------------------------------------------------------ search over stored frame vectors

_IN_SCOPE = ("NOT (EXISTS (SELECT 1 FROM files f WHERE f.asset_id = e.asset_id) AND NOT EXISTS (SELECT 1 FROM files f "
             "WHERE f.asset_id = e.asset_id AND (f.error IS NULL OR f.error != 'out_of_scope')))")


def indexed_count(db: sqlite3.Connection, asset_id: str | None) -> int:
    """Frame vectors of the CURRENT fingerprint in the current scope (and asset filter); 0 before schema 8."""
    try:
        return db.execute(
            f"SELECT COUNT(*) FROM image_embeddings e WHERE e.fingerprint = ? AND {_IN_SCOPE}"
            + (" AND e.asset_id = ?" if asset_id else ""),
            [fingerprint()] + ([asset_id] if asset_id else [])).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def image_candidates(db: sqlite3.Connection, query_vector: bytes, *, limit: int, asset_id: str | None,
                     check: Callable[[], None], assets: set[str] | None = None) -> tuple[list[dict], dict]:
    """Top frames by cosine of their stored IMAGE vectors; bounded chunked scan, current fingerprint and scope in SQL.

    The keyframe must still have the vector's frame SHA-256 recorded at embedding time (the row is joined to its
    keyframe). A stored vector that is not a finite unit vector of DIMENSION values is counted as corrupt and skipped.
    """
    import heapq

    import numpy as np

    query = decode_vector(query_vector)
    if query is None:
        raise EncoderOutputInvalid("the query vector is not a unit vector")
    where = f"e.fingerprint = ? AND {_IN_SCOPE}"
    params: list[Any] = [fingerprint()]
    if asset_id:
        where += " AND e.asset_id = ?"
        params.append(asset_id)
    if assets is not None:
        if not assets:
            return [], {"scanned": 0, "truncated": False, "corrupt": 0}
        where += f" AND e.asset_id IN ({','.join('?' for _ in assets)})"
        params.extend(sorted(assets))
    heap: list[tuple[float, int, tuple]] = []
    scanned, offset, counter, corrupt, truncated = 0, 0, 0, 0, False
    while True:
        check()
        rows = db.execute(
            "SELECT e.asset_id, e.scene_index, e.ordinal, e.time_ms, e.frame_sha256, c.vector "
            "FROM image_embeddings e JOIN keyframes k ON k.asset_id = e.asset_id AND k.scene_index = e.scene_index "
            "AND k.ordinal = e.ordinal "
            "JOIN image_vector_cache c ON c.fingerprint = e.fingerprint AND c.frame_sha256 = e.frame_sha256 "
            f"WHERE {where} ORDER BY e.rowid LIMIT {SCAN_CHUNK} OFFSET {offset}", params).fetchall()
        if not rows:
            break
        for row in rows:
            vector = decode_vector(row["vector"])
            if vector is None:
                corrupt += 1
                continue
            counter += 1
            score = float(np.dot(vector, query))
            item = (score, -counter, (row["asset_id"], row["scene_index"], row["ordinal"], row["time_ms"],
                                      row["frame_sha256"]))
            if len(heap) < limit:
                heapq.heappush(heap, item)
            elif item[0] > heap[0][0]:
                heapq.heapreplace(heap, item)
        scanned += len(rows)
        offset += SCAN_CHUNK
        if scanned >= MAX_SCAN_ROWS:
            truncated = True
            break
    best = sorted(heap, key=lambda entry: (-entry[0], -entry[1]))
    return [{"assetId": a, "sceneIndex": s, "ordinal": o, "timeMs": t, "frameSha256": sha,
             "similarity": round(score, 6)} for score, _, (a, s, o, t, sha) in best], \
        {"scanned": scanned, "truncated": truncated, "corrupt": corrupt}


def row_provenance(frame_sha256: str, frame_bytes: int, time_ms: int, scene_index: int, ordinal: int) -> dict[str, Any]:
    return {**model_provenance(), "kind": "image_embedding", "stageVersion": STAGE_VERSION,
            "frameSha256": frame_sha256, "frameBytes": frame_bytes, "timeMs": time_ms, "sceneIndex": scene_index,
            "ordinal": ordinal, "embeds": "the pixels of this stored keyframe"}


def frame_hit(db: sqlite3.Connection, item: dict[str, Any], *, dialogue: bool = True) -> dict[str, Any]:
    """One image-channel hit: the frame (time, SHA-256, its place), how it matched, the Vision caption of the SAME frame
    if one is stored (labelled generated), and - for the visual search - the dialogue that overlaps its time."""
    from qronos_media import visual_evidence

    hit: dict[str, Any] = {"assetId": item["assetId"], "sceneIndex": item["sceneIndex"], "ordinal": item["ordinal"],
                           "timeMs": item["timeMs"], "frameSha256": item["frameSha256"], "channels": ["image"],
                           "imageSimilarity": item["similarity"],
                           "scoreKind": "cosine over SigLIP 2 frame image vectors (uncalibrated)",
                           "caption": None, "visibleText": [], "observations": [], "generated": False,
                           "verified": False, "note": "matched by image similarity of the frame's pixels; not an "
                                                      "identification",
                           "provenance": {"kind": "image_embedding", "frameSha256": item["frameSha256"],
                                          "model": SIGLIP2_BASE_224_INT8.name, "fingerprint": fingerprint()}}
    row = db.execute("SELECT * FROM visual_evidence WHERE asset_id = ? AND scene_index = ? AND ordinal = ? "
                     "AND frame_sha256 = ? AND status = 'described'",
                     (item["assetId"], item["sceneIndex"], item["ordinal"], item["frameSha256"])).fetchone()
    if row is not None:
        described = visual_evidence.row_json(row)
        hit.update({key: described[key] for key in ("caption", "visibleText", "observations")})
        hit.update({"generated": True, "captionNote": "the Vision model's generated text of this same frame "
                                                      "(unverified, not OCR)"})
    if dialogue:
        title = db.execute("SELECT title FROM assets WHERE asset_id = ?", (item["assetId"],)).fetchone()
        hit["title"] = title["title"] if title else None
        hit["dialogue"] = visual_evidence.joined_dialogue(db, item["assetId"], item["timeMs"])
    return hit


def current_row(db: sqlite3.Connection, asset_id: str, time_ms: int, frame_sha256: str) -> bool:
    """The frame is still embedded for the CURRENT fingerprint with this SHA-256 at this time (report recheck)."""
    try:
        return db.execute("SELECT 1 FROM image_embeddings WHERE asset_id = ? AND time_ms = ? AND frame_sha256 = ? "
                          "AND fingerprint = ?", (asset_id, time_ms, frame_sha256, fingerprint())).fetchone() is not None
    except sqlite3.OperationalError:
        return False
