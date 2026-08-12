#!/usr/bin/env python3
"""Build an AI Labs sample set in the signed-in user's own Google Drive.

Uses the bundled 1200-file balanced sample list (no Excel needed on the client):

  data/ai_labs_1200_balanced_sample.json

Flow:
  1. Client signs in with **their** Google account
  2. Script creates a new My Drive folder (e.g. AI Labs Sample Set (date))
  3. Copies each listed file into bucket subfolders via Drive ``files.copy``

Examples:

  python tools/export_ai_labs_samples.py --dry-run
  python tools/export_ai_labs_samples.py
  python tools/export_ai_labs_samples.py --folder-name "Goldsetu AI Labs Samples"
  python tools/export_ai_labs_samples.py --materialize --limit 50
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import env_loader  # noqa: F401

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from gdrive.credentials import build_drive_service, default_client_secrets_path
from gdrive.fetch import call_with_retry, fetch_drive_file_to_path, suggested_local_suffix
from gdrive.scan import FOLDER_MIME, normalize_folder_id

_SCOPES_RW = ("https://www.googleapis.com/auth/drive",)
_RW_TOKEN = _ROOT / ".secrets" / "google_drive_rw_token.json"
DEFAULT_FOLDER_NAME = "AI Labs Sample Set"
DEFAULT_MANIFEST = _ROOT / "data" / "ai_labs_1200_balanced_sample.json"


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def load_manifest(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files") if isinstance(data, dict) else data
    if not isinstance(files, list):
        raise ValueError(f"manifest must be a list or {{files: [...]}}: {path}")
    out: list[dict] = []
    seen: set[str] = set()
    for row in files:
        fid = str(row.get("file_id") or "").strip()
        if not fid or fid in seen:
            continue
        seen.add(fid)
        out.append(
            {
                "file_id": fid,
                "name": str(row.get("name") or fid),
                "path": str(row.get("path") or ""),
                "bucket": str(row.get("bucket") or "Uncategorized").strip() or "Uncategorized",
            }
        )
    return out


def _load_done(path: Path) -> set[str]:
    done: set[str] = set()
    if not path.is_file():
        return done
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            fid = row.get("file_id")
            if fid and row.get("status") == "ok":
                done.add(str(fid))
    return done


def _append_done(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _get_rw_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_secrets = default_client_secrets_path()
    token_path = Path(
        __import__("os").environ.get("GOOGLE_DRIVE_RW_TOKEN_PATH") or _RW_TOKEN
    ).expanduser().resolve()

    creds = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), list(_SCOPES_RW))
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
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), list(_SCOPES_RW))
    creds = flow.run_local_server(port=0, prompt="consent")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    _log(f"saved RW OAuth token → {token_path}")
    return creds


def _ensure_child_folder(service, parent_id: str, name: str, cache: dict[str, str]) -> str:
    if name in cache:
        return cache[name]
    safe = name.replace("'", "\\'")
    q = (
        f"'{parent_id}' in parents and name = '{safe}' "
        f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    resp = call_with_retry(
        service,
        lambda: (
            service.files()
            .list(
                q=q, spaces="drive", fields="files(id,name)", pageSize=10,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            )
            .execute()
        ),
    )
    files = resp.get("files") or []
    if files:
        cache[name] = files[0]["id"]
        return cache[name]
    created = call_with_retry(
        service,
        lambda: (
            service.files()
            .create(
                body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                fields="id", supportsAllDrives=True,
            )
            .execute()
        ),
    )
    cache[name] = created["id"]
    _log(f"created folder {name!r} → {cache[name]}")
    return cache[name]


def _create_root_folder(service, name: str, parent_id: str = "root") -> str:
    created = call_with_retry(
        service,
        lambda: (
            service.files()
            .create(
                body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                fields="id", supportsAllDrives=True,
            )
            .execute()
        ),
    )
    return created["id"]


def _load_dest_meta(path: Path) -> dict | None:
    meta_path = path.with_suffix(path.suffix + ".dest.json")
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_dest_meta(path: Path, dest_id: str, folder_name: str) -> None:
    meta_path = path.with_suffix(path.suffix + ".dest.json")
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "dest_folder_id": dest_id,
                "folder_name": folder_name,
                "url": f"https://drive.google.com/drive/folders/{dest_id}",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _drive_copy(service, file_id: str, dest_parent: str, name: str, *, max_retries: int = 8) -> str:
    copied = call_with_retry(
        service,
        lambda: (
            service.files()
            .copy(
                fileId=file_id,
                body={"name": name, "parents": [dest_parent]},
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        ),
        max_retries=max_retries,
    )
    return copied["id"]


def _materialize_upload(service, meta: dict, dest_parent: str, tmp_dir: Path) -> str:
    fid = meta["id"]
    name = meta.get("name") or fid
    mime = meta.get("mimeType") or "application/octet-stream"
    suffix = suggested_local_suffix(mime, name)
    local = tmp_dir / f"{fid}{suffix}"
    err = fetch_drive_file_to_path(
        service, file_id=fid, mime_type=mime, display_name=name, dest=local, max_export_bytes=None,
    )
    if err:
        raise RuntimeError(err)
    upload_name = name if Path(name).suffix or not suffix else f"{name}{suffix}"
    media = MediaFileUpload(
        str(local),
        mimetype=None if mime.startswith("application/vnd.google-apps") else mime,
        resumable=True,
    )
    created = call_with_retry(
        service,
        lambda: (
            service.files()
            .create(
                body={"name": upload_name, "parents": [dest_parent]},
                media_body=media, fields="id", supportsAllDrives=True,
            )
            .execute()
        ),
    )
    try:
        local.unlink(missing_ok=True)
    except OSError:
        pass
    return created["id"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Bundled sample list JSON (default: data/ai_labs_1200_balanced_sample.json)",
    )
    p.add_argument(
        "--folder-name",
        default=DEFAULT_FOLDER_NAME,
        help=f"New My Drive folder name (default: {DEFAULT_FOLDER_NAME!r}; date suffix added automatically)",
    )
    p.add_argument(
        "--dest-folder-id",
        default="",
        help="Optional existing folder ID/URL instead of creating a new one",
    )
    p.add_argument("--checkpoint", type=Path, default=_ROOT / "out" / "ai_labs_samples.checkpoint.jsonl")
    p.add_argument("--dry-run", action="store_true", help="List matches only; no Drive writes")
    p.add_argument("--limit", type=int, default=0, help="Cap files to transfer (0=all)")
    p.add_argument(
        "--materialize",
        action="store_true",
        help="Download to temp then upload (slow). Default: Drive copy.",
    )
    p.add_argument("--bucket", action="append", default=[], help="Only these bucket names (repeatable)")
    args = p.parse_args(argv)

    manifest = args.manifest.expanduser().resolve()
    if not manifest.is_file():
        print(f"ERROR: sample manifest not found: {manifest}", flush=True)
        return 1

    rows = load_manifest(manifest)
    if args.bucket:
        rows = [r for r in rows if r["bucket"] in args.bucket]
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    by_bucket: dict[str, int] = {}
    for row in rows:
        by_bucket[row["bucket"]] = by_bucket.get(row["bucket"], 0) + 1
    _log(f"loaded {len(rows)} file(s) from {manifest.name}")
    for b, n in sorted(by_bucket.items(), key=lambda x: (-x[1], x[0])):
        _log(f"  {b}: {n}")

    if args.dry_run:
        folder_name = args.folder_name
        if folder_name == DEFAULT_FOLDER_NAME:
            folder_name = f"{DEFAULT_FOLDER_NAME} ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"
        _log(f"DRY would create My Drive/{folder_name}/<bucket>/...")
        for row in rows[:40]:
            _log(f"DRY {row['bucket']} ← {row['path'] or row['name']}")
        if len(rows) > 40:
            _log(f"... and {len(rows) - 40} more")
        return 0

    ckpt = args.checkpoint.expanduser().resolve()
    done = _load_done(ckpt)
    _log(f"checkpoint resume: {len(done)} already done")

    creds = _get_rw_credentials()
    service = build_drive_service(creds)

    if args.dest_folder_id.strip():
        dest_id = normalize_folder_id(args.dest_folder_id)
        folder_name = args.folder_name
        _log(f"using existing dest folder id={dest_id}")
    else:
        meta = _load_dest_meta(ckpt)
        if meta and meta.get("dest_folder_id"):
            dest_id = str(meta["dest_folder_id"])
            folder_name = str(meta.get("folder_name") or args.folder_name)
            _log(f"resuming into existing folder {folder_name!r} → {dest_id}")
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            folder_name = args.folder_name
            if folder_name == DEFAULT_FOLDER_NAME:
                folder_name = f"{DEFAULT_FOLDER_NAME} ({stamp})"
            dest_id = _create_root_folder(service, folder_name, parent_id="root")
            _log(f"created My Drive folder {folder_name!r} → {dest_id}")

    _save_dest_meta(ckpt, dest_id, folder_name)
    folder_cache: dict[str, str] = {}

    ok = skip = fail = 0
    with tempfile.TemporaryDirectory(prefix="ai-labs-samples-") as td:
        tmp_dir = Path(td)
        for i, row in enumerate(rows, start=1):
            fid = row["file_id"]
            if fid in done:
                skip += 1
                continue
            try:
                parent = _ensure_child_folder(service, dest_id, row["bucket"], folder_cache)
                if args.materialize:
                    meta = call_with_retry(
                        service,
                        lambda: (
                            service.files()
                            .get(fileId=fid, fields="id,name,mimeType,size", supportsAllDrives=True)
                            .execute()
                        ),
                    )
                    new_id = _materialize_upload(service, meta, parent, tmp_dir)
                    mode = "upload"
                else:
                    new_id = _drive_copy(service, fid, parent, row["name"])
                    mode = "copy"
                _append_done(
                    ckpt,
                    {
                        "file_id": fid,
                        "new_id": new_id,
                        "bucket": row["bucket"],
                        "name": row["name"],
                        "path": row["path"],
                        "mode": mode,
                        "status": "ok",
                    },
                )
                ok += 1
                if ok % 25 == 0 or i == len(rows):
                    _log(
                        f"progress i={i}/{len(rows)} ok={ok} skip={skip} fail={fail} "
                        f"last={row['name'][:60]!r}"
                    )
            except (HttpError, RuntimeError, OSError) as exc:
                fail += 1
                _append_done(
                    ckpt,
                    {
                        "file_id": fid,
                        "bucket": row["bucket"],
                        "name": row["name"],
                        "path": row["path"],
                        "status": "error",
                        "error": str(exc)[:500],
                    },
                )
                _log(f"FAIL {row['name'][:60]!r} — {exc}")

    _log(f"finished ok={ok} skipped={skip} failed={fail}")
    _log(f"their folder: https://drive.google.com/drive/folders/{dest_id}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
