from __future__ import annotations

import pytest

from core.app_workflow_intents import (
    PrintIntent,
    TelegramDownloadIntent,
    WorkflowClarification,
    parse_workflow_intent,
)


@pytest.mark.parametrize("text, contact", [
    ("آخرین فایل «سارا احمدی» را در تلگرام دانلود کن", "سارا احمدی"),
    ("جدیدترین فایل را از سارا احمدی در تلگرام دانلود کن", "سارا احمدی"),
    ("آخرین فایل سارا احمدی را از تلگرام دانلود کن", "سارا احمدی"),
    ('Download the latest file from "Sara Ahmadi" on Telegram', "Sara Ahmadi"),
    ("download the newest attachment from Sara Ahmadi on Telegram", "Sara Ahmadi"),
    ("آخرين فايل «علي» را در تلگرام دانلود كن", "علی"),  # Arabic letter variants
])
def test_telegram_download_requests_extract_one_exact_contact(text, contact):
    assert parse_workflow_intent(text) == TelegramDownloadIntent(contact=contact)


@pytest.mark.parametrize("text, fragment", [
    ("آخرین فایل را در تلگرام دانلود کن", "نام دقیق مخاطب"),
    ("download the latest file on Telegram", "نام دقیق مخاطب"),
    ("آخرین فایل «سارا» را در تلگرام دانلود کن و برایش پیام بفرست", "پیام نمی‌فرستد"),
    ("download the latest file from Sara on Telegram and reply thanks", "پیام نمی‌فرستد"),
    ("همه فایل‌های «سارا» را در تلگرام دانلود کن", "نه همه"),
    ("download all files from Sara on Telegram", "نه همه"),
    ("آخرین فایل «سارا» یا «مینا» را در تلگرام دانلود کن", "چند نام"),
])
def test_unclear_or_unsafe_telegram_requests_ask_instead_of_guessing(text, fragment):
    result = parse_workflow_intent(text)
    assert isinstance(result, WorkflowClarification)
    assert fragment in result.question


@pytest.mark.parametrize("text, expected", [
    ("فایل report.pdf را با چاپگر «Office Laser» چاپ کن", PrintIntent("report.pdf", "Office Laser")),
    ("فایل report.pdf را با پرینتر Office Laser پرینت کن", PrintIntent("report.pdf", "Office Laser")),
    ("فایل گزارش ماهانه را چاپ کن", PrintIntent("گزارش ماهانه", None)),
    ("print report.pdf on printer Office Laser", PrintIntent("report.pdf", "Office Laser")),
    ('print "Q3 results on paper.pdf" on "Office Laser"', PrintIntent("Q3 results on paper.pdf", "Office Laser")),
    ("print the file report.pdf", PrintIntent("report.pdf", None)),
    ("print report.pdf 2 copies on Office Laser", PrintIntent("report.pdf", "Office Laser", copies=2)),
    ("فایل report.pdf را دو نسخه چاپ کن", PrintIntent("report.pdf", None, copies=2)),
])
def test_print_requests_extract_file_printer_and_copies(text, expected):
    assert parse_workflow_intent(text) == expected


@pytest.mark.parametrize("text", ["print", "چاپ کن", "print the file", "فایل را چاپ کن"])
def test_print_without_a_file_asks_which_file(text):
    result = parse_workflow_intent(text)
    assert isinstance(result, WorkflowClarification)
    assert "کدام فایل" in result.question


@pytest.mark.parametrize("text", [
    "وضعیت پرینتر چطور است؟",
    "what printer do I have?",
    "تلگرام را باز کن",
    "open Telegram",
    "سلام",
    "",
])
def test_unrelated_sentences_are_not_workflows(text):
    assert parse_workflow_intent(text) is None


@pytest.mark.parametrize("text, expected", [
    ("print report.pdf pages 1-3 in black and white on Office Laser",
     PrintIntent("report.pdf", "Office Laser", color="monochrome", pages="1-3")),
    ("فایل report.pdf را صفحه ۲ تا ۴ رنگی با چاپگر «Office Laser» چاپ کن",
     PrintIntent("report.pdf", "Office Laser", color="color", pages="2-4")),
    ("print report.pdf page 5 in colour", PrintIntent("report.pdf", None, color="color", pages="5")),
    ("فایل گزارش را سیاه و سفید دو نسخه چاپ کن", PrintIntent("گزارش", None, copies=2, color="monochrome")),
    ("print notes.txt pages 2-3 on Office Laser", PrintIntent("notes.txt", "Office Laser", pages="2-3")),
])
def test_print_options_are_extracted_and_removed_from_the_file_name(text, expected):
    assert parse_workflow_intent(text) == expected


def test_contradictory_colour_requests_ask_instead_of_guessing():
    result = parse_workflow_intent("print report.pdf in colour and black and white")
    assert isinstance(result, WorkflowClarification) and "رنگی" in result.question
