"""Build a curated sample set: 5-10 best files per bucket, selected by OpenAI scoring.

Two modes:

  LOCAL (default)
      Reads bucket subfolders from --src (e.g. sample_original/).
      Extracts text from each local file, scores with OpenAI, copies winners to --dst.

      python tools/build_sample_set.py --src sample_original --dst sample_final

  DRIVE
      Reads the drive inventory JSONL produced by run_drive.py.
      The JSONL has drive_url + snippet + word_count but no bucket assignments.
      OpenAI classifies AND scores each file in one step, then the script
      downloads the selected files from Google Drive into --dst.

      python tools/build_sample_set.py \\
          --drive-jsonl out/drive_1tb_inventory.drive_artifacts.jsonl \\
          --dst sample_final

Options common to both modes:
  --min 5          Minimum files per bucket
  --max 10         Maximum files per bucket
  --model gpt-4o-mini
  --dry-run        Score / classify and print report without copying or downloading
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import env_loader  # noqa: F401
from extractors.core import count_words, extract_snippet
from llm_provider import call_forced_tool, default_llm_model, llm_api_configured

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_SNIPPET_CHARS = 800
_BATCH_SIZE = 15

BUCKETS = [
    "Product & Engineering",
    "Customer & Sales",
    "Strategy & Planning",
    "Financial & Legal",
    "Operations & HR",
    "Marketing",
    "Meeting Notes & Internal Comms",
]

# ---------------------------------------------------------------------------
# Shared scoring prompt (local mode — bucket already known)
# ---------------------------------------------------------------------------

_SYSTEM_LOCAL = """\
You are a quality curator building a curated sample document set for a company knowledge base demo.
You evaluate candidate files and score each one on three axes (integers 1-10):

  content_richness  — Does the file contain substantial, informative content?
      1 = completely empty, whitespace-only, or just a title line
      5 = some useful content but sparse or fragmentary
     10 = rich, complete document with real information

  category_fit  — How well does this file exemplify the stated bucket category?
      1 = clearly wrong category or off-topic
      5 = loosely related
     10 = canonical example of the category

  sample_quality  — Would this make a good representative sample for demos or training?
      1 = template skeleton, duplicate, noise, or "untitled" stub
      5 = acceptable but unremarkable
     10 = unique, high-value, perfectly representative

Set should_reject = true only when the file is certainly worthless:
  completely empty, binary noise, a structural template with no data filled in,
  or a near-duplicate of a clearly better file in the same batch.
When in doubt, score low but do not reject.
"""

_SCORE_TOOL: dict = {
    "name": "score_files",
    "description": "Return quality scores for a batch of candidate sample files.",
    "input_schema": {
        "type": "object",
        "required": ["scores"],
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "index",
                        "content_richness",
                        "category_fit",
                        "sample_quality",
                        "should_reject",
                    ],
                    "properties": {
                        "index": {"type": "integer"},
                        "content_richness": {"type": "integer", "minimum": 1, "maximum": 10},
                        "category_fit": {"type": "integer", "minimum": 1, "maximum": 10},
                        "sample_quality": {"type": "integer", "minimum": 1, "maximum": 10},
                        "should_reject": {"type": "boolean"},
                        "note": {"type": "string"},
                    },
                },
            }
        },
    },
}

# ---------------------------------------------------------------------------
# Drive mode — classify + score in one step (no prior bucket assignment)
# ---------------------------------------------------------------------------

_SYSTEM_DRIVE = """\
You are a quality curator classifying and scoring company documents for a knowledge base sample set.

The seven buckets are:
  1. Product & Engineering   — technical specs, code docs, architecture, ML/AI, integrations
  2. Customer & Sales        — client lists, sales playbooks, proposals, deal tracking, CRM exports
  3. Strategy & Planning     — OKRs, roadmaps, board decks, market analysis, competitive intelligence
  4. Financial & Legal       — contracts, invoices, financial reports, legal filings, compliance
  5. Operations & HR         — HR policies, onboarding, org charts, payroll, internal processes
  6. Marketing               — campaign assets, brand guidelines, newsletters, outbound content
  7. Meeting Notes & Internal Comms — meeting notes, internal memos, team updates, retrospectives

For each file:
  1. Assign it to exactly one bucket (use the bucket name exactly as listed above).
  2. Score it on three axes (integers 1-10):

     content_richness  — how substantial and informative the content is
         1 = empty, noise, or binary-only
        10 = rich, complete document with real data

     category_fit      — how well it represents the assigned bucket
         1 = barely related      10 = canonical example

     sample_quality    — demo/training value
         1 = template, stub, duplicate, or noise
        10 = unique, high-value, perfectly representative

  3. Set should_reject = true only when completely certain the file is worthless
     (empty, binary noise, unfilled template, or near-duplicate of a better file in the same batch).
"""

_CLASSIFY_SCORE_TOOL: dict = {
    "name": "classify_and_score_files",
    "description": "Classify each file into a bucket and return quality scores.",
    "input_schema": {
        "type": "object",
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "index",
                        "bucket_name",
                        "content_richness",
                        "category_fit",
                        "sample_quality",
                        "should_reject",
                    ],
                    "properties": {
                        "index": {"type": "integer"},
                        "bucket_name": {
                            "type": "string",
                            "enum": BUCKETS,
                        },
                        "content_richness": {"type": "integer", "minimum": 1, "maximum": 10},
                        "category_fit": {"type": "integer", "minimum": 1, "maximum": 10},
                        "sample_quality": {"type": "integer", "minimum": 1, "maximum": 10},
                        "should_reject": {"type": "boolean"},
                        "note": {"type": "string"},
                    },
                },
            }
        },
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_file_id(drive_url: str) -> str | None:
    """Parse the file ID out of any Google Drive or Docs URL."""
    for pattern in (
        r"/file/d/([a-zA-Z0-9_-]{10,})",
        r"/document/d/([a-zA-Z0-9_-]{10,})",
        r"/spreadsheets/d/([a-zA-Z0-9_-]{10,})",
        r"/presentation/d/([a-zA-Z0-9_-]{10,})",
        r"[?&]id=([a-zA-Z0-9_-]{10,})",
    ):
        m = re.search(pattern, drive_url)
        if m:
            return m.group(1)
    return None


def _build_item_text(meta: dict, idx: int) -> str:
    return (
        f"\n--- File [{idx}] ---\n"
        f"Filename: {meta['filename']}\n"
        f"Path: {meta.get('path_hint', '')}\n"
        f"Type: {meta.get('extension', '')}  |  "
        f"Size: {meta['size_kb']} KB  |  "
        f"Word count: {meta['word_count']}\n"
        f"Content snippet:\n{meta['snippet'] or '(no extractable text)'}\n"
    )


def _score_local_batch(batch: list[dict], bucket_name: str, model: str) -> list[dict]:
    items_text = "".join(_build_item_text(m, i) for i, m in enumerate(batch))
    user_content = (
        f"Bucket category: {bucket_name}\n"
        f"Score each of the following {len(batch)} files.\n"
        f"{items_text}"
    )
    result = call_forced_tool(
        model=model,
        max_tokens=1200,
        system=_SYSTEM_LOCAL,
        tools=[_SCORE_TOOL],
        tool_name="score_files",
        user_content=user_content,
    )
    return result.get("scores", [])


def _classify_drive_batch(batch: list[dict], model: str) -> list[dict]:
    items_text = "".join(_build_item_text(m, i) for i, m in enumerate(batch))
    user_content = (
        f"Classify and score each of the following {len(batch)} files.\n"
        f"{items_text}"
    )
    result = call_forced_tool(
        model=model,
        max_tokens=1200,
        system=_SYSTEM_DRIVE,
        tools=[_CLASSIFY_SCORE_TOOL],
        tool_name="classify_and_score_files",
        user_content=user_content,
    )
    return result.get("results", [])


def _apply_scores(batch: list[dict], raw_scores: list[dict], bucket_key: str = "bucket_name") -> list[dict]:
    """Merge raw score dicts back onto the batch metas."""
    score_by_idx = {s["index"]: s for s in raw_scores if "index" in s and 0 <= s["index"] < len(batch)}
    out = []
    for local_idx, meta in enumerate(batch):
        sc = score_by_idx.get(local_idx, {})
        cr = sc.get("content_richness", 5)
        cf = sc.get("category_fit", 5)
        sq = sc.get("sample_quality", 5)
        out.append({
            **meta,
            "bucket_name": sc.get(bucket_key, meta.get("bucket_name", "")),
            "content_richness": cr,
            "category_fit": cf,
            "sample_quality": sq,
            "composite": round((cr + cf + sq) / 3, 2),
            "should_reject": sc.get("should_reject", False),
            "note": sc.get("note", ""),
        })
    return out


def _select(scored: list[dict], min_n: int, max_n: int) -> tuple[list[dict], list[dict]]:
    non_rej = sorted([s for s in scored if not s["should_reject"]], key=lambda x: x["composite"], reverse=True)
    rej = sorted([s for s in scored if s["should_reject"]], key=lambda x: x["composite"], reverse=True)
    selected = non_rej[:max_n]
    if len(selected) < min_n:
        backfill = rej[: min_n - len(selected)]
        for s in backfill:
            s["backfill"] = True
        selected += backfill
        log.warning("  Only %d non-rejected; backfilling %d from rejected pool", len(non_rej), len(backfill))
    return selected, [s for s in scored if s not in selected]


def _print_bucket(bucket_name: str, selected: list[dict], rejected: list[dict]) -> None:
    print(f"\n  SELECTED ({len(selected)}) — {bucket_name}:")
    for s in sorted(selected, key=lambda x: x["composite"], reverse=True):
        tag = " [backfill]" if s.get("backfill") else ""
        print(
            f"    {s['composite']:4.1f}  cr={s['content_richness']} "
            f"cf={s['category_fit']} sq={s['sample_quality']}  {s['filename']}{tag}"
        )
    if rejected:
        print(f"  EXCLUDED ({len(rejected)}):")
        for s in sorted(rejected, key=lambda x: x["composite"], reverse=True)[:8]:
            note = f"  [{s['note']}]" if s.get("note") else ""
            rej_tag = " [reject]" if s["should_reject"] else ""
            print(f"    {s['composite']:4.1f}{rej_tag}  {s['filename']}{note}")
        if len(rejected) > 8:
            print(f"    ... and {len(rejected) - 8} more excluded")


def _write_manifest(dst_root: Path, all_results: dict[str, dict]) -> None:
    manifest: dict = {}
    for bucket_name, result in all_results.items():
        manifest[bucket_name] = [
            {
                "filename": s["filename"],
                "drive_url": s.get("drive_url", ""),
                "composite": s["composite"],
                "content_richness": s["content_richness"],
                "category_fit": s["category_fit"],
                "sample_quality": s["sample_quality"],
                "should_reject": s["should_reject"],
                "backfill": s.get("backfill", False),
                "note": s.get("note", ""),
            }
            for s in sorted(result["selected"], key=lambda x: x["composite"], reverse=True)
        ]
    manifest_path = dst_root / "_selection_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"Manifest written: {manifest_path}")


# ---------------------------------------------------------------------------
# LOCAL mode
# ---------------------------------------------------------------------------

def run_local(args: argparse.Namespace, model: str) -> None:
    src_root = Path(args.src).resolve()
    dst_root = Path(args.dst).resolve()

    if not src_root.is_dir():
        sys.exit(f"Source directory not found: {src_root}")

    buckets = sorted(p for p in src_root.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not buckets:
        sys.exit(f"No bucket subdirectories found in {src_root}")

    log.info("Source: %s", src_root)
    log.info("Output: %s", dst_root)

    all_results: dict[str, dict] = {}

    for bucket_dir in buckets:
        bucket_name = bucket_dir.name
        log.info("\n=== Bucket: %s ===", bucket_name)

        files = sorted(f for f in bucket_dir.iterdir() if f.is_file() and not f.name.startswith("."))
        if not files:
            log.info("  No files — skipping")
            continue
        log.info("  Found %d files", len(files))

        file_metas: list[dict] = []
        for f in files:
            size_kb = round(f.stat().st_size / 1024, 1)
            snippet = extract_snippet(str(f), max_chars=_SNIPPET_CHARS) or ""
            file_metas.append({
                "path": f,
                "filename": f.name,
                "size_kb": size_kb,
                "word_count": count_words(snippet),
                "snippet": snippet,
            })

        scored: list[dict] = []
        for batch_start in range(0, len(file_metas), _BATCH_SIZE):
            batch = file_metas[batch_start: batch_start + _BATCH_SIZE]
            log.info("  Scoring files %d-%d / %d ...", batch_start + 1, batch_start + len(batch), len(file_metas))
            try:
                raw = _score_local_batch(batch, bucket_name, model)
            except Exception as exc:
                log.error("  Scoring batch failed: %s — neutral scores assigned", exc)
                raw = []
            scored.extend(_apply_scores(batch, raw))

        selected, rejected = _select(scored, args.min_n, args.max_n)
        all_results[bucket_name] = {"selected": selected, "rejected": rejected}
        _print_bucket(bucket_name, selected, rejected)

    if args.dry_run:
        print("\n[dry-run] No files copied.")
        return

    print(f"\nCopying selected files to {dst_root} ...")
    for bucket_name, result in all_results.items():
        bucket_dst = dst_root / bucket_name
        bucket_dst.mkdir(parents=True, exist_ok=True)
        for s in result["selected"]:
            shutil.copy2(s["path"], bucket_dst / s["filename"])

    _print_final_summary(dst_root, all_results)
    _write_manifest(dst_root, all_results)


# ---------------------------------------------------------------------------
# DRIVE mode
# ---------------------------------------------------------------------------

_DRIVE_BINARY_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff",
    "mp4", "mov", "avi", "mkv", "webm",
    "mp3", "wav", "m4a", "aac", "flac",
    "zip", "tar", "gz", "7z", "rar",
    "exe", "dmg", "pkg", "deb",
    "psd", "ai", "sketch",
})


def _load_drive_candidates(jsonl_path: Path, min_words: int = 50) -> list[dict]:
    """Read the drive artifacts JSONL and return scoreable candidates."""
    candidates = []
    skipped_binary = 0
    skipped_no_content = 0

    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            row = obj.get("row", obj)  # handle both {row: {...}} and flat
            ext = (row.get("extension") or "").lower().lstrip(".")
            if ext in _DRIVE_BINARY_EXTENSIONS:
                skipped_binary += 1
                continue
            word_count = int(row.get("word_count") or 0)
            snippet = (row.get("snippet") or "").strip()
            content_summary = (row.get("content_summary") or "").strip()
            drive_url = (row.get("drive_url") or "").strip()

            # Use snippet if available, fall back to content_summary
            text = snippet if len(snippet) > len(content_summary) else content_summary
            if word_count < min_words and len(text) < 100:
                skipped_no_content += 1
                continue

            file_id = _extract_file_id(drive_url) if drive_url else None

            candidates.append({
                "filename": row.get("filename", ""),
                "extension": ext,
                "path_hint": row.get("path", ""),
                "size_kb": round(int(row.get("size_bytes") or 0) / 1024, 1),
                "word_count": word_count,
                "snippet": text[:_SNIPPET_CHARS],
                "drive_url": drive_url,
                "file_id": file_id,
                "mime_type": row.get("mime_type") or row.get("modality") or "",
                "bucket_name": "",  # filled by classify step
            })

    log.info(
        "Drive JSONL: %d candidates loaded  (skipped %d binary, %d no-content)",
        len(candidates),
        skipped_binary,
        skipped_no_content,
    )
    return candidates


def _classify_all(candidates: list[dict], model: str) -> list[dict]:
    """Run classify+score batches over all candidates; returns flat list with bucket_name filled."""
    all_scored: list[dict] = []
    total = len(candidates)
    for batch_start in range(0, total, _BATCH_SIZE):
        batch = candidates[batch_start: batch_start + _BATCH_SIZE]
        log.info("  Classifying files %d-%d / %d ...", batch_start + 1, batch_start + len(batch), total)
        try:
            raw = _classify_drive_batch(batch, model)
        except Exception as exc:
            log.error("  Classify batch failed: %s — neutral scores + Product & Engineering assigned", exc)
            raw = []
        all_scored.extend(_apply_scores(batch, raw, bucket_key="bucket_name"))
    return all_scored


def _download_file(service: Any, meta: dict, dest_dir: Path) -> Path | None:
    """Download a single Drive file into dest_dir. Returns local path or None on failure."""
    from gdrive.fetch import fetch_drive_file_to_path, suggested_local_suffix

    file_id = meta.get("file_id")
    if not file_id:
        log.warning("  No file_id for %s — skipping download", meta["filename"])
        return None

    mime_type = meta.get("mime_type", "")
    display_name = meta["filename"]
    suffix = suggested_local_suffix(mime_type, display_name)
    stem = Path(display_name).stem or file_id
    dest = dest_dir / f"{stem}{suffix}"

    # Avoid clobbering if two files have the same stem
    counter = 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    err = fetch_drive_file_to_path(
        service,
        file_id=file_id,
        mime_type=mime_type,
        display_name=display_name,
        dest=dest,
    )
    if err:
        log.warning("  Download failed for %s: %s", display_name, err)
        return None
    return dest


def run_drive(args: argparse.Namespace, model: str) -> None:
    jsonl_path = Path(args.drive_jsonl).resolve()
    dst_root = Path(args.dst).resolve()

    if not jsonl_path.is_file():
        sys.exit(f"Drive JSONL not found: {jsonl_path}")

    log.info("Drive JSONL: %s", jsonl_path)
    log.info("Output: %s", dst_root)

    # 1. Load candidates
    candidates = _load_drive_candidates(jsonl_path)
    if not candidates:
        sys.exit("No scoreable candidates found in the JSONL. Check --drive-jsonl path.")

    # 2. Classify + score (all candidates, in batches)
    log.info("\nClassifying and scoring %d candidates ...", len(candidates))
    all_scored = _classify_all(candidates, model)

    # 3. Group by bucket and select top 5-10
    from collections import defaultdict
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for s in all_scored:
        bn = s.get("bucket_name") or "Product & Engineering"
        by_bucket[bn].append(s)

    all_results: dict[str, dict] = {}
    for bucket_name in BUCKETS:
        pool = by_bucket.get(bucket_name, [])
        if not pool:
            log.info("\n=== Bucket: %s — no candidates ===", bucket_name)
            all_results[bucket_name] = {"selected": [], "rejected": []}
            continue
        log.info("\n=== Bucket: %s — %d candidates ===", bucket_name, len(pool))
        selected, rejected = _select(pool, args.min_n, args.max_n)
        all_results[bucket_name] = {"selected": selected, "rejected": rejected}
        _print_bucket(bucket_name, selected, rejected)

    if args.dry_run:
        print("\n[dry-run] No files downloaded.")
        _print_final_summary(dst_root, all_results)
        return

    # 4. Authenticate and download selected files
    print("\nAuthenticating with Google Drive ...")
    try:
        from gdrive.credentials import build_drive_service, get_credentials
        creds = get_credentials(full_read_scope=True)
        service = build_drive_service(creds)
    except Exception as exc:
        sys.exit(f"Google Drive auth failed: {exc}\nRun: python tools/gdrive_scan.py --login-only")

    print(f"Downloading selected files to {dst_root} ...")
    for bucket_name, result in all_results.items():
        if not result["selected"]:
            continue
        bucket_dst = dst_root / bucket_name
        bucket_dst.mkdir(parents=True, exist_ok=True)
        for s in result["selected"]:
            log.info("  Downloading: %s", s["filename"])
            local_path = _download_file(service, s, bucket_dst)
            if local_path:
                s["local_path"] = str(local_path)
                log.info("  → %s", local_path.name)

    _print_final_summary(dst_root, all_results)
    _write_manifest(dst_root, all_results)


# ---------------------------------------------------------------------------
# Shared summary
# ---------------------------------------------------------------------------

def _print_final_summary(dst_root: Path, all_results: dict[str, dict]) -> None:
    print("\n=== Final sample set ===")
    total = 0
    for bucket_name in BUCKETS:
        result = all_results.get(bucket_name, {})
        n = len(result.get("selected", []))
        total += n
        print(f"  {bucket_name}: {n} files")
    print(f"\nTotal: {total} files → {dst_root}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--src",
        default=None,
        metavar="DIR",
        help="LOCAL mode: source directory with bucket subfolders (default: sample_original)",
    )
    mode.add_argument(
        "--drive-jsonl",
        default=None,
        metavar="JSONL",
        help="DRIVE mode: path to drive_artifacts JSONL (e.g. out/drive_1tb_inventory.drive_artifacts.jsonl)",
    )

    parser.add_argument("--dst", default="sample_final", help="Output directory (default: sample_final)")
    parser.add_argument("--min", type=int, default=5, dest="min_n", help="Minimum files per bucket")
    parser.add_argument("--max", type=int, default=10, dest="max_n", help="Maximum files per bucket")
    parser.add_argument("--model", default=None, help="OpenAI model (default: gpt-4o-mini)")
    parser.add_argument("--dry-run", action="store_true", help="Score/classify and report without writing files")

    args = parser.parse_args()

    if not llm_api_configured():
        sys.exit("No LLM API key configured. Set OPENAI_API_KEY in .env")

    model = args.model or os.environ.get("OPENAI_MODEL") or default_llm_model()
    if model.startswith("claude-") or model.startswith("gemini"):
        log.warning("Non-OpenAI model %r detected — switching to gpt-4o-mini", model)
        model = "gpt-4o-mini"
    log.info("Model: %s", model)

    if args.drive_jsonl:
        run_drive(args, model)
    else:
        # Default to local mode; --src defaults to sample_original
        if args.src is None:
            args.src = "sample_original"
        run_local(args, model)


if __name__ == "__main__":
    main()
