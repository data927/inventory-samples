"""Recursive Google Drive folder listing (metadata)."""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError

from gdrive.fetch import _RETRYABLE_NETWORK_ERRORS, _reset_http_connections, _sleep_backoff, _sleep_backoff_network

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

_FOLDER_ID_IN_URL = re.compile(r"/folders/([a-zA-Z0-9_-]+)")

# Fields requested from the Drive API per file entry.
# Includes owners and lastModifyingUser for attribution columns in the XLSX.
_LIST_FIELDS = (
    "nextPageToken, files("
    "id, name, mimeType, modifiedTime, size, "
    "webViewLink, md5Checksum, shortcutDetails, driveId, "
    "owners(emailAddress), lastModifyingUser(emailAddress)"
    ")"
)


def normalize_folder_id(raw: str) -> str:
    """Accept a raw folder ID or a ``drive.google.com`` folder URL.

    ``my-drive`` / ``root`` and common "My Drive" URLs resolve to the API root
    folder id ``root``.
    """
    s = (raw or "").strip()
    low = s.lower()
    if low in ("root", "my-drive", "mydrive"):
        return "root"
    if "drive.google.com" in low and "my-drive" in low:
        return "root"
    m = _FOLDER_ID_IN_URL.search(s)
    if m:
        return m.group(1)
    return s.split("?")[0].split("/")[0]


def _list_children(service, folder_id: str, *, max_retries: int = 8) -> list[dict[str, Any]]:
    q = f"'{folder_id}' in parents and trashed = false"
    out: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        for attempt in range(max_retries + 1):
            try:
                resp = (
                    service.files()
                    .list(
                        q=q,
                        pageSize=200,
                        fields=_LIST_FIELDS,
                        pageToken=page_token,
                        corpora="allDrives",
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute()
                )
                break
            except HttpError as e:
                if attempt >= max_retries or e.resp.status not in (403, 429, 500, 503):
                    raise
                _sleep_backoff(attempt, e)
            except _RETRYABLE_NETWORK_ERRORS:
                if attempt >= max_retries:
                    raise
                _reset_http_connections(service)
                _sleep_backoff_network(attempt)
        out.extend(resp.get("files") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _row_from_file(f: dict[str, Any], path: str, *, is_folder: bool, is_shortcut: bool) -> dict[str, Any]:
    """Build a scan row dict from a Drive API file object."""
    return {
        "drive_file_id": f.get("id"),
        "path": path,
        "name": f.get("name") or "",
        "mime_type": f.get("mimeType") or "",
        "modified_time": f.get("modifiedTime"),
        "size_bytes": f.get("size"),
        "web_view_link": f.get("webViewLink"),
        "md5_checksum": f.get("md5Checksum"),
        "is_folder": is_folder,
        "is_shortcut": is_shortcut,
        "shortcut_target_id": (f.get("shortcutDetails") or {}).get("targetId"),
        # Attribution: first owner email; empty string when API doesn't return it.
        "owner_email": ((f.get("owners") or [{}])[0] or {}).get("emailAddress") or "",
        "last_modified_by": (f.get("lastModifyingUser") or {}).get("emailAddress") or "",
    }


def _modified_before_cutoff(modified_time: str | None, cutoff: str) -> bool:
    """True if ``modified_time`` (RFC3339, e.g. from the Drive/Gmail APIs) is before
    ``cutoff`` (RFC3339). Missing/unparseable timestamps are never filtered out — we
    only exclude what we can positively confirm is too new."""
    if not modified_time:
        return True
    try:
        from datetime import datetime, timezone
        mt = datetime.fromisoformat(modified_time.replace("Z", "+00:00"))
        cut = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        if mt.tzinfo is None:
            mt = mt.replace(tzinfo=timezone.utc)
        if cut.tzinfo is None:
            cut = cut.replace(tzinfo=timezone.utc)
        return mt < cut
    except ValueError:
        return True


def walk_drive_folder(
    service,
    folder_id: str,
    *,
    path_prefix: str = "",
    include_folders: bool = False,
    max_files: int | None = None,
    modified_before: str | None = None,
    progress_log: Callable[[str], None] | None = None,
    progress_every: int = 500,
    scan_cache_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Depth-first walk from ``folder_id``; return flat rows (files and optionally folders).

    Each row: ``drive_file_id``, ``path``, ``name``, ``mime_type``, ``modified_time``,
    ``size_bytes``, ``web_view_link``, ``md5_checksum``, ``is_folder``, ``is_shortcut``,
    ``owner_email``, ``last_modified_by``.

    scan_cache_path
        If set, the complete walk result is saved to this JSONL file after the walk
        completes. On subsequent calls with the same path, the cache is loaded and
        the Drive API walk is skipped entirely — useful for large drives where the walk
        takes minutes and the downstream pipeline is what crashes/restarts.
        Delete the cache file to force a fresh walk.

    If ``max_files`` is set, stop after that many file rows (non-folder rows, including
    shortcuts). Folder rows from ``include_folders`` do not count toward the limit.

    If ``modified_before`` (RFC3339) is set, file/shortcut rows modified on or after it
    are skipped — but folders are **always** traversed regardless of their own
    ``modifiedTime``, so an old file inside a recently-touched folder is never missed.

    If ``progress_log`` is set, it is called every ``progress_every`` **file** rows
    (shortcuts and regular files; not folder-only rows) with a short status line.
    """
    # --- cache load ---
    cache_path: Path | None = Path(scan_cache_path) if scan_cache_path else None
    if cache_path is not None and cache_path.is_file() and cache_path.stat().st_size > 0:
        rows: list[dict[str, Any]] = []
        with cache_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
        if rows:
            if progress_log:
                progress_log(f"[scan_cache] loaded {len(rows)} rows from {cache_path}")
            if max_files is not None:
                file_rows = [r for r in rows if not r.get("is_folder")]
                folder_rows = [r for r in rows if r.get("is_folder")]
                rows = (folder_rows if include_folders else []) + file_rows[:max_files]
            return rows

    # --- live walk ---
    folder_id = normalize_folder_id(folder_id)
    rows = []
    file_rows = 0
    stop = False

    def _emit_progress(path_hint: str) -> None:
        if progress_log and file_rows > 0 and file_rows % progress_every == 0:
            progress_log(f"files={file_rows} last={path_hint[:240]}")

    def visit(fid: str, rel_path: str) -> None:
        nonlocal file_rows, stop
        if stop:
            return
        children = _list_children(service, fid)
        # Stable order: folders first, then files, by name
        children.sort(
            key=lambda f: (0 if f.get("mimeType") == FOLDER_MIME else 1, (f.get("name") or "").lower())
        )
        for f in children:
            if stop:
                break
            name = f.get("name") or ""
            mid = f.get("mimeType") or ""
            sub = f"{rel_path}/{name}".strip("/") if rel_path else name
            is_folder = mid == FOLDER_MIME
            is_shortcut = mid == SHORTCUT_MIME

            if is_shortcut:
                if modified_before is None or _modified_before_cutoff(f.get("modifiedTime"), modified_before):
                    rows.append(_row_from_file(f, sub, is_folder=False, is_shortcut=True))
                    file_rows += 1
                    _emit_progress(sub)
                    if max_files is not None and file_rows >= max_files:
                        stop = True
                continue

            if is_folder:
                if include_folders:
                    rows.append(_row_from_file(f, sub, is_folder=True, is_shortcut=False))
                visit(f["id"], sub)  # always recurse — a folder's own modifiedTime doesn't reflect its contents
                continue

            if modified_before is not None and not _modified_before_cutoff(f.get("modifiedTime"), modified_before):
                continue
            rows.append(_row_from_file(f, sub, is_folder=False, is_shortcut=False))
            file_rows += 1
            _emit_progress(sub)
            if max_files is not None and file_rows >= max_files:
                stop = True
                return

    visit(folder_id, path_prefix)

    # --- cache save (only when walk completed without a max_files cap, so the cache is complete) ---
    if cache_path is not None and not stop:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if progress_log:
            progress_log(f"[scan_cache] saved {len(rows)} rows to {cache_path}")

    return rows


def walk_my_drive_in_rounds(
    service,
    *,
    path_prefix: str = "",
    frontier: deque[tuple[str, str]] | list[tuple[str, str]] | None = None,
    folder_budget: int,
    modified_before: str | None = None,
    progress_log: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], deque[tuple[str, str]]]:
    """Breadth-first walk, visiting at most ``folder_budget`` folders this call.

    Returns ``(file_rows_found_this_round, remaining_frontier)``. An empty
    ``remaining_frontier`` means the walk is complete. Call again passing the returned
    frontier (as ``frontier=``) to continue in the next round from exactly where this
    call left off — nothing is re-visited or skipped across rounds.

    Purpose-built for round-based, early-stoppable scanning of one account at a time
    (see ``tools/build_quality_sample.py``) — unlike ``walk_drive_folder`` this has no
    ``scan_cache_path``/``max_files``/``include_folders`` support, since round-based
    scanning is fast by design and only ever needs file rows.

    ``modified_before`` (RFC3339), if given, excludes files modified on or after it —
    folders are still always traversed regardless of their own modified time, same as
    ``walk_drive_folder``.
    """
    if frontier is None:
        frontier = deque([("root", path_prefix)])
    elif not isinstance(frontier, deque):
        frontier = deque(frontier)

    rows: list[dict[str, Any]] = []
    visited = 0
    while frontier and visited < folder_budget:
        fid, rel_path = frontier.popleft()
        visited += 1
        children = _list_children(service, fid)
        children.sort(
            key=lambda f: (0 if f.get("mimeType") == FOLDER_MIME else 1, (f.get("name") or "").lower())
        )
        for f in children:
            name = f.get("name") or ""
            mid = f.get("mimeType") or ""
            sub = f"{rel_path}/{name}".strip("/") if rel_path else name
            is_folder = mid == FOLDER_MIME
            is_shortcut = mid == SHORTCUT_MIME

            if is_folder:
                frontier.append((f["id"], sub))  # always enqueued — a folder's own
                continue                          # modifiedTime doesn't reflect its contents

            if modified_before is not None and not _modified_before_cutoff(f.get("modifiedTime"), modified_before):
                continue
            rows.append(_row_from_file(f, sub, is_folder=False, is_shortcut=is_shortcut))

        if progress_log:
            progress_log(f"[round] visited {visited}/{folder_budget} folder(s) this round, "
                         f"{len(rows)} file(s) found, {len(frontier)} folder(s) queued")

    return rows, frontier


def list_shared_drives(service, *, use_domain_admin_access: bool = False) -> list[dict[str, Any]]:
    """Return all Shared Drives the user can access.

    Pass ``use_domain_admin_access=True`` when the service is authenticated as a
    super-admin (via service account DWD) to enumerate **all** drives in the domain,
    not just the ones the impersonated user is a member of.
    """
    drives: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        resp = (
            service.drives()
            .list(
                pageSize=100,
                fields="nextPageToken, drives(id, name)",
                pageToken=page_token,
                useDomainAdminAccess=use_domain_admin_access,
            )
            .execute()
        )
        drives.extend(resp.get("drives") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return drives


def list_workspace_users(admin_service) -> list[str]:
    """Return primary email addresses of all non-suspended users in the domain.

    Requires the Admin SDK Directory API and the service to be authenticated with
    ``https://www.googleapis.com/auth/admin.directory.user.readonly`` scope.
    """
    emails: list[str] = []
    page_token: str | None = None
    while True:
        resp = (
            admin_service.users()
            .list(
                customer="my_customer",
                maxResults=500,
                pageToken=page_token,
                fields="nextPageToken,users(primaryEmail,suspended)",
                orderBy="email",
            )
            .execute()
        )
        for u in resp.get("users") or []:
            if not u.get("suspended"):
                email = u.get("primaryEmail")
                if email:
                    emails.append(email)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return emails


def walk_all_user_my_drives(
    get_user_service,  # callable(email: str) -> Drive API service
    users: list[str],
    *,
    shared_drives: list[dict[str, Any]] | None = None,
    shared_drive_service=None,
    max_files: int | None = None,
    modified_before: str | None = None,
    progress_log: Callable[[str], None] | None = None,
    progress_every: int = 500,
    scan_cache_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Walk each user's My Drive via impersonation, then walk all Shared Drives.

    ``get_user_service(email)`` must return a Drive API service authenticated as
    that user (typically via service account DWD).

    ``shared_drives`` is a pre-fetched list from ``list_shared_drives``.
    ``shared_drive_service`` is a Drive service with access to walk each shared drive
    (typically the admin's impersonated service).

    ``modified_before`` (RFC3339), if set, excludes files modified on or after it —
    see ``walk_drive_folder`` for exactly how that's applied.
    """
    all_rows: list[dict[str, Any]] = []
    remaining = max_files

    for email in users:
        if remaining is not None and remaining <= 0:
            break
        if progress_log:
            progress_log(f"[workspace] My Drive → {email}")
        try:
            svc = get_user_service(email)
            user_cache = (
                str(scan_cache_path) + f".{email}.jsonl" if scan_cache_path else None
            )
            rows = walk_drive_folder(
                svc,
                "root",
                path_prefix=f"My Drive ({email})",
                max_files=remaining,
                modified_before=modified_before,
                progress_log=progress_log,
                progress_every=progress_every,
                scan_cache_path=user_cache,
            )
            # Downloads must impersonate this same user — admin creds cannot read
            # private My Drive files. Stamp even when rows came from scan cache.
            for r in rows:
                r["impersonate_as"] = email
            all_rows.extend(rows)
            if remaining is not None:
                remaining = max(0, remaining - len(rows))
        except Exception as exc:
            if progress_log:
                progress_log(f"[workspace] WARNING: skipping {email} — {exc}")

    if shared_drives and shared_drive_service is not None:
        if progress_log:
            progress_log(f"[workspace] walking {len(shared_drives)} Shared Drive(s)")
        for drv in shared_drives:
            if remaining is not None and remaining <= 0:
                break
            did = drv["id"]
            dname = drv.get("name") or did
            if progress_log:
                progress_log(f"[workspace] Shared Drive → {dname!r}")
            drv_cache = (
                str(scan_cache_path) + f".{did}.jsonl" if scan_cache_path else None
            )
            try:
                rows = walk_drive_folder(
                    shared_drive_service,
                    did,
                    path_prefix=dname,
                    max_files=remaining,
                    modified_before=modified_before,
                    progress_log=progress_log,
                    progress_every=progress_every,
                    scan_cache_path=drv_cache,
                )
                all_rows.extend(rows)
                if remaining is not None:
                    remaining = max(0, remaining - len(rows))
            except Exception as exc:
                if progress_log:
                    progress_log(f"[workspace] WARNING: skipping shared drive {dname!r} — {exc}")

    return all_rows


def walk_entire_workspace(
    service,
    *,
    include_my_drive: bool = True,
    include_shared_drives: bool = True,
    max_files: int | None = None,
    modified_before: str | None = None,
    progress_log: Callable[[str], None] | None = None,
    progress_every: int = 500,
    scan_cache_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Walk the full Google Workspace: My Drive + every Shared Drive the user can access.

    Each file row's ``path`` is prefixed with the drive name so sources are
    distinguishable in the inventory (e.g. ``My Drive/Finance/Q3.xlsx`` vs
    ``Engineering/repo-docs/README.md``).

    ``modified_before`` (RFC3339), if set, excludes files modified on or after it —
    see ``walk_drive_folder`` for exactly how that's applied.
    """
    all_rows: list[dict[str, Any]] = []
    remaining = max_files

    if include_my_drive:
        if progress_log:
            progress_log("[workspace] walking My Drive ...")
        rows = walk_drive_folder(
            service,
            "root",
            path_prefix="My Drive",
            max_files=remaining,
            modified_before=modified_before,
            progress_log=progress_log,
            progress_every=progress_every,
            scan_cache_path=(
                str(scan_cache_path) + ".mydrive.jsonl" if scan_cache_path else None
            ),
        )
        all_rows.extend(rows)
        if remaining is not None:
            remaining = max(0, remaining - len(rows))
            if remaining == 0:
                return all_rows

    if include_shared_drives:
        shared = list_shared_drives(service)
        if progress_log:
            progress_log(f"[workspace] found {len(shared)} Shared Drive(s)")
        for drv in shared:
            did = drv["id"]
            dname = drv.get("name") or did
            if progress_log:
                progress_log(f"[workspace] walking Shared Drive: {dname!r}")
            rows = walk_drive_folder(
                service,
                did,
                path_prefix=dname,
                max_files=remaining,
                modified_before=modified_before,
                progress_log=progress_log,
                progress_every=progress_every,
                scan_cache_path=(
                    str(scan_cache_path) + f".{did}.jsonl" if scan_cache_path else None
                ),
            )
            all_rows.extend(rows)
            if remaining is not None:
                remaining = max(0, remaining - len(rows))
                if remaining == 0:
                    break

    return all_rows
