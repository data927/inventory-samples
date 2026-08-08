# Inventory Segmentor — Client Setup Guide

Follow every step in order. Each command is on its own line — copy it exactly as shown.

---

## Prerequisites

Before starting, make sure you have the following installed on your computer.

### 1. Install Python

1. Open your browser and go to: **https://www.python.org/downloads/**
2. Click the big yellow **Download Python** button.
3. Open the downloaded file and run the installer.
   - **Important:** On the first screen, check the box that says **"Add Python to PATH"** before clicking Install.
4. When the installer finishes, click **Close**.

Verify it worked — open **Terminal** (Mac) or **Command Prompt** (Windows) and run:

```
python --version
```

You should see something like `Python 3.12.x`. If you see an error, restart your computer and try again.

---

### 2. Install Git

1. Go to: **https://git-scm.com/downloads**
2. Download and run the installer for your operating system.
3. Accept all default options during installation.

Verify it worked:

```
git --version
```

You should see something like `git version 2.x.x`.

---

## Part 1 — Get the Code

Open **Terminal** (Mac: press `Cmd + Space`, type `Terminal`, press Enter) or **Command Prompt** (Windows: press `Win + R`, type `cmd`, press Enter).

Run these commands one at a time, pressing Enter after each:

**Step 1 — Go to your home folder:**

```
cd ~
```

**Step 2 — Clone (download) the project:**

```
git clone https://github.com/data927/inventory-segmentor.git
```

> Or use the exact link provided to you if different.

**Step 3 — Enter the project folder:**

```
cd inventory-segmentor
```

(If you cloned into a different folder name, `cd` into that folder instead.)

---

## Part 2 — Set Up the Python Environment

**Step 4 — Create a virtual environment (isolated workspace for the tool):**

Mac / Linux:
```
python3 -m venv .venv
```

Windows:
```
python -m venv .venv
```

**Step 5 — Activate the environment:**

Mac / Linux:
```
source .venv/bin/activate
```

Windows:
```
.venv\Scripts\activate
```

After this step you will see `(.venv)` at the start of your terminal line. That means it worked.

**Step 6 — Install the required packages:**

```
pip install -r requirements.txt
```

This will take 1–3 minutes. Wait for it to finish before moving on.

---

## Part 3 — Add Your API Key

The tool uses an AI model to read and classify your files. It supports three providers — use whichever one you have a key for:

| Provider | Key starts with | Where to get one |
|---|---|---|
| **Anthropic** | `sk-ant-` | https://console.anthropic.com/ → API Keys |
| **Google Gemini** | `AIza` | https://aistudio.google.com/app/apikey |
| **OpenAI** | `sk-proj-` | https://platform.openai.com/api-keys |

**Step 7 — Run the setup wizard:**

```
python setup.py
```

When the wizard asks `API key:`, paste your key and press Enter. It will automatically detect which provider you are using based on the key prefix and save it correctly. Press Enter again to skip the Google Drive step for now (or continue to Part 4).

That's it — the wizard handles the `.env` file for you.

> **Manual alternative:** If you prefer to edit the file yourself, open `.env` in any text editor and fill in the line that matches your provider:
> - Anthropic: `ANTHROPIC_API_KEY=sk-ant-...`
> - Gemini: `GEMINI_API_KEY=AIza...`
> - OpenAI: `OPENAI_API_KEY=sk-proj-...`

---

## Part 4 — Google Drive Credentials (if scanning Google Drive)

Skip this part if you are only scanning a **local folder** on your computer. Jump to Part 5.

> **Which setup do you need?**
> - **Single folder or Shared Drive only** → follow Part 4A (OAuth, quick setup)
> - **All users' My Drives, or a selected list of user emails** → follow Part 4B (Service Account)

### Part 4A — OAuth (single folder / Shared Drive)

You should have received a file named something like `client_secret_xxxx.json` or `google_oauth_client.json`. This file is typically in your **Downloads** folder.

**Step 9 — Run the setup wizard:**

```
python setup.py
```

The wizard will ask you two questions:

**Question 1 — API key:**
Press `Enter` to keep the key you already set in Step 8.

**Question 2 — Google Drive access:**
Type `y` and press `Enter`.

When it asks:
```
Path to OAuth client JSON file:
```

Type the path to the credentials file. If it is in your Downloads folder:

Mac:
```
~/Downloads/client_secret_xxxx.json
```

Windows:
```
C:\Users\YourName\Downloads\client_secret_xxxx.json
```

> Replace `client_secret_xxxx.json` with the exact filename of the file you received.

**Question 3 — Authorize Google Drive:**
Type `y` and press `Enter`. A browser tab will open. Sign in with the Google account that has access to the Drive you want to scan. Click **Allow** when prompted.

When the browser shows a success message, return to the terminal. You will see:
```
→ Google Drive authorized successfully.
```

---

### Part 4B — Service Account with Domain-Wide Delegation (admin / workspace scan)

This is needed if you want to scan **users' personal My Drives** in the Google Workspace — either **everyone in the domain**, or **a selected list of email addresses**.

> **Important — why “sign in as admin” with OAuth is not enough**
>
> Logging into Google as a Workspace admin (Part 4A) only grants access to **that admin’s own Drive** and Shared Drives they can see. It cannot open other employees’ private My Drives.
>
> Scanning other users requires a **Service Account + Domain-Wide Delegation** (this section). The tool then impersonates each selected user to both *list* and *download* their files. Without DWD set up correctly, scans fail or return empty / permission errors.

**One-time admin setup (~10 minutes):**

1. **Create a Service Account**
   - Go to [Google Cloud Console](https://console.cloud.google.com/) → your project
   - IAM & Admin → Service Accounts → **Create Service Account**
   - Give it any name (e.g. `inventory-scanner`). Click **Create and Continue** → **Done**.
   - Click the service account → **Keys** tab → **Add Key** → **Create new key** → **JSON**
   - The JSON file downloads automatically. Move it to a safe place.

2. **Enable Domain-Wide Delegation on the service account**
   - Still on the service account page → **Details** tab
   - Expand **Advanced settings** → copy the **Client ID** (a long number)
   - Turn on **Domain-wide delegation** if there is a checkbox for it

3. **Authorise the scopes in Google Workspace Admin**
   - Go to [admin.google.com](https://admin.google.com) → Security → Access and data control → **API controls**
   - Click **Manage Domain Wide Delegation** → **Add new**
   - Paste the Client ID and add these two scopes (comma-separated, no spaces after the comma is fine):
     ```
     https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/admin.directory.user.readonly
     ```
   - Click **Authorise**.
   - Propagation can take a few minutes. If the first run fails with `unauthorized_client` or `access_denied`, wait 5–10 minutes and retry.

4. **Enable the APIs in Cloud Console**
   - Cloud Console → APIs & Services → Library
   - Enable **Google Drive API**
   - Enable **Admin SDK API**

**Step 9B — Run the setup wizard:**

```
python setup.py
```

When it asks `Set up Service Account for full workspace scan? (y/n)`, type `y`.
Paste the path to the service account JSON file and your **super-admin** email when prompted (must be a real Workspace user, e.g. `admin@yourdomain.com` — not the service account email).

The service account path and admin email are read from `.env` automatically (set by the wizard). You can also pass them explicitly with `--service-account` and `--admin-email` as shown below.

**Smoke test (recommended before a full run):**

Scan one known user with a small file cap. You should see `My Drive → that@user.com` and file counts increasing — not a wall of `WARNING: skipping` / `403` / `unauthorized_client`.

```
python run_drive.py --users alice@yourdomain.com --max-files 20 --out out/sa_smoke.xlsx
```

**Run — full workspace (every user + Shared Drives):**

```
python run_drive.py --all-drives --out out/workspace_inventory.xlsx
```

Or with paths passed explicitly:

```
python run_drive.py --all-drives --service-account ~/Downloads/service_account.json --admin-email admin@yourdomain.com --out out/workspace_inventory.xlsx
```

**Run — selected users only (N My Drives):**

List any number of Workspace emails after `--users` (spaces or commas both work). Only those users' My Drives are scanned — not the whole domain.

```
python run_drive.py --users alice@yourdomain.com bob@yourdomain.com carol@yourdomain.com --out out/selected_inventory.xlsx
```

Or comma-separated:

```
python run_drive.py --users alice@yourdomain.com,bob@yourdomain.com,carol@yourdomain.com --out out/selected_inventory.xlsx
```

**Run — selected users + Shared Drives:**

Add `--all-drives` together with `--users`. Shared Drives are included; My Drive scanning is limited to the emails you listed.

```
python run_drive.py --all-drives --users alice@yourdomain.com bob@yourdomain.com --out out/selected_inventory.xlsx
```

> `--users` requires the service account setup above. If a listed email is wrong or cannot be impersonated, that user is skipped with a warning and the rest continue.

**If it still fails — checklist:**

| Symptom | Likely cause |
| --- | --- |
| `unauthorized_client` / `Client is unauthorized to retrieve access tokens` | Client ID not added under Admin → Domain-Wide Delegation, or scopes don’t match exactly |
| `Not Authorized to access this resource` on Admin SDK | Admin SDK API not enabled, or `--admin-email` is not a super-admin / real user |
| Files listed but content empty / many `empty_download` | Old bug path — update to latest code (downloads must impersonate each user) |
| Works for admin email only, not other users | DWD missing or scopes wrong; OAuth-only setup cannot read other My Drives |

---

## Part 5 — Run the Tool

### Option A — Scan a local folder on your computer

**Step 10 — Point the tool at your data folder:**

Mac / Linux:
```
python run_dump.py --dump ~/Downloads/company-files --out out/inventory.xlsx
```

Windows:
```
python run_dump.py --dump C:\Users\YourName\Downloads\company-files --out out\inventory.xlsx
```

> Replace `~/Downloads/company-files` (or the Windows equivalent) with the actual path to the folder containing your company's files.

---

### Option B — Scan Google Drive / Workspace

Pick the command that matches what you want to inventory.

**B1 — All Shared Drives + signed-in My Drive (OAuth, Part 4A):**

```
python run_drive.py --all-drives --out out/drive_inventory.xlsx
```

**B2 — Full organisation (every user's My Drive + Shared Drives — Part 4B):**

```
python run_drive.py --all-drives --out out/workspace_inventory.xlsx
```

**B3 — Selected users' My Drives only (Part 4B):**

Replace the example emails with the Workspace accounts you want to scan. You can list as many as you need.

```
python run_drive.py --users alice@yourdomain.com bob@yourdomain.com --out out/selected_inventory.xlsx
```

**B4 — Selected users' My Drives + Shared Drives (Part 4B):**

```
python run_drive.py --all-drives --users alice@yourdomain.com bob@yourdomain.com --out out/selected_inventory.xlsx
```

**B5 — A specific folder only:**

```
python run_drive.py --folder-id "https://drive.google.com/drive/folders/PASTE_FOLDER_URL_HERE" --out out/drive_inventory.xlsx
```

> Paste the full URL of the Google Drive folder you want to scan between the quotes.

---

## Part 6 — Find Your Output

When the scan finishes, the output file will be in the `out/` folder inside the project:

Mac:
```
open out/
```

Windows:
```
explorer out
```

Open the `.xlsx` file with Excel or Google Sheets. It contains:
- A **Master** sheet with every file
- One sheet per category (Product & Engineering, Customer & Sales, etc.)
- A **Summary** sheet with counts and totals

---

## Resuming an Interrupted Scan

If the scan stops midway (internet cut out, computer sleep, etc.), just run the **exact same command** again. The tool saves its progress automatically and will pick up where it left off.

---

## Common Issues

| Problem | Fix |
|---|---|
| `python: command not found` | Use `python3` instead of `python`, or reinstall Python with "Add to PATH" checked |
| `(.venv)` not showing | Re-run Step 5 (activate the environment) |
| `No module named ...` | Re-run Step 6 (install packages) |
| `AuthenticationError` or `Invalid API key` | Double-check the key in `.env` — no spaces, no quotes around it |
| Google login fails | Make sure you are signing in with the correct Google account, and that the OAuth credentials file is a **Desktop app** type |
| Scan seems slow | Normal — each file is read by an AI. A 1,000-file drive takes ~10–20 minutes |

---

## Updating the Tool

To pull the latest version of the code, run these two commands:

```
git pull
pip install -r requirements.txt
```

Your `.env` and credentials are not affected by updates.
