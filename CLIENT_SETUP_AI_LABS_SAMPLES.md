# AI Labs Sample Set — End-to-End Setup Guide

This guide is self-contained — it does not assume you've set up or run anything else in this repo before. It builds a curated sample set in **your own Google Drive** (and, optionally, your own Gmail).

It creates a new folder like `AI Labs Sample Set (YYYY-MM-DD)` in your My Drive, with subfolders, and copies the selected files into it. Gmail threads (if you build a manifest that includes them) go into your own Gmail Inbox instead. By default, Part 5 scans, selects, **and transfers** in one run — no separate manual step needed.

You sign in with **your** Google account for the copy step. No shared folder and no one else's token is required.

There are two roles in this guide:

- **Client** — signs in with their own Google account and runs the copy steps (Parts 1, 2, 3, 4, 6, 8). No admin access needed. Only relevant if the operator used `--scan-only` in Part 5 (see below) instead of transferring it themselves.
- **Operator** — the person running the extraction for this engagement (Part 5). A Service Account with Domain-Wide Delegation is only needed if they're building a manifest across the **whole Workspace**; scanning just their own Drive + Gmail needs no extra setup beyond Part 3/4. By default the operator also signs in once as the destination account (same as Parts 4/8) so Part 5 can transfer everything itself in the same run — pass `--scan-only` instead if the operator and the destination account owner are different people.

---

## What you need

- A Google account — the one whose Drive/Gmail you're building samples in
- Python 3 and Git (steps below if you don't have them)
- Access to [Google Cloud Console](https://console.cloud.google.com/) with that same Google account, to create an OAuth client (Part 3 — free, ~5 minutes, one time)
- **(Operator, Part 5 — whole-Workspace mode only)** A Service Account JSON with Domain-Wide Delegation, and a Workspace super-admin email — covered in full in Part 5
- **(Optional, Part 8 — Gmail thread samples)** The `gmail.insert` scope added to your OAuth client's consent screen — covered in full in Part 8

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

Confirm the clone worked:

```
ls tools/build_quality_sample.py
```

For most engagements, Part 5 below builds a **fresh** sample manifest by scanning live — that's what you'll actually use in Part 6. The repo also carries one legacy fixed list, `data/ai_labs_1200_balanced_sample.json`, kept only as a fallback default for Part 6 if Part 5 is skipped entirely.

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

## Part 3 — Create a Google Cloud OAuth client

This is the one-time Google Cloud Console setup that lets this tool sign in as you and access your Drive/Gmail. If someone already handed you a `client_secret_....json` file for this exact project, you can skip to Part 4 — otherwise, here's where it comes from:

1. Go to **https://console.cloud.google.com/** and sign in with the Google account you'll use for this tool.
2. **Create a project** — top-left project dropdown → **New Project** → give it any name (e.g. `AI Labs Sample Set`) → **Create**. Wait ~30 seconds for it to finish provisioning, then make sure it's selected in that same dropdown.
3. **Enable the APIs you'll need** — left menu → **APIs & Services** → **Library**:
   - Search **Google Drive API** → **Enable**
   - Search **Gmail API** → **Enable** (skip this one only if you're certain you'll never use Part 8)
4. **Configure the OAuth consent screen** — **APIs & Services** → **OAuth consent screen**:
   - User type: **External** (unless this Google account belongs to a Google Workspace org you manage, in which case **Internal** also works) → **Create**
   - Fill in **App name**, **User support email**, and **Developer contact email** (any values are fine — this app is only ever used by you) → **Save and Continue**
   - **Scopes**: click **Add or Remove Scopes**, search for and check:
     - `.../auth/drive` (full Drive access — needed to create the sample folder and copy files in Part 6)
     - `.../auth/gmail.insert` (only if you'll use Part 8 — Gmail thread samples; you can add this later instead, see Part 8)
     → **Update** → **Save and Continue**
   - **Test users**: click **Add Users** and add the Google account you'll sign in with. This is required while the app is in **Testing** publish status — without it, sign-in will be blocked with an error → **Save and Continue** → **Back to Dashboard**
5. **Create the OAuth client** — **APIs & Services** → **Credentials** → **Create Credentials** → **OAuth client ID**:
   - Application type: **Desktop app**
   - Name it anything (e.g. `Inventory Segmentor`) → **Create**
   - A dialog shows your client ID/secret — click **Download JSON**
6. Save that downloaded file somewhere you can find it (e.g. your Downloads folder). You'll point the tool at it in Part 4.

> **If you're the operator and will also do Part 5's Workspace mode:** this same Cloud project can hold the Service Account from Part 5 too — you don't need a second project. The OAuth client here and the Service Account there are separate, independent credentials that happen to live in the same place; either reuse this project or use a different one, both work.

---

## Part 4 — Connect the tool to your OAuth credentials

```
python setup.py
```

- **API key** prompt → press Enter to skip (not needed for sampling).
- **"Set up Google Drive access? (y/n)"** → type `y`.
- **"Path to OAuth client JSON file"** → paste the path to the file you downloaded in Part 3, e.g.:
  - Mac: `~/Downloads/client_secret_xxxx.json`
  - Windows: `C:\Users\YourName\Downloads\client_secret_xxxx.json`
- It copies that file to `.secrets/google_oauth_client.json` and asks to authorize now — type `y`. A browser tab opens; sign in with the same Google account and click **Allow**.

> **Manual alternative**, if you'd rather not use the wizard: copy the downloaded JSON to `.secrets/google_oauth_client.json` yourself, or set `GOOGLE_OAUTH_CLIENT_SECRETS=/path/to/file.json` in `.env`.

---

## Part 5 — Build a fresh, size-capped sample manifest

**Skip this part if you're the client running a manifest someone already built for you — jump to Part 6.** This part is for whoever is running the extraction for the engagement.

`data/ai_labs_1200_balanced_sample.json` (used by default in Part 6) is a fixed, hand-picked list. `tools/build_quality_sample.py` instead scans live and builds a fresh manifest, in one of two modes — **picked automatically** depending on what auth you give it:

- **Workspace mode** — pass `--service-account` + `--admin-email` (Domain-Wide Delegation): scans **every user's** My Drive + Shared Drives + Gmail across the whole org.
- **My Drive mode** — run it with no service account: scans just **your own** Drive + Gmail, reusing the OAuth token from Part 4. If you haven't yet authorized Gmail access specifically, a browser opens once the first time it's needed.

Either way, the manifest is built using these selection rules:

- **Binary files** (PDFs, Office docs, images, etc.) — largest first (size is used as a quality proxy; there's no LLM scoring pass) until the **~75GB** Drive cap is hit.
- **Gmail threads** — largest first, until the **~10–15GB** cap (default 12.5GB) is hit. Grouped into **whole threads** before selection — a thread is included or skipped as one unit, so a thread never gets split across the include/exclude line.
- **Google-native Docs/Sheets/Slides** — these have no fixed byte size, so they can't be size-ranked. Instead each type gets its own fixed count, most-recently-modified first, **on top of** (not counted against) the 75GB Drive cap:
  - **Google Sheets:** 350
  - **Google Docs:** 300
  - **Google Slides:** 150

**In Workspace mode specifically**, every rule above is applied *per account* first, so one account can't crowd out everyone else — but Drive and Gmail use different fairness strategies:

- **Binary files (Drive):** every account is guaranteed a small minimal slice of the 75GB cap first — no account with data is ever fully shut out, even if its files are large and don't neatly fit a proportional share. Whatever's left of the cap is then filled in **priority order** (accounts with more data going first each round) until the cap is used up. In the typical case this means bigger accounts end up with proportionally more; in rare cases (very few accounts, or a cap barely bigger than one account's smallest file) the guarantee can cost a big account a bit of its edge — that trade-off is intentional so nobody gets zero.
- **Gmail threads:** the 12.5GB cap is split **equally** across accounts (not weighted by how much data each account has) — every account gets the same nominal share. If a share can't be filled exactly (an account's threads don't fit its slice evenly), the leftover is reclaimed and reused so the full cap still gets used — this can let an account end up with a bit more than its nominal equal share, the same full-utilization trade-off as Drive's guarantee.
- **Native Sheets/Docs/Slides:** every account is guaranteed its own baseline first (most-recently-modified) — **30 Sheets, 40 Docs, 20 Slides per account** by default. If that guaranteed total is still under the overall target (350/300/150), the remainder is topped up with the next most-recently-modified files from anywhere in the Workspace. The per-account guarantee is never trimmed back down, so with enough accounts the final total can end up above the overall target — that's expected.

In My Drive mode there's only one account, so all of this simplifies back to plain global behavior.

### Prerequisite for Workspace mode: Service Account + Domain-Wide Delegation

Skip this whole box for My Drive mode — nothing extra is needed there beyond Part 3/4.

**Two different people are usually involved here** — don't assume one person needs to do all of this:

- **The operator** (whoever is running this extraction — often an external vendor, not the client's own staff) does almost everything below: creates the Service Account, gets its Client ID, enables the APIs. A regular Google account with access to *any* Google Cloud project is enough — no special access to the target Workspace is needed for this part.
- **The target Workspace's own super-admin** (the client's IT admin) is the **only** person who can do step 4 — it happens inside `admin.google.com` for that specific Workspace, which an outside operator has no access to. The operator sends them the Client ID (never the JSON key file — that stays private) and asks them to authorize it.

**Done by the operator (~5 minutes):**

1. **Create a Service Account** — [Google Cloud Console](https://console.cloud.google.com/) → a project you control (this can be the same project from Part 3, or a separate one — either works) → **IAM & Admin** → **Service Accounts** → **Create Service Account** → give it any name (e.g. `inventory-scanner`) → **Create and Continue** → **Done**.
2. Click the service account → **Keys** tab → **Add Key** → **Create new key** → **JSON**. The file downloads automatically — save it somewhere safe. This file is a credential — treat it like a password.
3. Still on the service account page → **Details** tab → expand **Advanced settings** → copy the **Client ID** (a long number) → turn on **Domain-wide delegation** if there's a checkbox for it.
4. **Enable the APIs** — Cloud Console → **APIs & Services** → **Library** → enable **Google Drive API**, **Admin SDK API**, and **Gmail API** (in the same project as the service account).
5. Send just the **Client ID** to the target Workspace's super-admin, and ask them to do the next step.

**Done by the target Workspace's super-admin** (a different Google account/organization than the operator, if this is a client engagement):

6. **Authorize the scopes** — go to [admin.google.com](https://admin.google.com) → **Security** → **Access and data control** → **API controls** → **Manage Domain Wide Delegation** → **Add new** → paste the Client ID the operator sent you and add these three scopes (comma-separated):
   ```
   https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/admin.directory.user.readonly,https://www.googleapis.com/auth/gmail.readonly
   ```
   → **Authorise**. Propagation can take a few minutes — if the first run fails with `unauthorized_client` or `access_denied`, wait 5–10 minutes and retry.

**Back to the operator, once step 6 is confirmed authorized:**

7. Run the wizard to save the credentials:
   ```
   python setup.py
   ```
   When it asks `Set up Service Account for full workspace scan? (y/n)`, type `y`, paste the path to the service account JSON key from step 2, and paste the target Workspace's super-admin email (e.g. `admin@yourdomain.com` — a real Workspace user, not the service account's own email).

### Run it

Workspace-wide:

```
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com
```

Just a few specific accounts in the Workspace, instead of everyone:

```
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com \
  --users alice@yourdomain.com bob@yourdomain.com
```

Only things dated before a cutoff (e.g. everything before this year):

```
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com \
  --before 2026-01-01
```

A huge Workspace, scanning each account in batches and stopping early once there's enough:

```
python tools/build_quality_sample.py \
  --service-account ~/Downloads/service_account.json \
  --admin-email admin@yourdomain.com \
  --folders-per-round 1000
```

Just your own Drive + Gmail:

```
python tools/build_quality_sample.py
```

This can take a while for a large Workspace — it caches scan progress under `out/` (`*.drive_scan_cache.jsonl`, `gmail_ids__*.txt`), so re-running the same command resumes rather than rescanning from zero.

### Optional flags

| Flag | Default | What it does |
| --- | --- | --- |
| `--drive-cap-gb` | `75` | Byte cap for binary Drive files, in GB (split across accounts by data volume in Workspace mode) |
| `--gmail-cap-gb` | `12.5` | Gmail selection cap, in GB (same per-account split) |
| `--gsheets-limit` | `350` | Overall target total for Google Sheets, most-recently-modified first |
| `--gdocs-limit` | `300` | Overall target total for Google Docs |
| `--gslides-limit` | `150` | Overall target total for Google Slides |
| `--gsheets-per-account` | `30` | Google Sheets guaranteed per account (Workspace mode) |
| `--gdocs-per-account` | `40` | Google Docs guaranteed per account (Workspace mode) |
| `--gslides-per-account` | `20` | Google Slides guaranteed per account (Workspace mode) |
| `--out` | `out/quality_sample_manifest.json` | Where the manifest is written |
| `--gmail-query` | (none) | Gmail search query to filter candidate messages (e.g. `after:2024/01/01`) |
| `--before` | (none) | Only scan Drive files and Gmail messages dated before this date (`YYYY-MM-DD`). Applies to both sources at once — Drive folders are still always traversed regardless of their own modified time, so an old file inside a recently-touched folder is never missed. |
| `--skip-drive` | off | Skip Drive scanning entirely |
| `--skip-gmail` | off | Skip Gmail scanning entirely |
| `--users` | (whole domain) | Workspace mode only: scan just these accounts instead of everyone — space- or comma-separated emails, e.g. `--users a@co.com b@co.com`. Skips Admin SDK enumeration entirely, so it doesn't even need the Admin SDK API/scope. |
| `--folders-per-round` | `0` (off) | Scan each account's Drive in batches of N folders, stopping that account early once it already has enough candidates for the configured targets — see below. `0` disables this and scans every folder exhaustively (the default). |
| `--rescan` | off | Force a fresh scan even if `--out` already exists — see **Resuming** below. |
| `--scan-only` | off | Stop after writing the manifest — don't transfer into the destination folder (transferring is the default now — see **Transferring automatically** below). |
| `--folder-name` | `AI Labs Sample Set` | Destination My Drive folder name (date suffix added automatically) |
| `--dest-folder-id` | (none) | Use an existing destination folder ID/URL instead of creating a new one |

### Resuming

If `--out` already points at a manifest file that exists, the script **skips scanning entirely** and goes straight to transferring what's already in it — safe to just re-run the exact same command after an interruption, at any stage (mid-scan, mid-transfer, or between the two). Pass `--rescan` to force a fresh scan instead (e.g. you want to pick up new files that have shown up since).

Scanning itself is also resumable at a finer grain even before a manifest exists: Drive per-account scans cache progress under `out/`, and Gmail message metadata is cached and resumed message-by-message — an interrupted scan doesn't restart from zero.

### Transferring automatically

By default, once scanning + selection finishes (or a manifest is reused per **Resuming** above), the script immediately continues into the **same** destination folder flow as Part 6/8 below — no separate manual step. **Drive goes first, completely, before Gmail starts**: every account's Drive files are copied (largest account first, each landing in a subfolder named after that account — `AI Labs Sample Set (date)/{email}/...`), and only once *all* Drive transfers are done does it move on to Gmail threads (again largest account first). That way your Drive samples are fully ready as an early, complete checkpoint, rather than Drive and Gmail interleaving account-by-account. This reuses the exact same checkpointed copy/insert logic as Part 6/8, so an interrupted transfer resumes without re-copying or re-inserting anything already done.

This needs the destination account's own consent the first time — the same OAuth (and, if Gmail threads are included, `gmail.insert`) login described in Parts 3/4/8. If the person running the scan (operator, with the Service Account) is a **different** person from whoever owns the destination account, use `--scan-only` here instead, and have the destination account's owner independently run Part 6/8 as their own separate step — that's the multi-party handoff flow those parts were originally built for, and it still works unchanged.

### Speeding up huge Workspaces with `--folders-per-round`

For an org with hundreds of users each holding tens of thousands of files, scanning every folder in every account before selecting anything can take a long time with no usable output until it's completely done. `--folders-per-round` changes the strategy, **per account**:

- Scan that account's Drive in batches of N folders (e.g. `--folders-per-round 1000`).
- After each batch, check whether the account already has comfortably more candidates than the configured targets could ever need (3× the per-account Sheets/Docs/Slides guarantees, and binary candidates already totaling more than the whole `--drive-cap-gb` cap on their own).
- If so, stop scanning that account and move to the next one — any folders not yet reached are simply skipped for that account.

This trades a small chance of missing a marginally-better file deep in an unscanned folder for meaningfully faster runs. Shared Drives are always scanned exhaustively regardless of this setting (they're not individually-owned accounts, so the same early-stop logic doesn't apply). Leave it at `0` (the default) for a guaranteed-complete, exhaustive scan.

### Output

`out/quality_sample_manifest.json`, containing:

- `"files"` — selected Drive files: binary files under the 75GB cap, plus the Sheets/Docs/Slides quotas (same shape Part 6's script already reads)
- `"gmail_threads"` — selected Gmail threads, each with its `message_ids` (used by Part 8)
- `"drive_native_selected"` — how many Sheets/Docs/Slides were actually included (useful if a Workspace has fewer than the quota of a given type)

If Part 5 was run **without** `--scan-only`, it already transferred everything by the time it finishes — skip ahead to **Part 7 — Find your output**. Parts 6 and 8 below are for the multi-party handoff case (`--scan-only` was used, or you're working from the bundled default list instead of a fresh scan).

---

## Part 6 — Build the sample set

**Only needed if Part 5 was run with `--scan-only`, or you're using the bundled default list instead of a fresh Part 5 scan.** If Part 5 already transferred everything for you, skip to Part 7.

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

Note the folder layout differs slightly from Part 5's own automatic transfer: this script groups files by content category (`Uncategorized`, since this manifest has no LLM classification pass); Part 5's built-in transfer groups by account email instead. Same destination folder either way.

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

If Part 5 transferred it automatically (subfolders by account):

```
AI Labs Sample Set (YYYY-MM-DD)/
  alice@yourdomain.com/
  bob@yourdomain.com/
  ...
```

If Part 6 (the separate script) copied it instead (subfolders by category):

```
AI Labs Sample Set (YYYY-MM-DD)/
  Product & Engineering/
  Financial & Legal/
  Operations & HR/
  Marketing/
  ...
```

Either way, the script prints a direct link at the end, for example:

```
their folder: https://drive.google.com/drive/folders/.........
```

---

## Part 8 — Gmail thread samples (optional)

**Only needed if Part 5 was run with `--scan-only`** — otherwise Part 5 already inserted these threads for you. Only relevant if the manifest includes a `"gmail_threads"` list. This copies those threads — **whole threads, never split** — into your own Gmail Inbox, so Gmail re-threads them correctly on arrival.

### One-time setup: add the `gmail.insert` scope

If you already checked this box in Part 3, skip ahead to **Copy the threads** below. Otherwise:

1. [Google Cloud Console](https://console.cloud.google.com/) → your project → **APIs & Services** → **OAuth consent screen**.
2. If you haven't already, enable the **Gmail API** first — **APIs & Services** → **Library** → search **Gmail API** → **Enable** (scopes for a disabled API won't show up in the next step).
3. Back on the consent screen: **Edit App** → step through to **Scopes** → **Add or Remove Scopes** → search `gmail.insert` → check `.../auth/gmail.insert` → **Update** → **Save and Continue** through to the end.
4. If the app is still in **Testing** publish status, also confirm the signing-in Google account is listed under **Test users** on that same screen — add it if it's missing.
5. This is a new scope, separate from Drive — the **first time** you run the command below, a fresh browser consent prompt appears even if you already authorized Drive access in Part 4.

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
| Browser asks to sign in again | Use the **same** account throughout Parts 3–8 |
| Many `FAIL` / `404` / `notFound` | Wrong Google account, or files were deleted/moved since the manifest was built |
| Many `403` / permission errors | Sign in as the account the manifest was built from; confirm you can open those files in Drive normally |
| Google sign-in blocked with an "app not verified" / "access blocked" error | The signing-in account isn't in the OAuth consent screen's **Test users** list yet (Part 3, step 4) — add it there |
| `OAuth client secrets not found` | Complete Part 3 (create the OAuth client) and Part 4 (`python setup.py`, Google Drive step), or place the client JSON under `.secrets/google_oauth_client.json` |
| `sample manifest not found` | Update/pull the latest code so `data/ai_labs_1200_balanced_sample.json` exists, or check the `--manifest` path |
| Interrupted halfway | Re-run the same command — both `export_ai_labs_samples.py` and `export_ai_labs_gmail_threads.py` resume automatically |
| Want a fresh destination folder | Delete `out/ai_labs_samples.checkpoint.jsonl` and `out/ai_labs_samples.checkpoint.jsonl.dest.json`, then run again |
| `build_quality_sample.py`: `--admin-email is required with --service-account` | You passed `--service-account` but not `--admin-email` — add it, or drop `--service-account` entirely to scan just your own Drive + Gmail instead |
| Manifest has no `gmail_threads` / Part 8 finds nothing | Run Part 5 first (with Gmail not skipped), or point `--manifest` at a manifest that has one |
| Gmail insert fails with a permissions / consent screen error | `gmail.insert` scope isn't on the OAuth client's consent screen yet — do Part 8's one-time setup, including the Test users check |
| Part 5 asks for a browser login you weren't expecting | It's transferring automatically now (the default) — that's the destination account's OAuth/`gmail.insert` consent, same as Parts 4/8. Use `--scan-only` if you only want the manifest and don't want to authorize a destination right now |
| Part 5 didn't re-scan even though you expected it to | `--out` already pointed at an existing manifest, so it reused it — pass `--rescan` to force a fresh scan |
| Want Part 5 to build a manifest without transferring, for someone else to run Part 6/8 later | Add `--scan-only` |

---

## Quick reference

```
# (Operator) Build a fresh manifest across the whole Workspace AND transfer it — one run
python tools/build_quality_sample.py --service-account ~/Downloads/service_account.json --admin-email admin@yourdomain.com

# (Operator) Or just your own Drive + Gmail — no service account needed
python tools/build_quality_sample.py

# Re-run any of the above any time — resumes automatically (scan or transfer, wherever it left off)

# Build the manifest only, don't transfer (for a separate person to handle Part 6/8)
python tools/build_quality_sample.py --service-account ~/Downloads/service_account.json --admin-email admin@yourdomain.com --scan-only

# Force a fresh scan even though a manifest already exists
python tools/build_quality_sample.py --service-account ~/Downloads/service_account.json --admin-email admin@yourdomain.com --rescan

# --- Manual handoff path (Parts 6/8) — only needed after --scan-only ---

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
