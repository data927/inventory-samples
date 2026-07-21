"""Download Gmail attachment bytes to a temp file."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from typing import Any


def _b64_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def get_attachment_bytes(
    service,
    message_id: str,
    attachment_id: str,
    *,
    inline_data: str = "",
    user_id: str = "me",
) -> bytes:
    """Return raw bytes for an attachment.

    Prefers ``inline_data`` (base64url already in the message payload) to avoid
    an extra API call. Falls back to ``users.messages.attachments.get`` for
    attachments whose data wasn't inlined.
    """
    if inline_data:
        return _b64_decode(inline_data)
    resp = service.users().messages().attachments().get(
        userId=user_id,
        messageId=message_id,
        id=attachment_id,
    ).execute()
    return _b64_decode(resp.get("data") or "")


def fetch_attachment_to_tempfile(
    service,
    message_id: str,
    attachment: dict[str, Any],
    *,
    user_id: str = "me",
) -> Path | None:
    """Download attachment to a named temp file; return its path (caller must delete).

    Returns ``None`` if the download fails or the attachment has no data.
    """
    att_id = attachment.get("attachment_id") or ""
    inline_data = attachment.get("inline_data") or ""
    if not att_id and not inline_data:
        return None

    fname = attachment.get("filename") or "attachment"
    suffix = Path(fname).suffix or ".bin"

    try:
        raw = get_attachment_bytes(
            service,
            message_id,
            att_id,
            inline_data=inline_data,
            user_id=user_id,
        )
    except Exception:
        return None

    if not raw:
        return None

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
        f.write(raw)
        return Path(f.name)
