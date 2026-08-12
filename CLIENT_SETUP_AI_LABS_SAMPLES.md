# AI Labs Sample Set — Setup Guide

Follow every step in order. Copy each command exactly.

**You do not need** an Anthropic / OpenAI / Gemini API key.

---

## Prerequisites

### 1. Install Python

1. Go to: **https://www.python.org/downloads/**
2. Download and run the installer.
   - **Windows:** check **"Add Python to PATH"** before Install.
3. Verify:

```
python --version
```

### 2. Install Git

1. Go to: **https://git-scm.com/downloads**
2. Install with defaults.
3. Verify:

```
git --version
```

---

## Part 1 — Get the code

```
cd ~
```

```
git clone https://github.com/data927/inventory-segmentor.git
```

```
cd inventory-segmentor
```

```
ls tools/build_quality_sample.py
```

---

## Part 2 — Python environment

**Create venv**

Mac / Linux:
```
python3 -m venv .venv
```

Windows:
```
python -m venv .venv
```

**Activate**

Mac / Linux:
```
source .venv/bin/activate
```

Windows:
```
.venv\Scripts\activate
```

You should see `(.venv)` at the start of the line.

**Install packages**

```
pip install -r requirements.txt
```

---

## Local dump — any parent folder (no Google needed)

Point at any parent folder. **Every immediate subfolder** is processed — names do not matter.

```
/path/to/main/
  Folder A/
  Folder B/
  ...
```

Skips Parts 3–8. Copies the **first 1000 files** in each subfolder onto the **Desktop**.

Full local-only guide: `CLIENT_SETUP_AI_LABS_SAMPLES_LOCAL.docx`

**Run**

Mac / Linux:

```
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump
```

Windows:

```
python tools/build_quality_sample_local.py --root C:\Users\YourName\Downloads\company-dump
```

Creates:

```
Desktop/AI Labs Sample Set (YYYY-MM-DD)/
  Folder A/
  Folder B/
```

**Useful variants**

```
# Different per-folder limit
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --limit 1000

# Specific subfolders only
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --only "Folder A" "Folder B"

# Explicit destination path
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --dest "~/Desktop/AI Labs Sample Set"
```

Manifest: `out/quality_sample_local_manifest.json`

---

## Part 3 — Create a Google Cloud OAuth client

One-time. If you already have a `client_secret_....json` for this project, skip to Part 4.

1. Open **https://console.cloud.google.com/** — sign in with the Google account you'll use.
2. Top-left project dropdown → **New Project** → name it (e.g. `AI Labs Sample Set`) → **Create**. Select it when ready.
3. **APIs & Services** → **Library**:
   - Enable **Google Drive API**
   - Enable **Gmail API** (needed for Gmail samples)
4. **APIs & Services** → **OAuth consent screen**:
   - User type: **External** (or **Internal** if Workspace you manage) → **Create**
   - Fill **App name**, **User support email**, **Developer contact email** → **Save and Continue**
   - **Scopes** → **Add or Remove Scopes** → check:
     - `.../auth/drive`
     - `.../auth/gmail.insert` (if you'll copy Gmail threads)
   - → **Update** → **Save and Continue**
   - **Test users** → **Add Users** → add your Google account → **Save and Continue** → **Back to Dashboard**
5. **APIs & Services** → **Credentials** → **Create Credentials** → **OAuth client ID**:
   - Application type: **Desktop app**
   - Name → **Create** → **Download JSON**
6. Keep the downloaded file (e.g. `~/Downloads/client_secret_xxxx.json`).

---

## Part 4 — Connect OAuth credentials

```
python setup.py
```

Prompts:
1. **API key** → press Enter (skip).
2. **Set up Google Drive access? (y/n)** → `y`
3. **Path to OAuth client JSON file** → paste path, e.g.:
   - Mac: `~/Downloads/client_secret_xxxx.json`
   - Windows: `C:\Users\YourName\Downloads\client_secret_xxxx.json`
4. Authorize now → `y` → browser → sign in → **Allow**

Manual alternative: copy the JSON to `.secrets/google_oauth_client.json`, or set `GOOGLE_OAUTH_CLIENT_SECRETS=/path/to/file.json` in `.env`.

---

## Part 5 — Build sample (scan + transfer)

Skip if someone already gave you a manifest → go to Part 6.

Default: scans, selects, and copies into your Drive (and Gmail if included) in one run.

### Caps / selection (defaults)

| Source | Rule |
| --- | --- |
| Binary Drive files | Largest first, up to **15GB** |
| Gmail threads | Whole threads, largest first, up to **12.5GB** |
| Google Sheets | 350 (most recent) |
| Google Docs | 300 (most recent) |
| Google Slides | 150 (most recent) |

Native Docs/Sheets/Slides sit **on top of** the 15GB binary cap.

### 5A — My Drive only (no Service Account)

```
python tools/build_quality_sample.py
```

### 5B — Whole Workspace (Service Account + Domain-Wide Delegation)

**Operator — create Service Account**

1. [Google Cloud Console](https://console.cloud.google.com/) → project (can reuse Part 3) → **IAM & Admin** → **Service Accounts** → **Create Service Account** → name → **Done**.
2. Service account → **Keys** → **Add Key** → **Create new key** → **JSON** → save the file.
3. **Details** → **Advanced settings** → copy **Client ID** → enable Domain-wide delegation if shown.
4. **APIs & Services** → **Library** → enable **Google Drive API**, **Admin SDK API**, **Gmail API**.
5. Send only the **Client ID** to the Workspace super-admin (not the JSON key).

**Workspace super-admin — authorize**

6. [admin.google.com](https://admin.google.com) → **Security** → **Access and data control** → **API controls** → **Manage Domain Wide Delegation** → **Add new** → paste Client ID + scopes:

```
https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/admin.directory.user.readonly,https://www.googleapis.com/auth/gmail.readonly
```

→ **Authorise**. Wait 5–10 min if first run fails with `unauthorized_client`.

**Operator — save credentials**

```
python setup.py
```

When asked `Set up Service Account for full workspace scan? (y/n)` → `y` → path to JSON key → Workspace super-admin email (e.g. `admin@yourdomain.com`).

**Run — whole Workspace (scan + transfer)**

```
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com
```

**Run — specific users only**

```
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com \
  --users alice@yourdomain.com bob@yourdomain.com
```

**Run — only items before a date**

```
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com \
  --before 2026-01-01
```

**Run — huge Workspace (faster streaming; less fairness)**

```
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com \
  --folders-per-round 1000
```

**Run — manifest only (no transfer; hand off to someone else)**

```
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com \
  --scan-only
```

**Force a fresh scan** (if `out/quality_sample_manifest.json` already exists)

```
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com \
  --rescan
```

Re-run the **same** command anytime to resume (scan or transfer).

If Part 5 transferred everything → skip to **Part 7**.

### Optional flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--drive-cap-gb` | `15` | Drive binary cap (GB) |
| `--gmail-cap-gb` | `12.5` | Gmail cap (GB) |
| `--gsheets-limit` | `350` | Google Sheets count |
| `--gdocs-limit` | `300` | Google Docs count |
| `--gslides-limit` | `150` | Google Slides count |
| `--gsheets-per-account` | `30` | Sheets guaranteed per account (Workspace) |
| `--gdocs-per-account` | `40` | Docs guaranteed per account (Workspace) |
| `--gslides-per-account` | `20` | Slides guaranteed per account (Workspace) |
| `--out` | `out/quality_sample_manifest.json` | Manifest path |
| `--gmail-query` | (none) | Gmail search filter |
| `--before` | (none) | Only items before `YYYY-MM-DD` |
| `--skip-drive` | off | Skip Drive |
| `--skip-gmail` | off | Skip Gmail |
| `--users` | (whole domain) | Limit to these emails |
| `--folders-per-round` | `0` | `0` = full scan then transfer; `>0` = streaming batches |
| `--messages-per-round` | `2000` | Gmail batch size when streaming |
| `--rescan` | off | Force fresh scan |
| `--scan-only` | off | Manifest only, no transfer |
| `--folder-name` | `AI Labs Sample Set` | Destination folder name |
| `--dest-folder-id` | (none) | Use existing folder ID/URL |

Output: `out/quality_sample_manifest.json`

---

## Part 6 — Manual Drive copy (only after `--scan-only`)

Skip if Part 5 already transferred.

```
cd ~/inventory-segmentor
source .venv/bin/activate
```

Windows:
```
cd %USERPROFILE%\inventory-segmentor
.venv\Scripts\activate
```

Use `--manifest out/quality_sample_manifest.json` if Part 5 built a fresh list. Otherwise the bundled default is `data/ai_labs_1200_balanced_sample.json`.

**Preview**

```
python tools/export_ai_labs_samples.py --dry-run
```

With Part 5 manifest:
```
python tools/export_ai_labs_samples.py --manifest out/quality_sample_manifest.json --dry-run
```

**Smoke test (5 files)**

```
python tools/export_ai_labs_samples.py --limit 5
```

Or:
```
python tools/export_ai_labs_samples.py --manifest out/quality_sample_manifest.json --limit 5
```

Browser → sign in → **Allow**. Check Drive for `AI Labs Sample Set (YYYY-MM-DD)`.

**Full copy**

```
python tools/export_ai_labs_samples.py
```

Or:
```
python tools/export_ai_labs_samples.py --manifest out/quality_sample_manifest.json
```

Custom folder name:
```
python tools/export_ai_labs_samples.py --folder-name "Goldsetu AI Labs Samples"
```

Safe to stop and re-run the same command — resumes from checkpoint.

---

## Part 7 — Find output

Google Drive → **My Drive** → folder like `AI Labs Sample Set (YYYY-MM-DD)`

Part 5 transfer (by account):
```
AI Labs Sample Set (YYYY-MM-DD)/
  alice@yourdomain.com/
  bob@yourdomain.com/
```

Part 6 transfer (by category):
```
AI Labs Sample Set (YYYY-MM-DD)/
  Product & Engineering/
  Financial & Legal/
  ...
```

Script also prints a folder link when done.

---

## Part 8 — Manual Gmail copy (only after `--scan-only`)

Skip if Part 5 already inserted threads. Needs `"gmail_threads"` in the manifest.

### Add `gmail.insert` scope (if not done in Part 3)

1. Cloud Console → **APIs & Services** → **OAuth consent screen**
2. Enable **Gmail API** if needed (**Library** → **Gmail API** → **Enable**)
3. **Edit App** → **Scopes** → add `.../auth/gmail.insert` → save
4. Confirm your account is under **Test users**

### Copy threads

```
python tools/export_ai_labs_gmail_threads.py --service-account ~/Downloads/service_account.json --dry-run
```

```
python tools/export_ai_labs_gmail_threads.py --service-account ~/Downloads/service_account.json --limit 5
```

```
python tools/export_ai_labs_gmail_threads.py --service-account ~/Downloads/service_account.json
```

Browser on first insert → **Allow**. Threads land in **Inbox**. Re-run same command to resume.

Default manifest: `out/quality_sample_manifest.json` (override with `--manifest`).

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Asked to sign in again | Use the **same** Google account for Parts 3–8 |
| Many `FAIL` / `404` / `notFound` | Wrong account, or files moved/deleted since manifest |
| Many `403` | Sign in as the account the manifest was built from |
| App not verified / access blocked | Add account under OAuth **Test users** (Part 3) |
| `OAuth client secrets not found` | Finish Parts 3–4, or put JSON at `.secrets/google_oauth_client.json` |
| `sample manifest not found` | Pull latest code, or fix `--manifest` path |
| Interrupted | Re-run the **same** command |
| Fresh destination folder | Delete `out/ai_labs_samples.checkpoint.jsonl` and `out/ai_labs_samples.checkpoint.jsonl.dest.json`, re-run |
| `--admin-email is required with --service-account` | Add `--admin-email`, or drop `--service-account` for My Drive mode |
| No `gmail_threads` / Part 8 empty | Re-run Part 5 without `--skip-gmail` |
| Gmail insert permission error | Add `gmail.insert` scope (Part 8 setup) |
| Unexpected browser login in Part 5 | Destination OAuth consent (default transfer). Use `--scan-only` to skip |
| Part 5 didn't re-scan | Manifest already exists — add `--rescan` |
| Local: `--root not found` | Fix the path to the parent folder |
| Local: no subfolders | `--root` must contain at least one subfolder |

---

## Quick reference

```
# Local dump → Desktop (every subfolder under --root)
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump

# My Drive only
python tools/build_quality_sample.py

# Whole Workspace + transfer
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com

# Manifest only (handoff)
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com \
  --scan-only

# Force rescan
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com \
  --rescan

# Manual Drive copy (after --scan-only)
python tools/export_ai_labs_samples.py --manifest out/quality_sample_manifest.json --dry-run
python tools/export_ai_labs_samples.py --manifest out/quality_sample_manifest.json --limit 5
python tools/export_ai_labs_samples.py --manifest out/quality_sample_manifest.json

# Manual Gmail copy (after --scan-only)
python tools/export_ai_labs_gmail_threads.py --service-account ~/Downloads/service_account.json
```

Manifests:
- Bundled default: `data/ai_labs_1200_balanced_sample.json`
- Fresh from Part 5 (Drive): `out/quality_sample_manifest.json`
- Fresh from local dump: `out/quality_sample_local_manifest.json`
