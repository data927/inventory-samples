from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _print(msg: str = "") -> None:
    print(msg, flush=True)


def _ask(prompt: str, *, default: str = "", secret: bool = False) -> str:
    if default:
        display = f"{prompt} [{default}]: "
    else:
        display = f"{prompt}: "
    try:
        if secret:
            import getpass
            val = getpass.getpass(display)
        else:
            val = input(display)
    except (EOFError, KeyboardInterrupt):
        _print()
        sys.exit(0)
    return val.strip() or default


def _read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def _write_env(path: Path, env: dict[str, str]) -> None:
    # Preserve comments and ordering from .env.example; update matching keys.
    example = ROOT / ".env.example"
    if example.exists():
        lines = example.read_text(encoding="utf-8").splitlines()
        written: set[str] = set()
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.partition("=")[0].strip()
                # Uncomment and set if we have a value for this key.
                if k in env and env[k]:
                    out.append(f"{k}={env[k]}")
                    written.add(k)
                    continue
            out.append(line)
        # Append any keys not in the example.
        for k, v in env.items():
            if k not in written and v:
                out.append(f"{k}={v}")
    else:
        out = [f"{k}={v}" for k, v in env.items() if v]

    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def step_llm_api_key(env: dict[str, str]) -> dict[str, str]:
    _print("=" * 60)
    _print("STEP 1 — LLM API Key")
    _print("=" * 60)
    _print("The tool uses an LLM API (Anthropic, OpenAI, or Gemini) to classify files.")
    _print()

    current_ant = env.get("ANTHROPIC_API_KEY", "")
    current_oai = env.get("OPENAI_API_KEY", "")
    current_gem = env.get("GEMINI_API_KEY", "")
    if current_ant:
        _print(f"  Anthropic key already set: {current_ant[:12]}…")
    if current_oai:
        _print(f"  OpenAI key already set:    {current_oai[:12]}…")
    if current_gem:
        _print(f"  Gemini key already set:    {current_gem[:12]}…")
    _print()

    _print("Paste your API key below (sk-ant-… for Anthropic, sk-proj-… for OpenAI, AIza… for Gemini).")
    _print("Press Enter to keep the existing key.")
    key = _ask("API key", secret=True)
    if key:
        if key.startswith("sk-ant-") or key.startswith("sk-or-v1-"):
            env["ANTHROPIC_API_KEY"] = key
            env.pop("OPENAI_API_KEY", None)
            env.pop("GEMINI_API_KEY", None)
            _print("  → saved as ANTHROPIC_API_KEY")
        elif key.startswith("AIza"):
            env["GEMINI_API_KEY"] = key
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("OPENAI_API_KEY", None)
            _print("  → saved as GEMINI_API_KEY")
        else:
            env["OPENAI_API_KEY"] = key
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("GEMINI_API_KEY", None)
            _print("  → saved as OPENAI_API_KEY")
    else:
        _print("  → keeping existing key")
    return env


def step_google_oauth(env: dict[str, str]) -> dict[str, str]:
    _print()
    _print("=" * 60)
    _print("STEP 2 — Google Drive / Workspace OAuth (optional)")
    _print("=" * 60)
    _print("Skip this step if you are only scanning local folders.")
    _print()

    want = _ask("Set up Google Drive access? (y/n)", default="y")
    if want.lower() not in ("y", "yes"):
        _print("  → skipped")
        return env

    secrets_dir = ROOT / ".secrets"
    dest_json = secrets_dir / "google_oauth_client.json"

    _print()
    _print("You need an OAuth client JSON from Google Cloud Console.")
    _print("Steps (one-time, ~5 minutes):")
    _print("  1. Go to https://console.cloud.google.com/")
    _print("  2. Create or select a project.")
    _print("  3. APIs & Services → Library → enable 'Google Drive API' and 'Gmail API'.")
    _print("  4. APIs & Services → Credentials → Create credentials → OAuth client ID.")
    _print("     Choose 'Desktop app'. Download the JSON file.")
    _print("  5. Paste the path to that file below.")
    _print()

    existing = env.get("GOOGLE_OAUTH_CLIENT_SECRETS", "")
    if existing and Path(existing).expanduser().is_file():
        _print(f"  OAuth JSON already configured: {existing}")
        reuse = _ask("  Use existing file? (y/n)", default="y")
        if reuse.lower() in ("y", "yes"):
            _print("  → keeping existing OAuth JSON")
            return env

    while True:
        src_raw = _ask("Path to OAuth client JSON file")
        if not src_raw:
            _print("  → skipped (no path provided)")
            return env
        src = Path(src_raw).expanduser().resolve()
        if not src.is_file():
            _print(f"  File not found: {src}")
            continue
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
            if "installed" not in data and "web" not in data:
                _print("  That doesn't look like a Google OAuth client JSON. Try again.")
                continue
        except Exception as e:
            _print(f"  Could not parse JSON: {e}")
            continue
        break

    secrets_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_json)
    _print(f"  → copied to {dest_json}")

    env["GOOGLE_OAUTH_CLIENT_SECRETS"] = str(dest_json)
    env["GOOGLE_OAUTH_TOKEN_PATH"] = str(secrets_dir / "google_drive_token.json")
    return env


def step_google_login(env: dict[str, str]) -> None:
    secrets_path = env.get("GOOGLE_OAUTH_CLIENT_SECRETS", "")
    if not secrets_path or not Path(secrets_path).expanduser().is_file():
        return

    token_path = env.get("GOOGLE_OAUTH_TOKEN_PATH", "")
    if token_path and Path(token_path).expanduser().is_file():
        _print()
        _print("  Google Drive token already exists — no need to log in again.")
        return

    _print()
    _print("=" * 60)
    _print("STEP 3 — Authorize Google Drive access")
    _print("=" * 60)
    _print("A browser tab will open for you to sign in with Google.")
    _print("After approving, the token is saved locally.")
    _print()
    go = _ask("Open browser and authorize now? (y/n)", default="y")
    if go.lower() not in ("y", "yes"):
        _print("  → skipped. Run this to authorize later:")
        _print("    python tools/gdrive_scan.py --login-only")
        return

    try:
        sys.path.insert(0, str(ROOT))
        import env_loader  # noqa: F401
        from gdrive.credentials import get_credentials
        get_credentials(
            client_secrets=Path(secrets_path).expanduser().resolve(),
            token_path=Path(token_path or str(ROOT / ".secrets" / "google_drive_token.json")).expanduser().resolve(),
            full_read_scope=True,
            login_only=True,
        )
        _print("  → Google Drive authorized successfully.")
    except Exception as e:
        _print(f"  Authorization failed: {e}")
        _print("  Run manually later: python tools/gdrive_scan.py --login-only")


def step_service_account(env: dict[str, str]) -> dict[str, str]:
    _print()
    _print("=" * 60)
    _print("STEP 4 — Google Workspace full scan (optional)")
    _print("=" * 60)
    _print("This step is only needed to scan ALL users' My Drives in the organisation.")
    _print("If you only need Shared Drives or a single folder, skip this.")
    _print()

    want = _ask("Set up Service Account for full workspace scan? (y/n)", default="n")
    if want.lower() not in ("y", "yes"):
        _print("  → skipped")
        return env

    secrets_dir = ROOT / ".secrets"

    existing_sa = env.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    if existing_sa and Path(existing_sa).expanduser().is_file():
        _print(f"  Service account JSON already configured: {existing_sa}")
        reuse = _ask("  Use existing file? (y/n)", default="y")
        if reuse.lower() in ("y", "yes"):
            _print("  → keeping existing service account")
        else:
            existing_sa = ""

    if not existing_sa:
        _print()
        _print("Paste the path to your service account JSON key file.")
        _print("(Download it from Google Cloud Console → IAM & Admin → Service Accounts → Keys)")
        while True:
            src_raw = _ask("Path to service account JSON file")
            if not src_raw:
                _print("  → skipped (no path provided)")
                return env
            src = Path(src_raw).expanduser().resolve()
            if not src.is_file():
                _print(f"  File not found: {src}")
                continue
            try:
                data = json.loads(src.read_text(encoding="utf-8"))
                if data.get("type") != "service_account":
                    _print("  That doesn't look like a service account JSON. Try again.")
                    continue
            except Exception as e:
                _print(f"  Could not parse JSON: {e}")
                continue
            break
        dest = secrets_dir / "service_account.json"
        secrets_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        env["GOOGLE_SERVICE_ACCOUNT_FILE"] = str(dest)
        _print(f"  → copied to {dest}")

    existing_admin = env.get("GOOGLE_ADMIN_EMAIL", "")
    admin_email = _ask("Super-admin email (e.g. admin@yourdomain.com)", default=existing_admin)
    if admin_email:
        env["GOOGLE_ADMIN_EMAIL"] = admin_email
        _print(f"  → saved GOOGLE_ADMIN_EMAIL={admin_email}")
    else:
        _print("  → no admin email set; add GOOGLE_ADMIN_EMAIL to .env before running")

    return env


def main() -> None:
    _print()
    _print("  Inventory Segmentor — Setup Wizard")
    _print()

    env_path = ROOT / ".env"
    env = _read_env(env_path)

    env = step_llm_api_key(env)
    env = step_google_oauth(env)
    env = step_service_account(env)

    _write_env(env_path, env)
    _print()
    _print(f"  Configuration saved to {env_path}")

    step_google_login(env)

    _print()
    _print("=" * 60)
    _print("Setup complete. You can now run:")
    _print()
    _print("  Local folder:     python run_dump.py --dump /path/to/data --out out/inventory.xlsx")
    _print("  Google Drive:     python run_drive.py --out out/inventory.xlsx")
    _print("  Shared Drives:    python run_drive.py --all-drives --out out/inventory.xlsx")
    _print("  Full workspace:   python run_drive.py --all-drives --service-account .secrets/service_account.json --admin-email admin@yourdomain.com --out out/inventory.xlsx")
    _print("  Gmail (OAuth):    python run_gmail.py --out out/gmail_inventory.xlsx")
    _print("  Gmail (all users): python run_gmail.py --all-users --out out/workspace_gmail_inventory.xlsx")
    _print("=" * 60)
    _print()


if __name__ == "__main__":
    main()
