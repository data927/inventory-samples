"""Copy files from every subfolder under a parent folder onto the Desktop.

Point ``--root`` at any parent folder. Every immediate subfolder is processed
(names do not matter).

Per subfolder:
  - take files in walk order (whatever comes first)
  - up to ``--limit`` files (default 1000; fewer if the folder has less)
  - under an equal slice of the overall ``--cap-gb`` budget (default 15GB)

Copies to::

    ~/Desktop/AI Labs Sample Set (YYYY-MM-DD)/
      <subfolder-name>/
      ...

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
GB = 1024 ** 3


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


list_account_dirs = list_subfolders  # back-compat


def select_files(folder: Path, *, limit: int, cap_bytes: int) -> list[tuple[Path, int]]:
    """First files under ``folder`` (walk order) that fit ``cap_bytes``, up to ``limit``.

    Skips a file that does not fit and keeps looking for a smaller one (same idea as
    Drive greedy fill). Stops once ``limit`` files are kept, or the walk ends.
    If the folder has fewer files / less data, returns whatever fits.
    """
    selected: list[tuple[Path, int]] = []
    used = 0
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
            if not path.is_file():
                continue
            try:
                size = int(path.stat().st_size)
            except OSError:
                continue
            if size <= 0:
                continue
            if used + size > cap_bytes:
                continue  # skip oversized / doesn't fit; try next
            selected.append((path, size))
            used += size
            if len(selected) >= limit:
                return selected
    return selected


def first_n_files(folder: Path, limit: int) -> list[Path]:
    """Back-compat helper used by older tests — no byte cap."""
    return [p for p, _ in select_files(folder, limit=limit, cap_bytes=10**18)]


def dest_folder_name(folder_name: str) -> str:
    if folder_name == "AI Labs Sample Set" or not folder_name.strip():
        return f"AI Labs Sample Set ({datetime.now().strftime('%Y-%m-%d')})"
    return folder_name


def copy_files(
    files: list[tuple[Path, int]], source_folder: Path, dest_folder: Path,
) -> tuple[int, int]:
    ok = fail = 0
    for src, _size in files:
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
    p.add_argument("--cap-gb", type=float, default=15.0,
                    help="Overall byte cap in GB, split equally across subfolders (default: 15)")
    p.add_argument("--folder-name", default="AI Labs Sample Set",
                    help="Destination folder name on the Desktop")
    p.add_argument("--dest", default="",
                    help="Full destination path (default: ~/Desktop/<folder-name>)")
    p.add_argument("--only", nargs="+", metavar="NAME",
                    help="Optional: only these subfolder names (space- or comma-separated)")
    p.add_argument("--users", nargs="+", metavar="NAME",
                    help=argparse.SUPPRESS)
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
    if args.cap_gb <= 0:
        print("ERROR: --cap-gb must be > 0", flush=True)
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

    cap_bytes = int(args.cap_gb * GB)
    share = max(1, cap_bytes // len(subfolders))
    dest_root.mkdir(parents=True, exist_ok=True)
    _log(f"{len(subfolders)} subfolder(s) → up to {args.limit} file(s) each, "
         f"{args.cap_gb:g}GB total (~{share / GB:.2f}GB each) → {dest_root}")

    manifest_files: list[dict] = []
    total_ok = total_fail = 0
    total_bytes = 0
    for i, folder in enumerate(subfolders, 1):
        files = select_files(folder, limit=args.limit, cap_bytes=share)
        folder_bytes = sum(sz for _, sz in files)
        dest_sub = dest_root / folder.name
        ok, fail = copy_files(files, folder, dest_sub)
        total_ok += ok
        total_fail += fail
        total_bytes += folder_bytes
        note = ""
        if len(files) < args.limit:
            note = f" (under limit; folder/cap allowed {len(files)})"
        _log(f"({i}/{len(subfolders)}) {folder.name}: copied {ok} file(s), "
             f"{folder_bytes / GB:.2f}GB{note}"
             + (f" fail={fail}" if fail else ""))
        for src, size in files:
            rel = src.relative_to(folder).as_posix()
            manifest_files.append({
                "name": src.name,
                "path": f"{folder.name}/{rel}",
                "folder": folder.name,
                "size_bytes": size,
            })

    out_path = (_ROOT / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scan_mode": "local_first_n",
        "root": str(root),
        "limit_per_folder": args.limit,
        "cap_bytes": cap_bytes,
        "cap_share_per_folder": share,
        "total_bytes": total_bytes,
        "dest": str(dest_root),
        "files": manifest_files,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    _log(f"done: ok={total_ok} fail={total_fail} bytes={total_bytes / GB:.2f}GB / {args.cap_gb:g}GB → {dest_root}")
    _log(f"manifest: {out_path}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
