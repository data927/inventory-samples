"""Build a size-capped "quality" sample manifest from Google Drive + Gmail.

Two scan modes, chosen automatically from whatever auth is available:

  Workspace mode  — pass --service-account + --admin-email (Domain-Wide Delegation):
                    scans every user's My Drive + Shared Drives, every user's Gmail.
  My Drive mode   — no service account given: scans just the signed-in account's own
                    Drive + Gmail, reusing the same OAuth tokens the rest of this repo's
                    tools already use (.secrets/google_drive_token.json, .secrets/gmail_token.json),
                    prompting a browser login the first time either is missing.

Largest-first selection is used as a quality proxy for binary Drive files (PDFs, Office
docs, images, etc.) and Gmail threads: each is sorted by size, then greedily kept until
its own byte cap is hit. Gmail messages are grouped by thread first — a thread is
included or excluded as a whole unit, never split, so exported mail reads correctly.

Google-native Docs/Sheets/Slides have no fixed byte size (nothing to rank by size), so
they're selected separately by a fixed count each, most-recently-modified first, on top
of (not counted against) the binary Drive byte cap. In workspace mode, both the binary
cap and the native quotas are spread **per account** rather than pooled globally: each
account is guaranteed its own baseline (native files) or a cap slice proportional to its
own data volume (binary files), so one large account can't crowd out the rest.

No LLM classification pass is used; this only needs raw scan metadata (size, thread id,
modified time), which is far cheaper than the full pipeline (gdrive/pipeline_1tb.py,
gmail/pipeline.py) that also downloads/snippets/classifies every file.

Usage:

  # Workspace-wide (needs Domain-Wide Delegation)
  python tools/build_quality_sample.py \\
      --service-account .secrets/service_account.json --admin-email admin@yourdomain.com

  # Just your own Drive + Gmail (no service account)
  python tools/build_quality_sample.py

  # One account — Drive as-is until 5GB + Gmail (first 100 msgs → 20 threads, 3GB cap)
  python tools/build_quality_sample.py --full-account
  python tools/build_quality_sample.py \\
      --service-account .secrets/service_account.json --admin-email admin@yourdomain.com \\
      --users alice@yourdomain.com --full-account

  # As-is until 40GB (walk order, no quality scan) — transfer starts immediately
  python tools/build_quality_sample.py --as-is
  python tools/build_quality_sample.py --as-is --cap-gb 40
  python tools/build_quality_sample.py \\
      --service-account .secrets/service_account.json --admin-email admin@yourdomain.com \\
      --users alice@yourdomain.com --as-is --cap-gb 40

  python tools/build_quality_sample.py --drive-cap-gb 75 --gmail-cap-gb 12.5 \\
      --gsheets-limit 350 --gdocs-limit 300 --gslides-limit 150 \\
      --out out/quality_sample_manifest.json
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import env_loader  # noqa: F401

from googleapiclient.errors import HttpError

from gdrive.credentials import (
    SCOPES_ADMIN_USERS,
    SCOPES_DRIVE,
    SCOPES_GMAIL,
    SCOPES_READONLY,
    build_admin_service,
    build_drive_service,
    build_gmail_service,
    default_client_secrets_path,
    default_service_account_path,
    default_token_path,
    get_credentials,
    get_service_account_credentials,
)
from gdrive.fetch import _RETRYABLE_NETWORK_ERRORS, call_with_retry
from gdrive.scan import (
    list_shared_drives,
    list_workspace_users,
    normalize_folder_id,
    walk_all_user_my_drives,
    walk_drive_folder,
    walk_entire_workspace,
    walk_my_drive_in_rounds,
)
from gmail.fetch import fetch_thread_raw
from gmail.scan import fetch_message_meta, scan_mailbox
from tools.export_ai_labs_gmail_threads import _get_insert_credentials, _insert_message
from tools.export_ai_labs_samples import (
    _append_done,
    _create_root_folder,
    _drive_copy,
    _ensure_child_folder,
    _get_rw_credentials,
    _load_dest_meta,
    _load_done,
    _save_dest_meta,
)

_GMAIL_TOKEN = _ROOT / ".secrets" / "gmail_token.json"

GB = 1024 ** 3

MIME_GDOC = "application/vnd.google-apps.document"
MIME_GSHEET = "application/vnd.google-apps.spreadsheet"
MIME_GSLIDES = "application/vnd.google-apps.presentation"


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def _parse_users(raw: list[str] | None) -> list[str]:
    """Flatten space- and/or comma-separated emails, preserve order, drop dupes."""
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        for part in item.split(","):
            email = part.strip()
            if not email:
                continue
            key = email.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(email)
    return out


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


def _group_by_account(
    rows: list[dict[str, Any]], key: str = "owner_email",
) -> dict[str, list[dict[str, Any]]]:
    by_account: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_account.setdefault(r.get(key) or "", []).append(r)
    return by_account


def select_native_by_account(
    rows: list[dict[str, Any]], *, per_account_limit: int, overall_cap: int,
) -> list[dict[str, Any]]:
    """Guarantee up to ``per_account_limit`` most-recent files per account, so no single
    account crowds out the rest; if that guaranteed total is under ``overall_cap``, top
    up with the next most-recent files from anywhere. The guarantee is never trimmed
    back down, so with enough accounts the final total can exceed ``overall_cap``."""
    floor_selected: list[dict[str, Any]] = []
    for acct_rows in _group_by_account(rows).values():
        floor_selected.extend(select_top_by_recency(acct_rows, per_account_limit))

    if len(floor_selected) >= overall_cap:
        return floor_selected

    floor_ids = {r["file_id"] for r in floor_selected}
    remaining_pool = [r for r in rows if r["file_id"] not in floor_ids]
    topup = select_top_by_recency(remaining_pool, overall_cap - len(floor_selected))
    return floor_selected + topup


def _round_robin_fill(
    queues: dict[str, list[dict[str, Any]]],
    cursor: dict[str, int],
    priority_order: list[str],
    cap_bytes: int,
    total_selected: int,
) -> tuple[list[dict[str, Any]], int]:
    """Each round, every account (in ``priority_order``) contributes its next
    not-yet-selected item that still fits, skipping any that don't (an item that doesn't
    fit now never will later, since remaining cap only shrinks). Repeats until the cap is
    full or nobody has anything left that fits. Mutates ``cursor`` in place."""
    selected: list[dict[str, Any]] = []
    progressed = True
    while total_selected < cap_bytes and progressed:
        progressed = False
        for email in priority_order:
            queue = queues[email]
            i = cursor[email]
            while i < len(queue) and total_selected + queue[i]["size_bytes"] > cap_bytes:
                i += 1
            cursor[email] = i
            if i < len(queue):
                selected.append(queue[i])
                total_selected += queue[i]["size_bytes"]
                cursor[email] = i + 1
                progressed = True
    return selected, total_selected


def allocate_binary_by_account(
    rows: list[dict[str, Any]], cap_bytes: int,
) -> tuple[list[dict[str, Any]], int]:
    """Two stages, both accounting for every account (ordered by its own total data,
    biggest first):

    1. **Guarantee** — reserve an equal minimal slice per account (``cap_bytes`` /
       number of accounts) and fill it with that account's *smallest* file that fits.
       Cheapest possible way to guarantee every account with at least one fitting file
       gets included, leaving the rest of the cap free for stage 2.
    2. **Priority round-robin** — with whatever cap remains, each account contributes
       its next-largest not-yet-selected file, biggest-data account first each round,
       repeating until the cap is full or nobody has anything left that fits. Gives
       accounts with more data a bigger absolute share without letting any single one
       consume the whole cap in one turn.

    Together: no account is ever shut out purely because its file sizes don't line up
    with a fixed slice, while accounts with more data still end up with more selected in
    the typical case. (In pathological cases — very few accounts, a cap barely bigger
    than one account's smallest file — the inclusion guarantee can occasionally cost a
    big account some of its proportional edge; that trade-off is intentional.)
    """
    by_account = _group_by_account(rows)
    if not by_account:
        return [], 0

    priority_order = sorted(by_account, key=lambda e: -sum(r["size_bytes"] for r in by_account[e]))
    queues = {e: sorted(acct_rows, key=lambda r: -r["size_bytes"]) for e, acct_rows in by_account.items()}
    cursor = {e: 0 for e in by_account}

    equal_share = cap_bytes // len(by_account)
    selected: list[dict[str, Any]] = []
    total_selected = 0
    for email in priority_order:
        queue = queues[email]
        if not queue:
            continue
        smallest = queue[-1]  # already sorted largest-first, so the smallest is last
        if smallest["size_bytes"] <= equal_share and total_selected + smallest["size_bytes"] <= cap_bytes:
            selected.append(smallest)
            total_selected += smallest["size_bytes"]
            queues[email] = queue[:-1]  # drop it; round-robin below starts fresh at index 0

    topup, total_selected = _round_robin_fill(queues, cursor, priority_order, cap_bytes, total_selected)
    selected.extend(topup)
    return selected, total_selected


def allocate_equally_by_account(
    candidates: list[dict[str, Any]], cap_bytes: int,
) -> tuple[list[dict[str, Any]], int]:
    """Split ``cap_bytes`` equally across accounts — every account gets the same
    share, not weighted by its own data volume (unlike ``allocate_binary_by_account``)
    — then greedy-fill each account's own share largest-first. A leftover-reclaim pass
    mops up any cap a share couldn't use, same fix as ``allocate_binary_by_account``,
    so the cap doesn't go to waste when one account's items don't fit its share evenly.
    """
    by_account = _group_by_account(candidates, key="user_email")
    if not by_account:
        return [], 0

    equal_share = cap_bytes // len(by_account)
    selected: list[dict[str, Any]] = []
    total_selected = 0
    for acct_rows in by_account.values():
        acct_selected, acct_total = greedy_fill(acct_rows, equal_share)
        selected.extend(acct_selected)
        total_selected += acct_total

    leftover_cap = cap_bytes - total_selected
    if leftover_cap > 0:
        selected_keys = {(r["user_email"], r["thread_id"]) for r in selected}
        remaining = [r for r in candidates if (r["user_email"], r["thread_id"]) not in selected_keys]
        topup, topup_total = greedy_fill(remaining, leftover_cap)
        selected.extend(topup)
        total_selected += topup_total

    return selected, total_selected


def _bucket_drive_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
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


_EARLY_STOP_SAFETY_MULTIPLIER = 3  # e.g. gsheets-per-account=30 -> stop once 90 found


def _account_has_enough(
    buckets: dict[str, list[dict[str, Any]]],
    *,
    gsheets_per_account: int,
    gdocs_per_account: int,
    gslides_per_account: int,
    drive_cap_bytes: int,
) -> bool:
    """True once an account's accumulated candidates comfortably exceed what the
    existing per-account targets could ever need — used to stop scanning that account
    early rather than walking its entire Drive exhaustively."""
    native_ok = (
        len(buckets["gsheets"]) >= gsheets_per_account * _EARLY_STOP_SAFETY_MULTIPLIER
        and len(buckets["gdocs"]) >= gdocs_per_account * _EARLY_STOP_SAFETY_MULTIPLIER
        and len(buckets["gslides"]) >= gslides_per_account * _EARLY_STOP_SAFETY_MULTIPLIER
    )
    binary_ok = sum(r["size_bytes"] for r in buckets["binary"]) >= drive_cap_bytes
    return native_ok and binary_ok


def _scan_account_in_rounds(
    service,
    path_prefix: str,
    *,
    folders_per_round: int,
    modified_before: str | None,
    gsheets_per_account: int,
    gdocs_per_account: int,
    gslides_per_account: int,
    drive_cap_bytes: int,
    progress_log=_log,
) -> dict[str, list[dict[str, Any]]]:
    """Scan one account's My Drive in rounds of ``folders_per_round`` folders, stopping
    early once ``_account_has_enough`` is satisfied instead of walking exhaustively."""
    buckets: dict[str, list[dict[str, Any]]] = {"binary": [], "gsheets": [], "gdocs": [], "gslides": []}
    frontier = None
    round_num = 0
    while True:
        round_num += 1
        batch_rows, frontier = walk_my_drive_in_rounds(
            service, path_prefix=path_prefix, frontier=frontier,
            folder_budget=folders_per_round, modified_before=modified_before,
            progress_log=progress_log,
        )
        batch_buckets = _bucket_drive_rows(batch_rows)
        for kind in buckets:
            buckets[kind].extend(batch_buckets[kind])
        if not frontier:
            break
        if _account_has_enough(
            buckets, gsheets_per_account=gsheets_per_account, gdocs_per_account=gdocs_per_account,
            gslides_per_account=gslides_per_account, drive_cap_bytes=drive_cap_bytes,
        ):
            progress_log(
                f"[drive] enough found after {round_num} round(s) of {folders_per_round} folders — "
                f"stopping early, {len(frontier)} folder(s) left unscanned"
            )
            break
    return buckets


def _merge_buckets(into: dict[str, list[dict[str, Any]]], other: dict[str, list[dict[str, Any]]]) -> None:
    for kind in into:
        into[kind].extend(other[kind])


def _walk_shared_drives_exhaustively(
    shared: list[dict[str, Any]], service, *, modified_before: str | None, progress_log,
) -> dict[str, list[dict[str, Any]]]:
    """Shared Drives are always walked exhaustively — round-based early-stop only
    applies per individually-owned account (confirmed scope), not shared/team drives."""
    buckets: dict[str, list[dict[str, Any]]] = {"binary": [], "gsheets": [], "gdocs": [], "gslides": []}
    for drv in shared:
        did = drv["id"]
        dname = drv.get("name") or did
        progress_log(f"[drive] Shared Drive → {dname!r}")
        rows = walk_drive_folder(
            service, did, path_prefix=dname, modified_before=modified_before, progress_log=progress_log,
        )
        _merge_buckets(buckets, _bucket_drive_rows(rows))
    return buckets


def collect_drive_candidates_workspace(
    *, sa_file: Path, admin_email: str, scan_cache: str, users: list[str] | None = None,
    modified_before: str | None = None, folders_per_round: int = 0,
    gsheets_per_account: int = 30, gdocs_per_account: int = 40, gslides_per_account: int = 20,
    drive_cap_bytes: int = 0, progress_log=_log,
) -> dict[str, list[dict[str, Any]]]:
    """Every user's My Drive + every Shared Drive (Domain-Wide Delegation).

    ``users``, if given, restricts the scan to those emails and skips the Admin SDK
    enumeration call entirely (so ``--users`` mode doesn't even need Admin SDK access).

    ``modified_before`` (RFC3339), if given, excludes files modified on or after it —
    folders are still always traversed regardless of their own modified time.

    ``folders_per_round`` > 0 switches each user's My Drive to round-based scanning
    (see ``_scan_account_in_rounds``) instead of an exhaustive walk. Shared Drives are
    always walked exhaustively either way.
    """
    if users is None:
        admin_sdk_creds = get_service_account_credentials(sa_file, admin_email, SCOPES_ADMIN_USERS)
        admin_svc = build_admin_service(admin_sdk_creds)
        users = list_workspace_users(admin_svc)
        progress_log(f"[drive] {len(users)} workspace user(s) found")
    else:
        progress_log(f"[drive] scanning {len(users)} selected user(s): {', '.join(users)}")

    admin_drive_creds = get_service_account_credentials(sa_file, admin_email, SCOPES_READONLY)
    admin_drive_svc = build_drive_service(admin_drive_creds)
    shared = list_shared_drives(admin_drive_svc, use_domain_admin_access=True)
    progress_log(f"[drive] {len(shared)} Shared Drive(s)")

    def _user_drive_service(email: str):
        u_creds = get_service_account_credentials(sa_file, email, SCOPES_READONLY)
        return build_drive_service(u_creds)

    if folders_per_round > 0:
        buckets: dict[str, list[dict[str, Any]]] = {"binary": [], "gsheets": [], "gdocs": [], "gslides": []}
        for email in users:
            progress_log(f"[drive] My Drive → {email} (round-based, {folders_per_round} folders/round)")
            svc = _user_drive_service(email)
            acct_buckets = _scan_account_in_rounds(
                svc, f"My Drive ({email})", folders_per_round=folders_per_round,
                modified_before=modified_before, gsheets_per_account=gsheets_per_account,
                gdocs_per_account=gdocs_per_account, gslides_per_account=gslides_per_account,
                drive_cap_bytes=drive_cap_bytes, progress_log=progress_log,
            )
            _merge_buckets(buckets, acct_buckets)
        _merge_buckets(
            buckets,
            _walk_shared_drives_exhaustively(
                shared, admin_drive_svc, modified_before=modified_before, progress_log=progress_log,
            ),
        )
        progress_log("[drive] round-based scan complete")
        return buckets

    rows = walk_all_user_my_drives(
        _user_drive_service,
        users,
        shared_drives=shared,
        shared_drive_service=admin_drive_svc,
        modified_before=modified_before,
        progress_log=progress_log,
        scan_cache_path=scan_cache,
    )
    progress_log(f"[drive] scan complete: {len(rows)} row(s)")
    return _bucket_drive_rows(rows)


def collect_drive_candidates_single(
    *, service, scan_cache: str, modified_before: str | None = None, folders_per_round: int = 0,
    gsheets_per_account: int = 30, gdocs_per_account: int = 40, gslides_per_account: int = 20,
    drive_cap_bytes: int = 0, progress_log=_log,
) -> dict[str, list[dict[str, Any]]]:
    """Just the signed-in account's own My Drive + Shared Drives it can see."""
    shared = list_shared_drives(service)
    progress_log(f"[drive] {len(shared)} Shared Drive(s) visible to this account")

    if folders_per_round > 0:
        buckets = _scan_account_in_rounds(
            service, "My Drive", folders_per_round=folders_per_round, modified_before=modified_before,
            gsheets_per_account=gsheets_per_account, gdocs_per_account=gdocs_per_account,
            gslides_per_account=gslides_per_account, drive_cap_bytes=drive_cap_bytes,
            progress_log=progress_log,
        )
        _merge_buckets(
            buckets,
            _walk_shared_drives_exhaustively(shared, service, modified_before=modified_before, progress_log=progress_log),
        )
        progress_log("[drive] round-based scan complete")
        return buckets

    rows = walk_entire_workspace(
        service,
        include_my_drive=True,
        include_shared_drives=True,
        modified_before=modified_before,
        progress_log=progress_log,
        scan_cache_path=scan_cache,
    )
    progress_log(f"[drive] scan complete: {len(rows)} row(s)")
    return _bucket_drive_rows(rows)


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


def _load_message_meta_cache(cache_path: Path) -> dict[str, dict[str, Any]]:
    """Resume support for the per-message metadata fetch loop below — a long scan can
    run for hours and hit a transient outage that outlasts even the retry budget in
    fetch_message_meta; without this, a crash at message 14,000/133,853 would throw
    away all of that work on the next run."""
    cached: dict[str, dict[str, Any]] = {}
    if not cache_path.is_file():
        return cached
    with cache_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = row.get("message_id")
            if mid:
                cached[mid] = row
    return cached


def _append_message_meta_cache(cache_path: Path, meta: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(meta, ensure_ascii=False) + "\n")


def collect_gmail_candidates(
    *,
    get_user_service: Callable[[str], Any],
    users: list[str],
    gmail_query: str,
    scan_cache_dir: Path,
    cache_suffix: str = "",
    progress_log=_log,
) -> list[dict[str, Any]]:
    by_thread: dict[tuple[str, str], dict[str, Any]] = {}
    for email in users:
        svc = get_user_service(email)
        safe_email = email.replace("@", "_at_")
        cache_path = scan_cache_dir / f"gmail_ids__{safe_email}{cache_suffix}.txt"
        ids = scan_mailbox(
            svc, email, query=gmail_query, progress_log=progress_log,
            scan_cache_path=str(cache_path),
        )

        meta_cache_path = scan_cache_dir / f"gmail_meta__{safe_email}{cache_suffix}.jsonl"
        meta_cache = _load_message_meta_cache(meta_cache_path)
        if meta_cache:
            progress_log(f"[gmail] {email}: resuming from {len(meta_cache)} cached message(s)")

        for i, mid in enumerate(ids, start=1):
            meta = meta_cache.get(mid)
            if meta is None:
                meta = fetch_message_meta(svc, mid, user_id=email)
                _append_message_meta_cache(meta_cache_path, meta)
            add_to_thread(by_thread, email, meta)
            if progress_log and i % 500 == 0:
                progress_log(f"[gmail] {email}: {i}/{len(ids)} message(s) scanned")
    return list(by_thread.values())


# --full-account Gmail sample: first N messages → up to M threads, under a byte cap.
_FULL_ACCOUNT_GMAIL_MAX_MESSAGES = 100
_FULL_ACCOUNT_GMAIL_MAX_THREADS = 20
_FULL_ACCOUNT_GMAIL_CAP_GB = 3.0


def select_threads_first_n(
    threads_in_order: list[dict[str, Any]],
    *,
    max_threads: int,
    cap_bytes: int,
) -> list[dict[str, Any]]:
    """Take threads in discovery order until ``max_threads`` or the byte cap.

    Oversized threads that don't fit the remaining budget are skipped (same as
    Drive as-is), so a single huge thread doesn't block filling the sample.
    """
    selected: list[dict[str, Any]] = []
    used = 0
    for t in threads_in_order:
        if len(selected) >= max_threads:
            break
        size = _int_size(t.get("size_bytes"))
        if size > 0 and used + size > cap_bytes:
            continue
        selected.append(t)
        used += size
    return selected


def collect_gmail_first_n_sample(
    *,
    get_user_service: Callable[[str], Any],
    email: str,
    gmail_query: str,
    scan_cache_dir: Path,
    cache_suffix: str = "",
    max_messages: int = _FULL_ACCOUNT_GMAIL_MAX_MESSAGES,
    max_threads: int = _FULL_ACCOUNT_GMAIL_MAX_THREADS,
    cap_bytes: int,
    progress_log=_log,
) -> list[dict[str, Any]]:
    """Scan the first ``max_messages`` mailbox messages, group into threads in
    discovery order, then keep up to ``max_threads`` under ``cap_bytes``.

    Transfer still pulls each selected thread in full (``fetch_thread_raw``);
    sizes from the partial message window are only used for the sample budget.
    """
    svc = get_user_service(email)
    safe_email = email.replace("@", "_at_")
    # Dedicated cache name so a prior full-mailbox ID list isn't reused/truncated.
    cache_path = scan_cache_dir / f"gmail_ids__{safe_email}__fa{max_messages}{cache_suffix}.txt"
    ids = scan_mailbox(
        svc, email, query=gmail_query, max_messages=max_messages,
        progress_log=progress_log, scan_cache_path=str(cache_path),
    )
    meta_cache_path = scan_cache_dir / f"gmail_meta__{safe_email}__fa{max_messages}{cache_suffix}.jsonl"
    meta_cache = _load_message_meta_cache(meta_cache_path)
    if meta_cache:
        progress_log(f"[gmail] {email}: resuming from {len(meta_cache)} cached message(s)")

    by_thread: dict[tuple[str, str], dict[str, Any]] = {}
    discovery_order: list[tuple[str, str]] = []
    for i, mid in enumerate(ids, start=1):
        meta = meta_cache.get(mid)
        if meta is None:
            meta = fetch_message_meta(svc, mid, user_id=email)
            _append_message_meta_cache(meta_cache_path, meta)
        key = (email, meta["thread_id"])
        if key not in by_thread:
            discovery_order.append(key)
        add_to_thread(by_thread, email, meta)
        if progress_log and i % 50 == 0:
            progress_log(f"[gmail] {email}: {i}/{len(ids)} message(s) scanned")

    ordered = [by_thread[k] for k in discovery_order]
    selected = select_threads_first_n(
        ordered, max_threads=max_threads, cap_bytes=cap_bytes,
    )
    progress_log(
        f"[gmail] {email}: first {len(ids)} message(s) → {len(ordered)} thread(s) seen, "
        f"selected {len(selected)}/{max_threads} under {cap_bytes / GB:.2f}GB"
    )
    return selected


def _stream_gmail_account_in_rounds(
    service,
    email: str,
    *,
    gmail_cap_bytes: int,
    messages_per_round: int,
    gmail_query: str,
    scan_cache_dir: Path,
    before_suffix: str,
    work_queue: "queue.Queue | None",
    progress_log=_log,
) -> list[dict[str, Any]]:
    """Scan one mailbox's messages in batches of ``messages_per_round``, selecting
    threads round by round against a shared running budget instead of waiting for the
    whole mailbox to be enumerated first — used by both the workspace and single-account
    streaming pipelines so each account's own Gmail scan overlaps with its own transfer,
    not just with other accounts' scans.

    Approximate by necessity, unlike Drive's round-based selection: Gmail message IDs
    aren't returned thread-grouped, so a thread's accounted size only reflects the
    messages seen by the round it happens to get selected in — a thread's remaining
    messages can still show up in a later round. The transfer itself always grabs the
    *complete* thread (``fetch_thread_raw``), so nothing is ever missed on disk, but the
    bytes actually moved for that thread can exceed what was charged against the cap,
    bounded by that one thread's own total size. Confirmed acceptable trade-off for the
    concurrency it buys — see ``--folders-per-round`` docs.

    Pushes ``(email, round_selected)`` onto ``work_queue`` after each round for a
    caller-owned background transfer worker to drain. Pass ``work_queue=None`` (e.g.
    ``--scan-only``) to just select without queuing any transfer.
    """
    safe_email = email.replace("@", "_at_")
    cache_path = scan_cache_dir / f"gmail_ids__{safe_email}{before_suffix}.txt"
    ids = scan_mailbox(service, email, query=gmail_query, progress_log=progress_log,
                        scan_cache_path=str(cache_path))
    meta_cache_path = scan_cache_dir / f"gmail_meta__{safe_email}{before_suffix}.jsonl"
    meta_cache = _load_message_meta_cache(meta_cache_path)

    by_thread: dict[tuple[str, str], dict[str, Any]] = {}
    already_selected: set[str] = set()
    all_selected: list[dict[str, Any]] = []
    remaining = gmail_cap_bytes
    batch_size = max(messages_per_round, 1)
    round_num = 0

    for batch_start in range(0, len(ids), batch_size):
        round_num += 1
        if remaining <= 0:
            progress_log(f"[stream] {email}: gmail cap reached — stopping scan after round {round_num - 1}")
            break

        batch_ids = ids[batch_start:batch_start + batch_size]
        for mid in batch_ids:
            meta = meta_cache.get(mid)
            if meta is None:
                meta = fetch_message_meta(service, mid, user_id=email)
                _append_message_meta_cache(meta_cache_path, meta)
            add_to_thread(by_thread, email, meta)

        candidates = [t for t in by_thread.values() if t["thread_id"] not in already_selected]
        round_selected, round_total = greedy_fill(candidates, remaining)
        for t in round_selected:
            already_selected.add(t["thread_id"])
        all_selected.extend(round_selected)
        remaining -= round_total
        progress_log(f"[stream] {email}: round {round_num} selected {len(round_selected)} thread(s) "
                     f"({round_total / GB:.2f}GB), {remaining / GB:.2f}GB left of the cap, "
                     f"{batch_start + len(batch_ids)}/{len(ids)} message(s) scanned")

        if work_queue is not None and round_selected:
            work_queue.put((email, round_selected))
            progress_log(f"[stream] {email}: round {round_num} queued for transfer — "
                         f"continuing to scan the next round")

    return all_selected


def my_drive_service():
    """OAuth Drive service for the signed-in account, reusing the same token this repo's
    other tools already use (prompts a browser login the first time it's missing)."""
    creds = get_credentials(
        client_secrets=default_client_secrets_path(),
        token_path=default_token_path(),
        full_read_scope=True,
        login_only=False,
    )
    return build_drive_service(creds)


def my_gmail_service():
    """OAuth Gmail service for the signed-in account, reusing run_gmail.py's token file."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = Path(os.environ.get("GMAIL_TOKEN_PATH") or _GMAIL_TOKEN).expanduser().resolve()
    scopes = list(SCOPES_GMAIL)

    creds = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if not (creds and creds.valid):
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_secrets = default_client_secrets_path()
            if not client_secrets.is_file():
                raise FileNotFoundError(
                    f"OAuth client secrets not found: {client_secrets}\nRun: python setup.py"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), scopes)
            creds = flow.run_local_server(port=0, prompt="consent", open_browser=True)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    service = build_gmail_service(creds)
    email = service.users().getProfile(userId="me").execute().get("emailAddress") or "me"
    return service, email


def transfer_selection_by_account(
    drive_selected: list[dict[str, Any]],
    gmail_selected: list[dict[str, Any]],
    *,
    folder_name: str,
    dest_folder_id: str,
    checkpoint_dir: Path,
    get_source_gmail_service: Callable[[str], Any],
    progress_log=_log,
) -> None:
    """Copy the finalized selection into the destination sample folder — **all** Drive
    files first (largest account first), then **all** Gmail threads (largest account
    first) — so Drive samples are fully ready before Gmail transfer even starts, rather
    than interleaving the two per account. Runs right after scan+select finishes (or
    after resuming from an existing manifest), so there's no separate manual export
    step. Reuses the same checkpointed copy/insert logic as the standalone
    export_ai_labs_samples.py / export_ai_labs_gmail_threads.py scripts, so an
    interrupted transfer resumes exactly like they do — this doesn't reimplement that
    logic.
    """
    drive_by_account = _group_by_account(drive_selected)
    gmail_by_account = _group_by_account(gmail_selected, key="user_email")
    if not drive_by_account and not gmail_by_account:
        progress_log("[transfer] nothing selected — skipping")
        return

    def _by_size_desc(by_account: dict[str, list[dict[str, Any]]]) -> list[str]:
        return sorted(by_account, key=lambda e: -sum(r["size_bytes"] for r in by_account[e]))

    ckpt = checkpoint_dir / "ai_labs_samples.checkpoint.jsonl"
    gmail_ckpt = checkpoint_dir / "ai_labs_gmail_threads.checkpoint.jsonl"

    if drive_selected:
        drive_creds = _get_rw_credentials()
        drive_service = build_drive_service(drive_creds)

        dest_meta = _load_dest_meta(ckpt)
        if dest_folder_id.strip():
            dest_id = normalize_folder_id(dest_folder_id)
            progress_log(f"[transfer] using existing dest folder id={dest_id}")
        elif dest_meta and dest_meta.get("dest_folder_id"):
            dest_id = str(dest_meta["dest_folder_id"])
            folder_name = str(dest_meta.get("folder_name") or folder_name)
            progress_log(f"[transfer] resuming into existing folder {folder_name!r} → {dest_id}")
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if folder_name == "AI Labs Sample Set":
                folder_name = f"{folder_name} ({stamp})"
            dest_id = _create_root_folder(drive_service, folder_name, parent_id="root")
            progress_log(f"[transfer] created My Drive folder {folder_name!r} → {dest_id}")
        _save_dest_meta(ckpt, dest_id, folder_name)

        drive_done = _load_done(ckpt)
        folder_cache: dict[str, str] = {}
        drive_accounts = _by_size_desc(drive_by_account)
        progress_log(f"[transfer] Drive: {len(drive_accounts)} account(s), largest first")

        for email in drive_accounts:
            d_ok = d_skip = d_fail = 0
            for row in drive_by_account[email]:
                fid = row["file_id"]
                if fid in drive_done:
                    d_skip += 1
                    continue
                try:
                    parent = _ensure_child_folder(drive_service, dest_id, email, folder_cache)
                    new_id = _drive_copy(drive_service, fid, parent, row["name"])
                    _append_done(ckpt, {
                        "file_id": fid, "new_id": new_id, "bucket": email,
                        "name": row["name"], "path": row["path"], "mode": "copy", "status": "ok",
                    })
                    d_ok += 1
                except (HttpError, RuntimeError, OSError) as exc:
                    d_fail += 1
                    _append_done(ckpt, {
                        "file_id": fid, "bucket": email, "name": row["name"], "path": row["path"],
                        "status": "error", "error": str(exc)[:500],
                    })
                    progress_log(f"[transfer] FAIL drive {row['name'][:60]!r} — {exc}")
            progress_log(f"[transfer] drive {email}: done — ok={d_ok} skip={d_skip} fail={d_fail}")

        progress_log(f"[transfer] Drive done — folder: https://drive.google.com/drive/folders/{dest_id}")

    if gmail_selected:
        gmail_dest_service = build_gmail_service(_get_insert_credentials())
        gmail_done = _load_done(gmail_ckpt)
        gmail_accounts = _by_size_desc(gmail_by_account)
        progress_log(f"[transfer] Gmail: {len(gmail_accounts)} account(s), largest first")

        for email in gmail_accounts:
            g_ok = g_skip = g_fail = 0
            for row in gmail_by_account[email]:
                thread_key = f"{email}:{row['thread_id']}"
                if thread_key in gmail_done:
                    g_skip += 1
                    continue
                try:
                    src_service = get_source_gmail_service(email)
                    raw_messages = fetch_thread_raw(src_service, row["thread_id"], user_id=email)
                    inserted_ids = [_insert_message(gmail_dest_service, raw) for raw in raw_messages]
                    _append_done(gmail_ckpt, {
                        "file_id": thread_key, "thread_id": row["thread_id"], "user_email": email,
                        "inserted_ids": inserted_ids, "status": "ok",
                    })
                    g_ok += 1
                except (HttpError, RuntimeError, OSError) as exc:
                    g_fail += 1
                    _append_done(gmail_ckpt, {
                        "file_id": thread_key, "thread_id": row["thread_id"], "user_email": email,
                        "status": "error", "error": str(exc)[:500],
                    })
                    progress_log(f"[transfer] FAIL gmail {row.get('subject', '')[:60]!r} — {exc}")
            progress_log(f"[transfer] gmail {email}: done — ok={g_ok} skip={g_skip} fail={g_fail}")

        progress_log("[transfer] Gmail done")

    progress_log("[transfer] finished")


def run_streaming_workspace_pipeline(
    *,
    sa_file: Path,
    admin_email: str,
    users: list[str],
    drive_cap_bytes: int,
    gmail_cap_bytes: int,
    gsheets_per_account: int,
    gdocs_per_account: int,
    gslides_per_account: int,
    folders_per_round: int,
    messages_per_round: int,
    modified_before: str | None,
    gmail_query: str,
    scan_cache_dir: Path,
    before_suffix: str,
    folder_name: str,
    dest_folder_id: str,
    scan_only: bool,
    skip_drive: bool,
    skip_gmail: bool,
    progress_log=_log,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Fast path for --folders-per-round in Workspace mode: process one account fully
    (scan -> select) then **hand its transfer off to a background thread** and move
    straight on to scanning the next account — scanning account N+1 and transferring
    account N's files happen concurrently, not sequentially.

    Drive uses a **shared running budget**, not a per-account slice: accounts are
    scanned in order, each one's selection eats into what's left of the overall
    --drive-cap-gb, and the moment that budget hits zero, remaining accounts are
    skipped entirely for Drive — scanning moves straight on to Gmail rather than
    working through every account regardless of whether the cap is already full.
    Gmail, once it starts, still splits its own cap into a flat equal share per
    account (unchanged) — this running-budget behavior is Drive-specific. Within each
    account's own share, Gmail is scanned and selected in batches of
    ``messages_per_round`` messages via ``_stream_gmail_account_in_rounds`` — so a
    single huge mailbox's own scan overlaps with its own transfer too, not just with
    other accounts' scans (see that function's docstring for the accounting caveat this
    implies: an approximate, not exact, cap).

    Trade-off, deliberate: no cross-account priority weighting, no reclaim of one
    account's unused capacity for another, no cross-account top-up for native files —
    simpler and faster, at the cost of the cross-account fairness/optimality that the
    global two-phase flow (collect_drive_candidates_* +
    allocate_binary_by_account/select_native_by_account) provides. Use
    --folders-per-round 0 for that global, thorough flow instead.

    Concurrency note: exactly one dedicated background thread does all the transfer
    work per phase (never more), so there's no multi-writer race on the checkpoint
    file or the destination-folder cache — the only overlap is that one background
    thread versus the main thread's scanning, which is the whole point. Each thread
    only ever touches its own Drive/Gmail API service object (scanning uses per-user
    services on the main thread; the worker builds its own destination service once),
    so no HTTP client object is ever shared across threads either.

    Returns (drive_selected, gmail_selected, native_counts) for manifest bookkeeping —
    by the time this returns, every queued transfer has been drained and completed
    (or recorded as failed), not just kicked off.
    """
    n = max(1, len(users))
    per_account_gmail_cap = gmail_cap_bytes // n
    progress_log(
        f"[stream] {n} account(s); drive={drive_cap_bytes / GB:.2f}GB as a shared running budget "
        f"(stops scanning further accounts the moment it's used up), "
        f"gmail={per_account_gmail_cap / GB:.2f}GB flat per account"
    )

    ckpt = scan_cache_dir / "ai_labs_samples.checkpoint.jsonl"
    gmail_ckpt = scan_cache_dir / "ai_labs_gmail_threads.checkpoint.jsonl"

    all_drive_selected: list[dict[str, Any]] = []
    native_counts = {"gsheets": 0, "gdocs": 0, "gslides": 0}

    if not skip_drive:
        dest_id = None
        drive_worker: threading.Thread | None = None
        drive_queue: queue.Queue = queue.Queue()

        if not scan_only:
            drive_dest_service = build_drive_service(_get_rw_credentials())
            dest_meta = _load_dest_meta(ckpt)
            if dest_folder_id.strip():
                dest_id = normalize_folder_id(dest_folder_id)
            elif dest_meta and dest_meta.get("dest_folder_id"):
                dest_id = str(dest_meta["dest_folder_id"])
                folder_name = str(dest_meta.get("folder_name") or folder_name)
                progress_log(f"[stream] resuming into existing folder {folder_name!r} → {dest_id}")
            else:
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if folder_name == "AI Labs Sample Set":
                    folder_name = f"{folder_name} ({stamp})"
                dest_id = _create_root_folder(drive_dest_service, folder_name, parent_id="root")
                progress_log(f"[stream] created My Drive folder {folder_name!r} → {dest_id}")
            _save_dest_meta(ckpt, dest_id, folder_name)
            drive_done = _load_done(ckpt)
            folder_cache: dict[str, str] = {}

            def _drive_transfer_worker() -> None:
                # Only this thread ever touches drive_dest_service, drive_done, and
                # folder_cache — one writer, no locking needed.
                while True:
                    item = drive_queue.get()
                    if item is None:
                        drive_queue.task_done()
                        return
                    email, account_selected = item
                    ok = skip = fail = 0
                    for row in account_selected:
                        fid = row["file_id"]
                        if fid in drive_done:
                            skip += 1
                            continue
                        try:
                            parent = _ensure_child_folder(drive_dest_service, dest_id, email, folder_cache)
                            new_id = _drive_copy(drive_dest_service, fid, parent, row["name"])
                            _append_done(ckpt, {
                                "file_id": fid, "new_id": new_id, "bucket": email,
                                "name": row["name"], "path": row["path"], "mode": "copy", "status": "ok",
                            })
                            drive_done.add(fid)
                            ok += 1
                        except (HttpError, RuntimeError, OSError) as exc:
                            fail += 1
                            _append_done(ckpt, {
                                "file_id": fid, "bucket": email, "name": row["name"], "path": row["path"],
                                "status": "error", "error": str(exc)[:500],
                            })
                            progress_log(f"[stream] FAIL drive {row['name'][:60]!r} — {exc}")
                    progress_log(f"[stream] {email}: transferred ok={ok} skip={skip} fail={fail}")
                    drive_queue.task_done()

            drive_worker = threading.Thread(target=_drive_transfer_worker, daemon=True)
            drive_worker.start()

        drive_remaining = drive_cap_bytes
        for email in users:
            if drive_remaining <= 0:
                progress_log(f"[stream] drive cap reached — {email} and any remaining accounts "
                              f"are skipped for Drive, moving on to Gmail")
                break

            progress_log(f"[stream] Drive → {email} ({drive_remaining / GB:.2f}GB of the cap left)")
            u_creds = get_service_account_credentials(sa_file, email, SCOPES_READONLY)
            svc = build_drive_service(u_creds)
            buckets = _scan_account_in_rounds(
                svc, f"My Drive ({email})", folders_per_round=max(folders_per_round, 1),
                modified_before=modified_before, gsheets_per_account=gsheets_per_account,
                gdocs_per_account=gdocs_per_account, gslides_per_account=gslides_per_account,
                drive_cap_bytes=drive_remaining, progress_log=progress_log,
            )
            binary_selected, binary_total = greedy_fill(buckets["binary"], drive_remaining)
            native_selected: list[dict[str, Any]] = []
            for kind, limit in (("gsheets", gsheets_per_account), ("gdocs", gdocs_per_account),
                                 ("gslides", gslides_per_account)):
                picked = select_top_by_recency(buckets[kind], limit)
                native_counts[kind] += len(picked)
                native_selected.extend(picked)
            account_selected = binary_selected + native_selected
            all_drive_selected.extend(account_selected)
            drive_remaining -= binary_total
            progress_log(f"[stream] {email}: selected {len(binary_selected)} binary file(s) "
                         f"({binary_total / GB:.2f}GB) + {len(native_selected)} native file(s), "
                         f"{drive_remaining / GB:.2f}GB left of the cap")

            if drive_worker is not None and account_selected:
                drive_queue.put((email, account_selected))
                progress_log(f"[stream] {email}: queued for transfer — continuing to scan the next account")

        if drive_worker is not None:
            drive_queue.put(None)
            progress_log("[stream] Drive scanning done (cap reached or every account scanned) — "
                          "waiting for the transfer queue to drain...")
            drive_worker.join()
            progress_log(f"[stream] Drive fully done — folder: https://drive.google.com/drive/folders/{dest_id}")

    all_gmail_selected: list[dict[str, Any]] = []
    if not skip_gmail:
        gmail_worker: threading.Thread | None = None
        gmail_work_queue: queue.Queue = queue.Queue()

        if not scan_only:
            gmail_dest_service = build_gmail_service(_get_insert_credentials())
            gmail_done = _load_done(gmail_ckpt)

            def _gmail_transfer_worker() -> None:
                # Only this thread ever touches gmail_dest_service/gmail_done — one
                # writer, no locking needed. It builds its own source-read service
                # per row (as before), never sharing one across threads.
                while True:
                    item = gmail_work_queue.get()
                    if item is None:
                        gmail_work_queue.task_done()
                        return
                    email, selected = item
                    ok = skip = fail = 0
                    for row in selected:
                        thread_key = f"{email}:{row['thread_id']}"
                        if thread_key in gmail_done:
                            skip += 1
                            continue
                        try:
                            src_creds = get_service_account_credentials(sa_file, email, SCOPES_GMAIL)
                            src_service = build_gmail_service(src_creds)
                            raw_messages = fetch_thread_raw(src_service, row["thread_id"], user_id=email)
                            inserted_ids = [_insert_message(gmail_dest_service, raw) for raw in raw_messages]
                            _append_done(gmail_ckpt, {
                                "file_id": thread_key, "thread_id": row["thread_id"], "user_email": email,
                                "inserted_ids": inserted_ids, "status": "ok",
                            })
                            gmail_done.add(thread_key)
                            ok += 1
                        except (HttpError, RuntimeError, OSError) as exc:
                            fail += 1
                            _append_done(gmail_ckpt, {
                                "file_id": thread_key, "thread_id": row["thread_id"], "user_email": email,
                                "status": "error", "error": str(exc)[:500],
                            })
                            progress_log(f"[stream] FAIL gmail {row.get('subject', '')[:60]!r} — {exc}")
                    progress_log(f"[stream] {email}: transferred ok={ok} skip={skip} fail={fail}")
                    gmail_work_queue.task_done()

            gmail_worker = threading.Thread(target=_gmail_transfer_worker, daemon=True)
            gmail_worker.start()

        for email in users:
            progress_log(f"[stream] Gmail → {email}")
            u_creds = get_service_account_credentials(sa_file, email, SCOPES_GMAIL)
            svc = build_gmail_service(u_creds)
            selected = _stream_gmail_account_in_rounds(
                svc, email, gmail_cap_bytes=per_account_gmail_cap, messages_per_round=messages_per_round,
                gmail_query=gmail_query, scan_cache_dir=scan_cache_dir, before_suffix=before_suffix,
                work_queue=gmail_work_queue if gmail_worker is not None else None,
                progress_log=progress_log,
            )
            all_gmail_selected.extend(selected)

        if gmail_worker is not None:
            gmail_work_queue.put(None)
            progress_log("[stream] Gmail scanning done for every account — waiting for the transfer queue to drain...")
            gmail_worker.join()

        progress_log("[stream] Gmail fully done")

    return all_drive_selected, all_gmail_selected, native_counts


def _drive_rows_all_files(rows: list[dict[str, Any]], *, default_owner: str = "") -> list[dict[str, Any]]:
    """Every non-folder, non-shortcut file — including zero-size Google natives.

    Unlike ``_bucket_drive_rows``, this does not drop Forms/Drawings/etc. Used by
    ``--full-account`` transfer (copy everything, no quality filter).
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("is_folder") or r.get("is_shortcut"):
            continue
        fid = r.get("drive_file_id") or ""
        if not fid:
            continue
        out.append({
            "file_id": fid,
            "name": r.get("name") or "",
            "path": r.get("path") or "",
            "owner_email": r.get("owner_email") or default_owner,
            "size_bytes": _int_size(r.get("size_bytes")),
            "modified_time": r.get("modified_time") or "",
        })
    return out


def _take_files_as_is_until_cap(
    batch: list[dict[str, Any]],
    *,
    cap_bytes: int | None,
    used_bytes: int,
    accounted: set[str],
    already_done: set[str],
) -> tuple[list[dict[str, Any]], int, bool]:
    """Pick files in walk order until ``cap_bytes`` is filled.

    Skips a file that does not fit and keeps looking for a smaller one. Files already
    transferred (``already_done``) count toward the cap when rediscovered but are not
    re-queued. Returns ``(to_transfer, new_used_bytes, cap_full)``.
    """
    taken: list[dict[str, Any]] = []
    used = used_bytes
    for row in batch:
        fid = row["file_id"]
        if fid in accounted:
            continue
        size = int(row.get("size_bytes") or 0)
        if cap_bytes is not None and size > 0 and used + size > cap_bytes:
            continue  # does not fit; try next
        accounted.add(fid)
        if size > 0:
            used += size
        if fid in already_done:
            continue  # already on dest — counted, not re-queued
        taken.append(row)
        if cap_bytes is not None and used >= cap_bytes:
            return taken, used, True
    cap_full = cap_bytes is not None and used >= cap_bytes
    return taken, used, cap_full


def _share_folder_writer(service, folder_id: str, email: str, progress_log=_log) -> None:
    """Grant ``email`` writer on ``folder_id`` so they can open + write the sample set.

    Idempotent: if they already have writer/organizer/owner, does nothing.
    """
    want = email.strip().lower()
    try:
        page_token = None
        while True:
            resp = call_with_retry(
                service,
                lambda pt=page_token: (
                    service.permissions()
                    .list(
                        fileId=folder_id,
                        fields="nextPageToken,permissions(id,emailAddress,role,type)",
                        supportsAllDrives=True,
                        pageToken=pt,
                        pageSize=100,
                    )
                    .execute()
                ),
            )
            for perm in resp.get("permissions") or []:
                if (perm.get("emailAddress") or "").strip().lower() != want:
                    continue
                if perm.get("role") in {"writer", "fileOrganizer", "organizer", "owner"}:
                    progress_log(f"[as-is] {email} already has access on dest ({perm.get('role')})")
                    return
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as exc:
        progress_log(f"[as-is] WARNING: could not list permissions before share ({exc})")

    try:
        call_with_retry(
            service,
            lambda: service.permissions().create(
                fileId=folder_id,
                body={"type": "user", "role": "writer", "emailAddress": email},
                sendNotificationEmail=False,
                supportsAllDrives=True,
                fields="id,role,emailAddress",
            ).execute(),
        )
        progress_log(f"[as-is] shared dest folder with {email} (writer) — they can open it in Drive")
    except HttpError as exc:
        status = getattr(exc, "resp", None)
        code = getattr(status, "status", None) if status is not None else None
        if code in (400, 409):
            # 409 = already exists; 400 sometimes for duplicate domain shares
            progress_log(f"[as-is] share {email}: already allowed ({code})")
            return
        raise


def run_full_account_drive_transfer(
    source_service,
    email: str,
    *,
    folder_name: str,
    dest_folder_id: str,
    checkpoint_dir: Path,
    modified_before: str | None = None,
    folders_per_round: int = 50,
    cap_bytes: int | None = None,
    dest_owner_service=None,
    copy_service=None,
    share_dest_with: str | None = None,
    progress_log=_log,
) -> list[dict[str, Any]]:
    """Walk one account's My Drive and copy files as-is into AI Labs Sample Set.

    Transfer starts as soon as the first round of folders is listed — a background
    worker copies that batch while the walk continues. No quality selection, no Gmail.
    If ``cap_bytes`` is set, keep taking walk-order files until that many bytes are
    queued (skipping ones that do not fit), then stop. Resumable via checkpoint.

    Domain-Wide Delegation (workspace): pass ``dest_owner_service`` as the super-admin
    Drive client (creates the sample folder in admin My Drive), ``copy_service`` as the
    selected user (can read their files and write into the shared dest), and
    ``share_dest_with=email``. No personal OAuth browser login is required.
    """
    ckpt = checkpoint_dir / "ai_labs_samples.checkpoint.jsonl"
    owner_svc = dest_owner_service or build_drive_service(_get_rw_credentials())
    writer_svc = copy_service or owner_svc
    dest_meta = _load_dest_meta(ckpt)
    if dest_folder_id.strip():
        dest_id = normalize_folder_id(dest_folder_id)
        progress_log(f"[as-is] using existing dest folder id={dest_id}")
    elif dest_meta and dest_meta.get("dest_folder_id"):
        dest_id = str(dest_meta["dest_folder_id"])
        folder_name = str(dest_meta.get("folder_name") or folder_name)
        progress_log(f"[as-is] resuming into existing folder {folder_name!r} → {dest_id}")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if folder_name == "AI Labs Sample Set":
            folder_name = f"{folder_name} ({stamp})"
        dest_id = _create_root_folder(owner_svc, folder_name, parent_id="root")
        progress_log(f"[as-is] created My Drive folder {folder_name!r} → {dest_id}")
    _save_dest_meta(ckpt, dest_id, folder_name)

    if share_dest_with:
        _share_folder_writer(owner_svc, dest_id, share_dest_with, progress_log=progress_log)
        progress_log(
            f"[as-is] selected user access: {share_dest_with} → "
            f"https://drive.google.com/drive/folders/{dest_id}"
        )

    done = _load_done(ckpt)
    folder_cache: dict[str, str] = {}
    all_transferred: list[dict[str, Any]] = []
    totals = {"ok": 0, "skip": 0, "fail": 0}
    work_queue: queue.Queue = queue.Queue()
    budget = max(1, folders_per_round)
    used_bytes = 0
    accounted: set[str] = set()
    child_shared = False

    def _transfer_worker() -> None:
        # Only this thread touches writer_svc / done / folder_cache / all_transferred.
        nonlocal child_shared
        while True:
            item = work_queue.get()
            if item is None:
                work_queue.task_done()
                return
            round_num, batch = item
            ok = skip = fail = 0
            for row in batch:
                row["owner_email"] = email
                fid = row["file_id"]
                if fid in done:
                    skip += 1
                    continue
                try:
                    parent = _ensure_child_folder(writer_svc, dest_id, email, folder_cache)
                    # Root is shared with the selected user; child is usually created by
                    # them so they already own it. Best-effort share via admin if needed.
                    if share_dest_with and not child_shared:
                        try:
                            _share_folder_writer(
                                owner_svc, parent, share_dest_with, progress_log=progress_log,
                            )
                        except HttpError as share_exc:
                            progress_log(
                                f"[as-is] child-folder share skipped ({share_exc}) — "
                                f"user already has access via shared root / ownership"
                            )
                        child_shared = True
                    new_id = _drive_copy(writer_svc, fid, parent, row["name"])
                    _append_done(ckpt, {
                        "file_id": fid, "new_id": new_id, "bucket": email,
                        "name": row["name"], "path": row["path"],
                        "size_bytes": row.get("size_bytes") or 0,
                        "mode": "copy", "status": "ok",
                    })
                    done.add(fid)
                    ok += 1
                    all_transferred.append(row)
                except (HttpError, RuntimeError, OSError) as exc:
                    fail += 1
                    _append_done(ckpt, {
                        "file_id": fid, "bucket": email, "name": row["name"], "path": row["path"],
                        "status": "error", "error": str(exc)[:500],
                    })
                    progress_log(f"[as-is] FAIL {row['name'][:60]!r} — {exc}")
            totals["ok"] += ok
            totals["skip"] += skip
            totals["fail"] += fail
            progress_log(f"[as-is] round {round_num}: transferred ok={ok} skip={skip} fail={fail}")
            work_queue.task_done()

    worker = threading.Thread(target=_transfer_worker, daemon=True)
    worker.start()
    if cap_bytes is None:
        cap_note = "no byte cap (entire account)"
    else:
        cap_note = f"cap {cap_bytes / GB:.2f}GB, walk order as-is"
    auth_note = "DWD (admin dest + user copy)" if share_dest_with else "OAuth dest"
    progress_log(f"[as-is] transferring My Drive for {email} → {folder_name!r} "
                 f"(starts immediately; {cap_note}; {auth_note}; Drive only; {budget} folders/round)")

    frontier = None
    round_num = 0
    path_prefix = f"My Drive ({email})"
    while True:
        round_num += 1
        batch_rows, frontier = walk_my_drive_in_rounds(
            source_service, path_prefix=path_prefix, frontier=frontier,
            folder_budget=budget, modified_before=modified_before, progress_log=progress_log,
        )
        batch = _drive_rows_all_files(batch_rows, default_owner=email)
        taken, used_bytes, cap_full = _take_files_as_is_until_cap(
            batch, cap_bytes=cap_bytes, used_bytes=used_bytes,
            accounted=accounted, already_done=done,
        )
        if taken:
            progress_log(
                f"[as-is] round {round_num}: listed {len(batch)} → queued {len(taken)} "
                f"({used_bytes / GB:.2f}GB"
                + (f" / {cap_bytes / GB:.2f}GB)" if cap_bytes is not None else ")")
            )
            work_queue.put((round_num, taken))
        else:
            progress_log(f"[as-is] round {round_num}: listed {len(batch)} → queued 0 "
                         f"({used_bytes / GB:.2f}GB)")
        if cap_full:
            progress_log(f"[as-is] cap reached ({used_bytes / GB:.2f}GB) — stopping walk")
            break
        if not frontier:
            break

    progress_log("[as-is] walk done — waiting for transfer queue to drain...")
    work_queue.put(None)
    worker.join()

    progress_log(f"[as-is] done {email}: ok={totals['ok']} skip={totals['skip']} "
                 f"fail={totals['fail']} bytes={used_bytes / GB:.2f}GB — "
                 f"https://drive.google.com/drive/folders/{dest_id}")
    return all_transferred



def run_streaming_single_account_drive(
    service,
    *,
    drive_cap_bytes: int,
    gsheets_per_account: int,
    gdocs_per_account: int,
    gslides_per_account: int,
    folders_per_round: int,
    modified_before: str | None,
    folder_name: str,
    dest_folder_id: str,
    checkpoint_dir: Path,
    scan_only: bool,
    progress_log=_log,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Single-account counterpart to ``run_streaming_workspace_pipeline``'s Drive
    phase: with only one account there's no "next account" to scan while the current
    one transfers, so instead each round of ``folders_per_round`` folders is selected
    and handed off to a background transfer thread while scanning continues into the
    *next round* of the same account's remaining folders. Same shared running budget
    across rounds (stops scanning further rounds the instant the cap is used up), same
    one-writer-thread-per-checkpoint concurrency guarantee as the workspace path.
    """
    ckpt = checkpoint_dir / "ai_labs_samples.checkpoint.jsonl"
    native_counts = {"gsheets": 0, "gdocs": 0, "gslides": 0}
    all_selected: list[dict[str, Any]] = []

    dest_id = None
    worker: threading.Thread | None = None
    work_queue: queue.Queue = queue.Queue()

    if not scan_only:
        dest_service = build_drive_service(_get_rw_credentials())
        dest_meta = _load_dest_meta(ckpt)
        if dest_folder_id.strip():
            dest_id = normalize_folder_id(dest_folder_id)
        elif dest_meta and dest_meta.get("dest_folder_id"):
            dest_id = str(dest_meta["dest_folder_id"])
            folder_name = str(dest_meta.get("folder_name") or folder_name)
            progress_log(f"[stream] resuming into existing folder {folder_name!r} → {dest_id}")
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if folder_name == "AI Labs Sample Set":
                folder_name = f"{folder_name} ({stamp})"
            dest_id = _create_root_folder(dest_service, folder_name, parent_id="root")
            progress_log(f"[stream] created My Drive folder {folder_name!r} → {dest_id}")
        _save_dest_meta(ckpt, dest_id, folder_name)
        done = _load_done(ckpt)
        folder_cache: dict[str, str] = {}

        def _transfer_worker() -> None:
            # Only this thread ever touches dest_service/done/folder_cache — one
            # writer, no locking needed.
            while True:
                item = work_queue.get()
                if item is None:
                    work_queue.task_done()
                    return
                round_num, batch = item
                ok = skip = fail = 0
                for row in batch:
                    fid = row["file_id"]
                    if fid in done:
                        skip += 1
                        continue
                    try:
                        parent = _ensure_child_folder(dest_service, dest_id, row["owner_email"], folder_cache)
                        new_id = _drive_copy(dest_service, fid, parent, row["name"])
                        _append_done(ckpt, {
                            "file_id": fid, "new_id": new_id, "bucket": row["owner_email"],
                            "name": row["name"], "path": row["path"], "mode": "copy", "status": "ok",
                        })
                        done.add(fid)
                        ok += 1
                    except (HttpError, RuntimeError, OSError) as exc:
                        fail += 1
                        _append_done(ckpt, {
                            "file_id": fid, "bucket": row["owner_email"], "name": row["name"],
                            "path": row["path"], "status": "error", "error": str(exc)[:500],
                        })
                        progress_log(f"[stream] FAIL drive {row['name'][:60]!r} — {exc}")
                progress_log(f"[stream] round {round_num}: transferred ok={ok} skip={skip} fail={fail}")
                work_queue.task_done()

        worker = threading.Thread(target=_transfer_worker, daemon=True)
        worker.start()

    drive_remaining = drive_cap_bytes
    native_selected: dict[str, list[dict[str, Any]]] = {"gsheets": [], "gdocs": [], "gslides": []}
    frontier = None
    round_num = 0
    while True:
        round_num += 1
        if drive_remaining <= 0:
            progress_log(f"[stream] drive cap reached — stopping scan after round {round_num - 1}")
            break

        batch_rows, frontier = walk_my_drive_in_rounds(
            service, path_prefix="", frontier=frontier, folder_budget=folders_per_round,
            modified_before=modified_before, progress_log=progress_log,
        )
        batch_buckets = _bucket_drive_rows(batch_rows)
        binary_selected, binary_total = greedy_fill(batch_buckets["binary"], drive_remaining)
        round_selected = list(binary_selected)
        for kind, limit in (("gsheets", gsheets_per_account), ("gdocs", gdocs_per_account),
                             ("gslides", gslides_per_account)):
            remaining_quota = limit - len(native_selected[kind])
            if remaining_quota > 0:
                picked = select_top_by_recency(batch_buckets[kind], remaining_quota)
                native_selected[kind].extend(picked)
                native_counts[kind] += len(picked)
                round_selected.extend(picked)

        all_selected.extend(round_selected)
        drive_remaining -= binary_total
        progress_log(f"[stream] round {round_num}: selected {len(binary_selected)} binary file(s) "
                     f"({binary_total / GB:.2f}GB) + {len(round_selected) - len(binary_selected)} native file(s), "
                     f"{drive_remaining / GB:.2f}GB left of the cap, {len(frontier)} folder(s) queued")

        if worker is not None and round_selected:
            work_queue.put((round_num, round_selected))
            progress_log(f"[stream] round {round_num}: queued for transfer — continuing to scan the next round")

        if not frontier:
            progress_log(f"[stream] entire My Drive scanned after {round_num} round(s)")
            break

    if worker is not None:
        work_queue.put(None)
        progress_log("[stream] Drive scanning done — waiting for the transfer queue to drain...")
        worker.join()
        progress_log(f"[stream] Drive fully done — folder: https://drive.google.com/drive/folders/{dest_id}")

    return all_selected, native_counts


def run_streaming_single_account_gmail(
    service,
    email: str,
    *,
    gmail_cap_bytes: int,
    messages_per_round: int,
    gmail_query: str,
    scan_cache_dir: Path,
    before_suffix: str,
    checkpoint_dir: Path,
    scan_only: bool,
    progress_log=_log,
) -> list[dict[str, Any]]:
    """Single-account counterpart to the round-based Gmail streaming used inside
    ``run_streaming_workspace_pipeline`` — same round-by-round scan+select+transfer
    overlap (via ``_stream_gmail_account_in_rounds``), just for the one mailbox instead
    of across accounts. See that function's docstring for the accounting caveat this
    implies (an approximate, not exact, cap — a thread's charged size only reflects
    messages seen by the round it's selected in).
    """
    ckpt = checkpoint_dir / "ai_labs_gmail_threads.checkpoint.jsonl"
    worker: threading.Thread | None = None
    work_queue: queue.Queue = queue.Queue()

    if not scan_only:
        dest_service = build_gmail_service(_get_insert_credentials())
        done = _load_done(ckpt)

        def _transfer_worker() -> None:
            # Only this thread ever touches dest_service/done — one writer, no locking needed.
            while True:
                item = work_queue.get()
                if item is None:
                    work_queue.task_done()
                    return
                acct_email, batch = item
                ok = skip = fail = 0
                for row in batch:
                    thread_key = f"{acct_email}:{row['thread_id']}"
                    if thread_key in done:
                        skip += 1
                        continue
                    try:
                        raw_messages = fetch_thread_raw(service, row["thread_id"], user_id=acct_email)
                        inserted_ids = [_insert_message(dest_service, raw) for raw in raw_messages]
                        _append_done(ckpt, {
                            "file_id": thread_key, "thread_id": row["thread_id"], "user_email": acct_email,
                            "inserted_ids": inserted_ids, "status": "ok",
                        })
                        done.add(thread_key)
                        ok += 1
                    except (HttpError, RuntimeError, OSError) as exc:
                        fail += 1
                        _append_done(ckpt, {
                            "file_id": thread_key, "thread_id": row["thread_id"], "user_email": acct_email,
                            "status": "error", "error": str(exc)[:500],
                        })
                        progress_log(f"[stream] FAIL gmail {row.get('subject', '')[:60]!r} — {exc}")
                progress_log(f"[stream] gmail: transferred ok={ok} skip={skip} fail={fail}")
                work_queue.task_done()

        worker = threading.Thread(target=_transfer_worker, daemon=True)
        worker.start()

    selected = _stream_gmail_account_in_rounds(
        service, email, gmail_cap_bytes=gmail_cap_bytes, messages_per_round=messages_per_round,
        gmail_query=gmail_query, scan_cache_dir=scan_cache_dir, before_suffix=before_suffix,
        work_queue=work_queue if worker is not None else None,
        progress_log=progress_log,
    )

    if worker is not None:
        work_queue.put(None)
        progress_log("[stream] Gmail scanning done — waiting for the transfer queue to drain...")
        worker.join()
        progress_log("[stream] Gmail fully done")

    return selected


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--drive-cap-gb", type=float, default=15.0,
                    help="Byte cap for binary Drive files, in GB (default: 15)")
    p.add_argument("--gmail-cap-gb", type=float, default=12.5, help="Gmail selection cap in GB (default: 12.5)")
    p.add_argument("--gsheets-limit", type=int, default=350,
                    help="Overall target total for Google Sheets, most-recently-modified first "
                         "(default: 350; topped up to this only if per-account guarantees fall short)")
    p.add_argument("--gdocs-limit", type=int, default=300,
                    help="Overall target total for Google Docs (default: 300, see --gsheets-limit)")
    p.add_argument("--gslides-limit", type=int, default=150,
                    help="Overall target total for Google Slides (default: 150, see --gsheets-limit)")
    p.add_argument("--gsheets-per-account", type=int, default=30,
                    help="Google Sheets guaranteed per account, most-recently-modified first (default: 30)")
    p.add_argument("--gdocs-per-account", type=int, default=40,
                    help="Google Docs guaranteed per account (default: 40)")
    p.add_argument("--gslides-per-account", type=int, default=20,
                    help="Google Slides guaranteed per account (default: 20)")
    p.add_argument("--out", default="out/quality_sample_manifest.json", help="Output manifest path")
    p.add_argument("--rescan", action="store_true",
                    help="Force a fresh scan even if --out already exists (default: reuse it and "
                         "skip straight to transferring)")
    p.add_argument("--scan-only", action="store_true",
                    help="Stop after writing the manifest — don't transfer into the destination "
                         "sample folder (transferring is the default)")
    p.add_argument("--folder-name", default="AI Labs Sample Set",
                    help="Destination My Drive folder name (default: 'AI Labs Sample Set'; "
                         "date suffix added automatically)")
    p.add_argument("--dest-folder-id", default="",
                    help="Optional existing destination folder ID/URL instead of creating a new one")
    p.add_argument("--gmail-query", default="", help="Optional Gmail search query to filter messages")
    p.add_argument("--before", default="", metavar="YYYY-MM-DD",
                    help="Only scan Drive files and Gmail messages dated before this date")
    p.add_argument("--folders-per-round", type=int, default=0, metavar="N",
                    help="Scan each account's Drive in rounds of N folders, stopping early once "
                         "it has enough candidates for the configured targets (0 = disabled, scan "
                         "every folder exhaustively — default). Speeds up huge workspaces at the "
                         "cost of a small chance of missing a marginally-better file in an "
                         "unscanned folder. Shared Drives are always scanned exhaustively. Also "
                         "enables Gmail round-based streaming (see --messages-per-round) — set > 0 "
                         "to speed up either source.")
    p.add_argument("--messages-per-round", type=int, default=2000, metavar="N",
                    help="Gmail equivalent of --folders-per-round: scan each mailbox in batches of "
                         "N messages, selecting and transferring each batch while scanning continues "
                         "into the next (default: 2000). Only takes effect when --folders-per-round "
                         "> 0. Approximate cap: a thread's charged size only reflects the messages "
                         "seen by the round it's selected in, since Gmail message IDs aren't returned "
                         "thread-grouped — the transfer itself always grabs the complete thread, so "
                         "actual bytes moved can modestly exceed the accounted total, bounded by that "
                         "thread's own size.")
    p.add_argument("--skip-drive", action="store_true", help="Skip Drive scanning/selection")
    p.add_argument("--skip-gmail", action="store_true", help="Skip Gmail scanning/selection")
    sa_default = str(default_service_account_path()) if default_service_account_path() else ""
    p.add_argument("--service-account", default=sa_default, metavar="FILE",
                    help="Service account JSON key (Domain-Wide Delegation)")
    p.add_argument("--admin-email", default="", metavar="EMAIL",
                    help="Admin email to impersonate (or GOOGLE_ADMIN_EMAIL env var)")
    p.add_argument("--users", nargs="+", metavar="EMAIL",
                    help="Workspace mode only: scan just these users instead of the whole domain "
                         "(space- or comma-separated; skips Admin SDK enumeration entirely)")
    p.add_argument("--full-account", action="store_true",
                    help="One account sample: Drive as-is until --cap-gb (default 5GB), then "
                         "Gmail sample (first 100 messages → up to 20 threads, 3GB cap). "
                         "No quality ranking. My Drive mode: signed-in account. "
                         "Workspace mode: exactly one --users EMAIL.")
    p.add_argument("--as-is", action="store_true",
                    help="Transfer Drive files as-is (walk order) into AI Labs Sample Set — "
                         "no quality scan, Drive only. Stops at --cap-gb (default 40GB). "
                         "Same account rules as --full-account.")
    p.add_argument("--cap-gb", type=float, default=None, metavar="GB",
                    help="Drive byte cap for --as-is / --full-account (GB). "
                         "Default: 5 with --full-account, 40 with --as-is.")
    args = p.parse_args(argv)

    if args.full_account and args.as_is:
        print("ERROR: use either --full-account or --as-is, not both", flush=True)
        return 1

    sa_file = Path(args.service_account).expanduser().resolve() if args.service_account else None
    admin_email = args.admin_email or os.environ.get("GOOGLE_ADMIN_EMAIL", "").strip()
    workspace_mode = bool(sa_file and sa_file.is_file() and admin_email)
    if args.service_account and not workspace_mode:
        # A service account was explicitly passed but is incomplete/unusable — fail loud
        # rather than silently falling back to a single-account scan the user didn't ask for.
        if not (sa_file and sa_file.is_file()):
            print(f"ERROR: service account file not found: {sa_file}", flush=True)
            return 1
        print("ERROR: --admin-email (or GOOGLE_ADMIN_EMAIL env var) is required with --service-account", flush=True)
        return 1
    selected_users = _parse_users(args.users)
    if selected_users and not workspace_mode:
        print("ERROR: --users requires --service-account (Domain-Wide Delegation)", flush=True)
        return 1
    _log(f"[mode] {'workspace (Domain-Wide Delegation)' if workspace_mode else 'my Drive + Gmail only'}")
    if selected_users:
        _log(f"[mode] restricted to {len(selected_users)} selected user(s): {', '.join(selected_users)}")

    drive_before: str | None = None
    gmail_query = args.gmail_query
    if args.before:
        try:
            before_date = datetime.strptime(args.before, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            print(f"ERROR: --before must be YYYY-MM-DD, got {args.before!r}", flush=True)
            return 1
        drive_before = f"{before_date}T00:00:00Z"
        before_term = f"before:{before_date.replace('-', '/')}"
        gmail_query = f"{gmail_query} {before_term}".strip()
        _log(f"[mode] only scanning items modified/dated before {before_date}")

    out_path = (_ROOT / args.out).resolve()
    scan_cache_dir = out_path.parent
    scan_cache_dir.mkdir(parents=True, exist_ok=True)

    # --- As-is / full-account transfer (Drive walk+copy; full-account also does Gmail) ---
    if args.full_account or args.as_is:
        mode_label = "full-account" if args.full_account else "as-is"
        if args.scan_only:
            print(f"ERROR: --{mode_label} transfers immediately; --scan-only is not supported", flush=True)
            return 1
        if args.cap_gb is not None and args.cap_gb <= 0:
            print("ERROR: --cap-gb must be > 0", flush=True)
            return 1
        if workspace_mode:
            if len(selected_users) != 1:
                print(f"ERROR: --{mode_label} in workspace mode requires exactly one --users EMAIL", flush=True)
                return 1
            email = selected_users[0]
            # Domain-Wide Delegation only: admin owns the sample folder; selected user
            # lists + copies (no personal OAuth browser login).
            admin_creds = get_service_account_credentials(sa_file, admin_email, SCOPES_DRIVE)
            user_creds = get_service_account_credentials(sa_file, email, SCOPES_DRIVE)
            admin_svc = build_drive_service(admin_creds)
            source_svc = build_drive_service(user_creds)
            dest_owner_service = admin_svc
            copy_service = source_svc
            share_dest_with = email
            _log(f"[mode] DWD: dest folder in {admin_email} My Drive; copy as {email}")
        else:
            if selected_users:
                print("ERROR: --users requires --service-account (Domain-Wide Delegation)", flush=True)
                return 1
            source_svc = my_drive_service()
            # Resolve the signed-in account email for the dest subfolder name.
            try:
                about = call_with_retry(
                    source_svc, lambda: source_svc.about().get(fields="user(emailAddress)").execute()
                )
                email = (about.get("user") or {}).get("emailAddress") or "me"
            except (HttpError, *_RETRYABLE_NETWORK_ERRORS):
                email = "me"
            dest_owner_service = None
            copy_service = None
            share_dest_with = None

        if args.full_account:
            # --full-account: Drive as-is until 5GB (override with --cap-gb)
            cap_bytes = int((args.cap_gb if args.cap_gb is not None else 5.0) * GB)
        else:
            # --as-is: default 40GB, Drive only
            cap_bytes = int((args.cap_gb if args.cap_gb is not None else 40.0) * GB)

        _log(f"[mode] {mode_label} Drive transfer for {email} "
             f"(as-is until {cap_bytes / GB:.2f}GB"
             + ("; then Gmail first-100→20 threads / 3GB" if args.full_account and not args.skip_gmail else "; Drive only")
             + ")")

        # Small rounds so the first batch is listed quickly and transfer starts right away.
        folders_per_round = args.folders_per_round if args.folders_per_round > 0 else 50
        transferred = run_full_account_drive_transfer(
            source_svc, email,
            folder_name=args.folder_name, dest_folder_id=args.dest_folder_id,
            checkpoint_dir=scan_cache_dir, modified_before=drive_before,
            folders_per_round=folders_per_round, cap_bytes=cap_bytes,
            dest_owner_service=dest_owner_service, copy_service=copy_service,
            share_dest_with=share_dest_with, progress_log=_log,
        )
        total_bytes = sum(r.get("size_bytes") or 0 for r in transferred)

        gmail_selected: list[dict[str, Any]] = []
        gmail_total = 0
        if args.full_account and not args.skip_gmail:
            gmail_cap_bytes = int(_FULL_ACCOUNT_GMAIL_CAP_GB * GB)
            _log(
                f"[gmail] full-account: first {_FULL_ACCOUNT_GMAIL_MAX_MESSAGES} message(s) → "
                f"up to {_FULL_ACCOUNT_GMAIL_MAX_THREADS} thread(s), "
                f"cap {gmail_cap_bytes / GB:.2f}GB for {email}…"
            )
            if workspace_mode:
                def _gmail_service_for(user: str):
                    creds = get_service_account_credentials(sa_file, user, SCOPES_GMAIL)
                    return build_gmail_service(creds)
            else:
                single_gmail_svc, _my = my_gmail_service()
                def _gmail_service_for(_user: str):
                    return single_gmail_svc

            gmail_selected = collect_gmail_first_n_sample(
                get_user_service=_gmail_service_for, email=email, gmail_query=gmail_query,
                scan_cache_dir=scan_cache_dir,
                cache_suffix=f"__before_{args.before}" if args.before else "",
                max_messages=_FULL_ACCOUNT_GMAIL_MAX_MESSAGES,
                max_threads=_FULL_ACCOUNT_GMAIL_MAX_THREADS,
                cap_bytes=gmail_cap_bytes,
                progress_log=_log,
            )
            gmail_total = sum(int(r.get("size_bytes") or 0) for r in gmail_selected)
            _log(f"[gmail] full-account: transferring {len(gmail_selected)} thread(s), "
                 f"{gmail_total / GB:.2f}GB")
            transfer_selection_by_account(
                [], gmail_selected,
                folder_name=args.folder_name, dest_folder_id=args.dest_folder_id,
                checkpoint_dir=scan_cache_dir, get_source_gmail_service=_gmail_service_for,
                progress_log=_log,
            )

        manifest = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scan_mode": "full_account_drive_gmail" if args.full_account else "as_is_drive_transfer",
            "account": email,
            "cap_bytes": cap_bytes,
            "drive_total_bytes": total_bytes,
            "gmail_total_bytes": gmail_total,
            "files": [
                {"file_id": c["file_id"], "name": c["name"], "path": c["path"],
                 "size_bytes": c["size_bytes"], "owner_email": c["owner_email"]}
                for c in transferred
            ],
            "gmail_threads": gmail_selected,
        }
        out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        _log(f"manifest written: {out_path} (files={len(transferred)}, "
             f"drive={total_bytes / GB:.2f}GB, gmail_threads={len(gmail_selected)})")
        return 0

    drive_cap_bytes = int(args.drive_cap_gb * GB)
    gmail_cap_bytes = int(args.gmail_cap_gb * GB)

    resumed = False
    if out_path.is_file() and not args.rescan:
        try:
            manifest = json.loads(out_path.read_text(encoding="utf-8"))
            drive_selected = manifest.get("files", [])
            gmail_selected = manifest.get("gmail_threads", [])
            resumed = True
            _log(f"[resume] using existing manifest {out_path} — {len(drive_selected)} file(s), "
                 f"{len(gmail_selected)} gmail thread(s) — skipping scan (pass --rescan to force a fresh scan)")
        except (json.JSONDecodeError, OSError) as exc:
            _log(f"[resume] WARNING: {out_path} exists but couldn't be read ({exc}) — "
                 f"treating it as missing and running a fresh scan")
    streamed = False
    if not resumed and workspace_mode and args.folders_per_round > 0:
        streamed = True
        before_suffix = f"__before_{args.before}" if args.before else ""
        if selected_users:
            stream_users = selected_users
        else:
            admin_sdk_creds = get_service_account_credentials(sa_file, admin_email, SCOPES_ADMIN_USERS)
            admin_svc = build_admin_service(admin_sdk_creds)
            stream_users = list_workspace_users(admin_svc)
        drive_selected, gmail_selected, native_counts = run_streaming_workspace_pipeline(
            sa_file=sa_file, admin_email=admin_email, users=stream_users,
            drive_cap_bytes=drive_cap_bytes, gmail_cap_bytes=gmail_cap_bytes,
            gsheets_per_account=args.gsheets_per_account, gdocs_per_account=args.gdocs_per_account,
            gslides_per_account=args.gslides_per_account, folders_per_round=args.folders_per_round,
            messages_per_round=args.messages_per_round,
            modified_before=drive_before, gmail_query=gmail_query, scan_cache_dir=scan_cache_dir,
            before_suffix=before_suffix, folder_name=args.folder_name, dest_folder_id=args.dest_folder_id,
            scan_only=args.scan_only, skip_drive=args.skip_drive, skip_gmail=args.skip_gmail,
            progress_log=_log,
        )
        drive_total = sum(r["size_bytes"] for r in drive_selected)
        gmail_total = sum(r["size_bytes"] for r in gmail_selected)
        manifest = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scan_mode": "workspace_streaming",
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
        _log(f"manifest written: {out_path} (files={len(drive_selected)} gmail_threads={len(gmail_selected)}) "
             f"[streaming mode — already transferred incrementally per account above]")
    elif not resumed:
        drive_selected: list[dict[str, Any]] = []
        drive_total = 0
        native_counts = {"gsheets": 0, "gdocs": 0, "gslides": 0}
        if not args.skip_drive and not workspace_mode and args.folders_per_round > 0:
            drive_selected, native_counts = run_streaming_single_account_drive(
                my_drive_service(), drive_cap_bytes=drive_cap_bytes,
                gsheets_per_account=args.gsheets_per_account, gdocs_per_account=args.gdocs_per_account,
                gslides_per_account=args.gslides_per_account, folders_per_round=args.folders_per_round,
                modified_before=drive_before, folder_name=args.folder_name, dest_folder_id=args.dest_folder_id,
                checkpoint_dir=scan_cache_dir, scan_only=args.scan_only, progress_log=_log,
            )
            drive_total = sum(r["size_bytes"] for r in drive_selected)
            _log(f"[drive] streamed: selected {len(drive_selected)} file(s), {drive_total / GB:.2f}GB "
                 f"/ {args.drive_cap_gb:.2f}GB cap "
                 f"[streaming mode — already transferred incrementally per round above]")
        elif not args.skip_drive:
            cache_suffix = f".before_{args.before}" if args.before else ""
            drive_scan_cache = str(scan_cache_dir / (out_path.stem + cache_suffix + ".drive_scan_cache.jsonl"))
            if workspace_mode:
                buckets = collect_drive_candidates_workspace(
                    sa_file=sa_file, admin_email=admin_email, scan_cache=drive_scan_cache,
                    users=selected_users or None, modified_before=drive_before,
                    folders_per_round=args.folders_per_round,
                    gsheets_per_account=args.gsheets_per_account, gdocs_per_account=args.gdocs_per_account,
                    gslides_per_account=args.gslides_per_account, drive_cap_bytes=drive_cap_bytes,
                    progress_log=_log,
                )
            else:
                buckets = collect_drive_candidates_single(
                    service=my_drive_service(), scan_cache=drive_scan_cache,
                    modified_before=drive_before, folders_per_round=args.folders_per_round,
                    gsheets_per_account=args.gsheets_per_account, gdocs_per_account=args.gdocs_per_account,
                    gslides_per_account=args.gslides_per_account, drive_cap_bytes=drive_cap_bytes,
                    progress_log=_log,
                )
            binary_selected, drive_total = allocate_binary_by_account(buckets["binary"], drive_cap_bytes)
            n_accounts = len({r.get("owner_email") or "" for r in buckets["binary"]})
            _log(f"[drive] binary files: selected {len(binary_selected)}/{len(buckets['binary'])} "
                 f"across {n_accounts} account(s), {drive_total / GB:.2f}GB / {args.drive_cap_gb:.2f}GB cap")

            native_limits = {
                "gsheets": (args.gsheets_per_account, args.gsheets_limit),
                "gdocs": (args.gdocs_per_account, args.gdocs_limit),
                "gslides": (args.gslides_per_account, args.gslides_limit),
            }
            native_selected: list[dict[str, Any]] = []
            for kind, (per_acct, overall) in native_limits.items():
                picked = select_native_by_account(buckets[kind], per_account_limit=per_acct, overall_cap=overall)
                native_counts[kind] = len(picked)
                native_selected.extend(picked)
                _log(f"[drive] {kind}: selected {len(picked)}/{len(buckets[kind])} "
                     f"(guaranteed {per_acct}/account, topped up toward {overall} overall)")

            drive_selected = binary_selected + native_selected

        gmail_selected: list[dict[str, Any]] = []
        gmail_total = 0
        if not args.skip_gmail and not workspace_mode and args.folders_per_round > 0:
            single_gmail_svc, my_email = my_gmail_service()
            gmail_selected = run_streaming_single_account_gmail(
                single_gmail_svc, my_email, gmail_cap_bytes=gmail_cap_bytes,
                messages_per_round=args.messages_per_round, gmail_query=gmail_query,
                scan_cache_dir=scan_cache_dir, before_suffix=f"__before_{args.before}" if args.before else "",
                checkpoint_dir=scan_cache_dir, scan_only=args.scan_only, progress_log=_log,
            )
            gmail_total = sum(r["size_bytes"] for r in gmail_selected)
            _log(f"[gmail] streamed: selected {len(gmail_selected)} thread(s), {gmail_total / GB:.2f}GB "
                 f"/ {args.gmail_cap_gb:.2f}GB cap "
                 f"[streaming mode — already transferred incrementally per round above]")
        elif not args.skip_gmail:
            if workspace_mode:
                if selected_users:
                    gmail_users = selected_users
                else:
                    admin_sdk_creds = get_service_account_credentials(sa_file, admin_email, SCOPES_ADMIN_USERS)
                    admin_svc = build_admin_service(admin_sdk_creds)
                    gmail_users = list_workspace_users(admin_svc)

                def _gmail_service_for(email: str):
                    creds = get_service_account_credentials(sa_file, email, SCOPES_GMAIL)
                    return build_gmail_service(creds)
            else:
                single_gmail_svc, my_email = my_gmail_service()
                gmail_users = [my_email]
                _gmail_service_for = lambda _email: single_gmail_svc  # noqa: E731 — same account for every "user"

            gmail_candidates = collect_gmail_candidates(
                get_user_service=_gmail_service_for, users=gmail_users, gmail_query=gmail_query,
                scan_cache_dir=scan_cache_dir, cache_suffix=f"__before_{args.before}" if args.before else "",
                progress_log=_log,
            )
            gmail_selected, gmail_total = allocate_equally_by_account(gmail_candidates, gmail_cap_bytes)
            n_gmail_accounts = len({r["user_email"] for r in gmail_candidates})
            _log(f"[gmail] selected {len(gmail_selected)}/{len(gmail_candidates)} thread(s) "
                 f"across {n_gmail_accounts} account(s) (equal split), "
                 f"{gmail_total / GB:.2f}GB / {args.gmail_cap_gb:.2f}GB cap")

        manifest = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scan_mode": "workspace" if workspace_mode else "my_drive",
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

    if args.scan_only:
        return 0

    if streamed:
        _log("[main] streaming mode already transferred everything incrementally, per account — done")
        return 0

    if workspace_mode:
        def _source_gmail_service(email: str):
            creds = get_service_account_credentials(sa_file, email, SCOPES_GMAIL)
            return build_gmail_service(creds)
    else:
        def _source_gmail_service(_email: str):
            svc, _ = my_gmail_service()
            return svc

    transfer_selection_by_account(
        drive_selected, gmail_selected,
        folder_name=args.folder_name, dest_folder_id=args.dest_folder_id,
        checkpoint_dir=scan_cache_dir, get_source_gmail_service=_source_gmail_service,
        progress_log=_log,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
