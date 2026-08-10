"""Scan the entire Google Workspace and build a size-capped "quality" sample manifest.

Largest-first selection is used as a quality proxy for binary Drive files (PDFs, Office
docs, images, etc.) and Gmail threads: each is sorted by size, then greedily kept until
its own byte cap is hit. Gmail messages are grouped by thread first — a thread is
included or excluded as a whole unit, never split, so exported mail reads correctly.

Google-native Docs/Sheets/Slides have no fixed byte size (nothing to rank by size), so
they're selected separately by a fixed count each, most-recently-modified first, on top
of (not counted against) the binary Drive byte cap.

No LLM classification pass is used; this only needs raw scan metadata (size, thread id,
modified time), which is far cheaper than the full pipeline (gdrive/pipeline_1tb.py,
gmail/pipeline.py) that also downloads/snippets/classifies every file.

Usage:

  python tools/build_quality_sample.py \\
      --service-account .secrets/service_account.json --admin-email admin@yourdomain.com

  python tools/build_quality_sample.py --drive-cap-gb 75 --gmail-cap-gb 12.5 \\
      --gsheets-limit 350 --gdocs-limit 300 --gslides-limit 150 \\
      --service-account .secrets/service_account.json --admin-email admin@yourdomain.com \\
      --out out/quality_sample_manifest.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import env_loader  # noqa: F401

from gdrive.credentials import (
    SCOPES_ADMIN_USERS,
    SCOPES_GMAIL,
    SCOPES_READONLY,
    build_admin_service,
    build_drive_service,
    build_gmail_service,
    default_service_account_path,
    get_service_account_credentials,
)
from gdrive.scan import list_shared_drives, list_workspace_users, walk_all_user_my_drives
from gmail.scan import fetch_message_meta, scan_mailbox

GB = 1024 ** 3

MIME_GDOC = "application/vnd.google-apps.document"
MIME_GSHEET = "application/vnd.google-apps.spreadsheet"
MIME_GSLIDES = "application/vnd.google-apps.presentation"


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def _int_size(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def greedy_fill(candidates: list[dict[str, Any]], cap_bytes: int) -> tuple[list[dict[str, Any]], int]:
    """Largest-first selection: skip items that don't fit, keep filling with smaller ones."""
    ordered = sorted(candidates, key=lambda c: c["size_bytes"], reverse=True)
    selected: list[dict[str, Any]] = []
    total = 0
    for c in ordered:
        if total + c["size_bytes"] <= cap_bytes:
            selected.append(c)
            total += c["size_bytes"]
    return selected, total


def select_top_by_recency(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Most-recently-modified first — used for native files, which have no size to rank by."""
    ordered = sorted(rows, key=lambda r: r.get("modified_time") or "", reverse=True)
    return ordered[:limit]


def collect_drive_candidates(
    *, sa_file: Path, admin_email: str, scan_cache: str, progress_log=_log,
) -> dict[str, list[dict[str, Any]]]:
    admin_sdk_creds = get_service_account_credentials(sa_file, admin_email, SCOPES_ADMIN_USERS)
    admin_svc = build_admin_service(admin_sdk_creds)
    users = list_workspace_users(admin_svc)
    progress_log(f"[drive] {len(users)} workspace user(s) found")

    admin_drive_creds = get_service_account_credentials(sa_file, admin_email, SCOPES_READONLY)
    admin_drive_svc = build_drive_service(admin_drive_creds)
    shared = list_shared_drives(admin_drive_svc, use_domain_admin_access=True)
    progress_log(f"[drive] {len(shared)} Shared Drive(s)")

    def _user_drive_service(email: str):
        u_creds = get_service_account_credentials(sa_file, email, SCOPES_READONLY)
        return build_drive_service(u_creds)

    rows = walk_all_user_my_drives(
        _user_drive_service,
        users,
        shared_drives=shared,
        shared_drive_service=admin_drive_svc,
        progress_log=progress_log,
        scan_cache_path=scan_cache,
    )
    progress_log(f"[drive] scan complete: {len(rows)} row(s)")

    native_by_mime = {MIME_GSHEET: "gsheets", MIME_GDOC: "gdocs", MIME_GSLIDES: "gslides"}
    buckets: dict[str, list[dict[str, Any]]] = {"binary": [], "gsheets": [], "gdocs": [], "gslides": []}
    for r in rows:
        if r.get("is_folder") or r.get("is_shortcut"):
            continue
        entry = {
            "file_id": r.get("drive_file_id") or "",
            "name": r.get("name") or "",
            "path": r.get("path") or "",
            "owner_email": r.get("owner_email") or "",
            "modified_time": r.get("modified_time") or "",
        }
        native_bucket = native_by_mime.get(r.get("mime_type") or "")
        if native_bucket:
            entry["size_bytes"] = 0  # native files have no fixed byte size; selected by recency instead
            buckets[native_bucket].append(entry)
            continue
        size_bytes = _int_size(r.get("size_bytes"))
        if size_bytes <= 0:
            continue  # other native types (Forms, Drawings, ...) or unreadable size
        entry["size_bytes"] = size_bytes
        buckets["binary"].append(entry)
    return buckets


def add_to_thread(by_thread: dict[tuple[str, str], dict[str, Any]], email: str, meta: dict[str, Any]) -> None:
    """Merge one message's metadata into its (email, thread_id) group, summing size."""
    key = (email, meta["thread_id"])
    entry = by_thread.get(key)
    if entry is None:
        entry = {
            "user_email": email, "thread_id": meta["thread_id"],
            "subject": meta["subject"], "size_bytes": 0, "message_ids": [],
        }
        by_thread[key] = entry
    entry["size_bytes"] += meta["size_bytes"]
    entry["message_ids"].append(meta["message_id"])


def collect_gmail_candidates(
    *, sa_file: Path, users: list[str], gmail_query: str, scan_cache_dir: Path, progress_log=_log,
) -> list[dict[str, Any]]:
    by_thread: dict[tuple[str, str], dict[str, Any]] = {}
    for email in users:
        creds = get_service_account_credentials(sa_file, email, SCOPES_GMAIL)
        svc = build_gmail_service(creds)
        cache_path = scan_cache_dir / f"gmail_ids__{email.replace('@', '_at_')}.txt"
        ids = scan_mailbox(
            svc, email, query=gmail_query, progress_log=progress_log,
            scan_cache_path=str(cache_path),
        )
        for i, mid in enumerate(ids, start=1):
            meta = fetch_message_meta(svc, mid, user_id=email)
            add_to_thread(by_thread, email, meta)
            if progress_log and i % 500 == 0:
                progress_log(f"[gmail] {email}: {i}/{len(ids)} message(s) scanned")
    return list(by_thread.values())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--drive-cap-gb", type=float, default=75.0,
                    help="Byte cap for binary Drive files, in GB (default: 75)")
    p.add_argument("--gmail-cap-gb", type=float, default=12.5, help="Gmail selection cap in GB (default: 12.5)")
    p.add_argument("--gsheets-limit", type=int, default=350,
                    help="Max Google Sheets to include, most-recently-modified first (default: 350)")
    p.add_argument("--gdocs-limit", type=int, default=300,
                    help="Max Google Docs to include, most-recently-modified first (default: 300)")
    p.add_argument("--gslides-limit", type=int, default=150,
                    help="Max Google Slides to include, most-recently-modified first (default: 150)")
    p.add_argument("--out", default="out/quality_sample_manifest.json", help="Output manifest path")
    p.add_argument("--gmail-query", default="", help="Optional Gmail search query to filter messages")
    p.add_argument("--skip-drive", action="store_true", help="Skip Drive scanning/selection")
    p.add_argument("--skip-gmail", action="store_true", help="Skip Gmail scanning/selection")
    sa_default = str(default_service_account_path()) if default_service_account_path() else ""
    p.add_argument("--service-account", default=sa_default, metavar="FILE",
                    help="Service account JSON key (Domain-Wide Delegation)")
    p.add_argument("--admin-email", default="", metavar="EMAIL",
                    help="Admin email to impersonate (or GOOGLE_ADMIN_EMAIL env var)")
    args = p.parse_args(argv)

    sa_file = Path(args.service_account).expanduser().resolve() if args.service_account else None
    if not sa_file or not sa_file.is_file():
        print("ERROR: --service-account is required (full-workspace scan needs Domain-Wide Delegation)", flush=True)
        return 1
    admin_email = args.admin_email or os.environ.get("GOOGLE_ADMIN_EMAIL", "").strip()
    if not admin_email:
        print("ERROR: --admin-email (or GOOGLE_ADMIN_EMAIL env var) is required", flush=True)
        return 1

    out_path = (_ROOT / args.out).resolve()
    scan_cache_dir = out_path.parent
    scan_cache_dir.mkdir(parents=True, exist_ok=True)

    drive_cap_bytes = int(args.drive_cap_gb * GB)
    gmail_cap_bytes = int(args.gmail_cap_gb * GB)

    drive_selected: list[dict[str, Any]] = []
    drive_total = 0
    native_counts = {"gsheets": 0, "gdocs": 0, "gslides": 0}
    if not args.skip_drive:
        drive_scan_cache = str(scan_cache_dir / (out_path.stem + ".drive_scan_cache.jsonl"))
        buckets = collect_drive_candidates(
            sa_file=sa_file, admin_email=admin_email, scan_cache=drive_scan_cache, progress_log=_log,
        )
        binary_selected, drive_total = greedy_fill(buckets["binary"], drive_cap_bytes)
        _log(f"[drive] binary files: selected {len(binary_selected)}/{len(buckets['binary'])}, "
             f"{drive_total / GB:.2f}GB / {args.drive_cap_gb:.2f}GB cap")

        native_limits = {"gsheets": args.gsheets_limit, "gdocs": args.gdocs_limit, "gslides": args.gslides_limit}
        native_selected: list[dict[str, Any]] = []
        for kind, limit in native_limits.items():
            picked = select_top_by_recency(buckets[kind], limit)
            native_counts[kind] = len(picked)
            native_selected.extend(picked)
            _log(f"[drive] {kind}: selected {len(picked)}/{len(buckets[kind])} (limit {limit}, most-recent first)")

        drive_selected = binary_selected + native_selected

    gmail_selected: list[dict[str, Any]] = []
    gmail_total = 0
    if not args.skip_gmail:
        admin_sdk_creds = get_service_account_credentials(sa_file, admin_email, SCOPES_ADMIN_USERS)
        admin_svc = build_admin_service(admin_sdk_creds)
        gmail_users = list_workspace_users(admin_svc)
        gmail_candidates = collect_gmail_candidates(
            sa_file=sa_file, users=gmail_users, gmail_query=args.gmail_query,
            scan_cache_dir=scan_cache_dir, progress_log=_log,
        )
        gmail_selected, gmail_total = greedy_fill(gmail_candidates, gmail_cap_bytes)
        _log(f"[gmail] selected {len(gmail_selected)}/{len(gmail_candidates)} thread(s), "
             f"{gmail_total / GB:.2f}GB / {args.gmail_cap_gb:.2f}GB cap")

    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "drive_cap_bytes": drive_cap_bytes,
        "drive_total_bytes": drive_total,
        "drive_native_selected": native_counts,
        "gmail_cap_bytes": gmail_cap_bytes,
        "gmail_total_bytes": gmail_total,
        "files": [
            {"file_id": c["file_id"], "name": c["name"], "path": c["path"], "size_bytes": c["size_bytes"],
             "owner_email": c["owner_email"]}
            for c in drive_selected
        ],
        "gmail_threads": gmail_selected,
    }
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _log(f"manifest written: {out_path} (files={len(drive_selected)} gmail_threads={len(gmail_selected)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
