"""Subtitle parsing, decoding, sidecar discovery and search normalisation."""
from __future__ import annotations

from pathlib import Path

import pytest

from qronos_media.subtitles import (
    MAX_SUBTITLE_BYTES, Cue, SubtitleError, decode_subtitle_bytes, find_sidecars, normalise_language,
    parse_srt, parse_vtt, read_subtitle_file,
)
from qronos_media.text import normalise_for_search


def test_srt_tolerates_real_world_mess() -> None:
    text = (
        "﻿1\r\n00:00:01,000 --> 00:00:02,500\r\n<i>Hello</i> {\\an8}there\r\nfriend\r\n\r\n"
        "00:00:03.25 --> 00:00:04,000\r\nno index line, dot millis\r\n\r\n"
        "3\r\n00:00:05,000 --> 00:00:04,000\r\nends before it starts\r\n\r\n"
        "4\r\nnot a timing line\r\nskipped\r\n\r\n"
        "5\r\n100:00:00,000 --> 100:00:01,000\r\nthree-digit hours\r\n"
    )
    assert parse_srt(text) == [
        Cue(1_000, 2_500, "Hello there friend"),
        Cue(3_250, 4_000, "no index line, dot millis"),
        Cue(360_000_000, 360_001_000, "three-digit hours"),
    ]


def test_srt_cues_come_back_in_time_order() -> None:
    text = "1\n00:00:05,000 --> 00:00:06,000\nlater\n\n2\n00:00:01,000 --> 00:00:02,000\nearlier\n"
    assert [cue.text for cue in parse_srt(text)] == ["earlier", "later"]


def test_vtt_skips_metadata_blocks_and_reads_short_times() -> None:
    text = (
        "WEBVTT - title\n\nNOTE a comment\n\nSTYLE\n::cue { color: red }\n\n"
        "intro\n00:01.000 --> 00:02.500 align:start position:10%\n<v Roger>Hi there\n\n"
        "01:00:00.000 --> 01:00:01.000\nan hour in\n"
    )
    assert parse_vtt(text) == [Cue(1_000, 2_500, "Hi there"), Cue(3_600_000, 3_601_000, "an hour in")]


def test_vtt_without_header_is_refused() -> None:
    with pytest.raises(SubtitleError):
        parse_vtt("00:01.000 --> 00:02.000\nx\n")


def test_decoding_handles_boms_and_legacy_persian() -> None:
    persian = "سلام دنیا، این یک زیرنویس است"
    assert decode_subtitle_bytes(persian.encode("utf-8")) == (persian, "utf-8")
    assert decode_subtitle_bytes(b"\xef\xbb\xbf" + persian.encode("utf-8")) == (persian, "utf-8-sig")
    assert decode_subtitle_bytes(persian.encode("utf-16")) == (persian, "utf-16")
    # Windows-1256 has no Persian yeh; legacy files carry the Arabic one, and
    # search normalisation folds it back.
    legacy = persian.replace("ی", "ي")
    assert decode_subtitle_bytes(legacy.encode("cp1256")) == (legacy, "cp1256-guessed")
    assert normalise_for_search(legacy) == normalise_for_search(persian)
    assert decode_subtitle_bytes("Café crème".encode("cp1252")) == ("Café crème", "cp1252-guessed")
    with pytest.raises(SubtitleError):
        decode_subtitle_bytes(b"x" * (MAX_SUBTITLE_BYTES + 1))


def test_reading_a_legacy_encoded_srt_file(tmp_path: Path) -> None:
    path = tmp_path / "film.srt"
    path.write_bytes("1\n00:00:01,000 --> 00:00:02,000\nمن هرگز نخواستم\n".encode("cp1256"))
    cues, encoding = read_subtitle_file(path)
    assert encoding == "cp1256-guessed"
    assert cues == [Cue(1_000, 2_000, "من هرگز نخواستم")]


def test_sidecars_are_matched_by_stem_with_language_and_flags(tmp_path: Path) -> None:
    video = tmp_path / "Film (2001).mkv"
    video.write_bytes(b"")
    for name in ("Film (2001).srt", "Film (2001).fa.srt", "Film (2001).eng.forced.vtt", "Film (2001).hi.srt",
                 "Film (2001).cut.ass", "Film (2001) 2.srt", "Film (2001)ography.srt", "Film (2001).txt",
                 "Other.fa.srt"):
        (tmp_path / name).write_text("", encoding="utf-8")
    found = {item.path.name: (item.language, item.flags) for item in find_sidecars(video)}
    assert found == {
        "Film (2001).srt": (None, ()),
        "Film (2001).fa.srt": ("fa", ()),
        "Film (2001).eng.forced.vtt": ("en", ("forced",)),
        "Film (2001).hi.srt": (None, ("hi",)),  # hearing impaired, not Hindi
        "Film (2001).cut.ass": (None, ()),
    }


def test_stream_language_codes_are_normalised() -> None:
    assert normalise_language("fas") == "fa" and normalise_language("per") == "fa"
    assert normalise_language("eng") == "en" and normalise_language("und") is None
    assert normalise_language("x" * 20) is None and normalise_language(None) is None


@pytest.mark.parametrize("left,right", [
    ("كتاب علي", "کتاب علی"),              # Arabic kaf/yeh vs Persian
    ("سال ۱۴۰۲", "سال 1402"),               # Persian digits
    ("نرم‌افزار", "نرم افزار"),        # ZWNJ
    ("کِتاب", "کتاب"),                      # diacritics
    ("I'll be back!", "ill be BACK"),       # apostrophes, punctuation, case
    ("Café", "cafe"),                       # Latin accents
    ("<i>Hello</i>, world", "hello world"), # subtitle markup
    ("آب", "اب"),                           # alef madda folds for search
])
def test_normalisation_makes_spelling_variants_meet(left: str, right: str) -> None:
    assert normalise_for_search(left) == normalise_for_search(right)


def test_normalisation_keeps_words_apart() -> None:
    assert normalise_for_search("father—son").split() == ["father", "son"]
    assert normalise_for_search("...!?") == ""
