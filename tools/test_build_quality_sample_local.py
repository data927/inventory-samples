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

from tools.build_quality_sample_local import first_n_files, list_subfolders, main


def _write(p: Path, n: int = 1) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)


def test_any_subfolder_names() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "Team A" / "a.txt")
        _write(root / "random-bucket" / "b.txt")
        _write(root / "user@email.com" / "c.txt")
        names = [p.name for p in list_subfolders(root)]
        assert names == ["random-bucket", "Team A", "user@email.com"] or set(names) == {
            "Team A", "random-bucket", "user@email.com"
        }


def test_first_n_stops_early() -> None:
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td) / "anything"
        for i in range(5):
            _write(folder / f"f{i}.txt")
        got = first_n_files(folder, limit=3)
        assert len(got) == 3
        assert [p.name for p in got] == ["f0.txt", "f1.txt", "f2.txt"]


def test_end_to_end_limit_per_folder() -> None:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        root = td_path / "dump"
        dest = td_path / "out"
        for name in ("Folder One", "Folder Two"):
            for i in range(4):
                _write(root / name / f"{i}.bin")
        rc = main([
            "--root", str(root),
            "--limit", "2",
            "--dest", str(dest),
            "--out", str(td_path / "m.json"),
        ])
        assert rc == 0
        assert len(list_subfolders(root)) == 2
        assert len(list((dest / "Folder One").iterdir())) == 2
        assert len(list((dest / "Folder Two").iterdir())) == 2


if __name__ == "__main__":
    test_any_subfolder_names()
    test_first_n_stops_early()
    test_end_to_end_limit_per_folder()
    print("ok")
