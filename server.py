from __future__ import annotations

import env_loader  # noqa: F401 — load `.env` before LLM env reads

import os
import shutil
import tarfile
import tempfile
import threading
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from checkpoint import append_checkpoint, checkpoint_path_for_output, load_checkpoint
from excel_io import load_inventory, write_company_inventory_workbook, write_segmented_workbook
from extractors.core import (
    MAX_CHARS_DEFAULT,
    count_llm_tokens,
    count_words,
    extract_full_text,
    is_title_only_media,
    is_video_file,
    media_title_text,
    page_count_from_file,
)
from extractors.modality import format_modalities_cell, infer_modalities
from extractors.content_inventory import format_content_inventory_cell, gather_content_inventory
from extractors.content_type import infer_content_types_cell
from extractors.quality_tier import infer_quality_tier
from ingest import infer_source_system, iter_files
from inventory_writer import summarize_inventory_from_evidence
from pii import detect_pii
from artifact_summary import summarize_artifact
from llm_provider import llm_api_configured
from segmenter import DEFAULT_BATCH_SIZE, DEFAULT_MODEL, classify_dataframe, classify_items
from subcategory_classifier import attach_subcategories


def _download_and_extract_repo(
    *,
    provider: str,
    repo_url: str,
    ref: str,
    token: str | None,
) -> Path:
    """Download a repo archive (GitHub/GitLab) and extract to a temp folder.

    Token is used only for the HTTP request and is never persisted.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="inventory-segmenter-repo-")).resolve()
    archive_path = tmpdir / "repo.tar.gz"
    extract_dir = tmpdir / "extract"
    extract_dir.mkdir(parents=True, exist_ok=True)

    url = repo_url.strip()
    if provider == "github":
        parsed = urllib.parse.urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2:
            raise ValueError("GitHub repoUrl must look like https://github.com/<org>/<repo>")
        owner, repo = parts[0], parts[1].removesuffix(".git")
        # Use GitHub API tarball endpoint; this works for private repos with a token.
        # https://docs.github.com/en/rest/repos/contents?apiVersion=2022-11-28#download-a-repository-archive-tar
        download_url = f"https://api.github.com/repos/{owner}/{repo}/tarball/{urllib.parse.quote(ref)}"
        req = urllib.request.Request(download_url, method="GET")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "inventory-segmenter/0.1")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
    elif provider == "gitlab":
        parsed = urllib.parse.urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2:
            raise ValueError("GitLab repoUrl must look like https://gitlab.com/<group>/<project>")
        project_path = "/".join(parts).removesuffix(".git")
        project_enc = urllib.parse.quote(project_path, safe="")
        sha = urllib.parse.quote(ref)
        download_url = f"{parsed.scheme}://{parsed.netloc}/api/v4/projects/{project_enc}/repository/archive.tar.gz?sha={sha}"
        req = urllib.request.Request(download_url, method="GET")
        if token:
            req.add_header("PRIVATE-TOKEN", token)
    else:
        raise ValueError("provider must be 'github' or 'gitlab'")

    try:
        with urllib.request.urlopen(req) as resp, archive_path.open("wb") as f:
            shutil.copyfileobj(resp, f)
    except urllib.error.HTTPError as e:
        # GitHub returns 404 for private repos when unauthenticated, and 404 for bad refs.
        hint = ""
        if provider == "github" and e.code == 404 and not token:
            hint = " (repo may be private; provide a GitHub token with access)"
        raise RuntimeError(f"Repo archive download failed: HTTP {e.code}{hint}") from e

    with tarfile.open(archive_path, "r:gz") as tf:
        tf.extractall(path=extract_dir)

    children = [p for p in extract_dir.iterdir() if p.is_dir()]
    return children[0] if len(children) == 1 else extract_dir


def segment_inventory(
    input_path: str,
    output_path: str,
    sheet_name: str | None = None,
    description_column: str | None = None,
    source_column: str | None = None,
    model: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> str:
    """Segment a data-inventory spreadsheet into seven buckets.

    Reads the inventory at input_path, classifies every row using the
    LLM API, and writes a workbook to output_path containing a
    Master sheet, one sheet per bucket, and a Summary sheet.

    Args:
        input_path: Absolute path to the input .xlsx or .csv file.
        output_path: Absolute path where the segmented .xlsx will be written.
        sheet_name: Optional sheet name to read (defaults to the first sheet).
        description_column: Override auto-detection of the description column.
        source_column: Override auto-detection of the source/system column.
        model: Model id for the active provider (Anthropic or OpenAI).
        batch_size: Rows per LLM API call. Defaults to 25.

    Returns:
        A short status string with row counts per bucket.
    """
    in_path = Path(input_path).expanduser().resolve()
    out_path = Path(output_path).expanduser().resolve()

    if not in_path.exists():
        raise FileNotFoundError(f"Input file not found: {in_path}")
    if not llm_api_configured():
        raise RuntimeError(
            "No LLM API key configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY / "
            "an OpenAI-style key with LLM_PROVIDER=openai — see .env.example."
        )

    df, desc_col, src_col = load_inventory(
        str(in_path),
        sheet_name=sheet_name,
        description_column=description_column,
        source_column=source_column,
    )
    df = classify_dataframe(
        df,
        description_column=desc_col,
        source_column=src_col,
        model=model,
        batch_size=batch_size,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_segmented_workbook(df, str(out_path))

    counts = df["bucket_number"].value_counts().sort_index()
    lines = [
        f"Wrote {len(df)} classified items to {out_path}",
        f"Description column: {desc_col}",
        f"Source column: {src_col or '(none detected)'}",
        "Bucket counts:",
    ]
    for num, count in counts.items():
        lines.append(f"  {int(num)}: {int(count)}")
    return "\n".join(lines)


def build_inventory_from_dump(
    dump_path: str,
    output_path: str,
    model: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_files: int | None = None,
    strict_scan: bool = False,
    workers: int = 1,
) -> str:
    """Build the 7-row company inventory (desired format) from a dump folder.

    Scans files under dump_path, extracts text, classifies each artifact into one
    of seven buckets via the LLM, then writes an .xlsx to output_path containing:
      - Inventory (desired 4 columns)
      - Evidence (one row per artifact for auditability)

    workers > 1 parallelises file extraction (I/O + parsing) and LLM API batches.
    """
    dump_root = Path(dump_path).expanduser().resolve()
    out_path = Path(output_path).expanduser().resolve()

    if not dump_root.exists():
        raise FileNotFoundError(f"Dump folder not found: {dump_root}")
    if not llm_api_configured():
        raise RuntimeError(
            "No LLM API key configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY / "
            "an OpenAI-style key with LLM_PROVIDER=openai — see .env.example."
        )

    ckpt_path = checkpoint_path_for_output(str(out_path))
    existing = load_checkpoint(ckpt_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    exclude_dirs = () if strict_scan else (".venv", "__pycache__")
    include_hidden = bool(strict_scan)

    # Collect all file paths upfront for deterministic artifact_id assignment.
    all_files = list(iter_files(str(dump_root), exclude_dirs=exclude_dirs, include_hidden=include_hidden))
    if max_files is not None:
        all_files = all_files[:max_files]
    total = len(all_files)
    print(f"[scan] total files: {total}", flush=True)

    def _extract_one(artifact_id: int, p: Path) -> tuple[dict, dict | None]:
        """Extract evidence from one file. Thread-safe (no shared mutable state).
        Returns (ev_row, pending_item_or_None).
        """
        stat = p.stat()
        rel_path = str(p.resolve().relative_to(dump_root)).replace("\\", "/")
        filename = p.name
        extension = p.suffix.lower().lstrip(".")
        source_guess = infer_source_system(str(p))

        if is_title_only_media(p):
            title = media_title_text(p)
            snippet_str = (title or "")[:800]
            word_count = len(title.split()) if title else 0
            token_count = count_llm_tokens(title) if title else 0
            page_count = 0
            modalities = infer_modalities(p)
            inv_counts = gather_content_inventory(p)
            kind = "video" if is_video_file(p) else "audio"
            desc = (
                f"{filename} ({extension or 'noext'}, {kind} file). "
                "Only the file title/name is read for classification (no media content).\n"
                f"Title: {title}\nPath: {rel_path}"
            )
            pii = detect_pii(f"{title}\n{rel_path}")
            content_summary = summarize_artifact(
                filename=filename, rel_path=rel_path, extension=extension, snippet=title or None,
            )
        else:
            full_text = extract_full_text(p)
            snippet: str | None = (
                (full_text[:MAX_CHARS_DEFAULT] + "\n…(truncated)…")
                if full_text and len(full_text) > MAX_CHARS_DEFAULT
                else full_text
            )
            word_count = count_words(full_text) if full_text else 0
            token_count = count_llm_tokens(full_text) if full_text else 0
            page_count = page_count_from_file(p, word_count=word_count)
            modalities = infer_modalities(p)
            inv_counts = gather_content_inventory(p)
            desc = f"{filename} ({extension or 'noext'}, {int(stat.st_size)} bytes)"
            if snippet:
                desc = desc + "\n" + snippet
            pii = detect_pii(snippet)
            content_summary = summarize_artifact(
                filename=filename, rel_path=rel_path, extension=extension, snippet=snippet,
            )
            snippet_str = (snippet or "")[:800]

        ev: dict = {
            "artifact_id": artifact_id,
            "path": rel_path,
            "filename": filename,
            "extension": extension,
            "size_bytes": int(stat.st_size),
            "modified_time_unix": float(stat.st_mtime),
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
        }
        pend: dict | None = None
        if artifact_id not in existing:
            pend = {
                "row_id": artifact_id,
                "description": desc,
                "source": source_guess,
                "filename": filename,
                "extension": extension,
                "path_hint": rel_path,
                "snippet": snippet_str,
            }
        return ev, pend

    # --- Phase 1: Extract (parallel when workers > 1) ---
    evidence_rows: list[dict] = [{}] * total
    pending: list[dict] = []

    if workers > 1:
        done_count = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futs = {executor.submit(_extract_one, i + 1, p): i + 1 for i, p in enumerate(all_files)}
            for fut in as_completed(futs):
                aid = futs[fut]
                try:
                    ev, pend = fut.result()
                except Exception as exc:
                    print(f"[warn] extraction failed artifact_id={aid}: {exc}", flush=True)
                    continue
                evidence_rows[aid - 1] = ev
                if pend:
                    pending.append(pend)
                done_count += 1
                if done_count % 500 == 0:
                    print(f"[extract] done={done_count}/{total}", flush=True)
        evidence_rows = [r for r in evidence_rows if r]
        pending.sort(key=lambda x: x["row_id"])
    else:
        for i, p in enumerate(all_files):
            artifact_id = i + 1
            try:
                ev, pend = _extract_one(artifact_id, p)
            except Exception as exc:
                print(f"[warn] extraction failed artifact_id={artifact_id}: {exc}", flush=True)
                continue
            evidence_rows[i] = ev
            if pend:
                pending.append(pend)
            if artifact_id % 500 == 0:
                print(f"[progress] processed={artifact_id}/{total}", flush=True)
        evidence_rows = [r for r in evidence_rows if r]

    print(f"[extract] done. evidence={len(evidence_rows)} to_classify={len(pending)}", flush=True)

    # --- Phase 2: Classify (batches via workers) ---
    classified = 0
    ckpt_lock = threading.Lock()

    def flush_batch(batch: list[dict]) -> None:
        nonlocal classified
        if not batch:
            return
        batch_out = classify_items(batch, model=model, batch_size=batch_size, workers=workers)
        cleaned: list[dict] = []
        for c in (batch_out or []):
            if not isinstance(c, dict):
                print(f"[warn] classify_items returned non-dict: {c!r}", flush=True)
                continue
            rid = c.get("row_id")
            try:
                rid_i = int(rid)
            except Exception:
                print(f"[warn] uncastable row_id={rid!r}", flush=True)
                continue
            c["row_id"] = rid_i
            cleaned.append(c)
        append_checkpoint(ckpt_path, cleaned)
        with ckpt_lock:
            for c in cleaned:
                try:
                    existing[int(c["row_id"])] = c
                except Exception:
                    pass
            classified += len(cleaned)
        print(f"[batch] classified={classified} checkpoint={ckpt_path.name}", flush=True)

    for start in range(0, len(pending), batch_size):
        flush_batch(pending[start : start + batch_size])

    by_id = existing

    import pandas as pd

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

    evidence_df = attach_subcategories(
        evidence_df,
        model=model,
        batch_size=batch_size,
        output_path_for_checkpoint=str(out_path),
        workers=workers,
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
        f"Artifacts scanned: {total}",
        "Sheets: Inventory, Overview, Sample Files, Evidence, + bucket tabs",
        f"Checkpoint: {ckpt_path}",
    ]
    return "\n".join(lines)


def build_inventory_from_repo(
    provider: str,
    repo_url: str,
    output_path: str,
    ref: str = "main",
    model: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_files: int | None = None,
    workers: int = 1,
) -> str:
    """Build the clean multi-tab inventory workbook from a Git repo URL.

    Tokens (do NOT pass as args; set via env so they are not persisted):
      - GitHub: GITHUB_TOKEN
      - GitLab: GITLAB_TOKEN
    """
    token = None
    if provider == "github":
        token = os.environ.get("GITHUB_TOKEN")
    elif provider == "gitlab":
        token = os.environ.get("GITLAB_TOKEN")

    repo_root = _download_and_extract_repo(provider=provider, repo_url=repo_url, ref=ref, token=token)
    return build_inventory_from_dump(
        dump_path=str(repo_root),
        output_path=output_path,
        model=model,
        batch_size=batch_size,
        max_files=max_files,
        workers=workers,
    )


def build_inventory_from_drive(
    folder_id: str,
    output_path: str,
    model: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_files: int | None = None,
) -> str:
    """Walk Google Drive from ``folder_id``, download/export each file, classify and enrich (full quality).

    Uses the same extract + LLM path as :func:`build_inventory_from_dump`. Requires OAuth token
    with **drive.readonly** — if your token was created with metadata-only scope, run
    ``python tools/gdrive_scan.py --login-only --full-read-scope`` once, then re-run this tool.
    """
    from gdrive.credentials import (
        build_drive_service,
        default_client_secrets_path,
        default_token_path,
        get_credentials,
    )
    from gdrive.pipeline_build import build_inventory_from_drive as _drive_quality_run

    creds = get_credentials(
        client_secrets=default_client_secrets_path(),
        token_path=default_token_path(),
        full_read_scope=True,
        login_only=False,
    )
    service = build_drive_service(creds)
    return _drive_quality_run(
        service=service,
        folder_id=folder_id,
        output_path=output_path,
        model=model,
        batch_size=batch_size,
        max_files=max_files,
    )


def _run_mcp() -> None:
    """Start the MCP server (imports `mcp` only when this entrypoint runs)."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("inventory-segmenter")
    mcp.tool()(segment_inventory)
    mcp.tool()(build_inventory_from_dump)
    mcp.tool()(build_inventory_from_repo)
    mcp.tool()(build_inventory_from_drive)
    mcp.run()


if __name__ == "__main__":
    _run_mcp()
