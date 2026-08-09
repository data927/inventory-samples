# AI Labs Sample Set — Client Setup Guide

This guide builds a curated **~1200-file sample set** in **your own Google Drive**.

It creates a new folder like `AI Labs Sample Set (YYYY-MM-DD)` in your My Drive, with subfolders by category, and copies the selected files into it.

You sign in with **your** Google account. No shared folder and no one else’s token is required.

> **Already ran Inventory Segmentor on this machine?**  
> Skip to [Part 4 — Google Drive login](#part-4--google-drive-login) if the project is already cloned and `.venv` works. Then jump to [Part 5 — Build the sample set](#part-5--build-the-sample-set).

---

## What you need

- The **same Google account** you used when the Drive inventory was run
- Python 3 and Git (steps below if you don’t have them)
- An OAuth client JSON file (often already set up from Inventory Segmentor — see Part 4)

**You do not need** an Anthropic / OpenAI / Gemini API key for this script.

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

You should see `ai_labs_1200_balanced_sample.json`. That file is **bundled in the repo** — you do not need any Excel file.

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

This tool needs permission to **create a folder and copy files** in your Drive (broader than the inventory’s read-only login). The first run opens a browser — click **Allow**.

Your OAuth client file should already be at something like:

```
.secrets/google_oauth_client.json
```

If setup put it elsewhere, that is fine as long as `.env` points to it (`GOOGLE_OAUTH_CLIENT_SECRETS=...`).

---

## Part 5 — Build the sample set

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

### Step A — Preview (no changes to Drive)

```
python tools/export_ai_labs_samples.py --dry-run
```

You should see about **1200** files listed across categories such as:

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

### Step C — Full sample set (~1200 files)

```
python tools/export_ai_labs_samples.py
```

This can take a while. It is safe to stop and re-run the **same** command — it resumes from a checkpoint and will not re-copy files it already finished.

Optional custom folder name:

```
python tools/export_ai_labs_samples.py --folder-name "Goldsetu AI Labs Samples"
```

---

## Part 6 — Find your output

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

## Troubleshooting

| Problem | What to do |
| --- | --- |
| Browser asks to sign in again | Use the **same** account that ran the Drive inventory |
| Many `FAIL` / `404` / `notFound` | Wrong Google account, or files were deleted since the inventory |
| Many `403` / permission errors | Sign in as the inventory account; confirm you can open those files in Drive normally |
| `OAuth client secrets not found` | Re-run `python setup.py` and complete the Google Drive step, or place the client JSON under `.secrets/google_oauth_client.json` |
| `sample manifest not found` | Update/pull the latest code so `data/ai_labs_1200_balanced_sample.json` exists |
| Interrupted halfway | Re-run `python tools/export_ai_labs_samples.py` — it resumes automatically |
| Want a fresh destination folder | Delete `out/ai_labs_samples.checkpoint.jsonl` and `out/ai_labs_samples.checkpoint.jsonl.dest.json`, then run again |

---

## Quick reference

```
# Preview
python tools/export_ai_labs_samples.py --dry-run

# Test with 5 files
python tools/export_ai_labs_samples.py --limit 5

# Full run
python tools/export_ai_labs_samples.py
```

Manifest used (bundled — no Excel required):

```
data/ai_labs_1200_balanced_sample.json
```
