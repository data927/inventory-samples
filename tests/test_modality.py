"""Tests for ``extractors.modality``."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from extractors.modality import format_modalities_cell, infer_modalities


class TestModality(unittest.TestCase):
    def test_code_and_image(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            py_p = root / "x.py"
            py_p.write_text("print(1)\n", encoding="utf-8")
            self.assertEqual(infer_modalities(py_p), ["code", "text"])

            png_p = root / "shot.png"
            png_p.write_bytes(b"\x89PNG\r\n\x1a\n")
            self.assertEqual(infer_modalities(png_p), ["image"])

    def test_tabular_and_structured(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_p = root / "t.csv"
            csv_p.write_text("a,b\n1,2\n", encoding="utf-8")
            self.assertEqual(infer_modalities(csv_p), ["tabular"])

            j_p = root / "j.json"
            j_p.write_text("{}", encoding="utf-8")
            self.assertEqual(infer_modalities(j_p), ["structured_data"])

    def test_html_visual_sniff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plain = root / "a.html"
            plain.write_text("<html><body>hi</body></html>", encoding="utf-8")
            self.assertEqual(infer_modalities(plain), ["text"])

            img = root / "b.html"
            img.write_text('<html><body><img src="x.png"/></body></html>', encoding="utf-8")
            self.assertEqual(infer_modalities(img), ["text", "visual_embedded"])

    def test_docx_media_zip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "n.docx"
            with zipfile.ZipFile(docx, "w") as zf:
                zf.writestr("word/document.xml", "<xml/>")
                zf.writestr("word/media/r.png", b"fake")
            self.assertEqual(infer_modalities(docx), ["text", "visual_embedded"])

    def test_format_cell(self) -> None:
        self.assertEqual(
            format_modalities_cell(["slides", "text"]),
            "slides; text",
        )


if __name__ == "__main__":
    unittest.main()
