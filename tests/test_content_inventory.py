"""Tests for ``extractors.content_inventory``."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from extractors.content_inventory import (
    format_content_inventory_cell,
    gather_content_inventory,
)


class TestContentInventory(unittest.TestCase):
    def test_format_skips_zeros(self) -> None:
        self.assertEqual(
            format_content_inventory_cell({"png": 2, "tables": 0, "jpeg": 1}),
            "jpeg: 1; png: 2",
        )

    def test_html_tables_and_images(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".html",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(
                '<html><body><table></table>'
                '<img src="a.png"/><img src="b.jpeg"/>'
                '<canvas></canvas></body></html>'
            )
            p = Path(f.name)
        try:
            d = gather_content_inventory(p)
            self.assertEqual(d.get("tables"), 1)
            self.assertEqual(d.get("images"), 2)
            self.assertEqual(d.get("png"), 1)
            self.assertEqual(d.get("jpeg"), 1)
            self.assertEqual(d.get("canvas"), 1)
        finally:
            p.unlink()

    def test_docx_zip_counts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.docx"
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("word/document.xml", b"<w:tbl/><w:tbl/>root")
                z.writestr("word/media/x.png", b"x")
                z.writestr("word/charts/chart1.xml", b"<xml/>")
            d = gather_content_inventory(p)
            self.assertEqual(d.get("tables"), 2)
            self.assertEqual(d.get("images"), 1)
            self.assertEqual(d.get("png"), 1)
            self.assertEqual(d.get("charts"), 1)

    def test_standalone_png(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.png"
            p.write_bytes(b"\x89PNG\r\n\x1a\n")
            d = gather_content_inventory(p)
            self.assertEqual(d.get("images"), 1)
            self.assertEqual(d.get("png"), 1)

    def test_csv_rows(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".csv",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write("a,b\n1,2\n3,4\n")
            p = Path(f.name)
        try:
            d = gather_content_inventory(p)
            self.assertEqual(d.get("rows"), 3)
        finally:
            p.unlink()


if __name__ == "__main__":
    unittest.main()
