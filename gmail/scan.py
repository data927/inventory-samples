"""Gmail message enumeration and MIME parsing."""

from __future__ import annotations

import base64
import email.utils
import re
import time
from pathlib import Path
from typing import Any, Callable


def _b64_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def _get_header(headers: list[dict], name: str) -> str:
    lname = name.lower()
    for h in headers:
        if (h.get("name") or "").lower() == lname:
            return h.get("value") or ""
    return ""


def _parse_date_unix(date_str: str) -> float:
    if not date_str:
        return 0.0
    try:
        t = email.utils.parsedate(date_str)
        return float(time.mktime(t)) if t else 0.0
    except Exception:
        return 0.0


def _extract_parts(payload: dict) -> tuple[str, list[dict[str, Any]]]:
    """Recursively walk MIME payload; return (plain_text_body, [attachment_meta, ...])."""
    mime = payload.get("mimeType") or ""
    body_obj = payload.get("body") or {}
    parts = payload.get("parts") or []

    if mime == "text/plain":
        data = body_obj.get("data") or ""
        text = _b64_decode(data).decode("utf-8", errors="replace") if data else ""
        return text.strip(), []

    if mime == "text/html":
        data = body_obj.get("data") or ""
        if data:
            html = _b64_decode(data).decode("utf-8", errors="replace")
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()
        else:
            text = ""
        return text, []

    if mime.startswith("multipart/"):
        plain_chunks: list[str] = []
        html_chunks: list[str] = []
        attachments: list[dict[str, Any]] = []
        for part in parts:
            pmime = part.get("mimeType") or ""
            pbody = part.get("body") or {}
            fname = (part.get("filename") or "").strip()
            att_id = pbody.get("attachmentId") or ""
            inline_data = pbody.get("data") or ""
            size = int(pbody.get("size") or 0)

            if fname and (att_id or inline_data):
                attachments.append({
                    "filename": fname,
                    "mime_type": pmime,
                    "size_bytes": size,
                    "attachment_id": att_id,
                    "inline_data": inline_data,
                })
            elif pmime == "text/plain":
                data = pbody.get("data") or ""
                if data:
                    plain_chunks.append(_b64_decode(data).decode("utf-8", errors="replace"))
            elif pmime == "text/html":
                data = pbody.get("data") or ""
                if data:
                    html = _b64_decode(data).decode("utf-8", errors="replace")
                    html_chunks.append(re.sub(r"<[^>]+>", " ", html))
            elif pmime.startswith("multipart/") or part.get("parts"):
                sub_text, sub_att = _extract_parts(part)
                if sub_text:
                    plain_chunks.append(sub_text)
                attachments.extend(sub_att)

        body = "\n\n".join(plain_chunks).strip() or "\n\n".join(html_chunks).strip()
        return body, attachments

    # Fallback: raw body data
    data = body_obj.get("data") or ""
    if data:
        try:
            return _b64_decode(data).decode("utf-8", errors="replace").strip(), []
        except Exception:
            pass
    return "", []


def fetch_message(service, message_id: str, user_id: str = "me") -> dict[str, Any]:
    """Fetch one message (full format) and return parsed dict."""
    msg = service.users().messages().get(
        userId=user_id,
        id=message_id,
        format="full",
    ).execute()

    payload = msg.get("payload") or {}
    headers = payload.get("headers") or []

    subject = _get_header(headers, "Subject") or "(no subject)"
    sender = _get_header(headers, "From") or ""
    to_field = _get_header(headers, "To") or ""
    cc_field = _get_header(headers, "Cc") or ""
    date_str = _get_header(headers, "Date") or ""

    body_text, attachments = _extract_parts(payload)

    return {
        "message_id": msg.get("id") or "",
        "thread_id": msg.get("threadId") or "",
        "subject": subject,
        "sender": sender,
        "to": to_field,
        "cc": cc_field,
        "date_str": date_str,
        "date_unix": _parse_date_unix(date_str),
        "labels": ", ".join(msg.get("labelIds") or []),
        "body_text": body_text,
        "snippet": msg.get("snippet") or "",
        "size_estimate": int(msg.get("sizeEstimate") or 0),
        "attachments": attachments,
    }


def list_message_ids(
    service,
    user_id: str = "me",
    *,
    query: str = "",
    max_messages: int | None = None,
    progress_log: Callable[[str], None] | None = None,
) -> list[str]:
    """Return all message IDs for a mailbox. ``query`` is a Gmail search query."""
    ids: list[str] = []
    page_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "userId": user_id,
            "maxResults": 500,
            "includeSpamTrash": False,
        }
        if query:
            kwargs["q"] = query
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        batch = [m["id"] for m in (resp.get("messages") or [])]
        ids.extend(batch)
        if progress_log and len(ids) % 5000 < 500:
            progress_log(f"[gmail-scan] listed {len(ids)} messages ...")
        if max_messages and len(ids) >= max_messages:
            ids = ids[:max_messages]
            break
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def scan_mailbox(
    service,
    user_email: str,
    *,
    query: str = "",
    max_messages: int | None = None,
    progress_log: Callable[[str], None] | None = None,
    scan_cache_path: str | Path | None = None,
) -> list[str]:
    """Return message IDs for a mailbox, using a local cache to avoid re-enumerating."""
    cache_path = Path(scan_cache_path) if scan_cache_path else None

    if cache_path and cache_path.is_file() and cache_path.stat().st_size > 0:
        ids = [l.strip() for l in cache_path.read_text().splitlines() if l.strip()]
        if ids:
            if progress_log:
                progress_log(f"[gmail-scan] loaded {len(ids)} message IDs from cache for {user_email!r}")
            return ids[:max_messages] if max_messages else ids

    if progress_log:
        progress_log(f"[gmail-scan] enumerating messages for {user_email!r} ...")

    ids = list_message_ids(
        service, user_id=user_email,
        query=query, max_messages=max_messages,
        progress_log=progress_log,
    )

    if progress_log:
        progress_log(f"[gmail-scan] found {len(ids)} messages for {user_email!r}")

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("\n".join(ids) + "\n", encoding="utf-8")

    return ids
