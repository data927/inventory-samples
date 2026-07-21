"""Gmail inventory pipeline: scan → extract → classify → XLSX.

Output format is identical to the Drive pipeline so the same XLSX workbook
structure (Master + per-bucket sheets + Summary) is produced.

Resume: the pipeline saves two sidecar files next to the output xlsx:
  - <out>.gmail_scan.<user>.txt    — message IDs per user (skip re-enumeration)
  - <out>.gmail_artifacts.jsonl    — extracted evidence rows (skip re-download)
  - <out>.checkpoint.jsonl         — LLM classification results (skip re-classify)
  - <out>.subcat_checkpoint.jsonl  — subcategory results

Killing and re-running the exact same command resumes from where it left off.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from artifact_summary import summarize_artifact
from checkpoint import (
    append_checkpoint,
    append_drive_artifact_checkpoint,
    checkpoint_path_for_output,
    drive_artifact_checkpoint_path,
    load_checkpoint,
    load_drive_artifact_checkpoint,
)
from excel_io import write_company_inventory_workbook
from extractors.content_inventory import format_content_inventory_cell, gather_content_inventory
from extractors.content_type import infer_content_types_cell
from extractors.core import MAX_CHARS_DEFAULT, count_llm_tokens, count_words, extract_full_text
from extractors.modality import format_modalities_cell, infer_modalities
from extractors.quality_tier import infer_quality_tier
from ingest import infer_source_system
from inventory_writer import summarize_inventory_from_evidence
from llm_provider import default_llm_model
from pii import detect_pii
from segmenter import DEFAULT_BATCH_SIZE, classify_items
from subcategory_classifier import attach_subcategories

from gmail.fetch import fetch_attachment_to_tempfile
from gmail.scan import fetch_message, scan_mailbox

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
PASS1_BATCH = int(os.environ.get("GMAIL_PASS1_BATCH", "100"))
PASS2_BATCH = int(os.environ.get("GMAIL_PASS2_BATCH", str(DEFAULT_BATCH_SIZE)))
SUBCAT_BATCH = int(os.environ.get("GMAIL_SUBCAT_BATCH", "50"))
MAX_BODY_CHARS = int(os.environ.get("GMAIL_MAX_BODY_CHARS", "8000"))
_PASS1_MODEL_ENV = "GMAIL_PASS1_MODEL"

_ckpt_lock = threading.Lock()


def _default_pass1_model() -> str:
    from llm_provider import llm_provider
    explicit = os.environ.get(_PASS1_MODEL_ENV, "").strip()
    if explicit:
        return explicit
    p = llm_provider()
    if p == "openai":
        return "gpt-4o-mini"
    if p == "gemini":
        return "gemini-2.0-flash"
    return "claude-haiku-4-5-20251001"


def _human_size(b: int | None) -> str:
    if b is None:
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f} {unit}"
        b /= 1024  # type: ignore[assignment]
    return f"{b:.1f} TB"


# ---------------------------------------------------------------------------
# Classify pass (mirrors gdrive/pipeline_1tb.py)
# ---------------------------------------------------------------------------

def _run_classify_pass(
    pending: list[dict[str, Any]],
    *,
    model: str,
    batch_size: int,
    pass_num: int,
    ckpt_path: Path,
    evidence_rows: list[dict[str, Any]],
    progress_log: Callable[[str], None] | None,
    workers: int = 1,
) -> dict[int, dict[str, Any]]:
    classified: dict[int, dict[str, Any]] = {}
    total = len(pending)
    done = 0
    already_classified = set(load_checkpoint(ckpt_path).keys())

    work: list[tuple[int, list[dict[str, Any]]]] = []
    for batch_start in range(0, total, batch_size):
        batch = pending[batch_start: batch_start + batch_size]
        if all(item["row_id"] in already_classified for item in batch):
            done += len(batch)
            continue
        work.append((batch_start, batch))

    if not work:
        return classified

    lock = threading.Lock()

    def _process(batch_start: int, batch: list[dict[str, Any]]) -> None:
        nonlocal done
        try:
            results = classify_items(batch, model=model)
        except Exception as exc:
            msg = f"[pass{pass_num}] batch {batch_start // batch_size + 1} FAILED — {exc}"
            print(msg, flush=True)
            if progress_log:
                progress_log(msg)
            with lock:
                done += len(batch)
            return

        cleaned: list[dict[str, Any]] = []
        for c in (results or []):
            if not isinstance(c, dict):
                continue
            rid = c.get("row_id")
            try:
                rid_i = int(rid)
            except Exception:
                continue
            c["row_id"] = rid_i
            c["llm_pass"] = pass_num
            cleaned.append(c)

        with lock:
            for c in cleaned:
                classified[c["row_id"]] = c
            append_checkpoint(ckpt_path, cleaned)
            for c in cleaned:
                idx = c["row_id"] - 1
                if 0 <= idx < len(evidence_rows):
                    evidence_rows[idx]["llm_pass"] = pass_num
            done += len(batch)
            msg = (
                f"[pass{pass_num}] batch {batch_start // batch_size + 1} "
                f"done={done}/{total} classified={len(cleaned)} model={model}"
            )
            print(msg, flush=True)
            if progress_log:
                progress_log(msg)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futs = [executor.submit(_process, bs, b) for bs, b in work]
        for fut in as_completed(futs):
            fut.result()

    return classified


# ---------------------------------------------------------------------------
# Evidence builders
# ---------------------------------------------------------------------------

def _gmail_extra(
    *,
    message: dict[str, Any],
    user_email: str,
    item_type: str,
) -> dict[str, Any]:
    return {
        "gmail_user": user_email,
        "gmail_item_type": item_type,
        "gmail_subject": message.get("subject") or "",
        "gmail_sender": message.get("sender") or "",
        "gmail_to": message.get("to") or "",
        "gmail_labels": message.get("labels") or "",
        "gmail_message_id": message.get("message_id") or "",
        "gmail_thread_id": message.get("thread_id") or "",
        "gmail_date": message.get("date_str") or "",
    }


def _build_email_evidence(
    *,
    artifact_id: int,
    message: dict[str, Any],
    user_email: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    subject = message.get("subject") or "(no subject)"
    body_text = (message.get("body_text") or "")[:MAX_BODY_CHARS]
    sender = message.get("sender") or ""
    to_field = message.get("to") or ""
    date_unix = float(message.get("date_unix") or 0.0)
    size_bytes = int(message.get("size_estimate") or 0)
    labels = message.get("labels") or ""

    filename = f"{subject[:200]}.eml"
    primary_label = labels.split(",")[0].strip() if labels else "INBOX"
    path = f"Gmail/{user_email}/{primary_label}"

    body_snippet = "\n".join(filter(None, [
        f"From: {sender}" if sender else "",
        f"To: {to_field}" if to_field else "",
        body_text,
    ]))[:1200]

    pii = detect_pii(body_snippet)
    word_count = count_words(body_text)
    token_count = count_llm_tokens(body_text)
    content_summary = summarize_artifact(
        filename=filename, rel_path=path, extension=".eml",
        snippet=body_text[:400] or None,
    )
    inv_counts = gather_content_inventory(None, body_text, ".eml")

    desc = (
        f"Email — From: {sender}  To: {to_field}\n"
        f"Subject: {subject}\nUser: {user_email}\nLabels: {labels}\n\n"
        f"{body_text[:4000]}"
    )[:12000]

    ev = {
        "artifact_id": artifact_id,
        "path": path,
        "filename": filename,
        "extension": ".eml",
        "size_bytes": size_bytes,
        "modified_time_unix": date_unix,
        "source_guess": "Gmail",
        "snippet": body_snippet[:800],
        "content_summary": content_summary,
        "pii_flag": "Yes" if pii.has_pii else "No",
        "pii_types": ", ".join(pii.types),
        "word_count": int(word_count),
        "token_count": int(token_count),
        "page_count": 0,
        "modality": "Text Document",
        "content_inventory": format_content_inventory_cell(inv_counts),
        "bucket_number": "",
        "bucket_name": "",
        "confidence": "",
        "rationale": "",
        "llm_pass": "",
        **_gmail_extra(message=message, user_email=user_email, item_type="email"),
    }
    pending = {
        "row_id": artifact_id,
        "description": desc,
        "source": "Gmail",
        "filename": filename,
        "extension": ".eml",
        "path_hint": path,
        "snippet": body_snippet[:800],
    }
    return ev, pending


def _build_attachment_evidence(
    *,
    artifact_id: int,
    message: dict[str, Any],
    attachment: dict[str, Any],
    temp_path: Path | None,
    user_email: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    subject = message.get("subject") or "(no subject)"
    sender = message.get("sender") or ""
    fname = attachment.get("filename") or "attachment"
    mime_type = attachment.get("mime_type") or ""
    size_bytes = int(attachment.get("size_bytes") or 0)
    date_unix = float(message.get("date_unix") or 0.0)

    extension = Path(fname).suffix.lower() or ".bin"
    path = f"Gmail/{user_email}/{subject[:80]}/attachments"

    if temp_path and temp_path.is_file():
        text = extract_full_text(temp_path, max_chars=MAX_CHARS_DEFAULT) or ""
        snippet_str = text[:1200]
        word_count = count_words(text)
        token_count = count_llm_tokens(text)
        modalities = infer_modalities(temp_path)
        inv_counts = gather_content_inventory(temp_path, text, extension)
        content_summary = summarize_artifact(
            filename=fname, rel_path=path, extension=extension,
            snippet=text[:400] or None,
        )
    else:
        text = ""
        snippet_str = f"[attachment: {fname}, {_human_size(size_bytes)}, from: {subject!r}]"
        word_count = 0
        token_count = 0
        modalities = None
        inv_counts = {}
        content_summary = summarize_artifact(
            filename=fname, rel_path=path, extension=extension, snippet=None,
        )

    pii = detect_pii(snippet_str)

    desc = (
        f"Email attachment: {fname}\n"
        f"From email: {subject!r}  Sender: {sender}  User: {user_email}\n"
        f"MIME: {mime_type}  Size: {_human_size(size_bytes)}\n\n"
        f"{text[:4000] if text else '[binary / unreadable]'}"
    )[:12000]

    ev = {
        "artifact_id": artifact_id,
        "path": path,
        "filename": fname,
        "extension": extension,
        "size_bytes": size_bytes,
        "modified_time_unix": date_unix,
        "source_guess": "Gmail",
        "snippet": snippet_str[:800],
        "content_summary": content_summary,
        "pii_flag": "Yes" if pii.has_pii else "No",
        "pii_types": ", ".join(pii.types),
        "word_count": int(word_count),
        "token_count": int(token_count),
        "page_count": 0,
        "modality": format_modalities_cell(modalities) if modalities else "Document",
        "content_inventory": format_content_inventory_cell(inv_counts),
        "bucket_number": "",
        "bucket_name": "",
        "confidence": "",
        "rationale": "",
        "llm_pass": "",
        **_gmail_extra(message=message, user_email=user_email, item_type="attachment"),
    }
    pending = {
        "row_id": artifact_id,
        "description": desc,
        "source": "Gmail",
        "filename": fname,
        "extension": extension,
        "path_hint": path,
        "snippet": snippet_str[:800],
    }
    return ev, pending


# ---------------------------------------------------------------------------
# Msgmap sidecar — maps message_id → list of artifact_ids assigned to it
# ---------------------------------------------------------------------------

def _msgmap_path(output_path: str) -> Path:
    out = Path(output_path).expanduser().resolve()
    return out.with_name(out.stem + ".gmail_msgmap.json")


def _load_msgmap(path: Path) -> dict[str, list[int]]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_msgmap(path: Path, msgmap: dict[str, list[int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(msgmap, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_inventory_from_gmail(
    *,
    get_user_service,  # callable(user_email: str) -> Gmail API service
    users: list[str],
    output_path: str,
    pass1_model: str | None = None,
    pass2_model: str | None = None,
    gmail_query: str = "",
    max_messages_per_user: int | None = None,
    workers: int = 8,
    progress_log: Callable[[str], None] | None = None,
    progress_every: int = 200,
) -> str:
    """Build an inventory XLSX from Gmail mailboxes.

    ``get_user_service(email)`` must return a Gmail API v1 service authenticated
    as that user (OAuth creds or service account with DWD).

    ``users`` is the list of mailbox email addresses to scan.
    """
    if not progress_log:
        progress_log = lambda m: print(m, flush=True)  # noqa: E731

    out_path = Path(output_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    _pass1_model = pass1_model or _default_pass1_model()
    _pass2_model = pass2_model or default_llm_model()
    enrich_model = _pass2_model

    ckpt_path = checkpoint_path_for_output(str(out_path))
    arti_path = Path(str(out_path).replace(out_path.suffix, "") + ".gmail_artifacts.jsonl")
    msgmap_path = _msgmap_path(str(out_path))

    # -----------------------------------------------------------------------
    # Phase 1: Scan — enumerate message IDs per user
    # -----------------------------------------------------------------------
    progress_log(f"[gmail] scanning {len(users)} mailbox(es) ...")
    all_message_ids: list[tuple[str, str]] = []  # (user_email, message_id)

    for user_email in users:
        try:
            svc = get_user_service(user_email)
            cache_path = out_path.parent / (out_path.stem + f".gmail_scan.{user_email}.txt")
            ids = scan_mailbox(
                svc,
                user_email,
                query=gmail_query,
                max_messages=max_messages_per_user,
                progress_log=progress_log,
                scan_cache_path=cache_path,
            )
            for mid in ids:
                all_message_ids.append((user_email, mid))
        except Exception as exc:
            progress_log(f"[gmail] WARNING: failed to scan {user_email!r} — {exc}")

    total_messages = len(all_message_ids)
    progress_log(f"[gmail] total messages across all users: {total_messages}")

    if total_messages == 0:
        return "Gmail scan returned 0 messages. Check permissions and query."

    # -----------------------------------------------------------------------
    # Phase 2: Extract — fetch each message, build evidence rows
    # -----------------------------------------------------------------------
    progress_log("[gmail] extract phase: fetching messages and attachments ...")

    existing_rows = load_drive_artifact_checkpoint(arti_path)
    msgmap = _load_msgmap(msgmap_path)

    # Assign stable artifact_ids: use msgmap if available, else continue from max
    next_aid = max((max(v) for v in msgmap.values() if v), default=0) + 1 if msgmap else 1

    evidence_rows: list[dict[str, Any]] = []
    pending_classify: list[dict[str, Any]] = []

    done_count = 0
    fetch_count = 0

    for user_email, message_id in all_message_ids:
        key = f"{user_email}:{message_id}"

        if key in msgmap:
            # Already processed — load from artifact checkpoint
            for aid in msgmap[key]:
                if aid in existing_rows:
                    ev = existing_rows[aid].copy()
                    ev.setdefault("llm_pass", "")
                    for k in ("bucket_number", "bucket_name", "confidence", "rationale"):
                        ev.setdefault(k, "")
                    evidence_rows.append(ev)
                    if aid not in load_checkpoint(ckpt_path):
                        desc = (
                            f"{ev.get('filename')} ({ev.get('extension') or 'noext'})\n"
                            f"{ev.get('content_summary', '')}\n{ev.get('snippet', '')}"
                        )[:12000]
                        pending_classify.append({
                            "row_id": aid,
                            "description": desc,
                            "source": ev.get("source_guess") or "Gmail",
                            "filename": ev.get("filename") or "",
                            "extension": ev.get("extension") or "",
                            "path_hint": str(ev.get("path") or ""),
                            "snippet": str(ev.get("snippet") or "")[:800],
                        })
            done_count += 1
            continue

        # Fetch full message
        try:
            svc = get_user_service(user_email)
            msg = fetch_message(svc, message_id, user_id=user_email)
        except Exception as exc:
            progress_log(f"[gmail] WARNING: failed to fetch {message_id} for {user_email}: {exc}")
            continue

        item_aids: list[int] = []

        # Email body
        aid = next_aid
        next_aid += 1
        try:
            ev, pend = _build_email_evidence(
                artifact_id=aid, message=msg, user_email=user_email,
            )
            evidence_rows.append(ev)
            pending_classify.append(pend)
            item_aids.append(aid)
            append_drive_artifact_checkpoint(arti_path, aid, ev)
        except Exception as exc:
            progress_log(f"[gmail] WARNING: email body build failed for {message_id}: {exc}")

        # Attachments
        for att in msg.get("attachments") or []:
            a_aid = next_aid
            next_aid += 1
            temp_path: Path | None = None
            try:
                temp_path = fetch_attachment_to_tempfile(
                    svc, message_id, att, user_id=user_email,
                )
                a_ev, a_pend = _build_attachment_evidence(
                    artifact_id=a_aid, message=msg, attachment=att,
                    temp_path=temp_path, user_email=user_email,
                )
                evidence_rows.append(a_ev)
                pending_classify.append(a_pend)
                item_aids.append(a_aid)
                append_drive_artifact_checkpoint(arti_path, a_aid, a_ev)
            except Exception as exc:
                progress_log(f"[gmail] WARNING: attachment {att.get('filename')!r} failed: {exc}")
            finally:
                if temp_path and temp_path.is_file():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass

        # Save msgmap entry
        if item_aids:
            msgmap[key] = item_aids
            _save_msgmap(msgmap_path, msgmap)

        fetch_count += 1
        if fetch_count % progress_every == 0:
            progress_log(
                f"[extract] fetched={fetch_count} skipped={done_count} "
                f"total_items={len(evidence_rows)} msg={message_id[:16]}"
            )

    progress_log(
        f"[extract] done. evidence_rows={len(evidence_rows)} "
        f"pending_classify={len(pending_classify)}"
    )

    if not evidence_rows:
        return "No evidence rows extracted — nothing to classify."

    # -----------------------------------------------------------------------
    # Phase 3: Pass-1 classification
    # -----------------------------------------------------------------------
    existing_class = load_checkpoint(ckpt_path)

    pending_p1 = [p for p in pending_classify if p["row_id"] not in existing_class]
    if pending_p1:
        progress_log(
            f"[pass1] classifying {len(pending_p1)} items  "
            f"model={_pass1_model}  batch={PASS1_BATCH}"
        )
        pass1_results = _run_classify_pass(
            pending_p1,
            model=_pass1_model,
            batch_size=PASS1_BATCH,
            pass_num=1,
            ckpt_path=ckpt_path,
            evidence_rows=evidence_rows,
            progress_log=progress_log,
            workers=workers,
        )
        existing_class.update(pass1_results)
    else:
        progress_log("[pass1] all items already classified")

    # -----------------------------------------------------------------------
    # Phase 4: Pass-2 (low-confidence re-classify)
    # -----------------------------------------------------------------------
    low_conf: list[dict[str, Any]] = []
    for aid_int, c in existing_class.items():
        if str(c.get("confidence", "")).strip().lower() == "low" and c.get("llm_pass", 1) == 1:
            idx = aid_int - 1
            if 0 <= idx < len(evidence_rows):
                ev = evidence_rows[idx]
                desc = (
                    f"{ev.get('filename')} ({ev.get('extension') or 'noext'})\n"
                    f"{ev.get('content_summary', '')}\n{ev.get('snippet', '')}"
                )[:12000]
                low_conf.append({
                    "row_id": aid_int,
                    "description": desc,
                    "source": ev.get("source_guess") or "Gmail",
                    "filename": ev.get("filename") or "",
                    "extension": ev.get("extension") or "",
                    "path_hint": str(ev.get("path") or ""),
                    "snippet": str(ev.get("snippet") or "")[:800],
                })

    if low_conf:
        progress_log(
            f"[pass2] re-classifying {len(low_conf)} low-confidence items  "
            f"model={_pass2_model}  batch={PASS2_BATCH}"
        )
        pass2_results = _run_classify_pass(
            low_conf,
            model=_pass2_model,
            batch_size=PASS2_BATCH,
            pass_num=2,
            ckpt_path=ckpt_path,
            evidence_rows=evidence_rows,
            progress_log=progress_log,
            workers=workers,
        )
        existing_class.update(pass2_results)
    else:
        progress_log("[pass2] no low-confidence items")

    # -----------------------------------------------------------------------
    # Apply classifications to evidence_df
    # -----------------------------------------------------------------------
    evidence_df = pd.DataFrame(evidence_rows)

    def _apply_cls(col: str, default: Any = "") -> pd.Series:
        return evidence_df["artifact_id"].map(
            lambda aid: existing_class.get(int(aid), {}).get(col, default)
        )

    evidence_df["bucket_number"] = _apply_cls("bucket_number")
    evidence_df["bucket_name"] = _apply_cls("bucket_name")
    evidence_df["confidence"] = _apply_cls("confidence")
    evidence_df["rationale"] = _apply_cls("rationale")
    evidence_df["llm_pass"] = _apply_cls("llm_pass", "")

    # -----------------------------------------------------------------------
    # Phase 5: Subcategory enrichment + content_type + quality_tier
    # -----------------------------------------------------------------------
    evidence_df = attach_subcategories(
        evidence_df,
        model=enrich_model,
        batch_size=SUBCAT_BATCH,
        output_path_for_checkpoint=str(out_path),
        progress_log=progress_log,
        workers=workers,
    )

    def _s(col: str) -> pd.Series:
        if col not in evidence_df.columns:
            return pd.Series("", index=evidence_df.index)
        return evidence_df[col].fillna("").astype(str).replace({"nan": ""})

    evidence_df["content_type"] = [
        infer_content_types_cell(
            rationale=r, content_summary=cs, snippet=sn, filename=fn, path=ph,
        )
        for r, cs, sn, fn, ph in zip(
            _s("rationale"), _s("content_summary"), _s("snippet"), _s("filename"), _s("path"),
        )
    ]

    tok = pd.to_numeric(
        evidence_df.get("token_count", pd.Series(0, index=evidence_df.index)),
        errors="coerce",
    ).fillna(0).astype("int64")
    wct = pd.to_numeric(
        evidence_df.get("word_count", pd.Series(0, index=evidence_df.index)),
        errors="coerce",
    ).fillna(0).astype("int64")

    evidence_df["quality_tier"] = [
        infer_quality_tier(
            token_count=int(t), word_count=int(w), pii_flag=str(pi),
            modality=str(m), content_type=str(ct), extension=str(ex), confidence=str(cf),
        )
        for t, w, pi, m, ct, ex, cf in zip(
            tok, wct, _s("pii_flag"), _s("modality"), _s("content_type"),
            _s("extension"), _s("confidence"),
        )
    ]

    # -----------------------------------------------------------------------
    # Phase 6: Inventory summary + write XLSX
    # -----------------------------------------------------------------------
    inventory_df = summarize_inventory_from_evidence(evidence_df, model=enrich_model)
    write_company_inventory_workbook(
        inventory_df=inventory_df,
        evidence_df=evidence_df,
        output_path=str(out_path),
    )

    bn = pd.to_numeric(
        evidence_df.get("bucket_number", pd.Series([], dtype=object)), errors="coerce"
    )
    dist = bn.dropna().astype(int).value_counts().sort_index().to_dict()
    p1_count = int((evidence_df.get("llm_pass", pd.Series([], dtype=object)) == 1).sum())
    p2_count = int((evidence_df.get("llm_pass", pd.Series([], dtype=object)) == 2).sum())

    lines = [
        f"Wrote Gmail inventory to {out_path}",
        f"Evidence rows: {len(evidence_df)}  (emails + attachments)",
        f"  Pass-1 ({_pass1_model}): {p1_count}",
        f"  Pass-2 ({_pass2_model}):  {p2_count}",
        f"Bucket distribution: {dist}",
        f"Classification checkpoint: {ckpt_path}",
        f"Extract checkpoint (resume): {arti_path}",
    ]
    return "\n".join(lines)
