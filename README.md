# Inventory Segmentor

Scans a company's data — either a **local folder dump** or a **Google Drive** — and produces a structured Excel inventory segmented into seven business buckets:

| # | Bucket |
| --- | --- |
| 1 | Product & Engineering |
| 2 | Customer & Sales |
| 3 | Strategy & Planning |
| 4 | Financial & Legal |
| 5 | Operations & HR |
| 6 | Marketing |
| 7 | Meeting Notes & Internal Comms |

Each row in the output workbook gets a `bucket`, `confidence` score, and `rationale`. The workbook has a **Master** sheet, one sheet per bucket, and a **Summary**.

---

## Install

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Run the setup wizard — it will ask for your API key and optionally set up Google Drive access:

```bash
python setup.py
```

---

## Run — Local folder

Point it at any folder containing company files (exports, docs, CSVs, etc.):

```bash
python run_dump.py --dump /path/to/company/dump --out out/inventory.xlsx
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--dump` | *(required)* | Folder to scan |
| `--out` | *(required)* | Output `.xlsx` path |
| `--model` | from `.env` | LLM model override |
| `--batch-size` | `25` | Rows per API call |
| `--max-files` | `0` (unlimited) | Cap file count for testing |
| `--strict` | off | Include hidden files/dirs |

---

## Run — Google Drive

### 1. One-time Google Cloud setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → create or pick a project.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **OAuth consent screen** → set up (Internal for Workspace, External for personal Gmail).
4. **Credentials → Create credentials → OAuth client ID**
   - Choose **Desktop app** (simplest — no redirect URI needed).
   - Download the JSON → save as `.secrets/google_oauth_client.json`.
5. Set in `.env`:

```env
GOOGLE_OAUTH_CLIENT_SECRETS=.secrets/google_oauth_client.json
GOOGLE_OAUTH_TOKEN_PATH=.secrets/google_drive_token.json
```

### 2. First login (one time)

```bash
python tools/gdrive_scan.py --login-only
```

A browser tab opens — sign in with the Google account that has Drive access. A token is saved to `.secrets/google_drive_token.json`. Treat it like a password (it is gitignored).

### 3. Run the inventory

Scan your entire Google Workspace (My Drive + all Shared Drives):

```bash
python run_drive.py --all-drives --out out/drive_inventory.xlsx
```

Scan only selected users' My Drives (service account + Domain-Wide Delegation required):

```bash
python run_drive.py \
  --users alice@yourdomain.com bob@yourdomain.com \
  --service-account .secrets/service_account.json \
  --admin-email admin@yourdomain.com \
  --out out/selected_inventory.xlsx
```

Add `--all-drives` with `--users` to include Shared Drives while still limiting My Drive scans to those emails.

Or scan a specific folder only:

```bash
python run_drive.py \
  --folder-id "https://drive.google.com/drive/folders/1abc..." \
  --out out/drive_inventory.xlsx \
  --workers 8
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--all-drives` | off | Scan My Drive + every Shared Drive |
| `--users` | *(none)* | Space- or comma-separated emails — scan only those My Drives (requires `--service-account`) |
| `--folder-id` | `root` | Drive folder ID or full URL (ignored when `--all-drives` / `--users` is set) |
| `--service-account` | from `.env` | Service account JSON for Domain-Wide Delegation |
| `--admin-email` | from `.env` | Super-admin email to impersonate with the service account |
| `--out` | `out/drive_1tb_inventory.xlsx` | Output `.xlsx` path |
| `--workers` | `8` | Parallel download workers |
| `--max-files` | `0` (unlimited) | Cap file count for testing |
| `--snippet-bytes` | `2048` | Bytes extracted per text file |
| `--pass1-model` | auto | Fast model for first-pass classification |
| `--pass2-model` | from `.env` | Full model for low-confidence re-classification |

The Drive run is **fully resumable** — if interrupted, re-run the same command and it picks up from where it left off via checkpoint files in `out/`.

---

## Project layout

```text
run_dump.py               Entry point — local folder scan
run_drive.py              Entry point — Google Drive scan
server.py                 MCP server (exposes segment_inventory tool)
segmenter.py              LLM classification logic, batching, tool-use
excel_io.py               Reads input, writes segmented workbook
prompt.py                 System prompt + taxonomy (edit to tune classifier)
subcategory_classifier.py Sub-category enrichment pass
subcategory_taxonomy.py   Sub-category label definitions
checkpoint.py             Resume/checkpoint helpers
llm_provider.py           Anthropic / OpenAI abstraction
ingest.py                 Artifact scanner for local folders
extractors/               File-type extractors (docx, pdf, html, ...)
gdrive/                   Google Drive API client + pipeline
tools/                    Utility scripts (scan, merge, export, ...)
sample/                   Sample files organized by bucket (for testing)
web/                      Next.js viewer UI
```

---

## Tuning

- **Taxonomy** — edit `SEGMENTATION_SYSTEM_PROMPT` in `prompt.py`. The seven-bucket schema is enforced via structured tool-use; to change the count, also update `BUCKETS` in `excel_io.py` and the constraint in `segmenter.py`.
- **Model** — set `ANTHROPIC_MODEL` / `OPENAI_MODEL` / `GEMINI_MODEL` in `.env` for your provider (see `.env.example`).
- **Batch size** — `SEGMENTER_BATCH_SIZE` in `.env` (default `25`). Larger = fewer API calls; smaller = better for long descriptions.

---

## Environment variables

See `.env.example` for the full reference with defaults and comments.
