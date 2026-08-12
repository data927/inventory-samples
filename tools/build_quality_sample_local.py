"""Copy first N files from every subfolder under a parent folder onto the Desktop.

Point ``--root`` at any parent folder. Every immediate subfolder is processed
(names do not matter). Under each subfolder, the first ``--limit`` files
(walk order) are copied to::

    ~/Desktop/AI Labs Sample Set (YYYY-MM-DD)/
      <subfolder-name>/
      <subfolder-name>/

No size ranking — first N that show up in each subfolder.

Usage::

  python tools/build_quality_sample_local.py --root ~/Downloads/company-dump
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SKIP_DIR_NAMES = {".venv", "__pycache__", "node_modules", ".git"}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _parse_names(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        for part in item.split(","):
            name = part.strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    return out


def default_desktop() -> Path:
    return Path.home() / "Desktop"


def list_subfolders(root: Path, only: list[str] | None = None) -> list[Path]:
    """Every immediate child directory of ``root`` (dot-dirs skipped). Names unrestricted."""
    if not root.is_dir():
        raise FileNotFoundError(f"root folder not found: {root}")
    dirs = sorted(
        (p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )
    if only:
        wanted = {n.lower() for n in only}
        dirs = [p for p in dirs if p.name.lower() in wanted]
        missing = wanted - {p.name.lower() for p in dirs}
        if missing:
            _log(f"WARNING: no subfolder for: {', '.join(sorted(missing))}")
    return dirs


# Back-compat alias for tests / older imports
list_account_dirs = list_subfolders


def first_n_files(folder: Path, limit: int) -> list[Path]:
    """First ``limit`` files under ``folder`` (os.walk order)."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIR_NAMES and not d.startswith(".")
        ]
        dirnames.sort()
        filenames.sort()
        for fn in filenames:
            if fn.startswith("."):
                continue
            path = Path(dirpath) / fn
            if path.is_file():
                found.append(path)
                if len(found) >= limit:
                    return found
    return found


def dest_folder_name(folder_name: str) -> str:
    if folder_name == "AI Labs Sample Set" or not folder_name.strip():
        return f"AI Labs Sample Set ({datetime.now().strftime('%Y-%m-%d')})"
    return folder_name


def copy_files(files: list[Path], source_folder: Path, dest_folder: Path) -> tuple[int, int]:
    ok = fail = 0
    for src in files:
        rel = src.relative_to(source_folder)
        dst = dest_folder / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            ok += 1
        except OSError as exc:
            fail += 1
            _log(f"FAIL {src}: {exc}")
    return ok, fail


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", required=True, metavar="DIR",
                    help="Parent folder — every immediate subfolder is processed")
    p.add_argument("--limit", type=int, default=1000,
                    help="Max files to copy per subfolder (default: 1000)")
    p.add_argument("--folder-name", default="AI Labs Sample Set",
                    help="Destination folder name on the Desktop")
    p.add_argument("--dest", default="",
                    help="Full destination path (default: ~/Desktop/<folder-name>)")
    p.add_argument("--only", nargs="+", metavar="NAME",
                    help="Optional: only these subfolder names (space- or comma-separated)")
    p.add_argument("--users", nargs="+", metavar="NAME",
                    help=argparse.SUPPRESS)  # old alias for --only
    p.add_argument("--out", default="out/quality_sample_local_manifest.json",
                    help="Optional manifest of what was copied")
    args = p.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: --root not found or not a directory: {root}", flush=True)
        return 1
    if args.limit < 1:
        print("ERROR: --limit must be >= 1", flush=True)
        return 1

    if args.dest:
        dest_root = Path(args.dest).expanduser().resolve()
    else:
        dest_root = (default_desktop() / dest_folder_name(args.folder_name)).resolve()

    try:
        dest_root.relative_to(root)
        print(f"ERROR: --dest must not be inside --root ({dest_root} is under {root})", flush=True)
        return 1
    except ValueError:
        pass

    only = _parse_names(args.only) or _parse_names(args.users) or None
    subfolders = list_subfolders(root, only)
    if not subfolders:
        print(f"ERROR: no subfolders under {root}", flush=True)
        return 1

    dest_root.mkdir(parents=True, exist_ok=True)
    _log(f"{len(subfolders)} subfolder(s) → first {args.limit} file(s) each → {dest_root}")

    manifest_files: list[dict] = []
    total_ok = total_fail = 0
    for i, folder in enumerate(subfolders, 1):
        files = first_n_files(folder, args.limit)
        dest_sub = dest_root / folder.name
        ok, fail = copy_files(files, folder, dest_sub)
        total_ok += ok
        total_fail += fail
        _log(f"({i}/{len(subfolders)}) {folder.name}: copied {ok}/{len(files)}"
             + (f" fail={fail}" if fail else ""))
        for src in files:
            rel = src.relative_to(folder).as_posix()
            manifest_files.append({
                "name": src.name,
                "path": f"{folder.name}/{rel}",
                "folder": folder.name,
                "size_bytes": src.stat().st_size if src.is_file() else 0,
            })

    out_path = (_ROOT / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scan_mode": "local_first_n",
        "root": str(root),
        "limit_per_folder": args.limit,
        "dest": str(dest_root),
        "files": manifest_files,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    _log(f"done: ok={total_ok} fail={total_fail} → {dest_root}")
    _log(f"manifest: {out_path}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
