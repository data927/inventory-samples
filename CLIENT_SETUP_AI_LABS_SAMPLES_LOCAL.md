# AI Labs Sample Set — Local Dump Setup Guide

Follow every step in order. Copy each command exactly.

**This guide is for local folders only** (no Google Drive / Gmail / Cloud Console).

---

## What this does

Point the tool at **any parent folder**. It processes **every immediate subfolder** inside it — names do not matter.

```
company-dump/
  Folder A/
  Folder B/
  anything-else/
```

**One account as the full sample** — use `--only` with a single folder name. That copies that account's **entire** data (no 1000-file / 15GB trim):

```
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --only "Folder A"
```

**Multiple accounts** — from each subfolder:

1. Take up to **1000 files** first (or fewer if the folder has less)
2. If the total is still under **15GB**, keep taking **more files** from those folders until 15GB is reached

Copies land on your **Desktop**, keeping the same subfolder names:

```
Desktop/AI Labs Sample Set (YYYY-MM-DD)/
  Folder A/
  Folder B/
  anything-else/
```

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
git clone https://github.com/data927/inventory-samples.git
```

```
cd inventory-samples
```

```
ls tools/build_quality_sample_local.py
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

## Part 3 — Point at your parent folder and run

Your dump is any parent folder with subfolders inside:

```
/path/to/company-dump/
  Folder A/
  Folder B/
```

### Entire data of one account (recommended sample)

```
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --only "Folder A" --entire
```

Windows:

```
python tools/build_quality_sample_local.py --root C:\Users\YourName\Downloads\company-dump --only "Folder A" --entire
```

That copies **everything** under `Folder A` onto the Desktop (no 1000-file / 15GB trim).

### As-is until 40GB

```
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --as-is
```

```
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --as-is --cap-gb 40
```

### All subfolders (capped sample)

**Run** — replace the path with your real parent folder:

Mac / Linux:
```
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump
```

Windows:
```
python tools/build_quality_sample_local.py --root C:\Users\YourName\Downloads\company-dump
```

When it finishes, open your **Desktop**. You should see:

```
AI Labs Sample Set (YYYY-MM-DD)/
  Folder A/
  Folder B/
```

A list of what was copied is also written to:

```
out/quality_sample_local_manifest.json
```

---

## Optional flags

**Entire data of one account (use `--entire`):**

```
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --only "Folder A" --entire
```

**As-is until 40GB:**

```
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --as-is --cap-gb 40
```

**Different per-folder first pass (default 1000 when multiple folders, then fill to 15GB):**

```
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --limit 1000
```

**Different overall byte cap (default 15GB for multi-folder; unlimited for one account):**

```
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --cap-gb 15
```

**Only some subfolders (two or more → capped sample again):**

```
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --only "Folder A" "Folder B"
```

**Custom destination path:**

```
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --dest "~/Desktop/AI Labs Sample Set"
```

**Custom name on the Desktop:**

```
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --folder-name "My AI Labs Samples"
```

---

## How “local mode” is selected

There is **no mode switch** inside the Drive tool.

| What you have | Command |
| --- | --- |
| Local parent folder with subfolders | `python tools/build_quality_sample_local.py --root ...` |
| Google Drive / Workspace | `python tools/build_quality_sample.py` (see the other setup guide) |

Use **this** script for local. Use the other guide for Google.

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `python: command not found` | Use `python3`, or reinstall Python with “Add to PATH” |
| `(.venv)` not showing | Re-run the activate step in Part 2 |
| `--root not found` | Check the path to the parent folder |
| `no subfolders` | `--root` must contain at least one subfolder |
| Desktop folder empty / few files | That subfolder had fewer than 1000 files, or path pointed at the wrong place |
| Want a fresh Desktop folder | Delete or rename the old `AI Labs Sample Set (...)` folder on Desktop, then run again |

---

## Quick reference

```
cd ~/inventory-samples
source .venv/bin/activate

# Entire data of one account
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --only "Folder A" --entire

# As-is until 40GB
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --as-is --cap-gb 40

# Or multi-folder capped sample
python tools/build_quality_sample_local.py --root ~/Downloads/company-dump
```

Windows:
```
cd %USERPROFILE%\inventory-samples
.venv\Scripts\activate

python tools/build_quality_sample_local.py --root C:\Users\YourName\Downloads\company-dump --only "Folder A"
```
