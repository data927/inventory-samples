# AI Labs Sample Set — Client Setup Guide

This guide builds a curated sample set in **your own Google Drive** (and, optionally, your own Gmail).

It creates a new folder like `AI Labs Sample Set (YYYY-MM-DD)` in your My Drive, with subfolders by category, and copies the selected files into it. Gmail threads (if you build a manifest that includes them) go into your own Gmail Inbox instead.

You sign in with **your** Google account for the copy step. No shared folder and no one else's token is required.

There are two roles in this guide:

- **Client** — signs in with their own Google account and runs the copy steps (Parts 1, 2, 4, 6, 8). No admin access needed.
- **Operator** — the person running the extraction for this engagement, who has a Service Account with Domain-Wide Delegation and a Workspace admin email. Only needed for Part 5 (building a fresh manifest across the whole Workspace).

> **Already ran Inventory Segmentor on this machine?**  
> Skip to [Part 4 — Google Drive login](#part-4--google-drive-login) if the project is already cloned and `.venv` works. Then jump to [Part 6 — Build the sample set](#part-6--build-the-sample-set).

---

## What you need

- The **same Google account** you used when the Drive inventory was run
- Python 3 and Git (steps below if you don't have them)
- An OAuth client JSON file (often already set up from Inventory Segmentor — see Part 4)
- **(Operator only, Part 5)** A Service Account JSON with Domain-Wide Delegation, and a Workspace super-admin email — see `CLIENT_SETUP.md` → Part 4B
- **(Optional, Part 8 — Gmail thread samples)** The `gmail.insert` scope enabled on the OAuth client, added to its consent screen in Google Cloud Console

**You do not need** an Anthropic / OpenAI / Gemini API key for any of this.

---

## Prerequisites

### 1. Install Python

1. Go to: **https://www.python.org/downloads/**
2. Download and run the installer.
   - **Important (Windows):** check **"Add Python to PATH"** before Install.
3. Verify in Terminal / Command Prompt:

```
python --version
```

You should see something like `Python 3.12.x`.

### 2. Install Git

1. Go to: **https://git-scm.com/downloads**
2. Install with defaults.
3. Verify:

```
git --version
```

---

## Part 1 — Get the Code

Open **Terminal** (Mac) or **Command Prompt** (Windows).

```
cd ~
```

```
git clone https://github.com/data927/inventory-segmentor.git
```

> Or use the exact link / zip you were given.

```
cd inventory-segmentor
```

Confirm the sample list is present:

```
ls data/ai_labs_1200_balanced_sample.json
```

Mac / Linux: if `ls` fails, try:

```
ls data/
```

You should see `ai_labs_1200_balanced_sample.json`. That file is **bundled in the repo** as a fallback default — you do not need any Excel file. If Part 5 below has already been run for this engagement, you'll use its output manifest instead (see Part 6).

---

## Part 2 — Python environment

**Create the virtual environment:**

Mac / Linux:
```
python3 -m venv .venv
```

Windows:
```
python -m venv .venv
```

**Activate it:**

Mac / Linux:
```
source .venv/bin/activate
```

Windows:
```
.venv\Scripts\activate
```

You should see `(.venv)` at the start of the line.

**Install packages:**

```
pip install -r requirements.txt
```

---

## Part 3 — (Optional) If you never set up Google Drive here

If you **already** completed Google Drive setup for Inventory Segmentor, skip to Part 4.

If this is a fresh machine:

```
python setup.py
```

- When asked for an **API key**, press Enter to skip (not required for this tool).
- When asked about **Google Drive**, type `y` and follow the prompts.
- Point it at your OAuth client JSON (often in Downloads: `client_secret_....json`).
- A browser window will open — sign in with the **same Google account** that owns the Drive inventory.

---

## Part 4 — Google Drive login

### Important

Use the **same Google account** that was used to scan this Drive for the inventory.

The sample list refers to file IDs from that Drive. A different account will get many failures (`404` / `403`).

### First-time write access

This tool needs permission to **create a folder and copy files** in your Drive (broader than the inventory's read-only login). The first run opens a browser — click **Allow**.

Your OAuth client file should already be at something like:

```
.secrets/google_oauth_client.json
```

If setup put it elsewhere, that is fine as long as `.env` points to it (`GOOGLE_OAUTH_CLIENT_SECRETS=...`).

---

## Part 5 — (Operator) Build a fresh, size-capped sample manifest

**Skip this part if you're the client running the bundled sample set — jump to Part 6.** This part is for whoever is running the extraction for the engagement, using the admin-level Service Account.

`data/ai_labs_1200_balanced_sample.json` (used by default in Part 6) is a fixed, hand-picked list. For a fresh engagement, `tools/build_quality_sample.py` instead scans the **entire live Workspace** — every user's My Drive + Shared Drives, every user's Gmail — and builds a new manifest using two selection rules:

- **Binary files** (PDFs, Office docs, images, etc.) and **Gmail threads** — largest first (size is used as a quality proxy; there's no LLM scoring pass) until a byte cap is hit:
  - **Drive:** ~75GB
  - **Gmail:** ~10–15GB (default 12.5GB)
- **Google-native Docs/Sheets/Slides** — these have no fixed byte size, so they can't be size-ranked. Instead each type gets its own fixed count, most-recently-modified first, **on top of** (not counted against) the 75GB Drive cap:
  - **Google Sheets:** 350
  - **Google Docs:** 300
  - **Google Slides:** 150

Gmail messages are grouped into **whole threads** before selection — a thread is included or skipped as one unit, so a thread never gets split across the include/exclude line.

### Prerequisite

Service Account + Domain-Wide Delegation, already set up. If not, see `CLIENT_SETUP.md` → **Part 4B — Service Account with Domain-Wide Delegation** and come back here once you have the service account JSON path and a super-admin email.

### Run it

```
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com
```

This can take a while for a large Workspace — it caches scan progress under `out/` (`*.drive_scan_cache.jsonl`, `gmail_ids__*.txt`), so re-running the same command resumes rather than rescanning from zero.

### Optional flags

| Flag | Default | What it does |
| --- | --- | --- |
| `--drive-cap-gb` | `75` | Byte cap for binary Drive files, in GB |
| `--gmail-cap-gb` | `12.5` | Gmail selection cap, in GB |
| `--gsheets-limit` | `350` | Max Google Sheets, most-recently-modified first |
| `--gdocs-limit` | `300` | Max Google Docs, most-recently-modified first |
| `--gslides-limit` | `150` | Max Google Slides, most-recently-modified first |
| `--out` | `out/quality_sample_manifest.json` | Where the manifest is written |
| `--gmail-query` | (none) | Gmail search query to filter candidate messages (e.g. `after:2024/01/01`) |
| `--skip-drive` | off | Skip Drive scanning entirely |
| `--skip-gmail` | off | Skip Gmail scanning entirely |

### Output

`out/quality_sample_manifest.json`, containing:

- `"files"` — selected Drive files: binary files under the 75GB cap, plus the Sheets/Docs/Slides quotas (same shape Part 6's script already reads)
- `"gmail_threads"` — selected Gmail threads, each with its `message_ids` (used by Part 8)
- `"drive_native_selected"` — how many Sheets/Docs/Slides were actually included (useful if a Workspace has fewer than the quota of a given type)

Once this finishes, use `--manifest out/quality_sample_manifest.json` in Part 6 (and Part 8) instead of the bundled default.

---

## Part 6 — Build the sample set

Make sure you are in the project folder with `.venv` activated:

```
cd ~/inventory-segmentor
source .venv/bin/activate
```

Windows:
```
cd %USERPROFILE%\inventory-segmentor
.venv\Scripts\activate
```

By default this uses the bundled `data/ai_labs_1200_balanced_sample.json`. If an operator ran Part 5 for this engagement, add `--manifest out/quality_sample_manifest.json` to every command below to use that fresh manifest instead.

### Step A — Preview (no changes to Drive)

```
python tools/export_ai_labs_samples.py --dry-run
```

You should see the selected files listed across categories such as:

- Product & Engineering  
- Financial & Legal  
- Operations & HR  
- Marketing  
- Strategy & Planning  
- Customer & Sales  
- Meeting Notes & Internal Comms  
- Uncategorized  

### Step B — Smoke test (5 files)

```
python tools/export_ai_labs_samples.py --limit 5
```

1. Browser opens → sign in → **Allow** Drive access.
2. When it finishes, open [Google Drive](https://drive.google.com) → **My Drive**.
3. Look for a folder named like **`AI Labs Sample Set (YYYY-MM-DD)`**.
4. Inside it you should see category folders with a few copied files.

If those 5 look correct, continue.

### Step C — Full sample set

```
python tools/export_ai_labs_samples.py
```

This can take a while. It is safe to stop and re-run the **same** command — it resumes from a checkpoint and will not re-copy files it already finished.

Optional custom folder name:

```
python tools/export_ai_labs_samples.py --folder-name "Goldsetu AI Labs Samples"
```

---

## Part 7 — Find your output

In Google Drive → **My Drive**:

```
AI Labs Sample Set (YYYY-MM-DD)/
  Product & Engineering/
  Financial & Legal/
  Operations & HR/
  Marketing/
  ...
```

The script also prints a direct link at the end, for example:

```
their folder: https://drive.google.com/drive/folders/.........
```

---

## Part 8 — Gmail thread samples (optional)

Only relevant if Part 5's manifest includes a `"gmail_threads"` list. This copies those threads — **whole threads, never split** — into your own Gmail Inbox, so Gmail re-threads them correctly on arrival.

### One-time setup (operator, before first use)

The OAuth client used by this project needs the `gmail.insert` scope added to its **consent screen** in Google Cloud Console. If the app is still in **Testing** publish status, also add the signing-in Google account as a **test user** there. This is separate from the Drive scopes already in use and needs its own first-time consent.

### Copy the threads

Same shape as Part 6 — dry-run, then a small smoke test, then the full run:

```
python tools/export_ai_labs_gmail_threads.py --service-account ~/Downloads/service_account.json --dry-run
```

```
python tools/export_ai_labs_gmail_threads.py --service-account ~/Downloads/service_account.json --limit 5
```

```
python tools/export_ai_labs_gmail_threads.py --service-account ~/Downloads/service_account.json
```

1. Browser opens the **first time** you insert → sign in → **Allow**. The token is cached at `.secrets/google_gmail_insert_token.json` so you won't be asked again.
2. Open Gmail → the selected threads appear in your **Inbox**, correctly threaded.
3. Safe to stop and re-run — it resumes from `out/ai_labs_gmail_threads.checkpoint.jsonl` and won't re-insert threads it already finished.

By default this reads `out/quality_sample_manifest.json` (Part 5's output); pass `--manifest` to point elsewhere.

---

## Troubleshooting

| Problem | What to do |
| --- | --- |
| Browser asks to sign in again | Use the **same** account that ran the Drive inventory |
| Many `FAIL` / `404` / `notFound` | Wrong Google account, or files were deleted since the inventory |
| Many `403` / permission errors | Sign in as the inventory account; confirm you can open those files in Drive normally |
| `OAuth client secrets not found` | Re-run `python setup.py` and complete the Google Drive step, or place the client JSON under `.secrets/google_oauth_client.json` |
| `sample manifest not found` | Update/pull the latest code so `data/ai_labs_1200_balanced_sample.json` exists, or check the `--manifest` path |
| Interrupted halfway | Re-run the same command — both `export_ai_labs_samples.py` and `export_ai_labs_gmail_threads.py` resume automatically |
| Want a fresh destination folder | Delete `out/ai_labs_samples.checkpoint.jsonl` and `out/ai_labs_samples.checkpoint.jsonl.dest.json`, then run again |
| `build_quality_sample.py`: `--service-account is required` | Full-workspace scanning needs Domain-Wide Delegation — see `CLIENT_SETUP.md` → Part 4B |
| Manifest has no `gmail_threads` / Part 8 finds nothing | Run Part 5 first (with Gmail not skipped), or point `--manifest` at a manifest that has one |
| Gmail insert fails with a permissions / consent screen error | `gmail.insert` scope isn't on the OAuth client's consent screen yet, or (Testing mode) the account isn't added as a test user — operator needs to fix this in Cloud Console |

---

## Quick reference

```
# (Operator) Build a fresh manifest across the whole Workspace
python tools/build_quality_sample.py --service-account ~/Downloads/service_account.json --admin-email admin@yourdomain.com

# Preview the Drive copy
python tools/export_ai_labs_samples.py --dry-run

# Test with 5 files
python tools/export_ai_labs_samples.py --limit 5

# Full Drive copy run
python tools/export_ai_labs_samples.py

# Same, but from a freshly built manifest
python tools/export_ai_labs_samples.py --manifest out/quality_sample_manifest.json

# Gmail thread copy (needs a manifest with gmail_threads, e.g. from Part 5)
python tools/export_ai_labs_gmail_threads.py --service-account ~/Downloads/service_account.json
```

Manifest used:

- Bundled default (no Excel required): `data/ai_labs_1200_balanced_sample.json`
- Freshly built (Part 5, size-capped, includes Gmail threads): `out/quality_sample_manifest.json`
