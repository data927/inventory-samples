#!/usr/bin/env python3
"""Convert a UTF-8 JSON file to a Word ``.docx`` (pretty-printed when valid).

Conversion logic lives in ``extractors.plain_text_to_docx_convert`` alongside the TXT
pipeline so exports stay consistent.

Dependency:

    python -m pip install python-docx

Example:

    python tools/json_to_docx.py \\
      --input data/config.json \\
      --output out/config.docx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from extractors.plain_text_to_docx_convert import json_path_to_docx_bytes


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", "-i", required=True, type=Path, help="Path to the .json file")
    p.add_argument("--output", "-o", required=True, type=Path, help="Path to the output .docx")
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

    in_path = args.input.expanduser().resolve()
    if not in_path.is_file():
        print(f"Input not found: {in_path}", file=sys.stderr)
        return 1

    title_arg = (args.title or "").strip()
    title_disp = title_arg or in_path.stem
    buf = json_path_to_docx_bytes(in_path, title=title_arg or None)
    out_path = args.output.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(buf.getvalue())
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes, title={title_disp!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
