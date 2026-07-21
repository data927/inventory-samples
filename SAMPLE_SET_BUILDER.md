# Sample Set Builder — `tools/build_sample_set.py`

Builds a **curated, AI-scored sample set** where every bucket gets 5–10 files selected for content richness, category fit, and sample quality — chosen by OpenAI, not by hand.

The script has two modes depending on where your files live:

| Mode | When to use | How files are obtained |
| --- | --- | --- |
| **Local** | Files already downloaded into bucket folders | Copied from `--src` |
| **Drive** | Files are on Google Drive; you have a drive inventory JSONL | Downloaded fresh via Google Drive API |

---

## How it works

### Local mode

```
sample_original/
  Customer & Sales/      ← 9 files (already on disk)
  Product & Engineering/ ← 64 files
  ...
          │
          │  build_sample_set.py --src sample_original
          │    1. extract text snippet from each local file
          │    2. score with OpenAI (bucket already known)
          │    3. pick top 5-10 per bucket
          │    4. copy files
          ▼
sample_final/
  Customer & Sales/      ← up to 10 best
  Product & Engineering/ ← 10 best (54 excluded)
  ...
  _selection_manifest.json
```

### Drive mode

```
out/drive_1tb_inventory.drive_artifacts.jsonl
  (124k rows: drive_url + snippet + word_count, NO bucket assignments)
          │
          │  build_sample_set.py --drive-jsonl out/drive_1tb_...jsonl
          │    1. filter out binary/empty files
          │    2. OpenAI classifies bucket AND scores quality in one step
          │    3. pick top 5-10 per bucket
          │    4. download selected files from Google Drive API
          ▼
sample_final/
  Customer & Sales/      ← downloaded from Drive
  Product & Engineering/ ← downloaded from Drive
  ...
  _selection_manifest.json  ← includes drive_url for every file
```

The drive JSONL is produced by `run_drive.py`. It has `drive_url`, `snippet`, `word_count`, and `size_bytes` per file but no bucket assignments. The script uses OpenAI to classify and score in a single step, extracts the `file_id` from each `drive_url`, then downloads via `gdrive/fetch.py`.

---

### Scoring axes (both modes)

OpenAI scores every candidate on three axes (1–10):

- **content_richness** — how substantial and informative the content is
- **category_fit** — how well the file exemplifies its bucket
- **sample_quality** — training/demo value; penalizes templates, stubs, near-duplicates

Files are ranked by the composite mean. Files the model is certain are worthless (empty, binary noise, unfilled templates) are flagged `should_reject` and excluded first.

---

## Prerequisites

- Python ≥ 3.10
- An **OpenAI API key** (`sk-proj-…` prefix) — the script explicitly uses the OpenAI Chat Completions API.
- The repo already installed (see [Install](#install)).

---

## Install

### 1. Clone the repo

```bash
# HTTPS
git clone https://github.com/data927/inventory-segmentor.git
cd inventory-segmentor

# SSH (if you have a key set up)
# git clone git@github.com:data927/inventory-segmentor.git
```

Or pull the latest if you already have it:

```bash
git pull origin main
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows PowerShell
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

This installs everything in one shot: `openai`, `anthropic`, `PyMuPDF`, `python-docx`, `python-pptx`, `tiktoken`, `openpyxl`, `html2docx`, and the rest.

### 4. Configure your OpenAI API key

Copy the example env file and fill in your key:

```bash
cp .env.example .env
```

Open `.env` and set:

```env
OPENAI_API_KEY=sk-proj-...your-key-here...
```

> **Auto-detection:** The repo detects the provider from the key prefix.  
> A `sk-proj-…` key → OpenAI. A `sk-ant-…` key → Anthropic. `AIza…` → Gemini.  
> You do **not** need to set `LLM_PROVIDER` manually.

Optionally pin the model (default is `gpt-4o-mini`):

```env
OPENAI_MODEL=gpt-4o-mini
```

---

## Prepare your source folder

The script expects a directory where **each subdirectory is a bucket** and contains the candidate files:

```
sample_original/
  Customer & Sales/
    file1.html
    file2.pdf
    ...
  Financial & Legal/
    contract.pdf
    ...
  Product & Engineering/
    architecture.html
    ...
```

The bucket names can be anything — they are passed verbatim to OpenAI as the category label.  
Supported file types: `.html`, `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.xls`, `.csv`, `.txt`, `.md`.

If you are starting from the `sample_original/` directory already in the repo, you are ready to go.

---

## Run

### Dry run first (no files copied, full scoring report printed)

```bash
python tools/build_sample_set.py \
  --src sample_original \
  --dst sample_final \
  --dry-run
```

Sample output:

```
INFO Model: gpt-4o-mini
INFO Source: /path/to/sample_original
INFO Output: /path/to/sample_final

=== Bucket: Customer & Sales ===
INFO   Found 9 files
INFO   Scoring files 1-9 / 9 ...

  SELECTED (9):
     9.0  cr=8 cf=10 sq=9  Sales_Playbook.docx.pdf
     7.0  cr=6 cf=8 sq=7   Early incident detection - Customer story...html
     6.7  cr=6 cf=7 sq=7   Sales Update...html
     ...

=== Bucket: Product & Engineering ===
INFO   Found 64 files
INFO   Scoring files 1-15 / 64 ...
INFO   Scoring files 16-30 / 64 ...
  ...

  SELECTED (10):
     9.3  cr=9 cf=10 sq=9  LogNER...html
     9.3  cr=9 cf=10 sq=9  PacketAI A Human-Centric AIOps Framework...html
     ...
  EXCLUDED (54):
     8.3  Metron 07 11 22...html
     ...

[dry-run] No files copied.
```

Review the selections. If a bucket looks wrong, you can adjust `--min` / `--max` or swap files in the source folder before the real run.

### Real run (copies files)

```bash
python tools/build_sample_set.py \
  --src sample_original \
  --dst sample_final
```

When it finishes you'll see:

```
=== Final sample set ===
  Customer & Sales: 9 files
  Financial & Legal: 9 files
  Marketing: 6 files
  Meeting Notes & Internal Comms: 10 files
  Operations & HR: 6 files
  Product & Engineering: 10 files
  Strategy & Planning: 8 files

Total: 58 files → /path/to/sample_final
Manifest written: sample_final/_selection_manifest.json
```

---

## CLI reference

```
python tools/build_sample_set.py [OPTIONS]
```

| Flag | Default | Description |
| --- | --- | --- |
| `--src` | `sample_original` | Source directory containing bucket folders |
| `--dst` | `sample_final` | Output directory; bucket folders are created automatically |
| `--min` | `5` | Minimum files to select per bucket (backfills from rejects if needed) |
| `--max` | `10` | Maximum files to select per bucket |
| `--model` | `gpt-4o-mini` | OpenAI model; override with e.g. `gpt-4o` for higher quality |
| `--dry-run` | off | Score and report without copying any files |

---

## Output

### `sample_final/[bucket]/`

One subfolder per bucket containing only the selected files, unchanged from the source.

### `sample_final/_selection_manifest.json`

Machine-readable record of every selected file and its scores:

```json
{
  "Product & Engineering": [
    {
      "filename": "LogNER 0234d4f22f0646f4ade6dbc2c40967c3.html",
      "composite": 9.3,
      "content_richness": 9,
      "category_fit": 10,
      "sample_quality": 9,
      "should_reject": false,
      "backfill": false,
      "note": ""
    },
    ...
  ],
  ...
}
```

`composite` is the arithmetic mean of the three scores. Files are ordered highest-score first within each bucket.

---

## Scoring criteria

OpenAI evaluates each file in context of its bucket. The three axes:

| Axis | 1 (worst) | 10 (best) |
| --- | --- | --- |
| `content_richness` | Blank, whitespace-only, or just a title line | Rich, complete document with real information |
| `category_fit` | Clearly wrong bucket or off-topic | Canonical example of the category |
| `sample_quality` | Template skeleton, duplicate, or noise | Unique, high-value, perfectly representative |

A file is flagged `should_reject = true` only when the model is certain it is worthless — completely empty, a structural template with no data, or a near-duplicate of a clearly better file in the same batch. Borderline files are scored low but kept.

If a bucket has fewer than `--min` qualifying files after rejections, the script **backfills** from the rejected pool (lowest-harm rejects first) and logs a warning, so you are never left with an empty bucket.

---

## Troubleshooting

### `No LLM API key configured`

Your `.env` file is missing or the key is commented out. Check:

```bash
grep -i api_key .env
```

Make sure the line is uncommented and the value starts with `sk-proj-` (OpenAI).

### `Source directory not found`

The `--src` path does not exist. Run from the repo root, or pass an absolute path:

```bash
python tools/build_sample_set.py --src /absolute/path/to/sample_original
```

### Detected non-OpenAI model — switching to `gpt-4o-mini`

If `.env` points at a non-OpenAI model, the script auto-switches to `gpt-4o-mini`. To use a specific OpenAI model instead, pass `--model gpt-4o`.

### Extraction returns empty snippets for some files

Some PDF files are image-only (scanned documents). The extractor will return an empty snippet; the file gets a neutral score of `5/5/5` and may still be selected if there are no better alternatives. Use `--dry-run` to inspect which files came back empty before committing to a run.

### Batch scoring fails for a bucket

If an individual API call fails, the script assigns a neutral score (`5/5/5`) to all files in that batch, logs an error, and continues. Re-run the script — it will re-score everything from scratch (there is no partial-results cache).

---

## Cost estimate

All calls use `gpt-4o-mini`. A typical run over 130 source files (7 buckets) makes ~12 API calls and costs under **$0.02**.

To use a higher-quality model for critical selections:

```bash
python tools/build_sample_set.py --src sample_original --dst sample_final --model gpt-4o
```

---

## Related

- `sample_original/` — source bucket folders (HTML, PDF, DOCX, PPTX files)
- `sample_copy/` — same files converted to `.docx` (produced by `tools/batch_html_to_docx.py`)
- `sample_final/` — curated output of this script
- `extractors/core.py` — text extraction pipeline used by this script
- `llm_provider.py` — provider abstraction (OpenAI / Anthropic / Gemini)
