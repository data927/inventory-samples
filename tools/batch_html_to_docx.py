#!/usr/bin/env python3
"""Batch-convert every ``.html`` / ``.htm`` under a directory tree to ``.docx``.

Uses :func:`extractors.html_to_docx_convert.html_path_to_docx_bytes` (same pipeline as
``tools/html_to_docx.py``): Notion prep, relative images, relocated-export search,
visible table borders for Google Docs, etc.

**Resume:** successful conversions are appended to a JSONL checkpoint; re-running skips
those relative paths until you delete the checkpoint file.

**Parallelism:** ``--workers N`` runs up to ``N`` conversions concurrently (separate
processes). Default scales with CPU count (capped). Use ``--workers 1`` for sequential.

**Logs:** INFO lines go to stdout (line-buffered when interactive) and optionally to
``--log``; flush after each completed file.

Example:

    python tools/batch_html_to_docx.py \\
      --root Export-41bd0fad-2b81-42b4-9d17-aa2954f06765 \\
      --output-dir out/docx_export_41bd0fad \\
      --checkpoint out/docx_export_41bd0fad.checkpoint.jsonl \\
      --log out/docx_export_41bd0fad.log \\
      --workers 8
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path


def _configure_logging(log_path: Path | None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass
    root.addHandler(sh)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)
        root.addHandler(fh)


def _load_done(checkpoint: Path) -> set[str]:
    done: set[str] = set()
    if not checkpoint.is_file():
        return done
    with checkpoint.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ok") is True and isinstance(rec.get("rel"), str):
                done.add(rec["rel"])
    return done


def _append_checkpoint(checkpoint: Path, record: dict) -> None:
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    with checkpoint.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


def _iter_html_files(root: Path) -> list[Path]:
    """Faster than ``Path.rglob`` on very large trees (``os.walk``)."""
    out: list[Path] = []
    root = root.resolve()
    for dirpath, _dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        for name in filenames:
            lower = name.lower()
            if lower.endswith(".html") or lower.endswith(".htm"):
                out.append(Path(dirpath) / name)
    out.sort(key=lambda x: str(x).lower())
    return out


def _default_workers() -> int:
    n = mp.cpu_count() or 4
    return max(2, min(16, n))


def _convert_worker(item: tuple[str, str, str]) -> tuple[str, bool, int | None, str | None]:
    """Picklable worker: ``(rel, src_str, dst_str)`` → ``(rel, ok, nbytes|None, err|None)``."""
    rel, src_str, dst_str = item
    try:
        from extractors.html_to_docx_convert import html_path_to_docx_bytes

        src = Path(src_str)
        dst = Path(dst_str)
        dst.parent.mkdir(parents=True, exist_ok=True)
        buf = html_path_to_docx_bytes(src, title=None, raw=False)
        data = buf.getvalue()
        dst.write_bytes(data)
        return (rel, True, len(data), None)
    except Exception as e:
        return (rel, False, None, f"{type(e).__name__}: {e}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True, help="Directory tree to scan")
    p.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Destination root; mirrors paths under --root with .docx suffix",
    )
    p.add_argument("--checkpoint", type=Path, required=True, help="Append-only JSONL of finished files")
    p.add_argument("--log", type=Path, default=None, help="Also log to this file")
    p.add_argument(
        "--failures",
        type=Path,
        default=None,
        help="Append JSONL of failures (default: <checkpoint>.failures.jsonl)",
    )
    p.add_argument("--limit", type=int, default=0, help="Max files to process (0 = all)")
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel worker processes (default: auto; use 1 for sequential)",
    )
    args = p.parse_args(argv)

    try:
        import html2docx  # noqa: F401
    except ImportError:
        print("Missing dependency: html2docx\n  python -m pip install html2docx", file=sys.stderr)
        return 1

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"--root is not a directory: {root}", file=sys.stderr)
        return 1

    output_dir = args.output_dir.expanduser().resolve()
    checkpoint = args.checkpoint.expanduser().resolve()
    failures_path = args.failures
    if failures_path is None:
        failures_path = checkpoint.with_suffix(checkpoint.suffix + ".failures.jsonl")
    else:
        failures_path = failures_path.expanduser().resolve()

    workers = args.workers
    if workers is None:
        workers = _default_workers()
    workers = max(1, int(workers))

    _configure_logging(args.log.expanduser().resolve() if args.log else None)
    log = logging.getLogger("batch_html_to_docx")

    done = _load_done(checkpoint)
    log.info(
        "root=%s output_dir=%s checkpoint=%s already_done=%d workers=%d",
        root,
        output_dir,
        checkpoint,
        len(done),
        workers,
    )

    t_scan = time.perf_counter()
    paths = _iter_html_files(root)
    log.info("discovered %d html files in %.1fs", len(paths), time.perf_counter() - t_scan)

    total = len(paths)
    if args.limit and args.limit > 0:
        paths = paths[: args.limit]
        log.info("limit=%d (of %d total under root)", args.limit, total)

    tasks: list[tuple[str, str, str]] = []
    skipped = 0
    for src in paths:
        rel = src.relative_to(root).as_posix()
        if rel in done:
            skipped += 1
            continue
        rel_out = Path(rel).with_suffix(".docx").as_posix()
        dst = (output_dir / rel_out).resolve()
        tasks.append((rel, str(src.resolve()), str(dst)))

    ok = err = 0
    t0 = time.perf_counter()
    done_in_run = 0

    def _log_result(rel: str, rel_out: str, success: bool, nbytes: int | None, err_msg: str | None) -> None:
        nonlocal ok, err, done_in_run
        done_in_run += 1
        elapsed = time.perf_counter() - t0
        if success:
            ok += 1
            rec = {"ok": True, "rel": rel, "out": rel_out, "bytes": nbytes or 0}
            _append_checkpoint(checkpoint, rec)
            log.info(
                "[%d/%d] OK %s -> %s (%d bytes) total_ok=%d err=%d %.1fs",
                done_in_run,
                len(tasks),
                rel,
                rel_out,
                nbytes or 0,
                ok,
                err,
                elapsed,
            )
        else:
            err += 1
            log.error("[%d/%d] FAIL %s: %s", done_in_run, len(tasks), rel, err_msg)
            with failures_path.open("a", encoding="utf-8") as ff:
                ff.write(
                    json.dumps(
                        {"ok": False, "rel": rel, "error": err_msg, "type": "Error"},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                ff.flush()
        for handler in logging.getLogger().handlers:
            handler.flush()

    if not tasks:
        log.info("nothing to do (all skipped or empty tree)")
        return 0

    log.info("queued %d conversions (checkpoint skipped %d)", len(tasks), skipped)

    if workers == 1:
        for src in paths:
            rel = src.relative_to(root).as_posix()
            if rel in done:
                continue
            rel_out = Path(rel).with_suffix(".docx").as_posix()
            dst = (output_dir / rel_out).resolve()
            rel_r, success, nbytes, err_msg = _convert_worker((rel, str(src.resolve()), str(dst)))
            _log_result(rel_r, rel_out, success, nbytes, err_msg)
    else:
        # Never submit all futures at once (22k+ tasks can freeze for minutes / OOM).
        max_in_flight = max(workers * 8, workers + 1)
        task_iter = iter(tasks)
        pending: set = set()

        def _fill_pool(ex: ProcessPoolExecutor) -> None:
            while len(pending) < max_in_flight:
                try:
                    t = next(task_iter)
                except StopIteration:
                    break
                pending.add(ex.submit(_convert_worker, t))

        with ProcessPoolExecutor(max_workers=workers) as ex:
            _fill_pool(ex)
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    rel, success, nbytes, err_msg = fut.result()
                    rel_out = Path(rel).with_suffix(".docx").as_posix()
                    _log_result(rel, rel_out, success, nbytes, err_msg)
                _fill_pool(ex)

    elapsed = time.perf_counter() - t0
    log.info(
        "done tasks_this_run=%d skipped_checkpoint=%d ok=%d err=%d elapsed_s=%.1f workers=%d",
        len(tasks),
        skipped,
        ok,
        err,
        elapsed,
        workers,
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    return 0 if err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
