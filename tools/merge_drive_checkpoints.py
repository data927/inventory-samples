#!/usr/bin/env python3
"""Merge Drive ingest sidecars into one JSONL (evidence + bucket fields + sub_category).

The classifier checkpoint (``<out>.xlsx.checkpoint.jsonl``) only stores bucket assignment
rows. Full modality / counts / snippets live in ``<stem>.drive_artifacts.jsonl``.
This tool joins them on ``artifact_id`` / ``row_id`` so each output line is one
complete record.

Optional: merge ``<stem>.subcat.jsonl`` when present (same ``row_id`` as artifact id).

Example::

    python tools/merge_drive_checkpoints.py --xlsx-out out/drive_full_quality_live.xlsx
    # writes out/drive_full_quality_live.merged_evidence.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from checkpoint import (  # noqa: E402
    checkpoint_path_for_output,
    drive_artifact_checkpoint_path,
    load_checkpoint,
    load_drive_artifact_checkpoint,
)


def _subcat_path(xlsx_out: Path) -> Path:
    return xlsx_out.parent / f"{xlsx_out.stem}.subcat.jsonl"


def _deep_copy_json(obj: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(obj, ensure_ascii=False))


def merge_drive_sidecars(
    xlsx_out: Path,
    *,
    include_orphans: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    """Return sorted merged rows and paths used."""
    xlsx_out = xlsx_out.expanduser().resolve()
    ckpt = checkpoint_path_for_output(str(xlsx_out))
    arti = drive_artifact_checkpoint_path(str(xlsx_out))
    sub_path = _subcat_path(xlsx_out)

    by_ev = load_drive_artifact_checkpoint(arti)
    by_cls = load_checkpoint(ckpt)
    by_sub = load_checkpoint(sub_path) if sub_path.exists() else {}

    paths = {"classification_checkpoint": ckpt, "drive_artifacts": arti}
    if sub_path.exists():
        paths["subcategory_checkpoint"] = sub_path

    ids: set[int] = set(by_ev.keys()) | set(by_cls.keys())
    if not include_orphans:
        ids &= set(by_ev.keys())

    merged: list[dict[str, Any]] = []
    for aid in sorted(ids):
        row = _deep_copy_json(by_ev[aid]) if aid in by_ev else {"artifact_id": aid}
        if "artifact_id" not in row and aid in by_ev:
            row["artifact_id"] = aid

        c = by_cls.get(aid)
        if c:
            for k in ("bucket_number", "bucket_name", "confidence", "rationale"):
                if k in c and c[k] is not None and str(c[k]).strip() != "":
                    row[k] = c[k]
            # row_id duplicates artifact_id for the classifier; keep artifact_id canonical
            if "row_id" not in row:
                row["row_id"] = aid

        s = by_sub.get(aid)
        if s and str(s.get("sub_category") or "").strip():
            row["sub_category"] = str(s["sub_category"])

        merged.append(row)

    return merged, paths


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--xlsx-out",
        required=True,
        help="Same workbook path passed as --out for the Drive ingest run (the .xlsx file).",
    )
    p.add_argument(
        "--merged-out",
        default="",
        help="Output JSONL path (default: <stem>.merged_evidence.jsonl next to the .xlsx).",
    )
    p.add_argument(
        "--no-orphans",
        action="store_true",
        help="Omit artifact ids that appear only in the classification checkpoint (no evidence row).",
    )
    args = p.parse_args(argv)

    xlsx_out = Path(args.xlsx_out)
    merged, paths = merge_drive_sidecars(
        xlsx_out,
        include_orphans=not bool(args.no_orphans),
    )

    out_path = (
        Path(args.merged_out).expanduser().resolve()
        if str(args.merged_out).strip()
        else xlsx_out.expanduser().resolve().with_name(
            xlsx_out.expanduser().resolve().stem + ".merged_evidence.jsonl"
        )
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for row in merged:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"Wrote {len(merged)} lines to {out_path}")
    for label, path in paths.items():
        exists = "ok" if path.exists() else "missing"
        print(f"  {label}: {path} ({exists})")
    if not paths.get("subcategory_checkpoint"):
        subp = _subcat_path(xlsx_out.expanduser().resolve())
        print(f"  subcategory_checkpoint: {subp} (not used — file missing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
