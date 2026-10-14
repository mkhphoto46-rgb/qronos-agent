"""Brain/voice turns reach the multimodal search (qronos_media.intents -> the typed product route -> the service).

Fixture transcripts only (no audio, STT or TTS): the same text the voice and chat turns hand to the runtime's media
handler. The runtime is never constructed: the bridge's handler methods run on a small fake runtime whose media
product is either a recording fake or the REAL MediaProduct over the synthetic multimodal index of
tests/test_media_multimodal (fake bge-m3 sidecar, synthetic rows). Nothing native, live or model-backed runs.
"""
from __future__ import annotations

import json

import pytest

from qronos_media.intents import (
    MediaClarifyIntent, MediaMultimodalIntent, MediaMultimodalSplitIntent, MediaSearchIntent, MediaStatusIntent,
    media_request, parse_media_intent, spoken_answer,
)

FA_EXPLICIT = "در ویدیوهام بگرد: گفتگو «father died» و تصویر «kitchen»"


# ---------------------------------------------------------------- the grammar


@pytest.mark.parametrize("text, expected", [
    # explicit, Persian and English, with the bounded window and match words
    (FA_EXPLICIT, MediaMultimodalIntent("father died", "kitchen")),
    ("در فیلم‌ها پیدا کن دیالوگ: «مرگ پدر» تصویر: «آشپزخانه» در فاصلهٔ ۳۰ ثانیه، معنایی",
     MediaMultimodalIntent("مرگ پدر", "آشپزخانه", 30_000, "semantic", "semantic")),
    ('search my videos for dialogue "father died" and visual "kitchen" within 45 seconds',
     MediaMultimodalIntent("father died", "kitchen", 45_000)),
    ('find in my movies the line "father died" showing "kitchen", fuzzy',
     MediaMultimodalIntent("father died", "kitchen", 15_000, "fuzzy", "lexical")),
    # one quoted natural request with the multimodal word: only a proposed split
    ("جست‌وجوی ترکیبی در فیلم‌ها: «صحنه‌ای در آشپزخانه که درباره مرگ پدرش حرف می‌زنند»",
     MediaMultimodalSplitIntent("صحنه‌ای در آشپزخانه که درباره مرگ پدرش حرف می‌زنند")),
    # ambiguous or single-constraint: a question, never a guess
    ("جست‌وجوی ترکیبی در ویدیوها: گفتگو «مرگ پدر»", MediaClarifyIntent("missing_visual")),
    ('search my videos for visual "kitchen"', MediaClarifyIntent("missing_dialogue")),
    ('search my videos for dialogue "a" and dialogue "b" and visual "kitchen"', MediaClarifyIntent("ambiguous")),
    ('search my videos for dialogue "father" and visual "kitchen" within 300 seconds', MediaClarifyIntent("window")),
    ('search my videos for dialogue "!!!" and visual "kitchen"', MediaClarifyIntent("malformed")),
    ("جست‌وجوی ترکیبی در فیلم‌ها برای آشپزخانه و مرگ پدر", MediaClarifyIntent("needs_parts")),
    # legacy grammar unchanged (a dialogue-labelled phrase alone is the dialogue search)
    ("در ویدیوهام «کلید قدیمی» را پیدا کن", MediaSearchIntent("کلید قدیمی", "exact")),
    ("در فیلم‌ها دنبال «قطار ساحلی» بگرد، معنایی", MediaSearchIntent("قطار ساحلی", "semantic")),
    ("search my videos for the old key", MediaSearchIntent("the old key", "exact")),
    ('find "the train" in my movies, fuzzy', MediaSearchIntent("the train", "fuzzy")),
    ('search my videos for the line "the old key"', MediaSearchIntent("the old key", "exact")),
    ("در فیلم‌ها صحنه‌ای پیدا کن که موضوعش «خیانت» است", MediaSearchIntent("خیانت", "topic")),
    ("وضعیت فهرست ویدیوها", MediaStatusIntent()),
    # not a library request, or an unlabelled natural request without the multimodal word: left to the chat
    ("search for movies about space", None),
    ("در فیلم‌هام صحنه‌ای در آشپزخانه پیدا کن که درباره مرگ پدرش حرف می‌زنند", None),
])
def test_the_grammar_routes_explicit_requests_and_asks_instead_of_guessing(text, expected):
    assert parse_media_intent(text) == expected


def test_each_intent_maps_to_exactly_one_typed_request_and_a_question_maps_to_none():
    assert media_request(MediaMultimodalIntent("a b", "c", 30_000, "semantic", "semantic"), "r1") == {
        "op": "search", "requestId": "r1", "mode": "multimodal", "dialogue": "a b", "visual": "c", "windowMs": 30_000,
        "dialogueMatch": "semantic", "visualMatch": "semantic", "limit": 10}
    assert media_request(MediaMultimodalSplitIntent("q"), "r2") == {"op": "search", "requestId": "r2",
                                                                     "mode": "multimodal", "query": "q"}
    assert media_request(MediaSearchIntent("k", "exact"), "r3") == {"op": "search", "requestId": "r3", "query": "k",
                                                                    "mode": "exact", "limit": 10}
    assert media_request(MediaStatusIntent(), "r4") == {"op": "status", "requestId": "r4"}
    assert media_request(MediaClarifyIntent("missing_visual"), "r5") is None


# ---------------------------------------------------------------- the runtime's handlers (fake runtime)


class FakeProduct:
    def __init__(self, answer=None) -> None:
        self.requests: list[dict] = []
        self.answer = answer or {"ok": True, "multimodal": {"status": "complete", "outcome": "neither", "results": []}}

    def handle(self, data, reply=None):
        self.requests.append(dict(data))
        if reply is not None:
            reply(self.answer)


def fake_runtime(product):
    from core import runtime_bridge

    finished = []
    runtime = type("Runtime", (), {
        "media_product": lambda self: product,
        "_run_media_intent": runtime_bridge.QronosRuntime._run_media_intent,
        "_finish_text_file_action": lambda self, **kw: finished.append(kw),
    })()
    return runtime, finished


def test_the_text_turn_and_the_voice_turn_send_the_same_typed_request():
    from core import runtime_bridge

    product = FakeProduct()
    runtime, finished = fake_runtime(product)
    assert runtime_bridge.QronosRuntime._begin_text_media_request(runtime, FA_EXPLICIT, "t1", 0.0) is True
    report: dict = {}
    voice = runtime_bridge.QronosRuntime._execute_voice_computer_action(runtime, FA_EXPLICIT, report)
    assert report["voiceMediaRequest"] == "MediaMultimodalIntent"
    text_request, voice_request = product.requests
    for request in (text_request, voice_request):
        assert request["requestId"].startswith("brain_")
        assert {k: v for k, v in request.items() if k != "requestId"} == {
            "op": "search", "mode": "multimodal", "dialogue": "father died", "visual": "kitchen", "windowMs": 15_000,
            "dialogueMatch": "exact", "visualMatch": "lexical", "limit": 10}
    assert finished[0]["response"] == voice and "هیچ‌کدام از دو شرط پیدا نشد" in voice


def test_a_clarification_asks_and_never_reaches_the_product():
    from core import runtime_bridge

    product = FakeProduct()
    runtime, finished = fake_runtime(product)
    assert runtime_bridge.QronosRuntime._begin_text_media_request(
        runtime, "جست‌وجوی ترکیبی در ویدیوها: گفتگو «مرگ پدر»", "t2", 0.0) is True
    assert product.requests == []  # nothing searched, nothing guessed
    assert "بخش تصویر را هم بگویید" in finished[0]["response"] and "چیزی جست‌وجو نشد" in finished[0]["response"]


def test_a_split_proposal_is_only_a_proposal_the_person_confirms_by_saying_both_parts():
    from core import runtime_bridge

    product = FakeProduct({"ok": True, "query": "q", "interpretation": {
        "status": "proposed", "dialogue": "father died", "visual": "kitchen", "method": "fast_brain",
        "generated": True, "verified": False}})
    runtime, _ = fake_runtime(product)
    answer = runtime_bridge.QronosRuntime._run_media_intent(runtime, MediaMultimodalSplitIntent("a kitchen scene ..."))
    assert product.requests[0]["mode"] == "multimodal" and "dialogue" not in product.requests[0]
    assert "تولیدشده، تأییدنشده" in answer and "هنوز چیزی جست‌وجو نشد" in answer
    said = answer.split("اگر درست است بگویید: ", 1)[1].rstrip(".")
    assert said.startswith("«") and said.endswith("»»")
    confirmation = said[1:-1]  # only the outer quotation marks around the sentence to say
    assert parse_media_intent(confirmation) == MediaMultimodalIntent("father died", "kitchen")  # the confirmed route


# ---------------------------------------------------------------- honest answers from the REAL product and service


def real_runtime(service):
    from qronos_media.product import MediaProduct
    from tests.test_media_multimodal import SyncHost

    runtime, finished = fake_runtime(MediaProduct(SyncHost(service), emit=lambda *args: None))
    return runtime


def say(runtime, text):
    from core import runtime_bridge

    return runtime_bridge.QronosRuntime._run_media_intent(runtime, parse_media_intent(text))


def test_a_spoken_result_names_both_channels_the_time_alignment_and_the_generated_frame_text(tmp_path):
    from tests.test_media_multimodal import build

    service, ids, _ = build(tmp_path)
    answer = say(real_runtime(service), FA_EXPLICIT)
    assert answer.startswith("1 نتیجهٔ ترکیبی پیدا شد")
    assert "دقیقهٔ 0:01:00" in answer and "«My father died last winter.»" in answer and "(دقیق)" in answer
    assert "فریم هم‌زمان با همین جمله است" in answer and "«A small kitchen with a kettle on the stove.»" in answer
    assert "توصیف تولیدشده با مدل بینایی، تأییدنشده" in answer and "شباهت تصویر، تشخیص گوینده و OCR به کار نرفته" in answer
    assert "باز کردن در همین لحظه" in answer  # opening stays the existing approved action
    assert str(tmp_path) not in answer and str(tmp_path).replace("\\", "/") not in answer and ".mkv" not in answer
    apart = say(real_runtime(service), 'search my videos for dialogue "train is late" and visual "street" within 49 s')
    assert apart.startswith("هر دو شرط در یک ویدیو پیدا شدند، اما نه در فاصلهٔ زمانی")
    near = say(real_runtime(service), 'search my videos for dialogue "train is late" and visual "street" within 50 s')
    assert "فریم 50.0 ثانیه پیش از جمله است" in near


def test_scope_partial_and_unavailable_are_said_honestly(tmp_path):
    from tests.test_media_multimodal import build

    service, ids, sidecar = build(tmp_path)
    service.embedder = None  # the model is gone: the lexical pair still answers, and the answer says what is missing
    partial = say(real_runtime(service), FA_EXPLICIT + "، معنایی")
    assert partial.startswith("1 نتیجهٔ ترکیبی") and "این پاسخ ناقص است" in partial
    assert "جست‌وجوی هم‌معنای گفتگو در دسترس نبود" in partial and "جست‌وجوی هم‌معنای تصویر در دسترس نبود" in partial
    with service.store.transaction() as tx:
        tx.execute("UPDATE files SET error = 'out_of_scope' WHERE asset_id = ?", (ids["A"],))
    scoped = say(real_runtime(service), FA_EXPLICIT)
    assert scoped == "فقط شرط گفتگو پیدا شد؛ هیچ فریمی با شرط تصویر جور نبود."  # A's frames are out of scope
    with service.store.transaction() as tx:
        tx.execute("UPDATE files SET error = 'out_of_scope'")
    gone = say(real_runtime(service), FA_EXPLICIT)
    assert gone == "جست‌وجوی ترکیبی در دسترس نیست: هنوز هیچ فریمی با مدل بینایی توصیف نشده است."


def test_without_a_fast_brain_the_split_says_so_and_nothing_is_searched(tmp_path):
    from tests.test_media_multimodal import build

    service, _, _ = build(tmp_path)
    answer = say(real_runtime(service), "جست‌وجوی ترکیبی در فیلم‌ها: «صحنه‌ای در آشپزخانه که درباره مرگ پدرش حرف می‌زنند»")
    assert answer.startswith("تفکیک خودکار این درخواست ممکن نشد (model_unavailable)") and "چیزی جست‌وجو نشد" in answer


def test_indexed_and_generated_text_is_quoted_on_one_line_without_controls_and_paths_never_appear():
    result = {"ok": True, "multimodal": {"status": "complete", "outcome": "matched", "results": [{
        "assetId": "a_" + "1" * 32, "title": "Harbour\nfile", "startMs": 60_000, "endMs": 64_000,
        "dialogue": {"startMs": 60_000, "text": "He said \u202eevil\u202c\r\n«quoted»", "channels": ["exact"],
                     "paths": ["C:/test-library/film.mkv"]},
        "frames": [{"timeMs": 70_000, "caption": "x" * 400, "relation": "after", "gapMs": 6_000, "channels": ["lexical"],
                    "provenance": {"source": "C:/cache/keyframes/000001.jpg"}}]}]}}
    answer = spoken_answer(MediaMultimodalIntent("he said", "x"), result)
    assert "\u202e" not in answer and "\n" not in answer and "«quoted»" not in answer and "quoted" in answer
    assert "C:/" not in answer and "film.mkv" not in answer and "x" * 120 not in answer and "…" in answer
    assert "فریم 6.0 ثانیه پس از جمله است" in answer
    assert json.dumps(answer, ensure_ascii=False)  # a plain string for the chat
