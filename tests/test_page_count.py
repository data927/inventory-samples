"""Tests for ``page_count_from_file``."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from extractors.core import (
    DOCX_TEXT_ONLY_LONG_WORD_THRESHOLD,
    ESTIMATED_WORDS_PER_PAGE,
    ESTIMATED_WORDS_PER_PAGE_DOCUMENT,
    ESTIMATED_WORDS_PER_PAGE_DOCUMENT_TEXT_ONLY,
    ESTIMATED_WORDS_PER_PAGE_DOCUMENT_TEXT_ONLY_LONG,
    page_count_from_file,
)
from extractors import core as extract_core


class TestPageCount(unittest.TestCase):
    def test_txt_estimates_from_words(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "x.txt"
            p.write_text(
                " ".join(["word"] * (ESTIMATED_WORDS_PER_PAGE + 1)),
                encoding="utf-8",
            )
            self.assertEqual(page_count_from_file(p), 2)

    def test_document_divisor_example(self) -> None:
        # Rich-document heuristic: ~298 words → 3 pages at 120 w/page (Word-aligned sanity).
        self.assertEqual(
            extract_core._pages_from_word_estimate(
                298, per_page=ESTIMATED_WORDS_PER_PAGE_DOCUMENT
            ),
            3,
        )

    def test_docx_text_only_words_per_page_tier(self) -> None:
        self.assertEqual(
            extract_core._docx_text_only_words_per_page(
                DOCX_TEXT_ONLY_LONG_WORD_THRESHOLD - 1
            ),
            ESTIMATED_WORDS_PER_PAGE_DOCUMENT_TEXT_ONLY,
        )
        self.assertEqual(
            extract_core._docx_text_only_words_per_page(
                DOCX_TEXT_ONLY_LONG_WORD_THRESHOLD
            ),
            ESTIMATED_WORDS_PER_PAGE_DOCUMENT_TEXT_ONLY_LONG,
        )


if __name__ == "__main__":
    unittest.main()
