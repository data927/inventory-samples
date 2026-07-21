"""Tests for ``extractors.html_to_docx_convert`` (HTML images → DOCX)."""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

try:
    import html2docx  # noqa: F401

    _HAVE_HTML2DOCX = True
except ImportError:
    _HAVE_HTML2DOCX = False

# Minimal valid 1×1 PNG (grey pixel).
_MIN_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500001d0a2db40000000049454e44ae426082"
)


@unittest.skipUnless(_HAVE_HTML2DOCX, "html2docx not installed")
class TestHtmlToDocxConvert(unittest.TestCase):
    def test_relative_img_embeds_not_broken_placeholder(self) -> None:
        import html2docx as h2d_pkg

        broken = (Path(h2d_pkg.__file__).resolve().parent / "image-broken.png").read_bytes()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "shot.png").write_bytes(_MIN_PNG)
            html_path = root / "page.html"
            html_path.write_text(
                "<!DOCTYPE html><html><head><title>T</title></head>"
                '<body><article class="page sans">'
                '<div class="page-body"><p>Hi</p>'
                '<img src="shot.png" alt="x"/>'
                "</div></article></body></html>",
                encoding="utf-8",
            )
            from extractors.html_to_docx_convert import html_path_to_docx_bytes

            buf = html_path_to_docx_bytes(html_path, title="Doc")
            data = buf.getvalue()

        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            media = [n for n in zf.namelist() if n.startswith("word/media/")]
        self.assertTrue(media, "expected word/media/* in docx")
        payloads = []
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            for n in media:
                payloads.append(zf.read(n))
        self.assertTrue(any(p != broken for p in payloads), "embedded image should not be broken placeholder")

    def test_style_width_px_sets_inline_shape_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.png").write_bytes(_MIN_PNG)
            html_path = root / "page.html"
            html_path.write_text(
                "<!DOCTYPE html><html><body><article>"
                '<div class="page-body">'
                '<img style="width:200px" src="a.png"/>'
                "</div></article></body></html>",
                encoding="utf-8",
            )
            from extractors.html_to_docx_convert import html_path_to_docx_bytes

            import docx  # type: ignore

            doc = docx.Document(html_path_to_docx_bytes(html_path))
        self.assertEqual(len(doc.inline_shapes), 1)
        # html2docx maps px at 72dpi → EMU width ≈ 200 * 914400 / 72
        self.assertGreater(doc.inline_shapes[0].width, 0)

    def test_picture_srcset_flattens_to_embedded_image(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "b.png").write_bytes(_MIN_PNG)
            html_path = root / "page.html"
            html_path.write_text(
                "<!DOCTYPE html><html><body><article><div class=\"page-body\">"
                '<picture>'
                '<source type="image/png" srcset="b.png 1x, b.png 640w"/>'
                "</picture>"
                "</div></article></body></html>",
                encoding="utf-8",
            )
            from extractors.html_to_docx_convert import html_path_to_docx_bytes

            import docx  # type: ignore

            doc = docx.Document(html_path_to_docx_bytes(html_path))
        self.assertEqual(len(doc.inline_shapes), 1)

    def test_double_dot_src_found_inside_export_subfolder(self) -> None:
        """``../../…`` from a page beside an export folder still resolves (relocated Notion HTML)."""
        import html2docx as h2d_pkg

        broken = (Path(h2d_pkg.__file__).resolve().parent / "image-broken.png").read_bytes()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = root / "Export-bundle"
            img_path = bundle / "Pdir" / "nested" / "shot.png"
            img_path.parent.mkdir(parents=True)
            img_path.write_bytes(_MIN_PNG)
            html_path = root / "Early.html"
            html_path.write_text(
                "<!DOCTYPE html><html><body><article><div class=\"page-body\">"
                '<img src="../../Pdir/nested/shot.png"/>'
                "</div></article></body></html>",
                encoding="utf-8",
            )
            from extractors.html_to_docx_convert import html_path_to_docx_bytes

            buf = html_path_to_docx_bytes(html_path, title="T")
            data = buf.getvalue()

        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            media = [n for n in zf.namelist() if n.startswith("word/media/")]
            payloads = [zf.read(n) for n in media]
        self.assertTrue(any(p != broken for p in payloads))

    def test_table_has_tbl_borders_in_document_xml(self) -> None:
        from extractors.html_to_docx_convert import html_string_to_docx_bytes

        html = (
            "<!DOCTYPE html><html><head><title>T</title></head><body><article>"
            "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"
            "</article></body></html>"
        )
        buf = html_string_to_docx_bytes(html, title="T", raw=False, fallback_title="x")
        with zipfile.ZipFile(io.BytesIO(buf.getvalue()), "r") as zf:
            doc_xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
        self.assertIn("tblBorders", doc_xml)
        self.assertIn("insideH", doc_xml)


if __name__ == "__main__":
    unittest.main()
