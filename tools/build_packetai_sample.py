"""Build the PacketAI curated 400-file sample set across 7 buckets.

Sources (all under input/):
  Export-41bd0fad-*/          Notion HTML export
  May 23 - PacketAI DD/       Due-diligence docs (PDF, DOCX, XLSX)
  PacketAI Board/             Board PPTX presentations
  AIOP Product Features/      Competitive intelligence PPTX

Segmentation reference: out/combined-enriched-full.xlsx (Evidence sheet).

Output: sample_packetai/[bucket]/ with Notion subdirectory trees preserved
verbatim so that all internal href links remain accessible when browsed locally.

Usage:
    python tools/build_packetai_sample.py [--dst sample_packetai] [--budget 400] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import env_loader  # noqa: F401
import pandas as pd
from extractors.core import count_words, extract_snippet
from llm_provider import call_forced_tool, default_llm_model, llm_api_configured, llm_provider

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Corpus paths (relative to repo root)
# ---------------------------------------------------------------------------
_NOTION_DIR = Path("input/Export-41bd0fad-2b81-42b4-9d17-aa2954f06765")
_DD_DIR = Path("input/May 23 - PacketAI DD")
_BOARD_DIR = Path("input/PacketAI Board")
_AIOP_DIR = Path("input/AIOP Product Features")
_INVENTORY = Path("out/combined-enriched-full.xlsx")
_INVENTORY_SHEET = "Evidence"

_CORPUS_PREFIXES: dict[str, Path] = {
    "export-inventory": _NOTION_DIR,
    "may23-packetai-dd": _DD_DIR,
    "packetai-board": _BOARD_DIR,
    "aiop-product-features": _AIOP_DIR,
}

_SOURCE_TAG = {
    "export-inventory": "notion",
    "may23-packetai-dd": "dd",
    "packetai-board": "board",
    "aiop-product-features": "aiop",
}

# ---------------------------------------------------------------------------
# Bucket targets (must sum to --budget; rescaled automatically if budget differs)
# ---------------------------------------------------------------------------
BUCKETS = [
    "Product & Engineering",
    "Customer & Sales",
    "Strategy & Planning",
    "Financial & Legal",
    "Operations & HR",
    "Marketing",
    "Meeting Notes & Internal Comms",
]

# Product & Engineering is fixed at 100 when budget=400; the other six buckets share 300
# proportionally by the weights below.
_PE_TARGET_AT_400 = 100
_OTHER_BUCKET_WEIGHTS = {
    "Customer & Sales": 11,
    "Strategy & Planning": 14,
    "Financial & Legal": 15,
    "Operations & HR": 13,
    "Marketing": 7,
    "Meeting Notes & Internal Comms": 10,
}

_STATIC_EXTS = frozenset({".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".csv", ".txt"})
_NOTION_EXTS = frozenset({".html"})
_SKIP_EXTS = frozenset({".zip", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".mp4", ".mov"})

_SNIPPET_CHARS = 900
_BATCH_SIZE = 10
_LLM_POOL_MULTIPLIER = 2.5  # score top N * target per bucket with LLM
_MAX_LINK_CLOSURE = 3000      # cap transitive expansion (companion dirs cover most links)
_SCORES_CACHE = Path("out/build_packetai_sample.scores.jsonl")
_SELECTION_CACHE = Path("out/build_packetai_sample.selection.json")

# ---------------------------------------------------------------------------
# Scoring prompts
# ---------------------------------------------------------------------------

_SYS_NOTION = """\
You are a quality curator building a 400-file sample corpus for a company knowledge base.
You will receive Notion pages from PacketAI with their bucket assignment and a rationale written
by a prior classifier. Score each page on three axes (integers 1–10):

  content_richness  1=empty/stub  10=dense, real information
  category_fit      1=wrong bucket  10=canonical example of the bucket
  sample_quality    1=template/noise/duplicate  10=unique, high demo/training value

Reject (should_reject=true) only when certainly worthless: empty, untitled with no body,
or a near-duplicate of a far better page in the same batch.
"""

_SYS_STATIC = """\
You are a quality curator scoring company documents that are already bucket-classified.
Score content_richness / category_fit / sample_quality (1–10).
Reject only when certainly empty, binary noise, or an unfilled template.
"""

_SCORE_TOOL = {
    "name": "score_files",
    "description": "Quality scores for a batch of files.",
    "input_schema": {
        "type": "object", "required": ["scores"],
        "properties": {"scores": {"type": "array", "items": {
            "type": "object",
            "required": ["index", "content_richness", "category_fit", "sample_quality", "should_reject"],
            "properties": {
                "index":            {"type": "integer"},
                "content_richness": {"type": "integer", "minimum": 1, "maximum": 10},
                "category_fit":     {"type": "integer", "minimum": 1, "maximum": 10},
                "sample_quality":   {"type": "integer", "minimum": 1, "maximum": 10},
                "should_reject":    {"type": "boolean"},
                "note":             {"type": "string"},
            },
        }}},
    },
}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def resolve_inventory_path(raw_path: str) -> tuple[Path | None, str, str]:
    """Return (local_path, corpus_prefix, rel_within_corpus)."""
    raw = (raw_path or "").strip().replace("\\", "/")
    if not raw:
        return None, "", ""

    # Legacy: path relative to export root only (no prefix)
    if "/" not in raw:
        p = _NOTION_DIR / raw
        if p.exists():
            return p, "export-inventory", raw
        return None, "", ""

    prefix, rel = raw.split("/", 1)
    base = _CORPUS_PREFIXES.get(prefix)
    if base is None:
        return None, "", ""
    p = base / rel
    return (p, prefix, rel) if p.exists() else (None, prefix, rel)


def _tier_score(tier: str) -> float:
    t = (tier or "").strip().lower()
    if t == "high":
        return 10.0
    if t == "medium":
        return 6.0
    return 2.0


def _conf_score(conf: str) -> float:
    c = (conf or "").strip().lower()
    if c == "high":
        return 3.0
    if c == "medium":
        return 2.0
    return 0.0


def heuristic_score(row: dict) -> float:
    """Fast pre-rank before LLM scoring."""
    wc = float(row.get("word_count") or 0)
    wc_bonus = min(wc / 200.0, 8.0)
    child_bonus = min(float(row.get("child_html_count") or 0) * 0.3, 5.0)
    root_bonus = 1.5 if row.get("is_root") else 0.0
    rat_len = len(str(row.get("rationale") or ""))
    rat_bonus = min(rat_len / 80.0, 3.0)
    return (
        _tier_score(str(row.get("quality_tier") or ""))
        + _conf_score(str(row.get("confidence") or ""))
        + wc_bonus
        + child_bonus
        + root_bonus
        + rat_bonus
    )


# ---------------------------------------------------------------------------
# Step 1 — Load inventory from combined-enriched-full.xlsx
# ---------------------------------------------------------------------------

def load_inventory() -> dict[str, dict]:
    """Return {Path / Subsection: metadata} for all Evidence rows."""
    if not _INVENTORY.exists():
        raise FileNotFoundError(f"Inventory not found: {_INVENTORY}")
    df = pd.read_excel(_INVENTORY, sheet_name=_INVENTORY_SHEET)
    result: dict[str, dict] = {}
    for _, row in df.iterrows():
        raw_path = str(row.get("Path / Subsection") or "").strip()
        if not raw_path:
            continue
        local, prefix, rel = resolve_inventory_path(raw_path)
        if local is None:
            continue
        suffix = local.suffix.lower()
        if suffix in _SKIP_EXTS:
            continue
        is_notion = prefix == "export-inventory"
        if is_notion and suffix not in _NOTION_EXTS:
            continue
        if not is_notion and suffix not in _STATIC_EXTS:
            continue

        wc = row.get("Word Count")
        try:
            word_count = int(float(wc)) if wc == wc else 0  # NaN check
        except (TypeError, ValueError):
            word_count = 0

        is_root = is_notion and local.parent == _NOTION_DIR
        companion_dir = local.parent / local.stem if is_notion else None
        child_html_count = 0
        if companion_dir and companion_dir.is_dir():
            child_html_count = sum(1 for _ in companion_dir.rglob("*.html"))

        result[raw_path] = {
            "inventory_path": raw_path,
            "local_path": local,
            "rel_in_corpus": rel,
            "corpus_prefix": prefix,
            "source": _SOURCE_TAG.get(prefix, "static"),
            "filename": local.name,
            "item_name": str(row.get("Item Name") or local.name),
            "file_type": str(row.get("File Type") or ""),
            "bucket_name": str(row.get("bucket_name") or "Product & Engineering"),
            "confidence": str(row.get("confidence") or "low"),
            "rationale": str(row.get("rationale") or ""),
            "quality_tier": str(row.get("Quality tier") or ""),
            "word_count": word_count,
            "is_root": is_root,
            "child_html_count": child_html_count,
        }
    log.info("Inventory: %d resolvable files from %s", len(result), _INVENTORY.name)
    return result


def inventory_to_candidates(inventory: dict[str, dict]) -> list[dict]:
    """Filter to scorable candidates (skip obvious stubs)."""
    candidates: list[dict] = []
    for meta in inventory.values():
        conf = meta["confidence"]
        rationale = meta["rationale"]
        wc = meta["word_count"]
        tier = meta["quality_tier"].lower()
        is_notion = meta["source"] == "notion"

        if is_notion:
            if conf == "low" and len(rationale) < 40 and wc < 30:
                continue
            if tier == "low" and wc < 15 and conf == "low":
                continue
        else:
            if wc == 0 and tier == "low" and conf == "low":
                pass  # keep static — may be scanned PDFs

        c = dict(meta)
        c["heuristic"] = heuristic_score(meta)
        candidates.append(c)

    log.info("Candidates after stub filter: %d", len(candidates))
    return candidates


# ---------------------------------------------------------------------------
# Step 2 — LLM scoring (top pool per bucket only)
# ---------------------------------------------------------------------------

def _bucket_targets(budget: int) -> dict[str, int]:
    """PE gets 100/400 of budget; the other six buckets split the remainder."""
    pe_target = round(budget * _PE_TARGET_AT_400 / 400)
    other_budget = budget - pe_target
    other_buckets = [b for b in BUCKETS if b != "Product & Engineering"]
    weight_sum = sum(_OTHER_BUCKET_WEIGHTS[b] for b in other_buckets)

    targets: dict[str, int] = {"Product & Engineering": pe_target}
    allocated = 0
    for i, bucket in enumerate(other_buckets):
        if i == len(other_buckets) - 1:
            targets[bucket] = other_budget - allocated
        else:
            n = max(5, round(_OTHER_BUCKET_WEIGHTS[bucket] / weight_sum * other_budget))
            targets[bucket] = n
            allocated += n
    return targets


def pick_llm_pool(candidates: list[dict], targets: dict[str, int]) -> list[dict]:
    """Select top heuristic candidates per bucket for LLM refinement."""
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        bn = c.get("bucket_name") or "Product & Engineering"
        if bn in targets:
            by_bucket[bn].append(c)

    pool: list[dict] = []
    for bucket in BUCKETS:
        items = by_bucket.get(bucket, [])
        items.sort(key=lambda x: x["heuristic"], reverse=True)
        cap = max(15, int(targets[bucket] * _LLM_POOL_MULTIPLIER))
        pool.extend(items[:cap])
    log.info("LLM scoring pool: %d files (top %.1fx per-bucket targets)", len(pool), _LLM_POOL_MULTIPLIER)
    return pool


def score_candidates(pool: list[dict], model: str) -> dict[str, dict]:
    """Score pool with LLM; return keyed by inventory_path."""
    scored: dict[str, dict] = {}
    notion_items = [c for c in pool if c["source"] == "notion"]
    static_items = [c for c in pool if c["source"] != "notion"]

    for label, items, system, use_snippet in [
        ("Notion", notion_items, _SYS_NOTION, False),
        ("Static", static_items, _SYS_STATIC, True),
    ]:
        total = len(items)
        for batch_start in range(0, total, _BATCH_SIZE):
            batch = items[batch_start: batch_start + _BATCH_SIZE]
            if not batch:
                continue
            log.info("  Scoring %s %d–%d / %d ...", label, batch_start + 1, batch_start + len(batch), total)

            items_text = ""
            for i, c in enumerate(batch):
                if use_snippet:
                    snippet = extract_snippet(str(c["local_path"]), max_chars=_SNIPPET_CHARS) or ""
                    c["snippet"] = snippet
                    body = f"Snippet:\n{snippet or '(no extractable text)'}\n"
                else:
                    body = (
                        f"Classifier rationale: {c['rationale'][:600]}\n"
                        f"Word count (inventory): {c['word_count']}\n"
                        f"Children: {c['child_html_count']}  |  Quality tier: {c['quality_tier']}\n"
                    )
                items_text += (
                    f"\n--- [{i}] ---\n"
                    f"Filename: {c['filename']}\n"
                    f"Bucket: {c['bucket_name']}  |  Confidence: {c['confidence']}\n"
                    f"{body}"
                )

            user_content = f"Score the following {len(batch)} files.\n{items_text}"
            try:
                raw = call_forced_tool(
                    model=model, max_tokens=1400, system=system,
                    tools=[_SCORE_TOOL], tool_name="score_files",
                    user_content=user_content,
                )
                raw_scores = raw.get("scores", [])
            except Exception as exc:
                log.error("  Scoring batch failed: %s — using heuristic", exc)
                raw_scores = []

            score_by_idx = {s["index"]: s for s in raw_scores if 0 <= s.get("index", -1) < len(batch)}
            batch_entries: list[dict] = []
            for local_idx, c in enumerate(batch):
                sc = score_by_idx.get(local_idx, {})
                cr = sc.get("content_richness") or max(5, int(c["heuristic"] / 2))
                cf = sc.get("category_fit") or max(5, int(c["heuristic"] / 2))
                sq = sc.get("sample_quality") or max(5, int(c["heuristic"] / 2))
                entry = {
                    **c,
                    "content_richness": cr,
                    "category_fit": cf,
                    "sample_quality": sq,
                    "composite": round((cr + cf + sq) / 3, 2),
                    "should_reject": sc.get("should_reject", False),
                    "note": sc.get("note", ""),
                }
                scored[c["inventory_path"]] = entry
                batch_entries.append(entry)
            append_scores_cache(_SCORES_CACHE, batch_entries)

    return scored


def merge_scores(candidates: list[dict], llm_scored: dict[str, dict]) -> list[dict]:
    """All candidates get a composite; LLM overrides where available."""
    merged: list[dict] = []
    for c in candidates:
        key = c["inventory_path"]
        if key in llm_scored:
            merged.append(llm_scored[key])
        else:
            h = c["heuristic"]
            base = max(3, min(9, int(h / 2)))
            merged.append({
                **c,
                "content_richness": base,
                "category_fit": base,
                "sample_quality": base,
                "composite": round(base, 2),
                "should_reject": c["confidence"] == "low" and c["word_count"] < 10 and c["quality_tier"].lower() == "low",
                "note": "heuristic-only",
            })
    return merged


# ---------------------------------------------------------------------------
# Step 3 — Selection
# ---------------------------------------------------------------------------

def select_files(all_scored: list[dict], budget: int) -> dict[str, list[dict]]:
    targets = _bucket_targets(budget)

    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for s in all_scored:
        bn = s.get("bucket_name") or "Product & Engineering"
        if bn in targets:
            by_bucket[bn].append(s)

    selected_by_bucket: dict[str, list[dict]] = {}
    for bucket in BUCKETS:
        pool = [s for s in by_bucket[bucket] if not s.get("should_reject")]
        rejected_pool = [s for s in by_bucket[bucket] if s.get("should_reject")]

        pool.sort(
            key=lambda x: (
                0 if x.get("confidence") == "low" else 1,
                x.get("composite", 0),
                x.get("heuristic", 0),
            ),
            reverse=True,
        )
        rejected_pool.sort(key=lambda x: (x.get("composite", 0), x.get("heuristic", 0)), reverse=True)

        target = targets[bucket]
        chosen = pool[:target]
        if len(chosen) < 5 and rejected_pool:
            needed = 5 - len(chosen)
            for s in rejected_pool[:needed]:
                s["backfill"] = True
            chosen += rejected_pool[:needed]

        selected_by_bucket[bucket] = chosen

    total_selected = sum(len(v) for v in selected_by_bucket.values())
    log.info("Primary selection: %d files (target %d)", total_selected, budget)
    return selected_by_bucket


# ---------------------------------------------------------------------------
# Step 4 — Transitive link closure for Notion HTML
# ---------------------------------------------------------------------------

def _parse_local_hrefs(html_path: Path, base_dir: Path) -> list[Path]:
    try:
        content = html_path.read_text(errors="replace")
    except OSError:
        return []
    linked: list[Path] = []
    for raw in re.findall(r'href="([^"#][^"]*)"', content):
        decoded = urllib.parse.unquote(raw).split("#")[0].strip()
        if not decoded or decoded.startswith(("http", "mailto", "//")):
            continue
        target = (html_path.parent / decoded).resolve()
        if target.exists() and target.is_file():
            try:
                target.relative_to(base_dir.resolve())
                linked.append(target)
            except ValueError:
                pass
    return linked


def _inventory_by_local(inventory: dict[str, dict]) -> dict[Path, dict]:
    return {meta["local_path"].resolve(): meta for meta in inventory.values()}


def expand_link_closure(
    selected_notion: list[dict],
    inventory: dict[str, dict],
    *,
    max_additions: int = _MAX_LINK_CLOSURE,
) -> list[dict]:
    """BFS: include locally-linked files reachable from selections (capped)."""
    by_local = _inventory_by_local(inventory)
    seen: set[Path] = {c["local_path"].resolve() for c in selected_notion}
    additions: list[dict] = []
    queue = list(selected_notion)
    steps = 0

    while queue and len(additions) < max_additions:
        c = queue.pop(0)
        steps += 1
        if steps % 25 == 0:
            log.info("  link closure: scanned %d pages, +%d files ...", steps, len(additions))

        for target in _parse_local_hrefs(c["local_path"], _NOTION_DIR):
            if len(additions) >= max_additions:
                break
            resolved = target.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)

            base_meta = by_local.get(resolved)
            if base_meta:
                addition = dict(base_meta)
            else:
                try:
                    rel = str(target.relative_to(_NOTION_DIR))
                except ValueError:
                    continue
                addition = {
                    "inventory_path": f"export-inventory/{rel}",
                    "local_path": target,
                    "rel_in_corpus": rel,
                    "corpus_prefix": "export-inventory",
                    "source": "notion",
                    "filename": target.name,
                    "confidence": "closure",
                    "rationale": f"Linked from: {c['filename']}",
                    "quality_tier": "",
                    "word_count": 0,
                    "is_root": target.parent == _NOTION_DIR,
                    "child_html_count": 0,
                }

            addition.update({
                "content_richness": c.get("content_richness", 5),
                "category_fit": c.get("category_fit", 5),
                "sample_quality": c.get("sample_quality", 5),
                "composite": c.get("composite", 5),
                "should_reject": False,
                "note": "link-closure",
                "bucket_name": c["bucket_name"],
            })
            additions.append(addition)
            if target.suffix.lower() == ".html":
                queue.append(addition)

    if len(additions) >= max_additions:
        log.warning("Link closure hit cap (%d); companion dirs still copied on export", max_additions)
    log.info("Link closure: added %d linked files", len(additions))
    return selected_notion + additions


def validate_links(selected_by_bucket: dict[str, list[dict]]) -> list[str]:
    """Report broken local hrefs among selected Notion HTML files."""
    broken: list[str] = []
    notion_html = [
        f for files in selected_by_bucket.values() for f in files
        if f.get("source") == "notion" and f["local_path"].suffix.lower() == ".html"
    ]
    for f in notion_html:
        for target in _parse_local_hrefs(f["local_path"], _NOTION_DIR):
            if not target.exists():
                broken.append(f"{f['filename']} → {target.name}")
    return broken


# ---------------------------------------------------------------------------
# Step 5 — Copy with structure preservation
# ---------------------------------------------------------------------------

def copy_selection(selected_by_bucket: dict[str, list[dict]], dst_root: Path) -> None:
    copied_dirs: set[Path] = set()

    for bucket, files in selected_by_bucket.items():
        bucket_dst = dst_root / bucket
        bucket_dst.mkdir(parents=True, exist_ok=True)
        for f in files:
            src: Path = f["local_path"]
            if not src.exists():
                log.warning("Source missing: %s", src)
                continue

            if f["source"] == "notion":
                _copy_notion_file(src, bucket_dst, copied_dirs)
            else:
                rel = f.get("rel_in_corpus") or src.name
                dst = bucket_dst / rel
                _safe_copy(src, dst)

    log.info("Copy complete → %s", dst_root)


def _copy_notion_file(src: Path, bucket_dst: Path, copied_dirs: set[Path]) -> None:
    """Copy Notion HTML preserving relative path; include companion asset dir once."""
    try:
        rel = src.relative_to(_NOTION_DIR)
    except ValueError:
        rel = Path(src.name)

    dst = bucket_dst / rel
    _safe_copy(src, dst)

    companion = src.parent / src.stem
    if companion.is_dir() and companion.resolve() not in copied_dirs:
        dst_companion = bucket_dst / companion.relative_to(_NOTION_DIR)
        if dst_companion.exists():
            shutil.rmtree(dst_companion)
        shutil.copytree(companion, dst_companion)
        copied_dirs.add(companion.resolve())


def _safe_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        return
    shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_bucket_report(bucket: str, selected: list[dict], all_pool: list[dict]) -> None:
    excluded_count = len(all_pool) - len(selected)
    print(f"\n  {bucket} — SELECTED {len(selected)} / {len(all_pool)} candidates:")
    for s in sorted(selected, key=lambda x: x.get("composite", 0), reverse=True)[:12]:
        tag = ""
        if s.get("backfill"):
            tag += " [backfill]"
        if s.get("note") == "link-closure":
            tag += " [link-closure]"
        src_tag = f"[{s['source']}]"
        print(
            f"    {s.get('composite', 0):4.1f}  cr={s.get('content_richness')} "
            f"cf={s.get('category_fit')} sq={s.get('sample_quality')}  "
            f"{src_tag} {s['filename'][:60]}{tag}"
        )
    if len(selected) > 12:
        print(f"    ... and {len(selected) - 12} more selected")
    if excluded_count:
        print(f"  EXCLUDED: {excluded_count} files")


def save_selection_checkpoint(path: Path, selected_by_bucket: dict[str, list[dict]]) -> None:
    payload = {
        bucket: [f["inventory_path"] for f in files]
        for bucket, files in selected_by_bucket.items()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    log.info("Selection checkpoint: %d files → %s", sum(len(v) for v in payload.values()), path)


def load_scores_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    scored: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = row.get("inventory_path")
        if key:
            if "local_path" in row:
                row["local_path"] = Path(row["local_path"])
            scored[key] = row
    log.info("Loaded %d cached LLM scores from %s", len(scored), path)
    return scored


def _json_safe_entry(entry: dict) -> dict:
    out: dict = {}
    for k, v in entry.items():
        if isinstance(v, Path):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def append_scores_cache(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(_json_safe_entry(entry), ensure_ascii=False) + "\n")


def write_manifest(dst_root: Path, selected_by_bucket: dict[str, list[dict]]) -> None:
    manifest: dict = {}
    for bucket, files in selected_by_bucket.items():
        manifest[bucket] = [
            {
                "filename": f["filename"],
                "inventory_path": f.get("inventory_path", ""),
                "source": f["source"],
                "composite": f.get("composite"),
                "content_richness": f.get("content_richness"),
                "category_fit": f.get("category_fit"),
                "sample_quality": f.get("sample_quality"),
                "confidence": f.get("confidence", ""),
                "quality_tier": f.get("quality_tier", ""),
                "note": f.get("note", ""),
            }
            for f in sorted(files, key=lambda x: x.get("composite", 0), reverse=True)
        ]
    path = dst_root / "_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"\nManifest written: {path}")


def _resolve_model(explicit: str | None) -> str:
    if explicit:
        return explicit
    provider = llm_provider()
    if provider == "openai":
        return (os.environ.get("OPENAI_MODEL") or "gpt-4o").strip()
    if provider == "anthropic":
        return (os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-6").strip()
    return default_llm_model()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _setup_file_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)
    log.info("Logging to %s", log_path.resolve())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dst", default="out/sample_packetai", help="Output directory (default: out/sample_packetai)")
    parser.add_argument("--budget", type=int, default=400, help="Target file count (default: 400)")
    parser.add_argument("--model", default=None, help="LLM model (default: gpt-4o or claude-sonnet-4-6)")
    parser.add_argument("--log", default="out/build_packetai_sample.log", help="Log file path")
    parser.add_argument("--dry-run", action="store_true", help="Score and report without copying files")
    parser.add_argument("--skip-llm", action="store_true", help="Use heuristic scores only (no API calls)")
    parser.add_argument("--resume-scores", action="store_true", help="Reuse out/build_packetai_sample.scores.jsonl")
    parser.add_argument("--skip-link-closure", action="store_true", help="Skip transitive link expansion")
    args = parser.parse_args()

    log_file = Path(args.log).resolve()
    _setup_file_logging(log_file)
    print(f"Log file: {log_file}")

    if not args.skip_llm and not llm_api_configured():
        sys.exit("No LLM API key configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env")

    model = _resolve_model(args.model)
    targets = _bucket_targets(args.budget)
    log.info("Model: %s  |  Budget: %d  |  Output: %s", model, args.budget, args.dst)
    log.info("Per-bucket targets: %s", targets)

    log.info("\n[1/6] Loading inventory from %s ...", _INVENTORY)
    inventory = load_inventory()
    candidates = inventory_to_candidates(inventory)

    log.info("[2/6] Building scored candidate pool ...")
    if args.skip_llm:
        all_scored = merge_scores(candidates, {})
    elif args.resume_scores:
        cached = load_scores_cache(_SCORES_CACHE)
        all_scored = merge_scores(candidates, cached)
    else:
        _SCORES_CACHE.unlink(missing_ok=True)
        llm_pool = pick_llm_pool(candidates, targets)
        log.info("[3/6] LLM scoring top candidates ...")
        llm_scored = score_candidates(llm_pool, model)
        all_scored = merge_scores(candidates, llm_scored)

    log.info("[4/6] Selecting top files per bucket (budget=%d) ...", args.budget)
    selected_by_bucket = select_files(all_scored, args.budget)
    save_selection_checkpoint(_SELECTION_CACHE, selected_by_bucket)

    if not args.skip_link_closure:
        log.info("[5/6] Expanding transitive link closure on Notion selections ...")
        all_notion_selected = [
            f for files in selected_by_bucket.values() for f in files if f["source"] == "notion"
        ]
        all_with_closure = expand_link_closure(all_notion_selected, inventory)

        primary_paths = {f["local_path"].resolve() for files in selected_by_bucket.values() for f in files}
        for addition in all_with_closure:
            if addition["local_path"].resolve() not in primary_paths:
                bn = addition["bucket_name"]
                if bn in selected_by_bucket:
                    selected_by_bucket[bn].append(addition)
                primary_paths.add(addition["local_path"].resolve())
    else:
        log.info("[5/6] Skipping link closure")

    broken = validate_links(selected_by_bucket)
    if broken:
        log.warning("Broken local links after closure: %d (first 5: %s)", len(broken), broken[:5])
    else:
        log.info("Link validation: all local href targets present in corpus")

    all_pool_by_bucket: dict[str, list[dict]] = defaultdict(list)
    for s in all_scored:
        all_pool_by_bucket[s.get("bucket_name", "Product & Engineering")].append(s)

    for bucket in BUCKETS:
        print_bucket_report(bucket, selected_by_bucket.get(bucket, []), all_pool_by_bucket.get(bucket, []))

    total = sum(len(v) for v in selected_by_bucket.values())
    print(f"\n=== Total: {total} files (primary {args.budget} + link-closure) ===")
    for bucket in BUCKETS:
        n = len(selected_by_bucket.get(bucket, []))
        print(f"  {bucket}: {n}")

    by_source = defaultdict(int)
    for files in selected_by_bucket.values():
        for f in files:
            by_source[f["source"]] += 1
    print("\nBy source:", dict(by_source))

    if args.dry_run:
        print("\n[dry-run] No files copied.")
        return

    dst_root = Path(args.dst).resolve()
    print(f"\n[6/6] Copying to {dst_root} ...")
    copy_selection(selected_by_bucket, dst_root)
    write_manifest(dst_root, selected_by_bucket)
    print(f"\nDone. {total} files → {dst_root}")


if __name__ == "__main__":
    main()
