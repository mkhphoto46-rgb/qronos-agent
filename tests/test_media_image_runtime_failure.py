"""B1 (Main77 image peer): a failing image-encoder RUNTIME is contained as "image_encoder_failed".

onnxruntime raises its own pybind exception classes (Fail, RuntimeException, InvalidArgument, ...), which derive from
Exception only, and a MemoryError can occur while reading or allocating. These fixtures make the author's fake towers
raise the REAL classes (only instantiated here; no model runs): the visual and multimodal searches keep their other
answers and say why, the stage ends FAILED and the runner carries on, the slot is freed, no vector is committed, and a
ValueError keeps its documented meaning. Adapted from work/main-image-peer-r77/r77tests/test_r77_image_peer.py.
"""
from __future__ import annotations

from onnxruntime.capi import onnxruntime_pybind11_state as ort_errors
import pytest

from qronos_media import image_embeddings as ie
from tests.test_media_image_embeddings import Factory, build, embedded, stage_row


def failing(error: BaseException):
    def answer(_):
        raise error
    return answer


ORT_ERRORS = [lambda: ort_errors.RuntimeException("bad allocation"), lambda: ort_errors.Fail("session run failed"),
              MemoryError]  # factories: a fresh exception per test
ORT_IDS = ["RuntimeException", "Fail", "MemoryError"]


def test_control_a_value_error_at_the_same_point_keeps_its_documented_meaning(tmp_path):
    service, asset_id, auth, _, factory = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    factory.text_options["answer"] = failing(ValueError("not a vector"))
    answer = service.search_visual("red", limit=3)
    assert answer["exact"] is not None and auth.status()["modelSlot"] is None
    assert answer["visual"]["image"]["status"] == "unavailable"
    assert answer["visual"]["image"]["reason"] == "image_encoder_answer_rejected"


@pytest.mark.parametrize("error", ORT_ERRORS, ids=ORT_IDS)
def test_a_text_tower_runtime_error_keeps_the_other_answers_and_frees_the_slot(tmp_path, error):
    service, asset_id, auth, _, factory = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    factory.text_options["answer"] = failing(error())
    answer = service.search_visual("red", limit=3)
    assert auth.status()["modelSlot"] is None  # the lease is released and the session closed
    assert answer["exact"] is not None and answer["visual"]["reason"] == "no_visual_evidence"  # the caption channel
    image = answer["visual"]["image"]
    assert (image["status"], image["reason"], image["results"]) == ("unavailable", "image_encoder_failed", [])
    assert "Traceback" not in str(answer) and str(tmp_path) not in str(image)


@pytest.mark.parametrize("error", ORT_ERRORS, ids=ORT_IDS)
def test_a_text_tower_runtime_error_in_multimodal_falls_back_and_says_so(tmp_path, error):
    from tests.test_media_visual_semantic import write_frame

    service, asset_id, auth, _, factory = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    with service.store.transaction() as tx:  # a caption of the blue frame: the lexical fallback has evidence
        write_frame(tx, asset_id, 2, 0, 45_000, "A blue sea under a clear sky.",
                    sha=embedded(service, asset_id)[2]["frame_sha256"])
    factory.text_options["answer"] = failing(error())
    block = service.search_multimodal("sea", "blue", window_ms=5_000, visual_match="image")["multimodal"]
    assert auth.status()["modelSlot"] is None
    assert block["visualChannel"]["image"] == {"requested": True, "status": "unavailable",
                                               "reason": "image_encoder_failed"}
    assert block["status"] == "partial" and "visual_image_unavailable" in block["notes"]
    assert block["results"] and block["results"][0]["matched"]["visual"] == ["lexical"]


def test_an_image_tower_runtime_error_fails_the_stage_and_the_runner_carries_on(tmp_path):
    factory = Factory()

    def explode(batch):
        raise ort_errors.RuntimeException("bad allocation")

    factory.vision_answer = explode
    service, asset_id, auth, _, _ = build(tmp_path, factory=factory)
    report = service.index(max_seconds=60, max_stages=10)  # nothing escapes
    row = stage_row(service, asset_id)
    assert row["state"] == "FAILED" and row["error"] == "StageFailed: image encoder failed: RuntimeException"
    assert any("image_embeddings" in key for key in report["failed"])
    assert embedded(service, asset_id) == [] and auth.status()["modelSlot"] is None


def test_a_session_that_cannot_be_created_is_the_same_contained_failure(tmp_path):
    service, asset_id, auth, _, factory = build(tmp_path)
    service.index(max_seconds=60, max_stages=10)
    service.image_encoder._factory = failing(ort_errors.InvalidArgument("bad graph"))
    assert service._image_query_vector("red") == (None, "image_encoder_failed")
    assert auth.status()["modelSlot"] is None


def test_the_boundary_keeps_typed_failures_and_never_swallows_an_interrupt():
    assert ie._runtime(lambda: 7) == 7
    for own in (ie.EncoderOutputInvalid("x"), ValueError("x"), ie.TokenizerInvalid("x"),
                ie.EncoderUnavailable("image_encoder_contract", "x")):
        with pytest.raises(type(own)):
            ie._runtime(failing(own), None)
    with pytest.raises(ie.EncoderRuntimeFailed) as failed:
        ie._runtime(failing(ort_errors.Fail("C:/private/path/model.onnx")), None)
    assert failed.value.reason == "image_encoder_failed" and "private" not in str(failed.value)
    assert failed.value.__cause__ is None and failed.value.__suppress_context__
    for interrupt in (KeyboardInterrupt(), SystemExit(1)):
        with pytest.raises(type(interrupt)):
            ie._runtime(failing(interrupt), None)
