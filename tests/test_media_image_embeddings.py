"""Real frame vectors (stage "image_embeddings", schema 8): the declared SigLIP 2 image and text towers in ONE space.

FAKE onnxruntime sessions stand in for the two pinned graphs: the image tower answers from the frame's actual pixels
(its mean colour) and the text tower from the actual token ids of a small synthetic SentencePiece model, so a "red"
query is nearest to a red frame through the real preprocessing, tokenizer, batching, lease, stage, store and search.
Everything else is real: the runtime-issued authority and its model slot, the job runner, the store, the visual and
multimodal searches and the search-result report. The fakes prove contracts (what is embedded, where, how it is
stored, bounded, invalidated and labelled), never retrieval quality; no real model is loaded here.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import struct

import numpy as np
import pytest
from PIL import Image

from qronos_media import image_embeddings as ie
from qronos_media.resources import SIGLIP2_BASE_224_INT8, ModelAsset, ResourceRefused, StageNeeds

# ---------------------------------------------------------------- a synthetic SentencePiece model (protobuf bytes)

COLOURS = {"red": (230, 20, 20), "green": (20, 200, 30), "blue": (20, 30, 220)}
_NORMAL, _UNKNOWN, _CONTROL, _USER, _BYTE = 1, 2, 3, 4, 6


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number: int, wire: int, payload: bytes | int) -> bytes:
    key = _varint(number << 3 | wire)
    if wire == 0:
        return key + _varint(int(payload))
    if wire == 5:
        return key + payload
    return key + _varint(len(payload)) + payload


def _piece(text: str, score: float, kind: int) -> bytes:
    body = _field(1, 2, text.encode("utf-8")) + _field(2, 5, struct.pack("<f", score)) + _field(3, 0, kind)
    return _field(1, 2, body)


#: Pieces after the specials and the 256 bytes: (text, score). Higher score merges first (SentencePiece BPE).
WORDS = [("▁", -1.0), ("r", -2.0), ("e", -2.0), ("d", -2.0), ("g", -2.0), ("n", -2.0), ("b", -2.0), ("l", -2.0),
         ("u", -2.0), ("a", -2.0), ("re", -3.0), ("red", -4.0), ("▁red", -5.0), ("ee", -3.0), ("gr", -3.0),
         ("gree", -6.0), ("green", -7.0), ("▁green", -8.0), ("bl", -3.0), ("blu", -4.0), ("blue", -5.0),
         ("▁blue", -6.0), ("▁a", -9.0)]
USER = "<img>"


def sentencepiece_model(*, model_type: int = 2, byte_fallback: bool = True, normaliser: bool = True) -> bytes:
    pieces = [_piece("<pad>", 0.0, _CONTROL), _piece("<eos>", 0.0, _CONTROL), _piece("<bos>", 0.0, _CONTROL),
              _piece("<unk>", 0.0, _UNKNOWN), _piece(USER, 0.0, _USER)]
    pieces += [_piece(f"<0x{b:02X}>", 0.0, _BYTE) for b in range(256)]
    pieces += [_piece(text, score, _NORMAL) for text, score in WORDS]
    trainer = _field(3, 0, model_type) + (_field(35, 0, 1) if byte_fallback else b"")
    out = b"".join(pieces) + _field(2, 2, trainer)
    if normaliser:
        out += _field(3, 2, _field(1, 2, b"identity") + _field(3, 0, 0) + _field(4, 0, 0))
    return out


TOKENIZER = ie.SentencePieceBpe(sentencepiece_model())
IDS = {text: TOKENIZER._ids[text] for text, _ in WORDS}


# ---------------------------------------------------------------- fake onnxruntime sessions


class _Arg:
    def __init__(self, name, shape):
        self.name, self.shape = name, shape


class FakeVision:
    """Image tower stand-in: a 768-vector from the frame's own mean colour (pixels in, never a caption)."""

    def __init__(self, calls: list, *, inputs=None, outputs=None, answer=None):
        self.calls, self.closed = calls, False
        self._inputs = inputs or [_Arg("pixel_values", ["batch", 3, 224, 224])]
        self._outputs = outputs or [_Arg("last_hidden_state", ["batch", 196, 768]), _Arg("pooler_output", ["batch", 768])]
        self._answer = answer

    def get_inputs(self):
        return self._inputs

    def get_outputs(self):
        return self._outputs

    def run(self, names, feeds):
        assert names == ["pooler_output"] and set(feeds) == {"pixel_values"}
        batch = feeds["pixel_values"]
        assert batch.dtype == np.float32 and batch.shape[1:] == (3, 224, 224)
        self.calls.append(("vision", batch.shape[0]))
        if self._answer is not None:
            return [self._answer(batch)]
        out = np.full((batch.shape[0], 768), 0.01, dtype=np.float32)
        out[:, :3] = (batch.mean(axis=(2, 3)) + 1.0) ** 4  # channel means in [-1, 1] -> a dominant direction
        return [out]

    def close(self):
        self.closed = True


class FakeText:
    """Text tower stand-in: a 768-vector from the token ids (the colour word's piece sets its direction)."""

    def __init__(self, calls: list, *, mask=False, answer=None):
        self.calls, self.closed, self.mask, self._answer = calls, False, mask, answer

    def get_inputs(self):
        args = [_Arg("input_ids", ["batch", "sequence"])]
        return args + ([_Arg("attention_mask", ["batch", "sequence"])] if self.mask else [])

    def get_outputs(self):
        return [_Arg("last_hidden_state", ["batch", "sequence", 768]), _Arg("pooler_output", ["batch", 768])]

    def run(self, names, feeds):
        ids = feeds["input_ids"]
        assert ids.dtype == np.int64 and ids.shape == (1, 64)
        self.calls.append(("text", ids[0].tolist(), feeds.get("attention_mask")))
        if self._answer is not None:
            return [self._answer(ids)]
        out = np.full((1, 768), 0.01, dtype=np.float32)
        tokens = set(ids[0].tolist())
        for index, word in enumerate(("red", "green", "blue")):
            if IDS[word] in tokens or IDS["▁" + word] in tokens:
                out[0, index] = 16.0
        return [out]

    def close(self):
        self.closed = True


class Factory:
    def __init__(self, **text_options):
        self.calls: list = []
        self.opened: list = []
        self.text_options = text_options
        self.vision_answer = None

    def __call__(self, model: bytes, tower: str):
        session = (FakeVision(self.calls, answer=self.vision_answer) if tower == "vision"
                   else FakeText(self.calls, **self.text_options))
        self.opened.append((tower, model, session))
        return session


def fake_reader(root: Path, asset: ModelAsset) -> bytes:
    return sentencepiece_model() if asset.parts[-1] == "tokenizer.model" else b"verified graph bytes"


def provider(tmp_path: Path, factory: Factory | None = None, **options) -> ie.ImageEncoderProvider:
    return ie.ImageEncoderProvider(tmp_path / "runtime-assets", session_factory=factory or Factory(),
                                   reader=fake_reader, checker=lambda root, asset: None, **options)


def jpeg(colour, size=(64, 36)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, format="JPEG", quality=95)
    return out.getvalue()


def png(colour, size=(8, 8), mode="RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, colour).save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------- the tokenizer


def test_the_sentencepiece_reader_merges_by_score_keeps_user_pieces_whole_and_falls_back_to_bytes():
    assert TOKENIZER.encode("red") == [IDS["red"]]
    assert TOKENIZER.encode(" red") == [IDS["▁red"]]  # a space is "▁" (identity normaliser, no dummy prefix)
    assert TOKENIZER.encode("a red blue") == [IDS["a"], IDS["▁red"], IDS["▁blue"]]
    assert TOKENIZER.encode("green") == [IDS["green"]]
    assert TOKENIZER.encode(USER + "red") == [TOKENIZER._ids[USER], IDS["red"]]
    # Characters with no piece become their UTF-8 bytes (Persian included): nothing is dropped or replaced by <unk>.
    persian = "سرخ"
    assert TOKENIZER.encode(persian) == [TOKENIZER._bytes[b] for b in persian.encode("utf-8")]
    assert TOKENIZER.encode("<pad>")[0] != ie.PAD_ID  # a control piece is never produced from text


def test_a_query_is_lowercased_ends_with_eos_and_is_padded_or_truncated_to_64_tokens():
    ids = ie.query_ids(TOKENIZER, "  RED  ")
    assert ids[:2] == [IDS["red"], ie.EOS_ID] and ids[2:] == [ie.PAD_ID] * 62 and len(ids) == 64
    long = ie.query_ids(TOKENIZER, "red " * 100)
    assert len(long) == 64 and long[-1] == ie.EOS_ID and ie.PAD_ID not in long
    for bad in ("", "   ", "x" * (ie.MAX_QUERY_CHARS + 1), None):
        with pytest.raises(ValueError):
            ie.query_ids(TOKENIZER, bad)


@pytest.mark.parametrize("options", [{"model_type": 1}, {"byte_fallback": False}, {"normaliser": False}])
def test_a_tokenizer_this_reader_would_not_repeat_exactly_is_refused(options):
    with pytest.raises(ie.TokenizerInvalid):
        ie.SentencePieceBpe(sentencepiece_model(**options))
    with pytest.raises(ie.TokenizerInvalid):
        ie.SentencePieceBpe(b"\x0a\xff\xff")  # truncated protobuf


# ---------------------------------------------------------------- preprocessing


def test_a_frame_is_resized_to_224_bilinear_rescaled_and_normalised_as_the_pinned_processor():
    red = ie.preprocess_frame(png((255, 0, 0)))
    assert red.dtype == np.float32 and red.shape == (3, 224, 224) and red.flags["C_CONTIGUOUS"]
    assert np.all(red[0] == 1.0) and np.all(red[1] == -1.0) and np.all(red[2] == -1.0)  # (x/255 - 0.5) / 0.5
    grey = ie.preprocess_frame(png(128, mode="L"))  # converted to RGB first
    assert np.allclose(grey, (128 / 255 - 0.5) / 0.5, atol=1e-6)
    rgba = ie.preprocess_frame(png((0, 0, 255, 0), mode="RGBA"))
    assert np.all(rgba[2] == 1.0)
    # Bilinear: a two-colour image is blended at the seam exactly as PIL's own bilinear resize does.
    two = Image.new("RGB", (2, 1), (0, 0, 0))
    two.putpixel((1, 0), (255, 255, 255))
    data = io.BytesIO()
    two.save(data, format="PNG")
    expected = (np.asarray(two.resize((224, 224), Image.Resampling.BILINEAR), dtype=np.float32) / 255 - 0.5) / 0.5
    assert np.allclose(ie.preprocess_frame(data.getvalue()), expected.transpose(2, 0, 1), atol=1e-6)


def test_only_bounded_stored_frame_formats_are_accepted(monkeypatch):
    gif = io.BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(gif, format="GIF")
    for bad in (gif.getvalue(), b"not an image", b""):
        with pytest.raises(ie.FrameUnusable):
            ie.preprocess_frame(bad)
    monkeypatch.setattr(ie, "MAX_FRAME_PIXELS", 100)
    with pytest.raises(ie.FrameUnusable, match="out of bounds"):
        ie.preprocess_frame(png((1, 2, 3), size=(11, 10)))
    assert ie.preprocess_frame(jpeg((1, 2, 3), size=(10, 10))).shape == (3, 224, 224)


# ---------------------------------------------------------------- the runtime boundary


def test_both_towers_answer_finite_unit_vectors_of_the_declared_length_in_one_space():
    calls: list = []
    encoder = ie.FrameEncoder(FakeVision(calls), FakeText(calls), TOKENIZER)
    frames = [ie.preprocess_frame(jpeg(COLOURS[name])) for name in ("red", "green", "blue")]
    vectors = [np.frombuffer(v, dtype="<f4") for v in encoder.encode_frames(frames)]
    assert all(v.shape == (768,) and np.isfinite(v).all() and abs(np.linalg.norm(v) - 1) < 1e-5 for v in vectors)
    for index, word in enumerate(("red", "green", "blue")):
        query = np.frombuffer(encoder.encode_query(f"a {word}"), dtype="<f4")
        assert abs(np.linalg.norm(query) - 1) < 1e-5
        assert int(np.argmax([float(query @ v) for v in vectors])) == index  # the same space: text finds its frame
    assert calls[:3] == [("vision", 1)] * 3  # one frame per runtime call: no frame's vector depends on another
    with pytest.raises(ValueError):
        encoder.encode_frames(frames * 3)  # 9 > IMAGE_BATCH: never one unbounded call
    with pytest.raises(ValueError):
        encoder.encode_frames([])


def test_an_attention_mask_input_is_fed_all_ones_and_nothing_else_is_accepted():
    calls: list = []
    encoder = ie.FrameEncoder(None, FakeText(calls, mask=True), TOKENIZER)
    encoder.encode_query("red")
    assert np.all(calls[-1][2] == 1)
    extra = FakeText(calls)
    extra.get_inputs = lambda: [_Arg("input_ids", ["b", "s"]), _Arg("position_ids", ["b", "s"])]
    for vision in (FakeVision(calls, inputs=[_Arg("images", ["b", 3, 224, 224])]),
                   FakeVision(calls, inputs=[_Arg("pixel_values", ["b", 3, 384, 384])]),
                   FakeVision(calls, outputs=[_Arg("pooler_output", ["b", 1152])]),
                   FakeVision(calls, outputs=[_Arg("image_embeds", ["b", 768])])):
        with pytest.raises(ie.EncoderUnavailable) as refused:
            ie.FrameEncoder(vision, None, None)
        assert refused.value.reason == "image_encoder_contract"
    with pytest.raises(ie.EncoderUnavailable):
        ie.FrameEncoder(None, extra, TOKENIZER)


@pytest.mark.parametrize("answer", [
    lambda batch: np.full((batch.shape[0], 768), np.nan, dtype=np.float32),
    lambda batch: np.zeros((batch.shape[0], 768), dtype=np.float32),
    lambda batch: np.ones((batch.shape[0], 512), dtype=np.float32),
    lambda batch: np.ones((batch.shape[0], 768), dtype=np.float64),
    lambda batch: np.ones((batch.shape[0] + 1, 768), dtype=np.float32),
])
def test_an_answer_that_is_not_a_finite_vector_of_the_declared_length_is_refused(answer):
    encoder = ie.FrameEncoder(FakeVision([], answer=answer), None, None)
    with pytest.raises(ie.EncoderOutputInvalid):
        encoder.encode_frames([ie.preprocess_frame(png((1, 2, 3)))])


def test_the_default_session_is_cpu_only_bounded_and_built_from_the_verified_bytes(monkeypatch):
    import sys
    import types

    seen = {}

    class Options:
        pass

    fake = types.SimpleNamespace(
        SessionOptions=Options, ExecutionMode=types.SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        InferenceSession=lambda model, sess_options, providers: seen.update(
            model=model, options=vars(sess_options), providers=providers) or "session")
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    assert ie.default_session_factory(b"the verified bytes", "vision") == "session"
    assert seen["model"] == b"the verified bytes"  # bytes, never a path: no file beside it can be loaded
    assert seen["providers"] == ["CPUExecutionProvider"]
    assert seen["options"] == {"intra_op_num_threads": ie.INTRA_OP_THREADS, "inter_op_num_threads": 1,
                               "execution_mode": "sequential", "log_severity_level": 3}


def test_the_pinned_files_are_used_only_when_size_and_sha256_match(tmp_path):
    data = b"pinned graph"
    asset = ModelAsset(("models", "x", "g.onnx"), len(data), hashlib.sha256(data).hexdigest())
    root = tmp_path / "assets"
    assert ie._stat_reason(root, asset) == "image_encoder_not_provisioned"
    with pytest.raises(ie.EncoderUnavailable) as missing:
        ie.read_verified(root, asset)
    assert missing.value.reason == "image_encoder_not_provisioned"
    (root / "models" / "x").mkdir(parents=True)
    target = root / "models" / "x" / "g.onnx"
    target.write_bytes(b"other bytes!")
    assert ie._stat_reason(root, asset) is None  # the cheap check sees the pinned size only
    with pytest.raises(ie.EncoderUnavailable) as wrong:
        ie.read_verified(root, asset)
    assert wrong.value.reason == "image_encoder_mismatch"
    target.write_bytes(b"short")
    assert ie._stat_reason(root, asset) == "image_encoder_mismatch"
    target.write_bytes(data)
    assert ie._stat_reason(root, asset) is None and ie.read_verified(root, asset) == data
    target.unlink()
    target.mkdir()
    assert ie._stat_reason(root, asset) in {"image_encoder_asset_refused", "image_encoder_mismatch"}


def test_the_declared_encoder_is_a_separate_cpu_model_with_the_publisher_pins():
    from qronos_media.resources import BGE_M3, DECLARED_MODELS, QWEN3_VL_4B, WHISPER_LARGE_V3_TURBO_Q5_0

    spec = SIGLIP2_BASE_224_INT8
    assert DECLARED_MODELS == (WHISPER_LARGE_V3_TURBO_Q5_0, BGE_M3, QWEN3_VL_4B, spec)  # nothing replaced
    assert (spec.served_by, spec.weight, spec.vram_mb, spec.dimension) == ("in_process", "light", 0, 768)
    assert [(a.parts[-1], a.size, a.sha256) for a in (spec.weights, *spec.companions)] == [
        ("vision_model_quantized.onnx", 94_553_333, "5f2b401c1a4fc095702a5d45348e17ad46c4f87064085365b43c6e8eaa5c0070"),
        ("text_model_quantized.onnx", 283_438_275, "3a0603d3a00c05a80a6ded4743c16aaac7b1e62cdcc7e362e7ce418659b96400"),
        ("tokenizer.model", 4_241_003, "61a7b147390c64585d6c3543dd6fc636906c9af3865a5548f27f31aee1d4c8e2")]
    registry = json.loads((Path(__file__).resolve().parent.parent / "build_support" / "runtime_assets.json")
                          .read_text(encoding="utf-8"))
    entries = {e["path"]: e for e in registry["assets"]}
    for asset in (spec.weights, *spec.companions):
        entry = entries["runtime/" + "/".join(asset.parts)]
        # Verified only as the immutable publisher identity, which is exactly the pin (C97 registered it).
        assert entry["status"] == "verified" and (entry["bytes"], entry["sha256"]) == (asset.size, asset.sha256)
        assert asset.sha256 in entry["evidence"]
        assert ie.ONNX_REVISION in entry["source"] and "owner-review-required" in entry["licence"]
    assert ie.fingerprint().startswith("siglip2-b16-224-int8:") and ie.fingerprint() == ie.fingerprint(spec)


def test_the_new_module_has_no_network_process_or_dynamic_code_path():
    source = Path(ie.__file__).read_text(encoding="utf-8")
    for banned in ("import subprocess", "import socket", "urllib", "requests", "http", "eval(", "exec(",
                   "trust_remote_code", "CUDAExecutionProvider", "DmlExecutionProvider", "AzureExecutionProvider",
                   "register_custom_ops_library", "pickle", "import torch", "import transformers",
                   "from transformers", "import sentencepiece", "import tokenizers", "hf_hub", "snapshot_download"):
        assert banned not in source.replace("https://", ""), banned
    assert source.count("InferenceSession(") == 1 and 'providers=["CPUExecutionProvider"]' in source


# ---------------------------------------------------------------- the model slot


def test_the_in_process_slot_is_freed_only_once_every_session_is_closed(tmp_path):
    from tests.test_media_semantic import authority

    auth, state = authority(tmp_path)
    lease = auth.reserve("t1", StageNeeds("image_embeddings", model=SIGLIP2_BASE_224_INT8))
    reserve = next(e for e in auth.events if e["event"] == "reserve")
    assert (reserve["model"], reserve["vramMb"], reserve["ramMb"]) == (SIGLIP2_BASE_224_INT8.name, 0, 1536)
    factory = Factory()
    source = provider(tmp_path, factory)
    with source.open(lease, image=True, text=True) as encoder:
        assert lease.sessions == 1
        auth.release(lease)  # released with a session open: the slot stays held
        assert auth.status()["modelSlot"]["state"] == "session_open"
        with pytest.raises(ResourceRefused) as busy:
            auth.reserve("t2", StageNeeds("image_query", model=SIGLIP2_BASE_224_INT8))
        assert busy.value.reason == "session_open"
        encoder.encode_query("red")
    assert lease.sessions == 0 and auth.status()["modelSlot"] is None  # closed: freed (ledger and governor)
    assert all(session.closed for _, _, session in factory.opened)
    assert encoder._vision is None and encoder._text is None  # no reference survives the lease
    with pytest.raises(ResourceRefused):
        with source.open(lease, image=True):
            pass  # a released lease opens nothing
    other = auth.reserve("t3", StageNeeds("image_query", model=SIGLIP2_BASE_224_INT8))
    state.paused = True
    with pytest.raises(ResourceRefused) as paused:
        with source.open(other, text=True):
            pass
    assert paused.value.reason == "paused" and other.sessions == 0
    auth.release(other)
    assert auth.status()["modelSlot"] is None


def test_a_lease_for_another_model_opens_nothing(tmp_path):
    from qronos_media.resources import BGE_M3
    from tests.test_media_semantic import authority

    auth, _ = authority(tmp_path)
    lease = auth.reserve("t1", StageNeeds("semantic_query", model=BGE_M3))
    with pytest.raises(ResourceRefused) as refused:
        with provider(tmp_path).open(lease, image=True):
            pass
    assert refused.value.reason == "model_lease_required"
    auth.release(lease)


# ---------------------------------------------------------------- the stage, the store and the searches


FRAMES = [(0, 0, 5_000, "red"), (1, 0, 25_000, "green"), (2, 0, 45_000, "blue")]


def build(tmp_path: Path, *, encoder="fake", frames=FRAMES, factory: Factory | None = None):
    from qronos_media.service import MediaService
    from tests.media.media_support import FakePermissions
    from tests.test_media_semantic import authority

    tmp_path.mkdir(parents=True, exist_ok=True)
    auth, state = authority(tmp_path)
    factory = factory or Factory()
    image_encoder = provider(tmp_path, factory) if encoder == "fake" else encoder
    library = tmp_path / "library"
    library.mkdir()
    (library / "film.mkv").write_bytes(b"synthetic container stand-in")
    service = MediaService(tmp_path / "media-data", tools=None, authority=auth, permissions=FakePermissions(),
                           image_encoder=image_encoder)
    service.add_root(str(library), kind="folder", max_depth=2)
    service.scan()
    service.index(max_seconds=30, max_stages=0)
    asset_id = service.store.db.execute("SELECT asset_id FROM assets").fetchone()[0]
    with service.store.transaction() as tx:
        tx.execute("INSERT INTO subtitle_tracks(track_id, asset_id, source, stream_index, language, status, cue_count) "
                   "VALUES (?, ?, 'embedded', 2, 'eng', 'indexed', 2)", (f"{asset_id}:e2", asset_id))
        for seq, (start, text) in enumerate([(4_000, "Look at that car."), (44_000, "The sea is calm today.")]):
            tx.execute("INSERT INTO dialogue(asset_id, track_id, kind, seq, start_ms, end_ms, text, norm_text) "
                       "VALUES (?, ?, 'cue', ?, ?, ?, ?, ?)",
                       (asset_id, f"{asset_id}:e2", seq, start, start + 3_000, text, text.lower()))
        for scene, ordinal, at, colour in frames:
            write_keyframe(service, tx, asset_id, scene, ordinal, at, colour)
        tx.execute("UPDATE stages SET state = 'COMPLETED' WHERE asset_id = ? AND stage != 'image_embeddings'",
                   (asset_id,))
    return service, asset_id, auth, state, factory


def keyframe_dir(service, asset_id) -> Path:
    from qronos_media.paths import owned_child

    directory = owned_child(service.layout.cache, "keyframes", asset_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_keyframe(service, tx, asset_id, scene, ordinal, at, colour) -> str:
    data = jpeg(COLOURS[colour] if isinstance(colour, str) else colour)
    path = keyframe_dir(service, asset_id) / f"{scene:06d}_{ordinal}.jpg"
    path.write_bytes(data)
    tx.execute("INSERT OR IGNORE INTO scenes(asset_id, scene_index, start_ms, end_ms) VALUES (?, ?, ?, ?)",
               (asset_id, scene, at - 4_000, at + 4_000))
    tx.execute("DELETE FROM keyframes WHERE asset_id = ? AND scene_index = ? AND ordinal = ?", (asset_id, scene, ordinal))
    tx.execute("INSERT INTO keyframes(asset_id, scene_index, ordinal, time_ms, path, bytes) VALUES (?,?,?,?,?,?)",
               (asset_id, scene, ordinal, at, str(path), len(data)))
    return hashlib.sha256(data).hexdigest()


def stage_row(service, asset_id):
    return service.store.db.execute("SELECT * FROM stages WHERE asset_id = ? AND stage = 'image_embeddings'",
                                    (asset_id,)).fetchone()


def requeue(service, asset_id):
    with service.store.transaction() as tx:
        tx.execute("UPDATE stages SET state = 'QUEUED', checkpoint = NULL WHERE asset_id = ? AND stage = 'image_embeddings'",
                   (asset_id,))


def embedded(service, asset_id):
    return service.store.db.execute("SELECT * FROM image_embeddings WHERE asset_id = ? ORDER BY time_ms",
                                    (asset_id,)).fetchall()


def test_the_stage_embeds_the_pixels_of_every_stored_keyframe_with_provenance_and_the_fingerprint(tmp_path):
    service, asset_id, auth, _, factory = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    assert stage_row(service, asset_id)["state"] == "COMPLETED"
    rows = embedded(service, asset_id)
    shas = [hashlib.sha256((keyframe_dir(service, asset_id) / f"{s:06d}_{o}.jpg").read_bytes()).hexdigest()
            for s, o, _, _ in FRAMES]
    assert [r["time_ms"] for r in rows] == [5_000, 25_000, 45_000] and [r["frame_sha256"] for r in rows] == shas
    assert {r["fingerprint"] for r in rows} == {ie.fingerprint()}
    record = json.loads(rows[0]["provenance_json"])
    assert record["embeds"] == "the pixels of this stored keyframe" and record["frameSha256"] == shas[0]
    assert record["revision"] == ie.ONNX_REVISION and record["licence"].startswith("Apache-2.0")
    vectors = service.store.db.execute("SELECT dim, vector FROM image_vector_cache").fetchall()
    assert len(vectors) == 3 and all(r["dim"] == 768 and ie.decode_vector(r["vector"]) is not None for r in vectors)
    # Only the image tower was opened, once, for one batch; its sessions are closed and the slot is free.
    assert [t for t, _, _ in factory.opened] == ["vision"] and factory.calls == [("vision", 1)] * 3
    assert all(s.closed for _, _, s in factory.opened) and auth.status()["modelSlot"] is None
    events = [e["event"] for e in auth.events]
    assert events.count("reserve") == 1 and "released" in events


def test_unchanged_frames_load_no_model_and_only_a_changed_frame_is_embedded_again(tmp_path):
    service, asset_id, _, _, factory = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    requeue(service, asset_id)
    service.index(max_seconds=60, max_stages=10)
    assert stage_row(service, asset_id)["state"] == "COMPLETED"
    assert len(factory.opened) == 1 and len(embedded(service, asset_id)) == 3  # all cached: no session opened
    with service.store.transaction() as tx:  # keyframes re-ran: frame 1 now shows something else
        new_sha = write_keyframe(service, tx, asset_id, 1, 0, 25_000, (200, 200, 20))
    assert len(embedded(service, asset_id)) == 2  # the replaced keyframe took its vector row with it (FK cascade)
    requeue(service, asset_id)
    service.index(max_seconds=60, max_stages=10)
    assert factory.calls[-1] == ("vision", 1)  # only the changed frame
    assert [r["frame_sha256"] for r in embedded(service, asset_id)][1] == new_sha


def test_an_interrupted_run_resumes_from_the_stored_batches(tmp_path):
    frames = [(i, 0, 5_000 + 10_000 * i, ("red", "green", "blue")[i % 3] if i < 3 else (i * 20, 90, 200 - i * 10))
              for i in range(11)]
    service, asset_id, _, state, factory = build(tmp_path, frames=frames)
    real = factory.__call__

    def pausing(model, tower):
        session = real(model, tower)
        original = session.run

        def run(names, feeds):
            answer = original(names, feeds)
            state.paused = True  # Pause pressed while the first batch ran: observed at the next boundary
            return answer

        session.run = run
        return session

    service.image_encoder._factory = pausing
    service.index(max_seconds=60, max_stages=10)
    row = stage_row(service, asset_id)
    assert row["state"] == "QUEUED" and json.loads(row["checkpoint"]) == {"embedded": 8, "of": 11}
    assert service.store.db.execute("SELECT COUNT(*) FROM image_vector_cache").fetchone()[0] == 8
    assert not embedded(service, asset_id)  # nothing searchable before the stage commits
    state.paused = False
    service.image_encoder._factory = real
    service.index(max_seconds=60, max_stages=10)
    assert stage_row(service, asset_id)["state"] == "COMPLETED" and len(embedded(service, asset_id)) == 11
    assert factory.calls == [("vision", 1)] * 11  # the stored 8 were not embedded again


def test_visual_search_answers_by_image_similarity_with_frame_time_sha_and_overlapping_dialogue(tmp_path):
    service, asset_id, _, _, factory = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    answer = service.search_visual("red", limit=3)
    block = answer["visual"]
    # No Vision caption exists: the caption channel says so, the image channel answers anyway.
    assert block["status"] == "unavailable" and block["reason"] == "no_visual_evidence"
    image = block["image"]
    assert image["status"] == "complete" and image["scanned"] == 3 and image["corrupt"] == 0
    assert [hit["timeMs"] for hit in image["results"]][0] == 5_000
    top = image["results"][0]
    assert top["channels"] == ["image"] and top["caption"] is None and top["generated"] is False
    assert top["verified"] is False and "uncalibrated" in top["scoreKind"]
    assert top["imageSimilarity"] > image["results"][1]["imageSimilarity"]
    assert top["frameSha256"] == embedded(service, asset_id)[0]["frame_sha256"]
    assert [line["text"] for line in top["dialogue"]] == ["Look at that car."]  # joined by time only
    assert "not a probability" in image["scoreNote"] and image["model"]["fingerprint"] == ie.fingerprint()
    assert [t for t, _, _ in factory.opened][-1] == "text" and len([c for c in factory.calls if c[0] == "text"]) == 1
    assert answer["exact"] is not None  # the exact dialogue answer is still first and unchanged
    blue = service.search_visual("blue", limit=1)["visual"]["image"]["results"]
    assert len(blue) == 1 and blue[0]["timeMs"] == 45_000


def test_a_described_frame_shows_its_caption_beside_the_image_match_and_caption_search_is_unchanged(tmp_path):
    from tests.test_media_visual_semantic import write_frame

    service, asset_id, _, _, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    embedded_sha = embedded(service, asset_id)[0]["frame_sha256"]
    with service.store.transaction() as tx:
        write_frame(tx, asset_id, 0, 0, 5_000, "A red bicycle leans against a brick wall.", sha=embedded_sha)
    block = service.search_visual("bicycle red", limit=3)["visual"]
    assert block["status"] == "complete" and [h["channels"] for h in block["results"]] == [["lexical"]]
    top = block["image"]["results"][0]
    assert top["frameSha256"] == embedded_sha and top["caption"].startswith("A red bicycle")
    assert top["generated"] is True and "unverified" in top["captionNote"]


def test_vectors_of_another_fingerprint_corrupt_vectors_and_out_of_scope_assets_are_never_ranked(tmp_path):
    service, asset_id, _, _, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    with service.store.transaction() as tx:
        old = "siglip2-b16-224-int8:" + "0" * 64
        tx.execute("UPDATE image_embeddings SET fingerprint = ? WHERE time_ms = 25000", (old,))
        green = tx.execute("SELECT frame_sha256 FROM image_embeddings WHERE time_ms = 25000").fetchone()[0]
        tx.execute("INSERT INTO image_vector_cache(fingerprint, frame_sha256, dim, vector, created_at) "
                   "SELECT ?, frame_sha256, dim, vector, created_at FROM image_vector_cache WHERE frame_sha256 = ?",
                   (old, green))
        blue = tx.execute("SELECT frame_sha256 FROM image_embeddings WHERE time_ms = 45000").fetchone()[0]
        tx.execute("UPDATE image_vector_cache SET vector = ? WHERE frame_sha256 = ? AND fingerprint = ?",
                   (np.full(768, np.nan, dtype="<f4").tobytes(), blue, ie.fingerprint()))
    image = service.search_visual("green", limit=5)["visual"]["image"]
    assert [h["timeMs"] for h in image["results"]] == [5_000]  # green: another space; blue: corrupt
    assert image["scanned"] == 2 and image["corrupt"] == 1
    for bad in (b"\x00" * 12, np.ones(768, dtype="<f4").tobytes(), np.ones(767, dtype="<f4").tobytes(), None):
        assert ie.decode_vector(bad) is None  # wrong length, not unit, short, absent
    with service.store.transaction() as tx:
        tx.execute("UPDATE files SET error = 'out_of_scope' WHERE asset_id = ?", (asset_id,))
    gone = service.search_visual("red", limit=5)["visual"]["image"]
    assert gone["status"] == "unavailable" and gone["reason"] == "no_image_vectors"


def test_without_the_encoder_or_its_files_nothing_is_reserved_and_every_answer_says_why(tmp_path):
    missing = ie.ImageEncoderProvider(tmp_path / "empty-assets", session_factory=Factory())
    service, asset_id, auth, _, _ = build(tmp_path, encoder=missing)
    report = service.index(max_seconds=60, max_stages=10)
    row = stage_row(service, asset_id)
    assert row["state"] == "QUEUED" and row["note"] == "unavailable:image_encoder_not_provisioned"
    assert not [e for e in auth.events if e["event"] == "reserve"]  # no model slot was claimed for nothing
    assert any("image_embeddings" in item and "image_encoder_not_provisioned" in item for item in report["refused"])
    image = service.search_visual("red")["visual"]["image"]
    assert image["status"] == "unavailable" and image["reason"] == "no_image_vectors"
    assert image["encoder"] == "image_encoder_not_provisioned" and image["results"] == []
    vector, reason = service._image_query_vector("red")
    assert vector is None and reason == "image_encoder_not_provisioned"
    none = build(tmp_path / "no-encoder", encoder=None)[0]
    assert none.search_visual("red")["visual"]["image"]["reason"] == "image_encoder_unavailable"
    status = {item["capability"]: item["status"] for item in none.status()["pending"]}
    assert status["visual_embeddings"] == "implemented_unverified" and status["visual_semantic_text"] == "implemented_unverified"


def test_the_standalone_tool_never_runs_the_encoder(tmp_path):
    from qronos_media.resources import StandaloneCpuAuthority

    authority = StandaloneCpuAuthority(tmp_path, activity=lambda: None, reserve_bytes=0)
    with pytest.raises(ResourceRefused) as refused:
        authority.reserve("t", StageNeeds("image_embeddings", model=SIGLIP2_BASE_224_INT8))
    assert refused.value.reason == "requires_qronos_governor"


def test_a_busy_slot_leaves_the_caption_channels_and_reports_the_reason(tmp_path):
    service, asset_id, auth, _, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    held = auth.reserve("indexing", StageNeeds("image_embeddings", model=SIGLIP2_BASE_224_INT8))
    image = service.search_visual("red")["visual"]["image"]
    assert image["status"] == "unavailable" and image["reason"] == "model_busy"
    auth.release(held)
    assert service.search_visual("red")["visual"]["image"]["status"] == "complete"


def test_multimodal_image_match_fuses_dialogue_with_frames_by_their_pixels(tmp_path):
    service, asset_id, _, _, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    answer = service.search_multimodal("sea", "blue", window_ms=5_000, visual_match="image")
    block = answer["multimodal"]
    assert block["status"] == "complete" and block["outcome"] == "matched"
    assert block["scope"]["imageFrames"] == 3 and "imageEncoder" not in block["absent"]
    hit = block["results"][0]
    assert hit["dialogue"]["text"] == "The sea is calm today." and hit["matched"]["visual"] == ["image"]
    assert hit["frames"][0]["timeMs"] == 45_000 and hit["frames"][0]["imageSimilarity"] is not None
    channel = block["visualChannel"]["image"]
    assert channel["status"] == "complete" and channel["considered"] == 30 and "rank only" in channel["note"]
    lexical = service.search_multimodal("sea", "blue", window_ms=5_000)["multimodal"]
    assert lexical["status"] == "unavailable" and lexical["reason"] == "no_visual_evidence"  # captions only: none


def test_multimodal_without_the_encoder_falls_back_to_caption_frames_and_says_so(tmp_path):
    from tests.test_media_visual_semantic import write_frame

    service, asset_id, _, _, _ = build(tmp_path, encoder=None)
    with service.store.transaction() as tx:
        write_frame(tx, asset_id, 2, 0, 45_000, "A blue sea under a clear sky.")
    block = service.search_multimodal("sea", "blue", window_ms=5_000, visual_match="image")["multimodal"]
    assert block["status"] == "partial" and "visual_image_unavailable" in block["notes"]
    assert block["visualChannel"]["image"] == {"requested": True, "status": "unavailable",
                                               "reason": "image_encoder_unavailable"}
    assert block["results"] and block["results"][0]["matched"]["visual"] == ["lexical"]  # caption evidence only


def test_the_search_report_keeps_the_image_label_and_drops_a_frame_no_longer_embedded(tmp_path):
    from qronos_media import search_report

    service, asset_id, _, _, _ = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    answer = service.search_multimodal("sea", "blue", window_ms=5_000, visual_match="image")
    report = service.search_report(answer["multimodal"]["resultId"])
    frame = report["results"][0]["frames"][0]
    assert frame["channels"] == ["image"] and frame["imageSimilarity"] is not None and frame["generated"] is False
    assert any("SigLIP 2" in text and "شناسایی" in text for text in report["limitations"])
    assert report["channels"]["visual"]["image"]["requested"] is True
    assert search_report.render_bytes(report, "json") and search_report.render_bytes(report, "csv")
    with service.store.transaction() as tx:  # the keyframe was replaced: its embedding row is gone
        write_keyframe(service, tx, asset_id, 2, 0, 45_000, "red")
    again = service.search_report(answer["multimodal"]["resultId"])
    assert again["recheck"]["changed"] == 1 and again["results"] == []


def test_schema_7_indexes_gain_the_image_tables_without_losing_anything(tmp_path):
    from qronos_media.store import SCHEMA_VERSION, MediaStore

    path = tmp_path / "media.db"
    store = MediaStore(path)
    with store.transaction() as tx:
        tx.execute("DROP TABLE image_embeddings")
        tx.execute("DROP TABLE image_vector_cache")
        tx.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
    store.close()
    store = MediaStore(path)
    names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"image_embeddings", "image_vector_cache", "visual_semantic", "keyframes"} <= names
    assert SCHEMA_VERSION == 8 and store.get_meta("schema_version") == "8"
    store.close()


def test_an_asset_report_calls_image_similarity_available_only_when_this_asset_has_vectors(tmp_path):
    from qronos_media import reports

    service, asset_id, _, _, _ = build(tmp_path)
    with service.store.transaction() as tx:  # completed, but nothing embedded for this asset (e.g. no usable frame)
        tx.execute("UPDATE stages SET state = 'COMPLETED' WHERE asset_id = ? AND stage = 'image_embeddings'", (asset_id,))
    empty = reports.gather(service.store.db, asset_id, "full")
    assert "visual_embeddings" in {item["capability"] for item in empty["notAvailable"]}
    assert "image_similarity_uncalibrated" not in {item["code"] for item in empty["limitations"]}
    requeue(service, asset_id)
    service.index(max_seconds=60, max_stages=10)
    full = reports.gather(service.store.db, asset_id, "full")
    assert full["summary"]["keyframesImageEmbedded"] == 3
    assert "visual_embeddings" not in {item["capability"] for item in full["notAvailable"]}
    assert "image_similarity_uncalibrated" in {item["code"] for item in full["limitations"]}


def test_symbolic_graph_dimensions_are_accepted_and_a_different_fixed_size_is_not():
    # The pinned vision export declares ['batch_size', 'num_channels', 'height', 'width'] (found by the CPU smoke).
    calls: list = []
    symbolic = FakeVision(calls, inputs=[_Arg("pixel_values", ["batch_size", "num_channels", "height", "width"])],
                          outputs=[_Arg("pooler_output", ["batch_size", "hidden"])])
    encoder = ie.FrameEncoder(symbolic, None, None)
    assert len(encoder.encode_frames([ie.preprocess_frame(png((1, 2, 3)))])) == 1
    for fixed in ([_Arg("pixel_values", ["batch_size", 3, 256, 256])], [_Arg("pixel_values", ["b", 1, "h", "w"])]):
        with pytest.raises(ie.EncoderUnavailable):
            ie.FrameEncoder(FakeVision(calls, inputs=fixed), None, None)
    wrong = FakeVision(calls, outputs=[_Arg("pooler_output", ["b", "hidden"])],
                       answer=lambda batch: np.ones((batch.shape[0], 1152), dtype=np.float32))
    with pytest.raises(ie.EncoderOutputInvalid):  # a symbolic output is still held to [n, 768] at run time
        ie.FrameEncoder(wrong, None, None).encode_frames([ie.preprocess_frame(png((1, 2, 3)))])
