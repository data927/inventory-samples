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

from tools.build_quality_sample_local import GB, first_n_files, list_subfolders, main, select_files


def _write(p: Path, n: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)


def test_any_subfolder_names() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "Team A" / "a.txt", 1)
        _write(root / "random-bucket" / "b.txt", 1)
        names = {p.name for p in list_subfolders(root)}
        assert names == {"Team A", "random-bucket"}


def test_first_n_stops_early() -> None:
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td) / "anything"
        for i in range(5):
            _write(folder / f"f{i}.txt", 1)
        got = first_n_files(folder, limit=3)
        assert [p.name for p in got] == ["f0.txt", "f1.txt", "f2.txt"]


def test_cap_skips_oversized_keeps_smaller() -> None:
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td) / "f"
        _write(folder / "huge.bin", 900)
        _write(folder / "a.bin", 100)
        _write(folder / "b.bin", 100)
        picked = select_files(folder, limit=10, cap_bytes=250)
        names = [p.name for p, _ in picked]
        assert "huge.bin" not in names
        assert names == ["a.bin", "b.bin"]
        assert sum(sz for _, sz in picked) == 200


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
            "--cap-gb", str(15),
            "--dest", str(dest),
            "--out", str(td_path / "m.json"),
        ])
        assert rc == 0
        assert len(list((dest / "small").iterdir())) == 3
        assert len(list((dest / "big").iterdir())) == 5


def test_cap_enforced_across_share() -> None:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        root, dest = td_path / "dump", td_path / "out"
        # 1 folder only → full cap is its share. 3x 2MB files, cap ~3MB → 1 file fits
        for i in range(3):
            _write(root / "only" / f"{i}.bin", 2 * 1024 * 1024)
        rc = main([
            "--root", str(root),
            "--limit", "1000",
            "--cap-gb", str(3 * 1024 * 1024 / GB),  # 3 MiB
            "--dest", str(dest),
            "--out", str(td_path / "m.json"),
        ])
        assert rc == 0
        assert len(list((dest / "only").iterdir())) == 1


if __name__ == "__main__":
    test_any_subfolder_names()
    test_first_n_stops_early()
    test_cap_skips_oversized_keeps_smaller()
    test_under_limits_copies_whatever_exists()
    test_cap_enforced_across_share()
    print("ok")
