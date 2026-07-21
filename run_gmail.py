"""Entry point for Gmail inventory scans.

Usage examples:

  # Single user via OAuth (prompts browser login once):
  python run_gmail.py --out out/gmail_inventory.xlsx

  # Single specific user via OAuth:
  python run_gmail.py --user alice@company.com --out out/gmail_inventory.xlsx

  # All workspace users via service account (requires DWD setup):
  python run_gmail.py --all-users --out out/workspace_gmail_inventory.xlsx

  # Limit to recent mail:
  python run_gmail.py --all-users --query "after:2024/01/01" --out out/gmail_inventory.xlsx

Resuming: re-run the exact same command — progress is checkpointed automatically.
"""

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
    SCOPES_GMAIL,
    build_admin_service,
    build_gmail_service,
    default_client_secrets_path,
    default_service_account_path,
    default_token_path,
    get_credentials,
    get_service_account_credentials,
)
from gdrive.scan import list_workspace_users
from gmail.pipeline import build_inventory_from_gmail
from llm_provider import default_llm_model

# Gmail OAuth token is kept separately from the Drive token (different scopes).
_GMAIL_TOKEN = Path(_ROOT / ".secrets" / "gmail_token.json")


def _log(msg: str) -> None:
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build an inventory XLSX from Gmail mailboxes.")

    src = p.add_mutually_exclusive_group()
    src.add_argument("--user", default="", metavar="EMAIL",
                     help="Scan a specific user's Gmail (OAuth or SA). Defaults to the OAuth account.")
    src.add_argument("--all-users", action="store_true",
                     help="Scan all workspace users' Gmail (requires --service-account).")

    p.add_argument("--out", default="out/gmail_inventory.xlsx", help="Output .xlsx path")
    p.add_argument("--query", default="", metavar="QUERY",
                   help="Gmail search query to filter messages (e.g. 'after:2024/01/01')")
    p.add_argument("--max-messages", type=int, default=0,
                   help="Cap messages per user (0=unlimited)")
    p.add_argument("--pass1-model", default="", help="Fast pass-1 model (default: haiku/gpt-4o-mini)")
    p.add_argument("--pass2-model", default=default_llm_model(), help="Full pass-2 model")
    p.add_argument("--workers", type=int, default=8, help="Parallel classify workers (default: 8)")

    sa_default = str(default_service_account_path()) if default_service_account_path() else ""
    p.add_argument("--service-account", default=sa_default, metavar="FILE",
                   help="Path to service account JSON key (for --all-users or impersonated single user)")
    p.add_argument("--admin-email", default="", metavar="EMAIL",
                   help="Admin email for service account impersonation (or GOOGLE_ADMIN_EMAIL env var)")

    args = p.parse_args(argv)

    out_path = str((_ROOT / args.out).resolve())
    max_messages = None if args.max_messages == 0 else args.max_messages
    use_sa = bool(args.service_account)

    if use_sa:
        sa_file = Path(args.service_account).expanduser().resolve()
        if not sa_file.is_file():
            print(f"ERROR: service account file not found: {sa_file}", flush=True)
            return 1
        admin_email = (
            args.admin_email
            or os.environ.get("GOOGLE_ADMIN_EMAIL", "").strip()
        )
        if not admin_email and args.all_users:
            print("ERROR: --admin-email (or GOOGLE_ADMIN_EMAIL) is required with --all-users", flush=True)
            return 1

    # ---- Determine users to scan ----
    if args.all_users:
        if not use_sa:
            print("ERROR: --all-users requires --service-account", flush=True)
            return 1
        _log("enumerating workspace users via Admin SDK ...")
        admin_sdk_creds = get_service_account_credentials(sa_file, admin_email, SCOPES_ADMIN_USERS)
        admin_svc = build_admin_service(admin_sdk_creds)
        users = list_workspace_users(admin_svc)
        _log(f"found {len(users)} user(s) in domain")
    elif args.user:
        users = [args.user]
        _log(f"scanning single user: {args.user!r}")
    else:
        # OAuth: figure out who logged in
        if use_sa and admin_email:
            users = [admin_email]
        else:
            # OAuth flow — user will be whoever authenticates
            users = ["me"]  # Gmail API accepts "me" for the authed user

    # ---- Build get_user_service callable ----
    if use_sa:
        def get_user_service(email: str):
            creds = get_service_account_credentials(sa_file, email, SCOPES_GMAIL)
            return build_gmail_service(creds)
    else:
        # OAuth — single user
        gmail_token = (
            Path(os.environ.get("GMAIL_TOKEN_PATH", "")).expanduser().resolve()
            if os.environ.get("GMAIL_TOKEN_PATH") else _GMAIL_TOKEN
        )
        oauth_creds = get_credentials(
            client_secrets=default_client_secrets_path(),
            token_path=gmail_token,
            full_read_scope=False,   # use SCOPES_GMAIL via a monkey-patch below
            login_only=False,
        )
        # get_credentials uses drive scopes; for Gmail we need a separate token.
        # Re-run with Gmail scopes explicitly.
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        import json as _json

        gmail_scopes = list(SCOPES_GMAIL)
        _creds = None
        if gmail_token.is_file():
            from google.oauth2.credentials import Credentials as _Creds
            _creds = _Creds.from_authorized_user_file(str(gmail_token), gmail_scopes)
        if _creds and _creds.valid:
            pass
        elif _creds and _creds.expired and _creds.refresh_token:
            _creds.refresh(Request())
            gmail_token.parent.mkdir(parents=True, exist_ok=True)
            gmail_token.write_text(_creds.to_json(), encoding="utf-8")
        else:
            cs = default_client_secrets_path()
            if not cs.is_file():
                print(
                    "ERROR: No OAuth client secrets found. Run python setup.py first.",
                    flush=True,
                )
                return 1
            flow = InstalledAppFlow.from_client_secrets_file(str(cs), gmail_scopes)
            _creds = flow.run_local_server(port=0, prompt="consent", open_browser=True)
            gmail_token.parent.mkdir(parents=True, exist_ok=True)
            gmail_token.write_text(_creds.to_json(), encoding="utf-8")
            _log("Gmail OAuth token saved.")

        _gmail_service = build_gmail_service(_creds)

        # Resolve "me" to the actual address for labelling
        if "me" in users:
            try:
                profile = _gmail_service.users().getProfile(userId="me").execute()
                actual_email = profile.get("emailAddress") or "me"
                users = [actual_email]
                _log(f"authenticated as {actual_email!r}")
            except Exception:
                pass

        def get_user_service(_email: str):  # noqa: ARG001
            return _gmail_service

    _log(f"pass1={args.pass1_model or '(auto)'}  pass2={args.pass2_model}  workers={args.workers}")
    _log(f"query={args.query!r}  max_messages={max_messages}  out={out_path}")

    t0 = time.time()
    result = build_inventory_from_gmail(
        get_user_service=get_user_service,
        users=users,
        output_path=out_path,
        pass1_model=args.pass1_model or None,
        pass2_model=args.pass2_model,
        gmail_query=args.query,
        max_messages_per_user=max_messages,
        workers=args.workers,
        progress_log=_log,
    )

    _log(f"finished in {(time.time() - t0) / 60:.1f} min")
    _log(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
