#!/usr/bin/env python3
"""Insert selected Gmail threads (from a build_quality_sample.py manifest) into the
signed-in user's own Gmail.

Whole threads only — every message in a thread is inserted, never a subset, so Gmail's
own reply-chain headers re-thread them correctly in the destination inbox.

Flow:
  1. Read raw RFC822 bytes for each message in a selected thread from the SOURCE
     mailbox, impersonating that thread's owner via the service account (Domain-Wide
     Delegation) — read-only, no changes made to the source.
  2. Client signs in with **their** Google account (OAuth, gmail.insert scope).
  3. Insert each message into their mailbox via ``users.messages.insert``.

Examples:

  python tools/export_ai_labs_gmail_threads.py --service-account .secrets/service_account.json --dry-run
  python tools/export_ai_labs_gmail_threads.py --service-account .secrets/service_account.json --limit 20
"""

from __future__ import annotations

import argparse
import base64
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import env_loader  # noqa: F401

from googleapiclient.errors import HttpError

from gdrive.credentials import (
    SCOPES_GMAIL,
    SCOPES_GMAIL_INSERT,
    build_gmail_service,
    default_client_secrets_path,
    get_service_account_credentials,
)
from gmail.fetch import fetch_thread_raw
from tools.export_ai_labs_samples import _append_done, _load_done

_INSERT_TOKEN = _ROOT / ".secrets" / "google_gmail_insert_token.json"
_DEFAULT_MANIFEST = _ROOT / "out" / "quality_sample_manifest.json"


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def load_thread_manifest(path: Path) -> list[dict]:
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("gmail_threads") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"manifest must contain a 'gmail_threads' list: {path}")
    return rows


def _get_insert_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_secrets = default_client_secrets_path()
    token_path = _INSERT_TOKEN

    creds = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), list(SCOPES_GMAIL_INSERT))
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception:
            creds = None

    if not client_secrets.is_file():
        raise FileNotFoundError(
            f"OAuth client secrets not found: {client_secrets}\n"
            "Run: python setup.py  (Google Drive step) or place google_oauth_client.json in .secrets/"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), list(SCOPES_GMAIL_INSERT))
    creds = flow.run_local_server(port=0, prompt="consent")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    _log(f"saved Gmail insert OAuth token → {token_path}")
    return creds


def _insert_message(dest_service, raw_bytes: bytes) -> str:
    body = {"raw": base64.urlsafe_b64encode(raw_bytes).decode("ascii"), "labelIds": ["INBOX"]}
    created = dest_service.users().messages().insert(
        userId="me", body=body, internalDateSource="dateHeader",
    ).execute()
    return created["id"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manifest", type=Path, default=_DEFAULT_MANIFEST,
                   help="Manifest JSON with a 'gmail_threads' list (default: out/quality_sample_manifest.json)")
    p.add_argument("--service-account", required=True, metavar="FILE",
                   help="Service account JSON key (Domain-Wide Delegation, for reading source threads)")
    p.add_argument("--checkpoint", type=Path, default=_ROOT / "out" / "ai_labs_gmail_threads.checkpoint.jsonl")
    p.add_argument("--dry-run", action="store_true", help="List matches only; no inserts")
    p.add_argument("--limit", type=int, default=0, help="Cap threads to transfer (0=all)")
    args = p.parse_args(argv)

    manifest_path = args.manifest.expanduser().resolve()
    if not manifest_path.is_file():
        print(f"ERROR: thread manifest not found: {manifest_path}", flush=True)
        return 1
    sa_file = Path(args.service_account).expanduser().resolve()
    if not sa_file.is_file():
        print(f"ERROR: service account file not found: {sa_file}", flush=True)
        return 1

    rows = load_thread_manifest(manifest_path)
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]
    _log(f"loaded {len(rows)} thread(s) from {manifest_path.name}")

    if args.dry_run:
        for row in rows[:40]:
            _log(f"DRY {row['user_email']} thread={row['thread_id']} "
                 f"{len(row.get('message_ids', []))} msg(s) {row.get('subject', '')!r}")
        if len(rows) > 40:
            _log(f"... and {len(rows) - 40} more")
        return 0

    ckpt = args.checkpoint.expanduser().resolve()
    done = _load_done(ckpt)
    _log(f"checkpoint resume: {len(done)} already done")

    dest_creds = _get_insert_credentials()
    dest_service = build_gmail_service(dest_creds)

    ok = skip = fail = 0
    for i, row in enumerate(rows, start=1):
        thread_key = f"{row['user_email']}:{row['thread_id']}"
        if thread_key in done:
            skip += 1
            continue
        try:
            src_creds = get_service_account_credentials(sa_file, row["user_email"], SCOPES_GMAIL)
            src_service = build_gmail_service(src_creds)
            raw_messages = fetch_thread_raw(src_service, row["thread_id"], user_id=row["user_email"])
            inserted_ids = [_insert_message(dest_service, raw) for raw in raw_messages]
            _append_done(ckpt, {
                "file_id": thread_key, "thread_id": row["thread_id"], "user_email": row["user_email"],
                "inserted_ids": inserted_ids, "status": "ok",
            })
            ok += 1
            if ok % 25 == 0 or i == len(rows):
                _log(f"progress i={i}/{len(rows)} ok={ok} skip={skip} fail={fail} "
                     f"last={row.get('subject', '')[:60]!r}")
        except (HttpError, RuntimeError, OSError) as exc:
            fail += 1
            _append_done(ckpt, {
                "file_id": thread_key, "thread_id": row["thread_id"], "user_email": row["user_email"],
                "status": "error", "error": str(exc)[:500],
            })
            _log(f"FAIL {row.get('subject', '')[:60]!r} — {exc}")

    _log(f"finished ok={ok} skipped={skip} failed={fail}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
