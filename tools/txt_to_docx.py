#!/usr/bin/env python3
"""Convert a UTF-8 text file to a Word ``.docx`` (body text in one paragraph; newlines kept).

Logic lives in ``extractors.plain_text_to_docx_convert`` (same layer as
``extractors.html_to_docx_convert``) so CLIs and code can share one path.

Dependency:

    python -m pip install python-docx

Example:

    python tools/txt_to_docx.py --input notes.txt --output out/notes.docx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from extractors.plain_text_to_docx_convert import txt_path_to_docx_bytes


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", "-i", type=Path, required=True, help="Source text file path")
    p.add_argument("--output", "-o", type=Path, required=True, help="Destination .docx path")
    p.add_argument(
        "--title",
        default="",
        help="Document title (default: input file stem)",
    )
    args = p.parse_args(argv)

    try:
        import docx  # noqa: F401 — required by extractors.plain_text_to_docx_convert
    except ImportError:
        print(
            "Missing dependency: python-docx\n"
            "  python -m pip install python-docx",
            file=sys.stderr,
        )
        return 1

    src = args.input.expanduser().resolve()
    dst = args.output.expanduser().resolve()
    if not src.is_file():
        print(f"Input not found: {src}", file=sys.stderr)
        return 1

    title_arg = (args.title or "").strip()
    title_disp = title_arg or src.stem
    buf = txt_path_to_docx_bytes(src, title=title_arg or None)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(buf.getvalue())
    print(f"Wrote {dst} ({dst.stat().st_size} bytes, title={title_disp!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
