from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_ROOT = Path(__file__).resolve().parent
_ENV_PATH = _ROOT / ".env"

load_dotenv(_ENV_PATH)


def _fill_empty_api_key_from_file() -> None:
    """Use ``.env`` for ``ANTHROPIC_API_KEY`` when the process env has no non-empty value."""
    cur = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if cur:
        return
    vals = dotenv_values(_ENV_PATH)
    if not vals:
        return
    key = (vals.get("ANTHROPIC_API_KEY") or "").strip().strip('"').strip("'")
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key


def _fill_empty_openai_key_from_file() -> None:
    """Use ``.env`` for ``OPENAI_API_KEY`` when the process env has no non-empty value."""
    cur = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if cur:
        return
    vals = dotenv_values(_ENV_PATH)
    if not vals:
        return
    key = (vals.get("OPENAI_API_KEY") or "").strip().strip('"').strip("'")
    if key:
        os.environ["OPENAI_API_KEY"] = key


_fill_empty_api_key_from_file()
_fill_empty_openai_key_from_file()
