"""Copy files from every subfolder under a parent folder onto the Desktop.

Point ``--root`` at any parent folder. Every immediate subfolder is processed
(names do not matter).

Flags / modes:

  * **Multi-folder sample** (default) — ``--limit`` (default 1000) then fill to
    ``--cap-gb`` (default 15).
  * **Entire account** — ``--entire`` (optionally with ``--only NAME``): copy that
    folder's full data, no limit/cap unless you also pass ``--limit`` / ``--cap-gb``.
  * **As-is until N GB** — ``--as-is`` (default ``--cap-gb 10``): walk order, stop
    when the byte cap is filled (no per-folder first-pass).

Copies to::

    ~/Desktop/AI Labs Sample Set (YYYY-MM-DD)/
      <subfolder-name>/
      ...

Usage::

  # Entire data of one account
  python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --only "Alice" --entire

  # As-is until 10GB
  python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --as-is
  python tools/build_quality_sample_local.py --root ~/Downloads/company-dump --as-is --cap-gb 10

  # Multi-folder sample (1000 each, then fill to 15GB)
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


def list_files(folder: Path) -> list[tuple[Path, int]]:
    """All files under ``folder`` in walk order, with sizes. Skips empty / unreadable."""
    found: list[tuple[Path, int]] = []
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
            found.append((path, size))
    return found


def select_files_two_phase(
    by_folder: dict[str, list[tuple[Path, int]]],
    *,
    limit_per_folder: int,
    cap_bytes: int,
) -> list[tuple[str, Path, int]]:
    """Phase 1: up to ``limit_per_folder`` per folder. Phase 2: more until ``cap_bytes``.

    Walk order within each folder. Files that don't fit the remaining cap are skipped;
    selection keeps looking for ones that do. Returns ``(folder_name, path, size)``.
    """
    selected: list[tuple[str, Path, int]] = []
    used = 0
    names = list(by_folder.keys())
    cursors = {name: 0 for name in names}
    phase1_counts = {name: 0 for name in names}

    def _take_one(name: str) -> bool:
        """Advance cursor; if a file fits, append it and return True. Exhaust → False."""
        nonlocal used
        files = by_folder[name]
        while cursors[name] < len(files):
            path, size = files[cursors[name]]
            cursors[name] += 1
            if used + size <= cap_bytes:
                selected.append((name, path, size))
                used += size
                return True
            # skip — doesn't fit; try next in this folder
        return False

    # Phase 1 — up to limit_per_folder each
    for name in names:
        while phase1_counts[name] < limit_per_folder:
            if used >= cap_bytes:
                return selected
            if not _take_one(name):
                break
            phase1_counts[name] += 1

    # Phase 2 — keep filling until cap (round-robin across folders)
    while used < cap_bytes:
        progressed = False
        for name in names:
            if used >= cap_bytes:
                break
            if cursors[name] >= len(by_folder[name]):
                continue
            if _take_one(name):
                progressed = True
        if not progressed:
            break

    return selected


def select_files_as_is_until_cap(
    by_folder: dict[str, list[tuple[Path, int]]],
    *,
    cap_bytes: int,
) -> list[tuple[str, Path, int]]:
    """Walk order across folders (round-robin): take whatever fits until ``cap_bytes``.

    No per-folder first-pass — same idea as Drive ``--as-is``.
    """
    return select_files_two_phase(by_folder, limit_per_folder=0, cap_bytes=cap_bytes)


def first_n_files(folder: Path, limit: int) -> list[Path]:
    """Back-compat helper for older tests."""
    return [p for p, _ in list_files(folder)[:limit]]


def select_files(folder: Path, *, limit: int, cap_bytes: int) -> list[tuple[Path, int]]:
    """Back-compat: single-folder two-phase collapses to one phase when limit covers all."""
    picked = select_files_two_phase({"_": list_files(folder)}, limit_per_folder=limit, cap_bytes=cap_bytes)
    return [(path, size) for _name, path, size in picked]


def dest_folder_name(folder_name: str) -> str:
    if folder_name == "AI Labs Sample Set" or not folder_name.strip():
        return f"AI Labs Sample Set ({datetime.now().strftime('%Y-%m-%d')})"
    return folder_name


def copy_one(src: Path, source_folder: Path, dest_folder: Path) -> bool:
    rel = src.relative_to(source_folder)
    dst = dest_folder / rel
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    except OSError as exc:
        _log(f"FAIL {src}: {exc}")
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", required=True, metavar="DIR",
                    help="Parent folder — every immediate subfolder is processed")
    p.add_argument("--limit", type=int, default=None,
                    help="Files to take per subfolder first, before topping up to the GB cap "
                         "(default: 1000 for multi-folder sample mode)")
    p.add_argument("--cap-gb", type=float, default=None,
                    help="Overall byte cap in GB (default: 15 multi-sample; 10 with --as-is; "
                         "unlimited with --entire)")
    p.add_argument("--entire", action="store_true",
                    help="Copy selected account(s) entirely (no file/GB trim unless --limit/"
                         "--cap-gb also set). Use with --only NAME for one account.")
    p.add_argument("--as-is", action="store_true",
                    help="Walk-order copy until --cap-gb (default 10GB). No per-folder first-pass.")
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
    if args.entire and args.as_is:
        print("ERROR: use either --entire or --as-is, not both", flush=True)
        return 1
    if args.limit is not None and args.limit < 1:
        print("ERROR: --limit must be >= 1", flush=True)
        return 1
    if args.cap_gb is not None and args.cap_gb <= 0:
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

    folder_by_name = {f.name: f for f in subfolders}
    by_folder: dict[str, list[tuple[Path, int]]] = {}
    for folder in subfolders:
        files = list_files(folder)
        by_folder[folder.name] = files
        _log(f"  listed {folder.name}: {len(files)} file(s), "
             f"{sum(s for _, s in files) / GB:.2f}GB")

    listed_bytes = sum(sz for files in by_folder.values() for _, sz in files)
    listed_count = sum(len(files) for files in by_folder.values())
    # Back-compat: a single selected folder still means entire unless --as-is / caps set.
    entire_mode = args.entire or (len(subfolders) == 1 and not args.as_is
                                  and args.limit is None and args.cap_gb is None)

    if entire_mode:
        limit = args.limit if args.limit is not None else max(listed_count, 1)
        if args.cap_gb is not None:
            cap_bytes = int(args.cap_gb * GB)
            cap_note = f"{args.cap_gb:g}GB"
        else:
            cap_bytes = max(listed_bytes, 1)
            cap_note = "entire (no cap)"
        _log(f"--entire → {len(subfolders)} account(s), copy all "
             f"(limit={limit}, cap={cap_note}) → {dest_root}")
        scan_mode = "local_entire"
        selected = select_files_two_phase(
            by_folder, limit_per_folder=limit, cap_bytes=cap_bytes,
        )
    elif args.as_is:
        cap_gb = args.cap_gb if args.cap_gb is not None else 10.0
        cap_bytes = int(cap_gb * GB)
        _log(f"--as-is → walk order until {cap_gb:g}GB → {dest_root}")
        scan_mode = "local_as_is"
        selected = select_files_as_is_until_cap(by_folder, cap_bytes=cap_bytes)
    else:
        limit = args.limit if args.limit is not None else 1000
        cap_gb = args.cap_gb if args.cap_gb is not None else 15.0
        cap_bytes = int(cap_gb * GB)
        _log(f"{len(subfolders)} subfolder(s) → first {limit}/folder, then fill to "
             f"{cap_gb:g}GB → {dest_root}")
        scan_mode = "local_first_n_then_cap"
        selected = select_files_two_phase(
            by_folder, limit_per_folder=limit, cap_bytes=cap_bytes,
        )

    total_bytes = sum(sz for _, _, sz in selected)
    if entire_mode and args.cap_gb is None:
        _log(f"selected {len(selected)} file(s), {total_bytes / GB:.2f}GB (entire)")
    else:
        cap_gb_shown = (
            args.cap_gb if args.cap_gb is not None
            else (10.0 if args.as_is else (None if entire_mode else 15.0))
        )
        if cap_gb_shown is None:
            _log(f"selected {len(selected)} file(s), {total_bytes / GB:.2f}GB")
        else:
            _log(f"selected {len(selected)} file(s), {total_bytes / GB:.2f}GB / {cap_gb_shown:g}GB")

    dest_root.mkdir(parents=True, exist_ok=True)
    manifest_files: list[dict] = []
    total_ok = total_fail = 0
    per_folder_counts: dict[str, int] = {}
    for name, src, size in selected:
        source_folder = folder_by_name[name]
        dest_sub = dest_root / name
        if copy_one(src, source_folder, dest_sub):
            total_ok += 1
            per_folder_counts[name] = per_folder_counts.get(name, 0) + 1
        else:
            total_fail += 1
        rel = src.relative_to(source_folder).as_posix()
        manifest_files.append({
            "name": src.name,
            "path": f"{name}/{rel}",
            "folder": name,
            "size_bytes": size,
        })

    for name in by_folder:
        _log(f"  {name}: copied {per_folder_counts.get(name, 0)}")

    out_path = (_ROOT / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    limit_recorded = None if args.as_is else (args.limit if args.limit is not None else (
        max(listed_count, 1) if entire_mode else 1000
    ))
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scan_mode": scan_mode,
        "root": str(root),
        "entire": bool(entire_mode),
        "as_is": bool(args.as_is),
        "limit_per_folder": limit_recorded,
        "cap_bytes": cap_bytes,
        "total_bytes": total_bytes,
        "dest": str(dest_root),
        "files": manifest_files,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if entire_mode and args.cap_gb is None:
        _log(f"done: ok={total_ok} fail={total_fail} bytes={total_bytes / GB:.2f}GB (entire) → {dest_root}")
    elif args.as_is:
        cap_gb_done = args.cap_gb if args.cap_gb is not None else 10.0
        _log(f"done: ok={total_ok} fail={total_fail} bytes={total_bytes / GB:.2f}GB / {cap_gb_done:g}GB → {dest_root}")
    else:
        cap_gb_done = args.cap_gb if args.cap_gb is not None else 15.0
        _log(f"done: ok={total_ok} fail={total_fail} bytes={total_bytes / GB:.2f}GB / {cap_gb_done:g}GB → {dest_root}")
    _log(f"manifest: {out_path}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
