#!/usr/bin/env python3
"""Stress tests for quality-sample modes (no live Google APIs).

Flag matrix (use the matching flags — do not mix modes):

  Local multi-sample     python tools/build_quality_sample_local.py --root DIR
                         [--limit 1000] [--cap-gb 15]

  Local entire account   python tools/build_quality_sample_local.py --root DIR \\
                           --only NAME --entire

  Local as-is 10GB       python tools/build_quality_sample_local.py --root DIR \\
                           --as-is [--cap-gb 10]

  Drive entire account   python tools/build_quality_sample.py --full-account
                         (workspace: + --service-account … --admin-email … --users EMAIL)

  Drive as-is 10GB       python tools/build_quality_sample.py --as-is [--cap-gb 10]
                         (workspace: + --service-account … --admin-email … --users EMAIL)

Run::

  python tools/stress_test_quality_sample.py
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.build_quality_sample import (  # noqa: E402
    GB,
    _take_files_as_is_until_cap,
)
from tools.build_quality_sample_local import (  # noqa: E402
    list_files,
    main as local_main,
    select_files_as_is_until_cap,
    select_files_two_phase,
)


def _write(p: Path, n: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def stress_local_multi_sample() -> None:
    """--limit / --cap-gb multi-folder sample under load."""
    print("[stress] local multi-sample (--limit / --cap-gb)…", flush=True)
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        root, dest = td_path / "dump", td_path / "out"
        # 20 folders × 800 files × 8KB ≈ 128MB listed; cap 32MB with limit 50
        for f in range(20):
            folder = root / f"acct{f:02d}"
            for i in range(800):
                _write(folder / f"{i:04d}.bin", 8 * 1024)
        rc = local_main([
            "--root", str(root),
            "--limit", "50",
            "--cap-gb", str(32 * 1024 * 1024 / GB),
            "--dest", str(dest),
            "--out", str(td_path / "m.json"),
        ])
        _assert(rc == 0, f"local multi rc={rc}")
        total = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
        _assert(total <= 32 * 1024 * 1024, f"cap exceeded: {total}")
        _assert(total > 0, "copied nothing")
        # phase1 alone = 20*50*8KB = 8MB; with fill should reach near 32MB
        _assert(total >= 30 * 1024 * 1024, f"expected near-cap fill, got {total}")
    print(f"  ok in {time.perf_counter() - t0:.1f}s", flush=True)


def stress_local_entire() -> None:
    """--only NAME --entire copies everything."""
    print("[stress] local entire (--only --entire)…", flush=True)
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        root, dest = td_path / "dump", td_path / "out"
        for i in range(2500):
            _write(root / "Alice" / f"f{i:04d}.bin", 1024)
        _write(root / "Bob" / "nope.bin", 1024)
        rc = local_main([
            "--root", str(root),
            "--only", "Alice",
            "--entire",
            "--dest", str(dest),
            "--out", str(td_path / "m.json"),
        ])
        _assert(rc == 0, f"local entire rc={rc}")
        n = len(list((dest / "Alice").iterdir()))
        _assert(n == 2500, f"expected 2500, got {n}")
        _assert(not (dest / "Bob").exists(), "Bob should not be copied")
    print(f"  ok in {time.perf_counter() - t0:.1f}s", flush=True)


def stress_local_as_is_10gb_scale() -> None:
    """--as-is --cap-gb: walk order until cap (synthetic 10MB stand-in for 10GB logic)."""
    print("[stress] local as-is (--as-is --cap-gb)…", flush=True)
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        root, dest = td_path / "dump", td_path / "out"
        # Many small files across folders; one oversized that must be skipped
        for name in ("a", "b", "c", "d", "e"):
            for i in range(400):
                _write(root / name / f"{i:03d}.bin", 50_000)  # 50KB
            _write(root / name / "huge.bin", 5_000_000)  # 5MB — skip under 2MB cap
        cap = 2 * 1024 * 1024
        rc = local_main([
            "--root", str(root),
            "--as-is",
            "--cap-gb", str(cap / GB),
            "--dest", str(dest),
            "--out", str(td_path / "m.json"),
        ])
        _assert(rc == 0, f"local as-is rc={rc}")
        total = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
        _assert(total <= cap, f"as-is cap exceeded: {total}")
        _assert(total >= cap - 50_000, f"as-is under-filled: {total}")
        _assert(not any(p.name == "huge.bin" for p in dest.rglob("*")), "huge.bin should be skipped")
    print(f"  ok in {time.perf_counter() - t0:.1f}s", flush=True)


def stress_drive_as_is_10gb_selection() -> None:
    """Drive --as-is --cap-gb 10 selection logic at ~10GB scale (metadata only)."""
    print("[stress] drive as-is selection (--as-is --cap-gb 10)…", flush=True)
    t0 = time.perf_counter()
    # 50k files × ~256KB ≈ 12.8GB pool; plus some that don't fit
    batch = [{"file_id": f"f{i}", "size_bytes": 256 * 1024} for i in range(50_000)]
    batch.insert(100, {"file_id": "huge", "size_bytes": 11 * GB})  # never fits under 10GB alone mid-way
    accounted: set[str] = set()
    taken, used, full = _take_files_as_is_until_cap(
        batch, cap_bytes=10 * GB, used_bytes=0, accounted=accounted, already_done=set(),
    )
    _assert(full is True, "cap should be full")
    _assert(used <= 10 * GB, f"used {used} > 10GB")
    _assert(used >= 10 * GB - 256 * 1024, f"under-filled: {used}")
    _assert("huge" not in {r["file_id"] for r in taken}, "11GB file must be skipped")
    _assert(len(taken) >= 40_000, f"expected many files, got {len(taken)}")
    print(f"  ok files={len(taken)} used={used / GB:.3f}GB in {time.perf_counter() - t0:.1f}s", flush=True)


def stress_drive_full_account_selection() -> None:
    """Drive --full-account: no cap → take every listed file."""
    print("[stress] drive full-account selection (--full-account)…", flush=True)
    t0 = time.perf_counter()
    batch = [{"file_id": f"f{i}", "size_bytes": 1024} for i in range(20_000)]
    accounted: set[str] = set()
    taken, used, full = _take_files_as_is_until_cap(
        batch, cap_bytes=None, used_bytes=0, accounted=accounted, already_done=set(),
    )
    _assert(full is False, "unlimited must not report cap_full")
    _assert(len(taken) == 20_000, f"expected all 20000, got {len(taken)}")
    _assert(used == 20_000 * 1024, f"used={used}")
    print(f"  ok files={len(taken)} in {time.perf_counter() - t0:.1f}s", flush=True)


def stress_cli_flag_guards() -> None:
    """Proper flags: mutually exclusive modes reject mixes."""
    print("[stress] CLI flag guards…", flush=True)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "dump"
        _write(root / "A" / "x.bin", 10)
        rc = local_main(["--root", str(root), "--entire", "--as-is", "--dest", str(Path(td) / "o")])
        _assert(rc != 0, "local --entire + --as-is should fail")

    # Drive parser mutual exclusion
    from tools import build_quality_sample as bqs
    rc = bqs.main(["--full-account", "--as-is", "--scan-only"])
    # --scan-only also invalid, but mutual exclusion should fire first
    _assert(rc != 0, "drive --full-account + --as-is should fail")
    print("  ok", flush=True)


def stress_select_helpers_scale() -> None:
    """Pure selection helpers with large in-memory catalogs."""
    print("[stress] selection helpers scale…", flush=True)
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        by: dict[str, list[tuple[Path, int]]] = {}
        for name in ("u1", "u2", "u3", "u4"):
            files = []
            for i in range(5000):
                p = root / name / f"{i}.bin"
                _write(p, 4096)
                files.append((p, 4096))
            by[name] = files
        picked = select_files_two_phase(by, limit_per_folder=100, cap_bytes=50 * 1024 * 1024)
        total = sum(sz for _, _, sz in picked)
        _assert(total <= 50 * 1024 * 1024, f"two-phase over cap {total}")
        as_is = select_files_as_is_until_cap(by, cap_bytes=20 * 1024 * 1024)
        as_total = sum(sz for _, _, sz in as_is)
        _assert(as_total <= 20 * 1024 * 1024, f"as-is over cap {as_total}")
        _assert(len(list_files(root / "u1")) == 5000, "list_files count")
    print(f"  ok in {time.perf_counter() - t0:.1f}s", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--quick", action="store_true", help="Skip the heaviest local disk stress cases")
    args = p.parse_args(argv)

    stress_cli_flag_guards()
    stress_drive_as_is_10gb_selection()
    stress_drive_full_account_selection()
    stress_select_helpers_scale()
    if not args.quick:
        stress_local_entire()
        stress_local_as_is_10gb_scale()
        stress_local_multi_sample()
    print("ALL STRESS OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
