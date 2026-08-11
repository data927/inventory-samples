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
from gdrive.scan import list_shared_drives, list_workspace_users, walk_all_user_my_drives, walk_entire_workspace
from gmail.scan import fetch_message_meta, scan_mailbox

_GMAIL_TOKEN = _ROOT / ".secrets" / "gmail_token.json"

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


def _group_by_account(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_account: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_account.setdefault(r.get("owner_email") or "", []).append(r)
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


def collect_drive_candidates_workspace(
    *, sa_file: Path, admin_email: str, scan_cache: str, progress_log=_log,
) -> dict[str, list[dict[str, Any]]]:
    """Every user's My Drive + every Shared Drive (Domain-Wide Delegation)."""
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
    return _bucket_drive_rows(rows)


def collect_drive_candidates_single(*, service, scan_cache: str, progress_log=_log) -> dict[str, list[dict[str, Any]]]:
    """Just the signed-in account's own My Drive + Shared Drives it can see."""
    shared = list_shared_drives(service)
    progress_log(f"[drive] {len(shared)} Shared Drive(s) visible to this account")
    rows = walk_entire_workspace(
        service,
        include_my_drive=True,
        include_shared_drives=True,
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


def collect_gmail_candidates(
    *,
    get_user_service: Callable[[str], Any],
    users: list[str],
    gmail_query: str,
    scan_cache_dir: Path,
    progress_log=_log,
) -> list[dict[str, Any]]:
    by_thread: dict[tuple[str, str], dict[str, Any]] = {}
    for email in users:
        svc = get_user_service(email)
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
    _log(f"[mode] {'workspace (Domain-Wide Delegation)' if workspace_mode else 'my Drive + Gmail only'}")

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
        if workspace_mode:
            buckets = collect_drive_candidates_workspace(
                sa_file=sa_file, admin_email=admin_email, scan_cache=drive_scan_cache, progress_log=_log,
            )
        else:
            buckets = collect_drive_candidates_single(
                service=my_drive_service(), scan_cache=drive_scan_cache, progress_log=_log,
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
            get_user_service=_gmail_service_for, users=gmail_users, gmail_query=args.gmail_query,
            scan_cache_dir=scan_cache_dir, progress_log=_log,
        )
        gmail_selected, gmail_total = greedy_fill(gmail_candidates, gmail_cap_bytes)
        _log(f"[gmail] selected {len(gmail_selected)}/{len(gmail_candidates)} thread(s), "
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
