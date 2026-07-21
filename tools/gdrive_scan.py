#!/usr/bin/env python3
"""List all files under a Google Drive folder (recursive) using OAuth (user).

First-time setup saves a refresh token to ``.secrets/google_drive_token.json``
(or ``GOOGLE_OAUTH_TOKEN_PATH``).

Examples::

    # One-time browser login (writes token file)
    python tools/gdrive_scan.py --login-only

    # Scan a folder (ID or full drive.google.com URL) → CSV
    python tools/gdrive_scan.py --folder-id 1abc...xyz --out out/drive_inventory.csv

    # Same scan → multi-tab ``.xlsx`` (Inventory / Overview / Evidence / Sample Files layout
    # from ``excel_io.write_company_inventory_workbook``). Bucket columns are empty until
    # you run the segmenter / enrich pipeline on a dump-derived workbook.
    # Also writes ``<stem>_scan_rows.csv`` and appends sheet ``DriveScanRaw`` (file ids + mime)
    # so ``tools/gdrive_build_inventory.py --from-csv`` / ``--from-xlsx`` can skip a re-walk.

    # Include synthetic rows for subfolders (default: files + shortcuts only)
    python tools/gdrive_scan.py --folder-id 1abc...xyz --out out/drive.xlsx --include-folders
"""

from __future__ import annotations

import argparse
import datetime
import sys
import traceback
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import env_loader  # noqa: F401

from gdrive.credentials import (  # noqa: E402
    build_drive_service,
    default_client_secrets_path,
    get_credentials,
)
from excel_io import BUCKETS, write_company_inventory_workbook  # noqa: E402
from gdrive.scan import normalize_folder_id, walk_drive_folder  # noqa: E402

_MIME_EXT: dict[str, str] = {
    "application/vnd.google-apps.document": "gdoc",
    "application/vnd.google-apps.spreadsheet": "gsheet",
    "application/vnd.google-apps.presentation": "gslides",
    "application/vnd.google-apps.folder": "",
    "application/vnd.google-apps.shortcut": "",
}


def _ext_from_mime(mime: str) -> str:
    m = (mime or "").strip().lower()
    return _MIME_EXT.get(m, "")


def _placeholder_inventory_df():
    import pandas as pd

    rows = []
    for _, name in BUCKETS:
        rows.append(
            {
                "Category": name,
                "What it includes": "— (Drive listing only — no bucket classification)",
                "Where to find it": "—",
                "How hard to pull": "—",
            }
        )
    return pd.DataFrame(rows)


def _drive_rows_to_evidence_internal(rows: list[dict]):
    import math
    import pandas as pd

    out: list[dict] = []
    seq = 0
    for r in rows:
        if r.get("is_folder"):
            continue
        seq += 1
        name = str(r.get("name") or "")
        ext = Path(name).suffix.lower().lstrip(".")
        if not ext:
            ext = _ext_from_mime(str(r.get("mime_type") or ""))
        sz = r.get("size_bytes")
        size_b = None
        if sz is not None and str(sz) != "":
            try:
                x = float(sz)
                if math.isfinite(x) and x >= 0:
                    size_b = int(x)
            except (TypeError, ValueError, OverflowError):
                size_b = None
        link = str(r.get("web_view_link") or "")
        out.append(
            {
                "artifact_id": seq,
                "filename": name,
                "extension": ext,
                "path": str(r.get("path") or "").replace("\\", "/"),
                "snippet": link[:800],
                "content_summary": "",
                "pii_flag": "",
                "source_guess": "Google Drive",
                "size_bytes": size_b,
                "word_count": 0,
                "token_count": 0,
                "page_count": 0,
                "modality": "",
                "content_type": "",
                "quality_tier": "",
                "content_inventory": "",
                "bucket_number": "",
                "bucket_name": "",
                "sub_category": "",
                "confidence": "",
                "rationale": "",
            }
        )
    return pd.DataFrame(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--login-only",
        action="store_true",
        help="Run browser OAuth only and save token; do not scan.",
    )
    p.add_argument(
        "--client-secrets",
        type=Path,
        default=None,
        help="OAuth client JSON (Desktop). Default: GOOGLE_OAUTH_CLIENT_SECRETS or .secrets/google_oauth_client.json",
    )
    p.add_argument(
        "--token",
        type=Path,
        default=None,
        help="Token JSON written after login. Default: GOOGLE_OAUTH_TOKEN_PATH or .secrets/google_drive_token.json",
    )
    p.add_argument(
        "--full-read-scope",
        action="store_true",
        help="Request drive.readonly instead of drive.metadata.readonly (needed to download file bytes later).",
    )
    p.add_argument(
        "--folder-id",
        type=str,
        default="",
        help="Drive folder ID or folder URL (required unless --login-only).",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("out/drive_inventory.csv"),
        help="Output path: .csv = raw Drive columns; .xlsx = company workbook layout (Evidence tabs, empty buckets)",
    )
    p.add_argument(
        "--include-folders",
        action="store_true",
        help="Emit one row per subfolder in addition to files.",
    )
    p.add_argument(
        "--max-files",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N file rows (non-folders, including shortcuts). Useful for smoke tests.",
    )
    p.add_argument(
        "--no-open-browser",
        action="store_true",
        help="OAuth only: print the consent URL instead of opening a new tab (use an existing browser window).",
    )
    p.add_argument(
        "--log-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Live log path (truncated at start of this run): timestamps + progress (also printed).",
    )
    p.add_argument(
        "--progress-every",
        type=int,
        default=500,
        metavar="N",
        help="With --log-file, log a progress line every N file rows (default 500).",
    )
    args = p.parse_args()

    client = args.client_secrets or default_client_secrets_path()
    token = args.token
    try:
        creds = get_credentials(
            client_secrets=client,
            token_path=token,
            full_read_scope=args.full_read_scope,
            login_only=args.login_only,
            open_browser=False if args.no_open_browser else None,
        )
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    if args.login_only:
        return 0

    fid = (args.folder_id or "").strip()
    if not fid:
        print("Missing --folder-id (or use --login-only).", file=sys.stderr)
        return 1

    if args.progress_every < 1:
        print("--progress-every must be >= 1", file=sys.stderr)
        return 1

    log_fp = None
    progress_log = None
    if args.log_file is not None:
        log_path = args.log_file.expanduser().resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fp = log_path.open("w", encoding="utf-8", buffering=1)

        def progress_log(msg: str) -> None:
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            line = f"[{ts}] {msg}\n"
            log_fp.write(line)
            log_fp.flush()
            print(msg, flush=True)

    out_target = args.out.expanduser().resolve()

    try:
        service = build_drive_service(creds)
        if progress_log:
            progress_log(
                f"starting walk folder_id={normalize_folder_id(fid)!r} "
                f"progress_every={args.progress_every} out={out_target}"
            )
        rows = walk_drive_folder(
            service,
            normalize_folder_id(fid),
            include_folders=args.include_folders,
            max_files=args.max_files,
            progress_log=progress_log,
            progress_every=args.progress_every,
        )
        if not rows:
            print("No files found (empty folder or no access).", file=sys.stderr)
            if progress_log:
                progress_log("walk finished: 0 rows (empty or no access)")
            return 2

        out = out_target
        out.parent.mkdir(parents=True, exist_ok=True)

        import pandas as pd

        suf = out.suffix.lower()
        if progress_log:
            progress_log(f"walk done raw_rows={len(rows)} writing {suf or '(default csv)'} …")

        if suf == ".xlsx":
            ev = _drive_rows_to_evidence_internal(rows)
            if len(ev) == 0:
                print("No file rows to write (only folders?).", file=sys.stderr)
                if progress_log:
                    progress_log("abort: no file rows for workbook")
                return 2
            try:
                write_company_inventory_workbook(
                    inventory_df=_placeholder_inventory_df(),
                    evidence_df=ev,
                    output_path=str(out),
                )
                # Same walk: persist API ids + mime so quality ingest can ``--from-xlsx`` / ``--from-csv``
                # without re-walking (the human Evidence tab omits ``drive_file_id``).
                raw_df = pd.DataFrame(rows)
                sidecar_csv = out.with_name(out.stem + "_scan_rows.csv")
                raw_df.to_csv(sidecar_csv, index=False)
                if progress_log:
                    progress_log(f"wrote scan row sidecar for ingest resume: {sidecar_csv.name}")
                try:
                    with pd.ExcelWriter(
                        out,
                        engine="openpyxl",
                        mode="a",
                        if_sheet_exists="replace",
                    ) as xw:
                        raw_df.to_excel(xw, sheet_name="DriveScanRaw", index=False)
                    if progress_log:
                        progress_log("appended sheet DriveScanRaw (drive_file_id, mime_type, …)")
                except Exception as se:
                    warn = (
                        f"Could not append DriveScanRaw sheet ({type(se).__name__}: {se}); "
                        f"use sidecar CSV for --from-csv: {sidecar_csv.name}"
                    )
                    print(warn, file=sys.stderr)
                    if progress_log:
                        progress_log(warn)
            except Exception as we:
                ckpt = out.with_name(out.stem + "_rows_checkpoint.csv")
                pd.DataFrame(rows).to_csv(ckpt, index=False)
                msg_ck = (
                    f"XLSX write failed ({type(we).__name__}: {we}). "
                    f"Raw Drive rows saved to {ckpt}"
                )
                print(msg_ck, file=sys.stderr)
                if progress_log:
                    progress_log(msg_ck)
                if log_fp:
                    traceback.print_exc(file=log_fp)
                    log_fp.flush()
                traceback.print_exc()
                return 4
        else:
            df = pd.DataFrame(rows)
            if suf != ".csv":
                out = out.with_suffix(".csv")
            df.to_csv(out, index=False)

        note = f" (capped at --max-files {args.max_files})" if args.max_files else ""
        if suf == ".xlsx":
            msg = f"Wrote workbook with {len(ev)} Evidence file row(s) to {out}{note}"
        else:
            msg = f"Wrote {len(rows)} row(s) to {out}{note}"
        print(msg)
        if progress_log:
            progress_log(msg)
        return 0
    except Exception as e:
        if progress_log:
            progress_log(f"ERROR {type(e).__name__}: {e}")
        if log_fp:
            traceback.print_exc(file=log_fp)
            log_fp.flush()
        print(f"Drive scan failed: {e}", file=sys.stderr)
        traceback.print_exc()
        return 3
    finally:
        if log_fp is not None:
            log_fp.close()


if __name__ == "__main__":
    raise SystemExit(main())
