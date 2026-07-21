"""Post-process DOCX tables so borders survive Google Docs import."""

from __future__ import annotations

import io

from docx.oxml.ns import nsdecls, qn
from docx.oxml.parser import parse_xml


def _tbl_borders_element():
    xml = (
        "<w:tblBorders %s>"
        '<w:top w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:left w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:bottom w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:right w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:insideH w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:insideV w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        "</w:tblBorders>"
    ) % nsdecls("w")
    return parse_xml(xml)


def apply_visible_table_borders_to_table(table) -> None:
    """Replace or add ``w:tblBorders`` on one table (body or nested)."""
    tbl_pr = table._tbl.tblPr
    for child in list(tbl_pr):
        if child.tag == qn("w:tblBorders"):
            tbl_pr.remove(child)
    tbl_pr.append(_tbl_borders_element())


def apply_visible_table_borders_to_document(document) -> None:
    """Apply grid borders to every table in the document body."""
    for table in document.tables:
        apply_visible_table_borders_to_table(table)


def docx_bytes_with_visible_table_borders(data: bytes) -> bytes:
    """Return new ``.docx`` bytes with table borders (no-op if ``python-docx`` fails)."""
    from docx import Document

    doc = Document(io.BytesIO(data))
    apply_visible_table_borders_to_document(doc)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
