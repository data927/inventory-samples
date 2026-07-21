"""Tests for ``extractors.plain_text_to_docx_convert``."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from extractors.core import _extract_docx_full_text_from_bytes
from extractors.plain_text_to_docx_convert import (
    json_path_to_docx_bytes,
    json_pretty_or_raw,
    json_string_to_docx_bytes,
    plain_text_to_docx_bytes,
    txt_path_to_docx_bytes,
)


class TestPlainTextToDocxConvert(unittest.TestCase):
    def test_json_pretty_or_raw(self) -> None:
        self.assertEqual(json_pretty_or_raw(""), "")
        self.assertEqual(json_pretty_or_raw("   "), "")
        raw = '{"a":1,"b":[2]}'
        pretty = json_pretty_or_raw(raw)
        self.assertEqual(json.loads(pretty), {"a": 1, "b": [2]})
        bad = "{not json"
        self.assertEqual(json_pretty_or_raw(bad), bad)

    def test_plain_text_roundtrip_lines(self) -> None:
        buf = plain_text_to_docx_bytes("a\n\nb", title="T", fallback_title="x")
        t = _extract_docx_full_text_from_bytes(buf.getvalue())
        assert t is not None
        self.assertEqual(t, "a\n\nb")

    def test_txt_path_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "n.txt"
            p.write_text("hello world", encoding="utf-8")
            buf = txt_path_to_docx_bytes(p, title="MyTitle")
            t = _extract_docx_full_text_from_bytes(buf.getvalue())
        assert t is not None
        self.assertEqual(t, "hello world")

    def test_json_string_to_docx_pretty(self) -> None:
        buf = json_string_to_docx_bytes('{"z":1,"a":2}')
        t = _extract_docx_full_text_from_bytes(buf.getvalue())
        assert t is not None
        self.assertEqual(json.loads(t), {"z": 1, "a": 2})

    def test_json_path_invalid_json_passthrough(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            # Double newline so core normalization does not merge letter runs across a line break.
            p.write_text("plain\n\nlines", encoding="utf-8")
            buf = json_path_to_docx_bytes(p)
            t = _extract_docx_full_text_from_bytes(buf.getvalue())
        assert t is not None
        self.assertEqual(t, "plain\n\nlines")


if __name__ == "__main__":
    unittest.main()
