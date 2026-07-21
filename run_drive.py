from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import env_loader  # noqa: F401

from gdrive.credentials import (
    SCOPES_ADMIN_USERS,
    SCOPES_READONLY,
    build_admin_service,
    build_drive_service,
    default_client_secrets_path,
    default_service_account_path,
    default_token_path,
    get_credentials,
    get_service_account_credentials,
)
from gdrive.pipeline_1tb import build_inventory_from_drive_1tb
from gdrive.scan import (
    list_shared_drives,
    list_workspace_users,
    walk_all_user_my_drives,
    walk_entire_workspace,
)
from llm_provider import default_llm_model


def _log(msg: str) -> None:
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--folder-id", default="root", help="Drive folder ID or URL (default: My Drive root)")
    g.add_argument("--all-drives", action="store_true", help="Scan entire Google Workspace: My Drive + all Shared Drives")
    p.add_argument("--out", default="out/drive_1tb_inventory.xlsx", help="Output .xlsx path")
    p.add_argument("--pass1-model", default="", help="Fast model for pass 1 (default: haiku/gpt-4o-mini)")
    p.add_argument("--pass2-model", default=default_llm_model(), help="Full model for pass 2")
    p.add_argument("--max-files", type=int, default=0, help="Cap file count (0=unlimited)")
    p.add_argument("--snippet-bytes", type=int, default=8192, help="Bytes to fetch per file (default: 8192; PDFs need ≥8KB to get past binary header)")
    p.add_argument("--workers", type=int, default=16, help="Parallel download workers (default: 16)")
    # Service account (Domain-Wide Delegation) — required for full workspace scan of all users' My Drives
    sa_default = str(default_service_account_path()) if default_service_account_path() else ""
    p.add_argument("--service-account", default=sa_default, metavar="FILE",
                   help="Path to service account JSON key (enables DWD workspace scan)")
    p.add_argument("--admin-email", default="", metavar="EMAIL",
                   help="Admin email to impersonate when using --service-account")
    args = p.parse_args(argv)

    out_path = str((_ROOT / args.out).resolve())
    scan_cache = str(Path(out_path).with_name(Path(out_path).stem + ".scan_cache.jsonl"))
    max_files = None if args.max_files == 0 else args.max_files

    use_sa = bool(args.service_account)

    if use_sa:
        sa_file = Path(args.service_account).expanduser().resolve()
        if not sa_file.is_file():
            print(f"ERROR: service account file not found: {sa_file}", flush=True)
            return 1
        admin_email = args.admin_email or os.environ.get("GOOGLE_ADMIN_EMAIL", "").strip()
        if not admin_email:
            print("ERROR: --admin-email (or GOOGLE_ADMIN_EMAIL env var) is required with --service-account", flush=True)
            return 1
        # Drive service impersonating the admin (used for Shared Drives + fallback)
        admin_drive_creds = get_service_account_credentials(sa_file, admin_email, SCOPES_READONLY)
        service = build_drive_service(admin_drive_creds)
        creds = admin_drive_creds
    else:
        creds = get_credentials(
            client_secrets=default_client_secrets_path(),
            token_path=default_token_path(),
            full_read_scope=True,
            login_only=False,
        )
        service = build_drive_service(creds)

    if args.all_drives:
        if use_sa:
            # Full workspace: enumerate every user via Admin SDK, scan their My Drive
            admin_sdk_creds = get_service_account_credentials(sa_file, admin_email, SCOPES_ADMIN_USERS)
            admin_svc = build_admin_service(admin_sdk_creds)
            users = list_workspace_users(admin_svc)
            _log(f"[workspace] {len(users)} user(s) found in domain")

            shared = list_shared_drives(service, use_domain_admin_access=True)
            _log(f"[workspace] {len(shared)} Shared Drive(s)")
            for d in shared:
                _log(f"  shared drive: {d.get('name')!r} ({d['id']})")
            _log(f"out={out_path} workers={args.workers}")

            def _user_service(email: str):
                u_creds = get_service_account_credentials(sa_file, email, SCOPES_READONLY)
                return build_drive_service(u_creds)

            scan_rows = walk_all_user_my_drives(
                _user_service,
                users,
                shared_drives=shared,
                shared_drive_service=service,
                max_files=max_files,
                progress_log=_log,
                scan_cache_path=scan_cache,
            )
        else:
            shared = list_shared_drives(service)
            _log(f"starting full workspace scan: My Drive + {len(shared)} Shared Drive(s)")
            for d in shared:
                _log(f"  shared drive: {d.get('name')!r} ({d['id']})")
            _log(f"out={out_path} workers={args.workers}")

            scan_rows = walk_entire_workspace(
                service,
                include_my_drive=True,
                include_shared_drives=True,
                max_files=max_files,
                progress_log=_log,
                scan_cache_path=scan_cache,
            )

        _log(f"walk complete: {len(scan_rows)} files")
        folder_id = None  # signal to pipeline that scan_rows is pre-built
    else:
        _log(f"starting drive ingest folder_id={args.folder_id!r} out={out_path} workers={args.workers}")
        scan_rows = None
        folder_id = args.folder_id

    _log(f"pass1_model={args.pass1_model or '(auto)'} pass2_model={args.pass2_model}")
    _log(f"snippet_bytes={args.snippet_bytes} max_files={max_files}")

    t0 = time.time()
    result = build_inventory_from_drive_1tb(
        service=service,
        folder_id=folder_id or args.folder_id,
        output_path=out_path,
        pass1_model=args.pass1_model or None,
        pass2_model=args.pass2_model,
        max_files=max_files,
        scan_cache_path=scan_cache,
        snippet_export_bytes=args.snippet_bytes,
        progress_log=_log,
        progress_every=500,
        ingest_log_every=200,
        workers=args.workers,
        creds=creds,
        scan_rows=scan_rows,
    )

    elapsed = time.time() - t0
    _log(f"finished in {elapsed/60:.1f} min")
    _log(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
