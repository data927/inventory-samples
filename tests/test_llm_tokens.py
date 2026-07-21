"""Tests for ``count_llm_tokens`` / ``token_count_from_file``."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from extractors.core import count_llm_tokens, count_words, extract_full_text, token_count_from_file


class TestLLMTokens(unittest.TestCase):
    def test_count_llm_tokens_nonempty(self) -> None:
        # cl100k_base: "hello world" → 2 tokens (matches tiktoken smoke test).
        n = count_llm_tokens("hello world")
        self.assertGreaterEqual(n, 1)

    def test_matches_extracted_html_doc(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "page.html"
            p.write_text(
                "<html><body><script>x</script><p>One two</p></body></html>",
                encoding="utf-8",
            )
            t = extract_full_text(p, html_via_docx=False)
            self.assertIsNotNone(t)
            self.assertEqual(count_words(t), 2)
            tc = token_count_from_file(p, html_via_docx=False)
            self.assertGreaterEqual(tc, 2)


if __name__ == "__main__":
    unittest.main()
