#!/usr/bin/env python3
"""Convert a Notion-export (or any) HTML file to a Word ``.docx``.

This is **standalone** from the MCP / segmenter pipeline. Conversion logic lives in
``extractors.html_to_docx_convert`` so HTML word counts can use the same pipeline.

Dependency (install once in your venv):

    python -m pip install html2docx

``lxml`` is used to isolate page content (usually already installed here).

Links are converted to underlined text (label only); raw URLs are not appended
(``html2docx`` would otherwise suffix ``href`` after anchor text).

Relative ``<img src="...">`` paths are resolved against the input ``.html`` file's
directory so sibling image files embed correctly.

Tables are post-processed with visible grid borders so they import more cleanly into
Google Docs.

Example:

    python tools/html_to_docx.py \\
      --input "Export-41bd0fad-2b81-42b4-9d17-aa2954f06765/Tooling ab9dfc53c33147ae91e784da1e9c4d59.html" \\
      --output out/example-notion-page.docx
"""

from __future__ import annotations

import argparse
import html as html_module
import sys
from pathlib import Path

from extractors.html_to_docx_convert import html_path_to_docx_bytes, title_from_html


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--input",
        "-i",
        required=True,
        type=Path,
        help="Path to the .html file",
    )
    p.add_argument(
        "--output",
        "-o",
        required=True,
        type=Path,
        help="Path to the output .docx",
    )
    p.add_argument(
        "--title",
        default="",
        help="Document title (default: from <title> tag, else input stem)",
    )
    p.add_argument(
        "--raw",
        action="store_true",
        help="Feed full HTML to html2docx (not recommended for Notion exports)",
    )
    args = p.parse_args(argv)

    try:
        import html2docx  # noqa: F401 — required by extractors.html_to_docx_convert
    except ImportError:
        print(
            "Missing dependency: html2docx\n"
            "  python -m pip install html2docx",
            file=sys.stderr,
        )
        return 1

    in_path = args.input.expanduser().resolve()
    if not in_path.is_file():
        print(f"Input not found: {in_path}", file=sys.stderr)
        return 1

    html = in_path.read_text(encoding="utf-8", errors="replace")
    title_arg = (args.title or "").strip()
    title_disp = html_module.unescape(title_arg or title_from_html(html, in_path.stem))

    buf = html_path_to_docx_bytes(in_path, raw=args.raw, title=title_arg or None)
    out_path = args.output.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(buf.getvalue())
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes, title={title_disp!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
