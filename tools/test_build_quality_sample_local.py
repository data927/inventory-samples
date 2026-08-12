"""Self-check for tools/build_quality_sample_local.py.

Run: python tools/test_build_quality_sample_local.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.build_quality_sample_local import (
    GB,
    first_n_files,
    list_files,
    list_subfolders,
    main,
    select_files,
    select_files_two_phase,
)


def _write(p: Path, n: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)


def test_any_subfolder_names() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "Team A" / "a.txt", 1)
        _write(root / "random-bucket" / "b.txt", 1)
        assert {p.name for p in list_subfolders(root)} == {"Team A", "random-bucket"}


def test_first_n_stops_early() -> None:
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td) / "anything"
        for i in range(5):
            _write(folder / f"f{i}.txt", 1)
        got = first_n_files(folder, limit=3)
        assert [p.name for p in got] == ["f0.txt", "f1.txt", "f2.txt"]


def test_phase1_then_topup_to_cap() -> None:
    """After 2/folder, keep taking until cap fills."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # each folder: 5 x 100-byte files
        for name in ("a", "b"):
            for i in range(5):
                _write(root / name / f"{i}.bin", 100)
        by = {name: list_files(root / name) for name in ("a", "b")}
        # phase1: 2 each = 400 bytes; cap 700 → phase2 should add 3 more (300) = 700
        picked = select_files_two_phase(by, limit_per_folder=2, cap_bytes=700)
        assert sum(sz for _, _, sz in picked) == 700
        assert len(picked) == 7
        # each folder got at least the phase-1 two
        counts = {}
        for name, _, _ in picked:
            counts[name] = counts.get(name, 0) + 1
        assert counts["a"] >= 2 and counts["b"] >= 2


def test_under_limits_copies_whatever_exists() -> None:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        root, dest = td_path / "dump", td_path / "out"
        for name, n in (("small", 3), ("big", 5)):
            for i in range(n):
                _write(root / name / f"{i}.txt", 10)
        rc = main([
            "--root", str(root),
            "--limit", "1000",
            "--cap-gb", "15",
            "--dest", str(dest),
            "--out", str(td_path / "m.json"),
        ])
        assert rc == 0
        assert len(list((dest / "small").iterdir())) == 3
        assert len(list((dest / "big").iterdir())) == 5


def test_single_account_copies_entire_by_default() -> None:
    """--only one folder + --entire → all files."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        root, dest = td_path / "dump", td_path / "out"
        for i in range(12):
            _write(root / "Alice" / f"f{i}.bin", 50)
        _write(root / "Bob" / "ignored.bin", 50)
        rc = main([
            "--root", str(root),
            "--only", "Alice",
            "--entire",
            "--dest", str(dest),
            "--out", str(td_path / "m.json"),
        ])
        assert rc == 0
        assert len(list((dest / "Alice").iterdir())) == 12
        assert not (dest / "Bob").exists()


def test_as_is_stops_at_cap() -> None:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        root, dest = td_path / "dump", td_path / "out"
        for name in ("a", "b"):
            for i in range(20):
                _write(root / name / f"{i}.bin", 100)
        rc = main([
            "--root", str(root),
            "--as-is",
            "--cap-gb", str(500 / GB),
            "--dest", str(dest),
            "--out", str(td_path / "m.json"),
        ])
        assert rc == 0
        total = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
        assert total == 500


def test_entire_and_as_is_mutually_exclusive() -> None:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        root = td_path / "dump"
        _write(root / "A" / "x.bin", 1)
        rc = main([
            "--root", str(root),
            "--entire",
            "--as-is",
            "--dest", str(td_path / "out"),
            "--out", str(td_path / "m.json"),
        ])
        assert rc != 0


def test_single_account_respects_explicit_cap() -> None:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        root, dest = td_path / "dump", td_path / "out"
        for i in range(10):
            _write(root / "Alice" / f"{i}.bin", 100)
        # 10 * 100 = 1000 bytes; cap 450 → 4 files
        rc = main([
            "--root", str(root),
            "--only", "Alice",
            "--cap-gb", str(450 / GB),
            "--dest", str(dest),
            "--out", str(td_path / "m.json"),
        ])
        assert rc == 0
        assert len(list((dest / "Alice").iterdir())) == 4


def test_cap_skips_oversized() -> None:
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td) / "f"
        _write(folder / "huge.bin", 900)
        _write(folder / "a.bin", 100)
        _write(folder / "b.bin", 100)
        picked = select_files(folder, limit=10, cap_bytes=250)
        names = [p.name for p, _ in picked]
        assert "huge.bin" not in names
        assert sum(sz for _, sz in picked) == 200


if __name__ == "__main__":
    test_any_subfolder_names()
    test_first_n_stops_early()
    test_phase1_then_topup_to_cap()
    test_under_limits_copies_whatever_exists()
    test_single_account_copies_entire_by_default()
    test_as_is_stops_at_cap()
    test_entire_and_as_is_mutually_exclusive()
    test_single_account_respects_explicit_cap()
    test_cap_skips_oversized()
    print("ok")
