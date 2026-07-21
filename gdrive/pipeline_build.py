"""Build the inventory workbook from a Google Drive folder."""

from __future__ import annotations

import os
import tempfile
from collections import Counter
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
from extractors.core import (
    count_llm_tokens,
    count_words,
    extract_full_text,
    extract_snippet,
    is_title_only_media,
    is_video_file,
    media_title_text,
    page_count_from_file,
)
from extractors.modality import format_modalities_cell, infer_modalities
from extractors.quality_tier import infer_quality_tier
from gdrive.fetch import (
    fetch_drive_file_to_path,
    resolve_shortcut_target,
    suggested_local_suffix,
)
from gdrive.scan import normalize_folder_id, walk_drive_folder
from ingest import infer_source_system
from inventory_writer import summarize_inventory_from_evidence
from llm_provider import llm_api_configured
from pii import detect_pii
from segmenter import DEFAULT_BATCH_SIZE, DEFAULT_MODEL, classify_items
from subcategory_classifier import attach_subcategories


def _per_bucket_counts_from_evidence_rows(rows: list[dict[str, Any]]) -> dict[int, int]:
    counts: dict[int, int] = {b: 0 for b in range(1, 8)}
    for ev in rows:
        try:
            b = int(float(str(ev.get("bucket_number", "")).strip()))
        except Exception:
            continue
        if b in counts:
            counts[b] += 1
    return counts


def _trim_evidence_to_per_bucket(evidence_df: pd.DataFrame, per_bucket: int) -> pd.DataFrame:
    """Keep up to ``per_bucket`` rows for each bucket 1–7 (earliest ``artifact_id`` first)."""
    if per_bucket <= 0 or evidence_df.empty:
        return evidence_df
    if "bucket_number" not in evidence_df.columns or "artifact_id" not in evidence_df.columns:
        return evidence_df
    df = evidence_df.copy()
    df["_bn"] = pd.to_numeric(df["bucket_number"], errors="coerce")
    df["_aid"] = pd.to_numeric(df["artifact_id"], errors="coerce")
    df = df[df["_bn"].isin(range(1, 8))].sort_values("_aid", kind="mergesort")
    trimmed = df.groupby("_bn", as_index=False, sort=True).head(int(per_bucket))
    trimmed = trimmed.drop(columns=["_bn", "_aid"], errors="ignore")
    return trimmed.reset_index(drop=True)


def _parse_drive_time_unix(s: str | None) -> float:
    if not s or not isinstance(s, str):
        return 0.0
    try:
        t = s.rstrip("Z") + "+00:00" if s.endswith("Z") else s
        return float(datetime.fromisoformat(t).timestamp())
    except Exception:
        return 0.0


def _int_size(v: object) -> int | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
        if not x == x or x < 0:
            return None
        return int(x)
    except Exception:
        return None


def _drive_rows(
    service: Any,
    folder_id: str,
    max_files: int | None,
    progress_log: Callable[[str], None] | None,
    progress_every: int,
) -> list[dict[str, Any]]:
    return walk_drive_folder(
        service,
        normalize_folder_id(folder_id),
        max_files=max_files,
        progress_log=progress_log,
        progress_every=progress_every,
    )


def _apply_classification_to_evidence_row(ev: dict[str, Any], c: dict[str, Any]) -> None:
    ev["bucket_number"] = c.get("bucket_number", "")
    ev["bucket_name"] = c.get("bucket_name", "")
    ev["confidence"] = c.get("confidence", "")
    ev["rationale"] = c.get("rationale", "")


def _snapshot_evidence_csv(path: Path, evidence_rows: list[dict[str, Any]]) -> None:
    """Overwrite a CSV with current evidence rows (bucket columns filled when classified)."""
    if not evidence_rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(evidence_rows).to_csv(path, index=False)


def _effective_file_for_row(service: Any, row: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (file_id, mime_type, display_name, err)."""
    if row.get("is_folder"):
        return None, None, None, "folder"
    if row.get("is_shortcut"):
        tid = row.get("shortcut_target_id")
        if not tid:
            return None, None, None, "shortcut_without_target"
        tgt = resolve_shortcut_target(service, str(tid))
        if not tgt:
            return None, None, None, "shortcut_target_resolve_failed"
        mid = str(tgt.get("mimeType") or "")
        if mid == "application/vnd.google-apps.folder":
            return None, None, None, "shortcut_target_is_folder"
        return str(tgt.get("id")), mid, str(tgt.get("name") or row.get("name") or "file"), None
    fid = row.get("drive_file_id")
    if not fid:
        return None, None, None, "missing_drive_file_id"
    return str(fid), str(row.get("mime_type") or ""), str(row.get("name") or ""), None


def _build_evidence_from_temp_file(
    *,
    temp_path: Path,
    rel_path: str,
    filename: str,
    size_bytes: int | None,
    mtime_unix: float,
    artifact_id: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (evidence_row, pending_classify_item)."""
    extension = Path(filename).suffix.lower().lstrip(".")
    source_guess = infer_source_system(rel_path) or "Google Drive"
    stat_size = int(temp_path.stat().st_size) if temp_path.is_file() else (size_bytes or 0)

    if is_title_only_media(temp_path):
        title = media_title_text(temp_path)
        snippet = None
        word_count = len(title.split()) if title else 0
        token_count = count_llm_tokens(title) if title else 0
        page_count = 0
        modalities = infer_modalities(temp_path)
        inv_counts = gather_content_inventory(temp_path)
        kind = "video" if is_video_file(temp_path) else "audio"
        desc = (
            f"{filename} ({extension or 'noext'}, {kind} file). "
            "Only the file title/name is read for classification (no media content).\n"
            f"Title: {title}\nPath: {rel_path}"
        )
        pii = detect_pii(f"{title}\n{rel_path}")
        content_summary = summarize_artifact(
            filename=filename,
            rel_path=rel_path,
            extension=extension,
            snippet=title or None,
        )
        snippet_str = (title or "")[:800]
    else:
        snippet = extract_snippet(str(temp_path))
        full_text = extract_full_text(str(temp_path))
        word_count = count_words(full_text) if full_text else 0
        token_count = count_llm_tokens(full_text) if full_text else 0
        page_count = page_count_from_file(temp_path, word_count=word_count)
        modalities = infer_modalities(temp_path)
        inv_counts = gather_content_inventory(temp_path)
        desc = f"{filename} ({extension or 'noext'}, {int(stat_size)} bytes)"
        if snippet:
            desc = desc + "\n" + snippet
        pii = detect_pii(snippet)
        content_summary = summarize_artifact(
            filename=filename,
            rel_path=rel_path,
            extension=extension,
            snippet=snippet,
        )
        snippet_str = (snippet or "")[:800]

    ev = {
        "artifact_id": artifact_id,
        "path": rel_path,
        "filename": filename,
        "extension": extension,
        "size_bytes": int(stat_size) if stat_size is not None else None,
        "modified_time_unix": float(mtime_unix),
        "source_guess": source_guess,
        "snippet": snippet_str,
        "content_summary": content_summary,
        "pii_flag": "Yes" if pii.has_pii else "No",
        "pii_types": ", ".join(pii.types),
        "word_count": int(word_count),
        "token_count": int(token_count),
        "page_count": int(page_count),
        "modality": format_modalities_cell(modalities),
        "content_inventory": format_content_inventory_cell(inv_counts),
        "bucket_number": "",
        "bucket_name": "",
        "confidence": "",
        "rationale": "",
    }
    pending = {
        "row_id": artifact_id,
        "description": desc,
        "source": source_guess,
        "filename": filename,
        "extension": extension,
        "path_hint": rel_path,
        "snippet": snippet_str,
    }
    return ev, pending


def _degraded_evidence_from_metadata(
    *,
    artifact_id: int,
    rel_path: str,
    filename: str,
    extension: str,
    size_bytes: int | None,
    mtime_unix: float,
    err: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """When download/export fails: still classify from path + filename + error (lower fidelity)."""
    source_guess = infer_source_system(rel_path) or "Google Drive"
    snippet_str = f"[Drive body unavailable] {err}\n{filename}\n{rel_path}"[:800]
    desc = (
        f"{filename} ({extension or 'noext'}). Path: {rel_path}\n"
        f"Could not read file bytes from Google Drive: {err}"
    )
    pii = detect_pii(snippet_str)
    content_summary = summarize_artifact(
        filename=filename,
        rel_path=rel_path,
        extension=extension,
        snippet=snippet_str,
    )
    ev = {
        "artifact_id": artifact_id,
        "path": rel_path,
        "filename": filename,
        "extension": extension,
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
        "modified_time_unix": float(mtime_unix),
        "source_guess": source_guess,
        "snippet": snippet_str,
        "content_summary": content_summary,
        "pii_flag": "Yes" if pii.has_pii else "No",
        "pii_types": ", ".join(pii.types),
        "word_count": 0,
        "token_count": 0,
        "page_count": 0,
        "modality": "",
        "content_inventory": "",
        "bucket_number": "",
        "bucket_name": "",
        "confidence": "",
        "rationale": "",
    }
    pending = {
        "row_id": artifact_id,
        "description": desc,
        "source": source_guess,
        "filename": filename,
        "extension": extension,
        "path_hint": rel_path,
        "snippet": snippet_str,
    }
    return ev, pending


def build_inventory_from_drive(
    *,
    service: Any,
    folder_id: str,
    output_path: str,
    model: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_files: int | None = None,
    scan_rows: list[dict[str, Any]] | None = None,
    max_download_bytes: int | None = None,
    progress_log: Callable[[str], None] | None = None,
    progress_every: int = 500,
    ingest_log_every: int = 100,
    progress_csv_every: int = 500,
    progress_csv_path: str | Path | None = None,
    per_bucket_target: int | None = None,
    stratify_walk_max_files: int | None = None,
) -> str:
    """Walk ``folder_id`` (unless ``scan_rows`` is provided), ingest with full extract/classify, write workbook.

    When ``per_bucket_target`` is set (e.g. 10), ingestion keeps walking (up to
    ``stratify_walk_max_files`` file rows when no ``scan_rows``) until every bucket
    1–7 has at least that many **classified** evidence rows, then stops early.
    The written workbook is trimmed to at most that many rows per bucket (stable:
    lowest ``artifact_id`` first). Use a **fresh** ``output_path`` so checkpoints
    from non-stratified runs do not collide.
    """
    if not llm_api_configured():
        raise RuntimeError(
            "No LLM API key configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY / LLM_PROVIDER — see .env.example."
        )

    out_path = Path(output_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    snap_csv: Path | None = None
    if progress_csv_every > 0:
        snap_csv = (
            Path(progress_csv_path).expanduser().resolve()
            if progress_csv_path
            else out_path.with_name(out_path.stem + ".progress_evidence.csv")
        )
    ckpt_path = checkpoint_path_for_output(str(out_path))
    arti_path = drive_artifact_checkpoint_path(str(out_path))
    existing_class = load_checkpoint(ckpt_path)
    existing_rows = load_drive_artifact_checkpoint(arti_path)

    walk_cap: int | None = max_files
    if scan_rows is None and per_bucket_target is not None:
        walk_cap = stratify_walk_max_files
        if walk_cap is None:
            walk_cap = int(os.environ.get("DRIVE_STRATIFY_WALK_MAX", "50000"))
    rows = (
        scan_rows
        if scan_rows is not None
        else _drive_rows(service, folder_id, walk_cap, progress_log, progress_every)
    )
    file_rows = [r for r in rows if not r.get("is_folder")]

    if max_download_bytes is None:
        env = (os.environ.get("DRIVE_MAX_DOWNLOAD_BYTES") or "").strip()
        max_download_bytes = int(env) if env else None

    evidence_rows: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    classified = 0

    def flush_batch() -> None:
        nonlocal classified, pending, existing_class
        if not pending:
            return
        batch = pending
        pending = []
        batch_out = classify_items(batch, model=model, batch_size=len(batch))
        cleaned: list[dict[str, Any]] = []
        for c in (batch_out or []):
            if not isinstance(c, dict):
                print(f"[warn] classify_items returned non-dict element: {c!r}", flush=True)
                continue
            rid = c.get("row_id")
            try:
                rid_i = int(rid)
            except Exception:
                print(f"[warn] classify_items returned uncastable row_id={rid!r} in {c!r}", flush=True)
                continue
            c["row_id"] = rid_i
            cleaned.append(c)
        append_checkpoint(ckpt_path, cleaned)
        for c in cleaned:
            try:
                rid_i = int(c.get("row_id"))
                existing_class[rid_i] = c
                idx = rid_i - 1
                if 0 <= idx < len(evidence_rows):
                    _apply_classification_to_evidence_row(evidence_rows[idx], c)
            except Exception:
                continue
        classified += len(cleaned)
        hist = Counter(str(c.get("bucket_number", "")) for c in existing_class.values())
        nonempty_r = sum(1 for c in existing_class.values() if str(c.get("rationale") or "").strip())
        top = hist.most_common(8)
        msg = (
            f"[batch] +{len(cleaned)} classified_total={classified} "
            f"pending_queue={len(pending)} bucket_top={top} rationales_nonempty={nonempty_r}"
        )
        print(msg, flush=True)
        if progress_log:
            progress_log(msg)

    def maybe_progress_csv(aid: int, *, force: bool = False) -> None:
        if snap_csv is None or progress_csv_every <= 0:
            return
        if force or (aid % progress_csv_every == 0):
            _snapshot_evidence_csv(snap_csv, evidence_rows)
            if progress_log:
                progress_log(
                    f"[snapshot] wrote {snap_csv.name} rows={len(evidence_rows)} "
                    f"classified_rows={len(existing_class)} artifact_id={aid}"
                )

    def live_tail(aid: int, ev: dict[str, Any]) -> None:
        if ingest_log_every and aid % ingest_log_every == 0 and progress_log:
            bb = ev.get("bucket_number", "")
            progress_log(
                f"[ingest] artifact_id={aid} fn={str(ev.get('filename') or '')[:160]!r} "
                f"wc={ev.get('word_count')} tok={ev.get('token_count')} "
                f"mod={(str(ev.get('modality') or '')[:120])!r} "
                f"classified={len(existing_class)} pending={len(pending)} "
                f"bucket={bb!r} conf={ev.get('confidence')!r}"
            )
        maybe_progress_csv(aid)

    def stratify_should_stop() -> bool:
        """Use ``existing_class`` so quotas reflect flushed classifications (pending excluded)."""
        if per_bucket_target is None:
            return False
        c = {b: 0 for b in range(1, 8)}
        for _rid, obj in existing_class.items():
            try:
                b = int(float(str(obj.get("bucket_number", "")).strip()))
            except Exception:
                continue
            if b in c:
                c[b] += 1
        return all(c.get(b, 0) >= int(per_bucket_target) for b in range(1, 8))

    def stratify_stop_or_continue(aid: int) -> bool:
        """Return True if the outer ingest loop should break."""
        if not stratify_should_stop():
            return False
        c = {b: 0 for b in range(1, 8)}
        for _rid, obj in existing_class.items():
            try:
                b = int(float(str(obj.get("bucket_number", "")).strip()))
            except Exception:
                continue
            if b in c:
                c[b] += 1
        msg = f"[stop] stratified per-bucket target={per_bucket_target} counts={c} last_artifact_id={aid}"
        print(msg, flush=True)
        if progress_log:
            progress_log(msg)
        return True

    with tempfile.TemporaryDirectory(prefix="drive-ingest-") as td:
        td_path = Path(td)
        for aid, row in enumerate(file_rows, start=1):
            if per_bucket_target is None and max_files is not None and aid > max_files:
                break

            rel_path = str(row.get("path") or "").replace("\\", "/")
            filename = str(row.get("name") or "")
            extension = Path(filename).suffix.lower().lstrip(".")
            mtime_unix = _parse_drive_time_unix(row.get("modified_time"))
            size_b = _int_size(row.get("size_bytes"))

            if aid in existing_rows:
                ev = existing_rows[aid].copy()
                for k in ("bucket_number", "bucket_name", "confidence", "rationale"):
                    ev.setdefault(k, "")
                if aid in existing_class:
                    _apply_classification_to_evidence_row(ev, existing_class[aid])
                evidence_rows.append(ev)
                if aid not in existing_class:
                    # rebuild pending item from stored row (same shape classify_items needs)
                    desc = (
                        f"{ev.get('filename')} ({ev.get('extension') or 'noext'})\n"
                        f"{ev.get('content_summary', '')}\n{ev.get('snippet', '')}"
                    )[:12000]
                    pending.append(
                        {
                            "row_id": aid,
                            "description": desc,
                            "source": ev.get("source_guess") or "Google Drive",
                            "filename": ev.get("filename") or "",
                            "extension": ev.get("extension") or "",
                            "path_hint": rel_path,
                            "snippet": str(ev.get("snippet") or "")[:800],
                        }
                    )
                    if len(pending) >= batch_size:
                        flush_batch()
                live_tail(aid, ev)
                if stratify_stop_or_continue(aid):
                    break
                continue

            fid, mime, display_name, err0 = _effective_file_for_row(service, row)
            if err0:
                ev, pend = _degraded_evidence_from_metadata(
                    artifact_id=aid,
                    rel_path=rel_path,
                    filename=filename,
                    extension=extension,
                    size_bytes=size_b,
                    mtime_unix=mtime_unix,
                    err=err0,
                )
                evidence_rows.append(ev)
                append_drive_artifact_checkpoint(arti_path, aid, ev)
                existing_rows[aid] = ev
                if aid not in existing_class:
                    pending.append(pend)
                    if len(pending) >= batch_size:
                        flush_batch()
                live_tail(aid, ev)
                if stratify_stop_or_continue(aid):
                    break
                continue

            if max_download_bytes is not None and size_b is not None and size_b > max_download_bytes:
                ev, pend = _degraded_evidence_from_metadata(
                    artifact_id=aid,
                    rel_path=rel_path,
                    filename=filename,
                    extension=extension,
                    size_bytes=size_b,
                    mtime_unix=mtime_unix,
                    err=f"skipped_over_max_download_bytes ({size_b}>{max_download_bytes})",
                )
                evidence_rows.append(ev)
                append_drive_artifact_checkpoint(arti_path, aid, ev)
                existing_rows[aid] = ev
                if aid not in existing_class:
                    pending.append(pend)
                    if len(pending) >= batch_size:
                        flush_batch()
                live_tail(aid, ev)
                if stratify_stop_or_continue(aid):
                    break
                continue

            suffix = suggested_local_suffix(mime, display_name or filename)
            tmp = td_path / f"a{aid}{suffix}"
            err = fetch_drive_file_to_path(
                service,
                file_id=fid,
                mime_type=mime,
                display_name=display_name or filename,
                dest=tmp,
            )
            if err or not tmp.is_file() or tmp.stat().st_size == 0:
                ev, pend = _degraded_evidence_from_metadata(
                    artifact_id=aid,
                    rel_path=rel_path,
                    filename=filename,
                    extension=extension,
                    size_bytes=size_b,
                    mtime_unix=mtime_unix,
                    err=err or "empty_download",
                )
            else:
                ev, pend = _build_evidence_from_temp_file(
                    temp_path=tmp,
                    rel_path=rel_path,
                    filename=filename,
                    size_bytes=size_b,
                    mtime_unix=mtime_unix,
                    artifact_id=aid,
                )
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

            evidence_rows.append(ev)
            append_drive_artifact_checkpoint(arti_path, aid, ev)
            existing_rows[aid] = ev

            if aid not in existing_class:
                pending.append(pend)
                if len(pending) >= batch_size:
                    flush_batch()

            live_tail(aid, ev)
            if stratify_stop_or_continue(aid):
                break

    flush_batch()
    if evidence_rows:
        maybe_progress_csv(len(evidence_rows), force=True)
    by_id = existing_class

    evidence_df = pd.DataFrame(evidence_rows)
    evidence_df["bucket_number"] = evidence_df["artifact_id"].map(
        lambda rid: by_id.get(int(rid), {}).get("bucket_number")
    )
    evidence_df["bucket_name"] = evidence_df["artifact_id"].map(
        lambda rid: by_id.get(int(rid), {}).get("bucket_name")
    )
    evidence_df["confidence"] = evidence_df["artifact_id"].map(
        lambda rid: by_id.get(int(rid), {}).get("confidence")
    )
    evidence_df["rationale"] = evidence_df["artifact_id"].map(
        lambda rid: by_id.get(int(rid), {}).get("rationale")
    )

    if per_bucket_target is not None:
        pre = _per_bucket_counts_from_evidence_rows(evidence_df.to_dict("records"))
        short = [b for b in range(1, 8) if pre.get(b, 0) < int(per_bucket_target)]
        if short:
            warn = (
                f"warning: stratified ingest ended before every bucket reached "
                f"{per_bucket_target} classified rows; short={short} counts={pre}. "
                f"Increase --stratify-walk-max-files / DRIVE_STRATIFY_WALK_MAX, use "
                f"--from-xlsx/--from-csv with a richer pool, or pick a mixed Drive folder."
            )
            print(warn, flush=True)
            if progress_log:
                progress_log(warn)
        evidence_df = _trim_evidence_to_per_bucket(evidence_df, int(per_bucket_target))

    evidence_df = attach_subcategories(
        evidence_df,
        model=model,
        batch_size=batch_size,
        output_path_for_checkpoint=str(out_path),
    )

    def _series(name: str):
        if name not in evidence_df.columns:
            return pd.Series("", index=evidence_df.index)
        return evidence_df[name].fillna("").astype(str).replace({"nan": ""})

    evidence_df["content_type"] = [
        infer_content_types_cell(
            rationale=r,
            content_summary=cs,
            snippet=sn,
            filename=fn,
            path=ph,
        )
        for r, cs, sn, fn, ph in zip(
            _series("rationale"),
            _series("content_summary"),
            _series("snippet"),
            _series("filename"),
            _series("path"),
        )
    ]

    tok = (
        pd.to_numeric(evidence_df["token_count"], errors="coerce")
        .fillna(0)
        .astype("int64")
        if "token_count" in evidence_df.columns
        else pd.Series(0, index=evidence_df.index, dtype="int64")
    )
    wct = (
        pd.to_numeric(evidence_df["word_count"], errors="coerce")
        .fillna(0)
        .astype("int64")
        if "word_count" in evidence_df.columns
        else pd.Series(0, index=evidence_df.index, dtype="int64")
    )

    evidence_df["quality_tier"] = [
        infer_quality_tier(
            token_count=int(t),
            word_count=int(w),
            pii_flag=str(pi),
            modality=str(m),
            content_type=str(ct),
            extension=str(ex),
            confidence=str(cf),
        )
        for t, w, pi, m, ct, ex, cf in zip(
            tok,
            wct,
            _series("pii_flag"),
            _series("modality"),
            _series("content_type"),
            _series("extension"),
            _series("confidence"),
        )
    ]

    inventory_df = summarize_inventory_from_evidence(evidence_df, model=model)

    write_company_inventory_workbook(
        inventory_df=inventory_df,
        evidence_df=evidence_df,
        output_path=str(out_path),
    )

    lines = [
        f"Wrote company inventory to {out_path}",
        f"Evidence rows (workbook): {len(evidence_df)}",
        f"Ingested file rows (pre-trim): {len(evidence_rows)}",
        f"Classification checkpoint: {ckpt_path}",
        f"Extract checkpoint (resume): {arti_path}",
    ]
    return "\n".join(lines)
