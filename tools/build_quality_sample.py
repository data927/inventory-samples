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

  python tools/build_quality_sample.py --drive-cap-gb 75 --gmail-cap-gb 12.5 \\
      --gsheets-limit 350 --gdocs-limit 300 --gslides-limit 150 \\
      --out out/quality_sample_manifest.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--drive-cap-gb", type=float, default=75.0,
                    help="Byte cap for binary Drive files, in GB (default: 75)")
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
                         "unscanned folder. Shared Drives are always scanned exhaustively.")
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
    args = p.parse_args(argv)

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
    if not resumed:
        drive_selected: list[dict[str, Any]] = []
        drive_total = 0
        native_counts = {"gsheets": 0, "gdocs": 0, "gslides": 0}
        if not args.skip_drive:
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
        if not args.skip_gmail:
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
