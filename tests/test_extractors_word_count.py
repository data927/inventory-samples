"""Tests for full-file word counting in ``extractors.core``."""

from __future__ import annotations

import unittest

from extractors.core import count_words, count_words_from_file, extract_full_text


class TestWordCount(unittest.TestCase):
    def test_count_words_basic(self) -> None:
        self.assertEqual(count_words("  hello world  "), 2)
        # Lowercase letter across newline is merged (PDF-style de-wrap).
        self.assertEqual(count_words("hello\nworld"), 1)
        self.assertEqual(count_words(""), 0)
        self.assertEqual(count_words("   "), 0)

    def test_extract_full_html_ignores_script(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as td:
            p = Path(td) / "page.html"
            p.write_text(
                "<html><script>noise words here</script><body>One two</body></html>",
                encoding="utf-8",
            )
            t = extract_full_text(p)
            self.assertIsNotNone(t)
            self.assertEqual(count_words(t), 2)
            self.assertEqual(count_words_from_file(p), 2)


if __name__ == "__main__":
    unittest.main()
