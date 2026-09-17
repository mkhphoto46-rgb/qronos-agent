from __future__ import annotations

import unittest

from core.runtime_bridge import (
    _voice_action_transcript_quality,
    _voice_transcript_looks_actionish,
)


class VoiceActionRetryR4Tests(unittest.TestCase):
    def test_good_persian_action_does_not_need_low_quality_retry(self) -> None:
        text = "فایل در سال 1714 روی دسکتاپ رو باز کن"
        self.assertTrue(_voice_transcript_looks_actionish(text))
        self.assertGreaterEqual(_voice_action_transcript_quality(text), 8)

    def test_turkish_like_misdecode_is_actionish_but_low_quality(self) -> None:
        text = "Tuye desktop'e, desktop'e kompüüter, new text document. Ben gilisin eve işte."
        self.assertTrue(_voice_transcript_looks_actionish(text))
        self.assertLess(_voice_action_transcript_quality(text), 8)

    def test_capability_question_is_penalized_as_action(self) -> None:
        text = "چه فایل هایی رو میتونی برام باز کنی"
        self.assertLess(_voice_action_transcript_quality(text), 8)


if __name__ == "__main__":
    unittest.main()
