"""Deterministic Persian/English media requests for the brain and voice turns (no model, no guess).

    «کلید» را در ویدیوهام پیدا کن        در فیلم‌ها دنبال «قطار ساحلی» بگرد (معنایی)
    در فیلم‌ها صحنه‌ای پیدا کن که موضوعش «خیانت» است    find a scene in my movies about "betrayal" (topic)
    search my videos for "the old key"    find "the train" in my movies
    وضعیت فهرست ویدیوها                     media index status

A request names the media library (video/film/media words) AND asks to search or for status. The search phrase is
taken verbatim from quotes (« », " ", “ ”) or, in English, after "for"; without a phrase nothing is guessed. The
result is one of the typed :mod:`qronos_media.product` operations; the caller runs exactly that.

Multimodal (dialogue AND visual, qronos_media.multimodal), only with a label right before each quoted part:

    در ویدیوهام بگرد: گفتگو «مرگ پدر» و تصویر «آشپزخانه» در فاصلهٔ ۳۰ ثانیه
    search my videos for dialogue "father died" and visual "kitchen" within 30 seconds

One labelled part of each kind runs the multimodal search (``معنایی``/semantic: both channels by meaning;
``تقریبی``/fuzzy: fuzzy dialogue). A visual part alone, two parts of one kind, a window over 120 s, a constraint
the search would refuse, or the word ``ترکیبی``/multimodal without both parts is answered with a question and
nothing is searched. ``ترکیبی``/multimodal with ONE unlabelled quoted request only asks for a proposed split, which
the person confirms by saying the two labelled parts. A dialogue-labelled phrase alone stays the dialogue search.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from qronos_media import multimodal
from qronos_media.product import MAX_QUERY_CHARS


@dataclass(frozen=True)
class MediaSearchIntent:
    query: str
    mode: str  # "exact", "fuzzy", "semantic" or "topic"


@dataclass(frozen=True)
class MediaStatusIntent:
    pass


@dataclass(frozen=True)
class MediaMultimodalIntent:
    """Both explicit constraints, said by the person: the multimodal search runs with exactly these."""

    dialogue: str
    visual: str
    window_ms: int = multimodal.DEFAULT_WINDOW_MS
    dialogue_match: str = "exact"
    visual_match: str = "lexical"


@dataclass(frozen=True)
class MediaMultimodalSplitIntent:
    """One quoted natural request: only a PROPOSED split comes back; nothing is searched until confirmed."""

    query: str


@dataclass(frozen=True)
class MediaClarifyIntent:
    """A multimodal request that cannot run as said; a question is answered and nothing is searched."""

    reason: str  # missing_visual | missing_dialogue | ambiguous | window | malformed | needs_parts


MediaIntent = (MediaSearchIntent | MediaStatusIntent | MediaMultimodalIntent | MediaMultimodalSplitIntent
               | MediaClarifyIntent)

_MEDIA = re.compile(r"(?:\bvideos?\b|\bmovies?\b|\bfilms?\b|\bmedia\b|ویدیو|ویدئو|فیلم|رسانه)", re.IGNORECASE)
_SEARCH = re.compile(r"(?:\bsearch\b|\bfind\b|\blook for\b|بگرد|جستجو|جست‌وجو|جست و جو|پیدا کن|پیدا کنید)",
                     re.IGNORECASE)
_STATUS = re.compile(r"(?:\bstatus\b|\bprogress\b|وضعیت|پیشرفت)", re.IGNORECASE)
_INDEX = re.compile(r"(?:\bindex\b|\bindexing\b|\blibrary\b|فهرست|نمایه|کتابخانه)", re.IGNORECASE)
_SEMANTIC = re.compile(r"(?:\bsemantic\b|\bsimilar\b|\bmeaning\b|معنایی|مشابه|به معنی)", re.IGNORECASE)
_TOPIC = re.compile(r"(?:\btopic\b|\babout\b|\bscene\b|موضوع|درباره|دربارهٔ|صحنه‌ای که|صحنه ای که)",
                    re.IGNORECASE)
_FUZZY = re.compile(r"(?:\bfuzzy\b|\bapproximate\b|تقریبی)", re.IGNORECASE)
_MINE = re.compile(r"\bmy\s+(?:videos?|movies?|films?|media)\b", re.IGNORECASE)
_QUOTED = re.compile(r"[«\"“]([^«»\"“”]{1,1200})[»\"”]")
_ENGLISH_FOR = re.compile(r"\b(?:search|look)\b.*?\bfor\s+(.{1,1200}?)\s*[.?!]?\s*$", re.IGNORECASE)
_DIALOGUE_LABEL = r"(?:گفتگو|گفت‌وگو|گفت و گو|دیالوگ|جمله|\bdialogue\b|\bdialog\b|\bsaying\b|\bsays\b|\bline\b)"
_VISUAL_LABEL = r"(?:تصویر|\bvisual\b|\bshowing\b|\bshows\b|\bimage\b|\bpicture\b)"
_LABELLED = re.compile(rf"(?P<label>{_DIALOGUE_LABEL}|{_VISUAL_LABEL})\s*[:：]?\s*[«\"“](?P<text>[^«»\"“”]{{1,1200}})[»\"”]",
                       re.IGNORECASE)
_MULTIMODAL = re.compile(r"(?:\bmultimodal\b|\bcombined\b|ترکیبی)", re.IGNORECASE)
_WINDOW = re.compile(r"(?:\bwithin\b|در فاصله‌ی|در فاصلهٔ|در فاصله)\s*([0-9۰-۹٠-٩]{1,6})\s*(?:\bs\b|\bsec\w*|ثانیه)",
                     re.IGNORECASE)
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _multimodal_intent(text: str) -> MediaIntent | None:
    """The multimodal grammar; None leaves the request to the dialogue grammar (legacy behaviour)."""
    labelled = [(m.group("label"), m.group("text").strip()) for m in _LABELLED.finditer(text)]
    dialogue = [part for label, part in labelled if re.fullmatch(_DIALOGUE_LABEL, label, re.IGNORECASE)]
    visual = [part for label, part in labelled if not re.fullmatch(_DIALOGUE_LABEL, label, re.IGNORECASE)]
    outside = _QUOTED.sub(" ", text)
    cue = _MULTIMODAL.search(outside)
    if not visual and not cue:
        return None  # a dialogue-labelled phrase alone is the dialogue search
    if len(dialogue) > 1 or len(visual) > 1:
        return MediaClarifyIntent("ambiguous")
    if dialogue and visual:
        window = multimodal.DEFAULT_WINDOW_MS
        said = _WINDOW.search(outside)
        if said is not None:
            seconds = int(said.group(1).translate(_DIGITS))
            if seconds * 1000 > multimodal.MAX_WINDOW_MS:
                return MediaClarifyIntent("window")  # never quietly narrowed or widened
            window = seconds * 1000
        semantic = bool(_SEMANTIC.search(outside))
        dialogue_match = "semantic" if semantic else "fuzzy" if _FUZZY.search(outside) else "exact"
        try:
            spec = multimodal.check_constraints(dialogue[0], visual[0], window_ms=window, dialogue_match=dialogue_match,
                                                visual_match="semantic" if semantic else "lexical")
        except multimodal.ConstraintRefused:
            return MediaClarifyIntent("malformed")
        return MediaMultimodalIntent(spec.dialogue, spec.visual, spec.window_ms, spec.dialogue_match, spec.visual_match)
    if visual:
        return MediaClarifyIntent("missing_dialogue")
    if dialogue:
        return MediaClarifyIntent("missing_visual")
    quoted = [part.strip() for part in _QUOTED.findall(text) if part.strip()]
    if len(quoted) == 1 and len(quoted[0]) <= MAX_QUERY_CHARS:
        return MediaMultimodalSplitIntent(quoted[0])
    return MediaClarifyIntent("needs_parts")


def parse_media_intent(text: str) -> MediaIntent | None:
    if not isinstance(text, str) or not text.strip() or len(text) > 4 * MAX_QUERY_CHARS:
        return None
    if not _MEDIA.search(text):
        return None
    if _SEARCH.search(text):
        combined = _multimodal_intent(text)
        if combined is not None:
            return combined
        quoted = _QUOTED.search(text)
        phrase = quoted.group(1) if quoted else None
        if phrase is None and _MINE.search(text):  # unquoted only for "... my videos/movies ..." in English
            match = _ENGLISH_FOR.search(text)
            phrase = match.group(1) if match else None
            if phrase is not None:
                phrase = re.sub(r"\s+in\s+my\s+(?:videos?|movies?|films?|media)\b.*$", "", phrase, flags=re.IGNORECASE)
        if phrase is None or not phrase.strip() or len(phrase) > MAX_QUERY_CHARS:
            return None
        outside = _QUOTED.sub(" ", text)  # a cue word inside the quoted phrase is part of the phrase, not the mode
        mode = ("semantic" if _SEMANTIC.search(text) else "topic" if _TOPIC.search(outside)
                else "fuzzy" if _FUZZY.search(text) else "exact")
        return MediaSearchIntent(phrase.strip(), mode)
    if _STATUS.search(text) and _INDEX.search(text):
        return MediaStatusIntent()
    return None


def clock(ms: object) -> str:
    try:
        total = max(0, int(ms)) // 1000
    except (TypeError, ValueError):
        return "?"
    return f"{total // 3600:d}:{total // 60 % 60:02d}:{total % 60:02d}"


def media_request(intent: MediaIntent, request_id: str) -> dict | None:
    """The exact typed product request for an intent, or None for a clarification (nothing is asked of the product)."""
    if isinstance(intent, MediaClarifyIntent):
        return None
    if isinstance(intent, MediaSearchIntent):
        return {"op": "search", "requestId": request_id, "query": intent.query, "mode": intent.mode, "limit": 10}
    if isinstance(intent, MediaMultimodalIntent):
        return {"op": "search", "requestId": request_id, "mode": "multimodal", "dialogue": intent.dialogue,
                "visual": intent.visual, "windowMs": intent.window_ms, "dialogueMatch": intent.dialogue_match,
                "visualMatch": intent.visual_match, "limit": 10}
    if isinstance(intent, MediaMultimodalSplitIntent):
        return {"op": "search", "requestId": request_id, "mode": "multimodal", "query": intent.query}
    return {"op": "status", "requestId": request_id}


_EXAMPLE = "مثلاً: «در ویدیوهام بگرد: گفتگو «مرگ پدر» و تصویر «آشپزخانه»»"
_CLARIFY = {
    "missing_visual": "برای جست‌وجوی ترکیبی بخش تصویر را هم بگویید (آنچه دیده می‌شود)؛ " + _EXAMPLE + ". چیزی جست‌وجو نشد.",
    "missing_dialogue": "برای جست‌وجوی ترکیبی بخش گفتگو را هم بگویید (آنچه گفته می‌شود)؛ " + _EXAMPLE
                        + ". جست‌وجوی فقط تصویری از بخش رسانه انجام می‌شود. چیزی جست‌وجو نشد.",
    "ambiguous": "برای هر کدام از گفتگو و تصویر فقط یک بخش بگویید؛ کدام را می‌خواهید؟ چیزی جست‌وجو نشد.",
    "window": "فاصلهٔ زمانی حداکثر ۱۲۰ ثانیه است؛ فاصلهٔ کوتاه‌تری بگویید. چیزی جست‌وجو نشد.",
    "malformed": "هر بخش باید چند واژهٔ قابل جست‌وجو داشته باشد (حداکثر ۱۶ واژه و ۳۰۰ نویسه)؛ کوتاه‌تر یا روشن‌تر بگویید."
                 " چیزی جست‌وجو نشد.",
    "needs_parts": "برای جست‌وجوی ترکیبی دو بخش را جدا بگویید، یا کل درخواست را در گیومه بگذارید تا پیشنهاد تفکیک بدهم؛ "
                   + _EXAMPLE + ". چیزی جست‌وجو نشد.",
}
_OUTCOME = {
    "same_asset_outside_window": "هر دو شرط در یک ویدیو پیدا شدند، اما نه در فاصلهٔ زمانی خواسته‌شده؛ نتیجهٔ ترکیبی نیست.",
    "different_assets": "شرط گفتگو و شرط تصویر هر کدام پیدا شدند، اما در ویدیوهای جدا؛ نتیجهٔ ترکیبی نیست.",
    "only_dialogue": "فقط شرط گفتگو پیدا شد؛ هیچ فریمی با شرط تصویر جور نبود.",
    "only_visual": "فقط شرط تصویر پیدا شد؛ هیچ جمله‌ای با شرط گفتگو جور نبود.",
    "neither": "هیچ‌کدام از دو شرط پیدا نشد.",
}
_UNAVAILABLE = {"no_visual_evidence": "هنوز هیچ فریمی با مدل بینایی توصیف نشده است",
                "no_visual_index": "این نمایه قدیمی‌تر از شواهد تصویری است",
                "no_dialogue": "هنوز هیچ دیالوگی در محدودهٔ فعلی فهرست نشده است"}
_PARTIAL = {"time_budget": "وقت جست‌وجو تمام شد", "busy": "فهرست در دست نوشتن بود",
            "dialogue_candidates_cut": "همهٔ جمله‌های نامزد بررسی نشد",
            "visual_candidates_cut": "همهٔ فریم‌های نامزد بررسی نشد",
            "fusion_assets_cut": "ویدیوهای زیادی فریم هم‌خوان داشتند و فقط بخشی بررسی شد",
            "dialogue_semantic_unavailable": "جست‌وجوی هم‌معنای گفتگو در دسترس نبود",
            "visual_semantic_unavailable": "جست‌وجوی هم‌معنای تصویر در دسترس نبود"}
_CHANNEL_FA = {"exact": "دقیق", "fuzzy": "تقریبی", "semantic": "هم‌معنا", "lexical": "هم‌واژه"}
_UNSAFE = re.compile("[\x00-\x1f\x7f‎‏‪-‮⁦-⁩«»]")


def _said(text: object, limit: int = 120) -> str:
    """Indexed or generated text as a short, single-line quotation (no controls, bidi overrides or nested quotes)."""
    value = re.sub(r"\s+", " ", _UNSAFE.sub(" ", str(text or ""))).strip()
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _multimodal_answer(result: dict) -> str:
    block = result.get("multimodal") or {}
    status = block.get("status")
    if status == "unavailable":
        reason = str(block.get("reason") or "")
        return f"جست‌وجوی ترکیبی در دسترس نیست: {_UNAVAILABLE.get(reason, reason or 'نامشخص')}."
    partial = ""
    if status == "partial":
        reasons = [block.get("stoppedBy"), *(block.get("notes") or [])]
        why = "، ".join(dict.fromkeys(_PARTIAL.get(str(r), _said(r, 40)) for r in reasons
                                      if r and not str(r).startswith("dialogue:") and r != "results_cut_at_limit"))
        partial = (f" این پاسخ ناقص است ({why or 'نامشخص'})؛ هر نتیجه هر دو شرط را دارد، اما ممکن است همه پیدا نشده"
                   " باشند.")
    items = block.get("results") or []
    if not items:
        return _OUTCOME.get(str(block.get("outcome")), "نتیجهٔ ترکیبی پیدا نشد.") + partial
    first = items[0]
    line = first.get("dialogue") or {}
    frames = first.get("frames") or [{}]
    frame = frames[0]
    gap = max(0, int(frame.get("gapMs") or 0))
    relation = ("فریم هم‌زمان با همین جمله است" if frame.get("relation") == "during"
                else f"فریم {round(gap / 1000, 1)} ثانیه {'پیش از' if frame.get('relation') == 'before' else 'پس از'} جمله است")
    channels = lambda names: " + ".join(_CHANNEL_FA.get(str(n), str(n)) for n in names or [])  # noqa: E731
    return (f"{len(items)} نتیجهٔ ترکیبی پیدا شد (هر کدام با هر دو شرط در همان ویدیو و نزدیک هم در زمان). اولی در "
            f"«{_said(first.get('title') or 'ویدیو', 60)}»: دقیقهٔ {clock(line.get('startMs'))} این جمله گفته می‌شود "
            f"«{_said(line.get('text'))}» ({channels(line.get('channels'))})، و {relation}: "
            f"«{_said(frame.get('caption'))}» (توصیف تولیدشده با مدل بینایی، تأییدنشده؛ {channels(frame.get('channels'))}). "
            "شباهت تصویر، تشخیص گوینده و OCR به کار نرفته و رتبه احتمال نیست. برای باز کردن در همان لحظه از بخش رسانه "
            "«باز کردن در همین لحظه» را بزنید." + partial)


def _split_answer(result: dict) -> str:
    made = result.get("interpretation") or {}
    if made.get("status") == "proposed":
        origin = ("پیشنهاد مغز سریع از واژه‌های خود شما (تولیدشده، تأییدنشده)" if made.get("generated") is not False
                  else "از برچسب‌های خود شما")
        dialogue, visual = _said(made.get("dialogue"), 300), _said(made.get("visual"), 300)
        return (f"{origin}: گفتگو «{dialogue}» و تصویر «{visual}». هنوز چیزی جست‌وجو نشد؛ اگر درست است بگویید: "
                f"«در ویدیوهام بگرد: گفتگو «{dialogue}» و تصویر «{visual}»».")
    reason = _said(made.get("reason") or "unavailable", 60)
    return (f"تفکیک خودکار این درخواست ممکن نشد ({reason})؛ دو بخش را خودتان بگویید، " + _EXAMPLE
            + ". چیزی جست‌وجو نشد.")


def spoken_answer(intent: MediaIntent, result: dict | None) -> str:
    """A short Persian answer from the typed result (never more than what the result says)."""
    if isinstance(intent, MediaClarifyIntent):
        return _CLARIFY.get(intent.reason, _CLARIFY["needs_parts"])
    if result is not None and result.get("ok") and isinstance(intent, MediaMultimodalIntent):
        return _multimodal_answer(result)
    if result is not None and result.get("ok") and isinstance(intent, MediaMultimodalSplitIntent):
        return _split_answer(result)
    if result is None:
        return "جست‌وجوی رسانه به‌موقع جواب نداد؛ وضعیت را در بخش رسانه ببینید."
    if not result.get("ok"):
        code = result.get("code", "refused")
        if code == "paused":
            return "Qronos متوقف است؛ کارهای رسانه بعد از ادامه انجام می‌شود."
        return f"درخواست رسانه انجام نشد ({code})."
    if isinstance(intent, MediaStatusIntent):
        store = (result.get("status") or {}).get("store") or {}
        assets = store.get("assets")
        running = (result.get("status") or {}).get("running")
        state = "در حال فهرست‌سازی است" if running else "الان کاری در جریان نیست"
        return f"{assets if assets is not None else 'هنوز هیچ'} ویدیو در فهرست است؛ {state}."
    if intent.mode == "topic":
        block = result.get("topic") or {}
        items = block.get("results") or []
        exact = (result.get("exact") or {}).get("results") or []
        if not items:
            reason = block.get("reason") or block.get("status")
            return (f"جست‌وجوی موضوعی در دسترس نیست ({reason}); {len(exact)} نتیجهٔ دقیق پیدا شد."
                    if block.get("status") == "unavailable" else f"صحنهٔ هم‌موضوعی پیدا نشد؛ {len(exact)} نتیجهٔ دقیق.")
        first = items[0]
        line = (first.get("evidence") or [{}])[0].get("text") or ""
        made = (first.get("generated") or {}).get("label")
        if made:
            return (f"{len(items)} بخش هم‌موضوع پیدا شد؛ اولی در «{first.get('title') or 'ویدیو'}» از دقیقهٔ "
                    f"{clock(first.get('startMs'))} با برچسب تولیدشده و تأییدنشدهٔ «{made[:60]}»، و این جملهٔ خود فیلم: "
                    f"«{line[:120]}». امتیاز کالیبره نیست؛ جزئیات در بخش رسانه است.")
        return (f"{len(items)} بخش هم‌موضوع پیدا شد؛ اولی در «{first.get('title') or 'ویدیو'}» از دقیقهٔ "
                f"{clock(first.get('startMs'))}، با این جمله: «{line[:120]}». امتیاز کالیبره نیست؛ جزئیات در بخش رسانه است.")
    if intent.mode == "semantic":
        block = result.get("semantic") or {}
        items = block.get("results") or []
        exact = (result.get("exact") or {}).get("results") or []
        if not items:
            reason = block.get("reason") or block.get("status")
            return (f"جست‌وجوی معنایی در دسترس نیست ({reason}); {len(exact)} نتیجهٔ دقیق پیدا شد."
                    if block.get("status") == "unavailable" else f"نتیجهٔ معنایی پیدا نشد؛ {len(exact)} نتیجهٔ دقیق.")
    else:
        items = (result.get("results") or {}).get("results") or []
    if not items:
        return "در ویدیوهای فهرست‌شده چیزی پیدا نشد."
    first = items[0]
    title = first.get("title") or "ویدیو"
    return f"{len(items)} نتیجه پیدا شد؛ اولی در «{title}» دقیقهٔ {clock(first.get('startMs'))}. جزئیات در بخش رسانه است."
