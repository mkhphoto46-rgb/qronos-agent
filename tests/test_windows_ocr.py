"""
Windows' own text recogniser, used as a hint and never as an answer.

The parsing and the judgement are tested with no engine at all, because they
are where the decisions are: is this reading worth passing on, is it the
engine's rendering of a script it cannot read, does it fit. The engine itself
gets a live check at the end that skips off Windows.

Strings in :class:`TestWordLikeness` are real output, copied from runs against
the generated corpus. That matters: a threshold tuned against invented examples
is tuned against nothing.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import patch

from core import windows_ocr
from core.vision_ocr import read_screen_text
from core.windows_ocr import (
    MAX_HINT_CHARS,
    MINIMUM_WORD_LIKENESS,
    OcrReading,
    OcrWord,
    USEFUL_WORD_COUNT,
    _parse,
    available,
    read,
    useful_hint,
    word_likeness,
)


def reading(text: str, words: int = 10) -> OcrReading:
    return OcrReading(
        ok=True,
        lines=tuple(text.split("\n")),
        words=tuple(
            OcrWord(text=f"w{index}", x=0, y=0, width=1, height=1)
            for index in range(words)
        ),
    )


class TestWordLikeness(unittest.TestCase):
    """
    Telling text from debris.

    The engine has no Arabic-script recogniser, so it does not fail on Persian
    — it emits confident Latin gibberish. Checking for Persian characters would
    never fire, because it cannot produce any. What gives it away is that the
    output is not made of words.
    """

    def test_real_english_scores_high(self) -> None:
        # Measured output, from a generated dialog.
        self.assertEqual(word_likeness("System message The file was saved. OK"), 1.0)

    def test_the_engine_shown_persian_scores_low(self) -> None:
        # Also measured output, from a Persian dialog the engine cannot read.
        self.assertLess(word_likeness("I O _La IO_JiI an_A Qi"), 0.3)

    def test_the_two_are_far_apart(self) -> None:
        """So the exact threshold is not load-bearing."""
        english = word_likeness("System message The file was saved. OK")
        gibberish = word_likeness("I O _La IO_JiI an_A Qi")

        self.assertGreater(english - gibberish, 0.5)
        self.assertGreater(english, MINIMUM_WORD_LIKENESS)
        self.assertLess(gibberish, MINIMUM_WORD_LIKENESS)

    def test_text_that_is_mostly_a_code_still_counts_as_text(self) -> None:
        """
        Which is the case that decides the threshold, since an error dialog is
        exactly what somebody would ask Qronos to read.
        """
        self.assertGreater(
            word_likeness("Error code: 0x8024402C"),
            MINIMUM_WORD_LIKENESS,
        )

    def test_nothing_scores_zero_rather_than_dividing_by_it(self) -> None:
        for empty in ("", "   ", "\n\n"):
            with self.subTest(text=empty):
                self.assertEqual(word_likeness(empty), 0.0)

    def test_punctuation_does_not_disqualify_a_word(self) -> None:
        self.assertEqual(word_likeness("Saved. Closed, done!"), 1.0)


class TestWhatIsWorthPassingOn(unittest.TestCase):
    def test_a_good_reading_is_passed_on(self) -> None:
        self.assertIn(
            "The file was saved",
            useful_hint(reading("System message\nThe file was saved.\nOK")),
        )

    def test_a_failed_reading_becomes_nothing(self) -> None:
        self.assertEqual(useful_hint(OcrReading(ok=False, reason="no")), "")

    def test_a_reading_of_almost_nothing_becomes_nothing(self) -> None:
        """
        A handful of fragments off a mostly-graphical screen is noise with the
        shape of evidence.
        """
        self.assertEqual(
            useful_hint(reading("Ok\nGo", words=USEFUL_WORD_COUNT - 1)),
            "",
        )

    def test_gibberish_from_an_unreadable_script_becomes_nothing(self) -> None:
        self.assertEqual(useful_hint(reading("I O _La IO_JiI an_A Qi")), "")

    def test_a_long_reading_is_cut_rather_than_dropped(self) -> None:
        """
        "There is too much on your screen" is not an answer anybody wants to a
        question about their screen, and a partial hint is still a hint.
        """
        long = "\n".join(["a line of ordinary readable text"] * 400)

        hint = useful_hint(reading(long))

        self.assertTrue(hint)
        self.assertLessEqual(len(hint), MAX_HINT_CHARS)

    def test_it_is_cut_at_a_line_rather_than_mid_word(self) -> None:
        """Half a code looks like a whole one, which is worse than none."""
        long = "\n".join(["a line of ordinary readable text"] * 400)

        hint = useful_hint(reading(long))

        self.assertTrue(hint.endswith("text"))


class TestParsing(unittest.TestCase):
    """What comes back from PowerShell, including the shapes that surprise."""

    def test_lines_and_words_are_read(self) -> None:
        parsed = _parse(
            '{"ok":true,"lines":[{"text":"Hello there",'
            '"words":[{"text":"Hello","x":1,"y":2,"w":3,"h":4},'
            '{"text":"there","x":5,"y":6,"w":7,"h":8}]}]}'
        )

        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.text, "Hello there")
        self.assertEqual(parsed.words[0], OcrWord("Hello", 1, 2, 3, 4))

    def test_a_single_line_arrives_unwrapped_and_is_still_read(self) -> None:
        """
        PowerShell's JSON turns a one-element array into a bare object. So a
        screen with exactly one line of text on it comes back shaped
        differently from every other screen, which is the kind of thing that
        works for months and then does not.
        """
        parsed = _parse(
            '{"ok":true,"lines":{"text":"Only line",'
            '"words":{"text":"Only","x":0,"y":0,"w":1,"h":1}}}'
        )

        self.assertEqual(parsed.text, "Only line")
        self.assertEqual(len(parsed.words), 1)

    def test_a_declared_failure_carries_its_reason(self) -> None:
        parsed = _parse('{"ok":false,"reason":"no en-US recogniser"}')

        self.assertFalse(parsed.ok)
        self.assertIn("recogniser", parsed.reason)

    def test_nonsense_is_a_failure_and_not_an_exception(self) -> None:
        parsed = _parse("this is not json")

        self.assertFalse(parsed.ok)
        self.assertIn("Unreadable", parsed.reason)

    def test_nothing_at_all_is_a_failure(self) -> None:
        self.assertFalse(_parse("").ok)

    def test_a_json_array_where_an_object_belongs_is_a_failure(self) -> None:
        self.assertFalse(_parse("[1, 2, 3]").ok)

    def test_a_reading_with_no_lines_is_still_a_success(self) -> None:
        """A screen with no text on it is a true answer, not a failure."""
        parsed = _parse('{"ok":true,"lines":[]}')

        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.text, "")


class TestFailingQuietly(unittest.TestCase):
    """
    A hint that throws is worse than a hint that is missing.

    The picture goes to the model regardless, so nothing is lost by having no
    hint, and everything is lost by raising through the middle of a request
    somebody is waiting on. Each of these was a real way it could have.

    ``available`` is patched alongside ``subprocess.run``, so the failure
    handling is exercised on both CI platforms rather than only on Windows.
    Without that, these ran on Linux, returned at the platform guard before
    reaching anything they had patched, and passed by asserting the very thing
    the guard already guarantees. The one test here with a specific assertion
    is what caught the other two being vacuous.
    """

    def on_a_machine_with_an_engine(self):
        return patch("core.windows_ocr.available", return_value=True)

    def test_no_picture_is_a_reason_not_a_crash(self) -> None:
        with self.on_a_machine_with_an_engine():
            reading = read(b"")

        self.assertFalse(reading.ok)
        self.assertIn("no picture", reading.reason.lower())

    def test_powershell_missing_is_a_reason(self) -> None:
        with self.on_a_machine_with_an_engine(), patch(
            "core.windows_ocr.subprocess.run",
            side_effect=FileNotFoundError("powershell.exe"),
        ):
            reading = read(b"\x89PNG")

        self.assertFalse(reading.ok)
        self.assertIn("powershell", reading.reason.lower())

    def test_powershell_hanging_is_a_reason(self) -> None:
        with self.on_a_machine_with_an_engine(), patch(
            "core.windows_ocr.subprocess.run",
            side_effect=subprocess.TimeoutExpired("powershell.exe", 20),
        ):
            reading = read(b"\x89PNG")

        self.assertFalse(reading.ok)
        self.assertIn("timed out", reading.reason.lower())

    def test_powershell_failing_carries_what_it_said(self) -> None:
        class Failed:
            returncode = 1
            stdout = ""
            stderr = "Unable to find type [Windows.Globalization.Language]."

        with self.on_a_machine_with_an_engine(), patch(
            "core.windows_ocr.subprocess.run", return_value=Failed()
        ):
            self.assertIn("Globalization", read(b"\x89PNG").reason)

    def test_a_reply_that_is_not_json_is_a_reason(self) -> None:
        """
        PowerShell writes warnings to standard output given the chance, and a
        warning in front of the JSON is not JSON.
        """

        class Chatty:
            returncode = 0
            stdout = "WARNING: something unrelated\n"
            stderr = ""

        with self.on_a_machine_with_an_engine(), patch(
            "core.windows_ocr.subprocess.run", return_value=Chatty()
        ):
            self.assertIn("Unreadable", read(b"\x89PNG").reason)

    def test_the_adapter_turns_every_failure_into_no_hint(self) -> None:
        with patch("core.vision_ocr.read", side_effect=RuntimeError("boom")):
            self.assertEqual(read_screen_text(b"\x89PNG"), "")

    def test_off_windows_it_says_so_and_reads_nothing(self) -> None:
        original = windows_ocr.sys.platform
        windows_ocr.sys.platform = "linux"
        self.addCleanup(setattr, windows_ocr.sys, "platform", original)

        self.assertFalse(available())
        self.assertFalse(read(b"\x89PNG").ok)
        self.assertEqual(read_screen_text(b"\x89PNG"), "")


class TestDescription(unittest.TestCase):
    def test_a_reading_says_how_much_it_found(self) -> None:
        self.assertIn("10 words", reading("a b c").describe())

    def test_a_failure_says_why(self) -> None:
        self.assertIn(
            "no recogniser",
            OcrReading(ok=False, reason="no recogniser").describe(),
        )


@unittest.skipUnless(sys.platform == "win32", "Windows OCR is a Windows thing.")
class TestTheRealEngine(unittest.TestCase):
    """
    The engine itself, on a picture drawn here rather than captured.

    Shallow on purpose. What it reads is measured properly in
    ``tools/test_qronos_ocr_hint_live.py``; this only proves it is reachable,
    that it reads nothing off a blank picture without falling over, and that
    the picture never becomes a file on the way.
    """

    def png(self, text: str) -> bytes:
        from io import BytesIO

        from PIL import Image, ImageDraw

        image = Image.new("RGB", (600, 160), (255, 255, 255))
        ImageDraw.Draw(image).text((20, 60), text, fill=(0, 0, 0))

        buffer = BytesIO()
        image.save(buffer, format="PNG")

        return buffer.getvalue()

    def test_it_reads_text_that_is_there(self) -> None:
        result = read(self.png("HELLO WORLD"))

        self.assertTrue(result.ok, result.reason)
        self.assertIn("HELLO", result.text.upper())

    def test_it_reads_nothing_off_a_blank_picture_without_failing(self) -> None:
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (400, 200), (255, 255, 255)).save(buffer, format="PNG")

        result = read(buffer.getvalue())

        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.words, ())

    def test_words_come_back_with_their_positions(self) -> None:
        result = read(self.png("HELLO WORLD"))

        self.assertTrue(result.words)
        self.assertGreater(result.words[0].width, 0)

    def test_a_picture_that_is_not_a_picture_is_a_reason_not_a_crash(
        self,
    ) -> None:
        self.assertFalse(read(b"not a png at all").ok)


if __name__ == "__main__":
    unittest.main()
