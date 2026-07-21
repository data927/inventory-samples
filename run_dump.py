from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import env_loader  # noqa: F401 — load `.env` before default model

from llm_provider import default_llm_model

from server import build_inventory_from_dump


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dump", required=True, help="Path to dump folder")
    p.add_argument("--out", required=True, help="Output .xlsx path")
    p.add_argument("--model", default=default_llm_model())
    p.add_argument("--batch-size", type=int, default=int(os.environ.get("SEGMENTER_BATCH_SIZE", "25")))
    p.add_argument("--max-files", type=int, default=0, help="0 means no limit")
    p.add_argument("--workers", type=int, default=1, help="Parallel workers for extraction and LLM batches")
    p.add_argument(
        "--strict",
        action="store_true",
        help="Do not skip hidden dirs/files; do not prune any directories.",
    )
    args = p.parse_args(argv)

    dump_path = str(Path(args.dump).expanduser().resolve())
    out_path = str(Path(args.out).expanduser().resolve())
    max_files = None if args.max_files == 0 else int(args.max_files)

    msg = build_inventory_from_dump(
        dump_path=dump_path,
        output_path=out_path,
        model=str(args.model),
        batch_size=int(args.batch_size),
        max_files=max_files,
        strict_scan=bool(args.strict),
        workers=int(args.workers),
    )
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

