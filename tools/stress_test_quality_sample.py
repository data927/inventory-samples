#!/usr/bin/env python3
"""Stress tests for quality-sample modes (no live Google APIs).

Flag matrix (use the matching flags — do not mix modes):

  Local multi-sample     python tools/build_quality_sample_local.py --root DIR
                         [--limit 1000] [--cap-gb 15]

  Local entire account   python tools/build_quality_sample_local.py --root DIR \\
                           --only NAME --entire

  Local as-is 40GB       python tools/build_quality_sample_local.py --root DIR \\
                           --as-is [--cap-gb 40]

  Drive entire account   python tools/build_quality_sample.py --full-account
                         (workspace: + --service-account … --admin-email … --users EMAIL)

  Drive as-is 40GB       python tools/build_quality_sample.py --as-is [--cap-gb 40]
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
    """--as-is --cap-gb: walk order until cap (synthetic 10MB stand-in for 40GB logic)."""
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
    """Drive --as-is --cap-gb 40 selection logic at ~40GB scale (metadata only)."""
    print("[stress] drive as-is selection (--as-is --cap-gb 40)…", flush=True)
    t0 = time.perf_counter()
    # 200k files × 256KB ≈ 51.2GB pool; plus some that don't fit
    batch = [{"file_id": f"f{i}", "size_bytes": 256 * 1024} for i in range(200_000)]
    batch.insert(100, {"file_id": "huge", "size_bytes": 45 * GB})  # never fits under 40GB alone mid-way
    accounted: set[str] = set()
    taken, used, full = _take_files_as_is_until_cap(
        batch, cap_bytes=40 * GB, used_bytes=0, accounted=accounted, already_done=set(),
    )
    _assert(full is True, "cap should be full")
    _assert(used <= 40 * GB, f"used {used} > 40GB")
    _assert(used >= 40 * GB - 256 * 1024, f"under-filled: {used}")
    _assert("huge" not in {r["file_id"] for r in taken}, "oversized file must be skipped")
    _assert(len(taken) >= 160_000, f"expected many files, got {len(taken)}")
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


def stress_drive_natives_free_on_cap() -> None:
    """Google Docs/Sheets/Slides (size 0) copy without consuming the 40GB budget."""
    print("[stress] drive natives free on 40GB cap…", flush=True)
    from tools.build_quality_sample import _drive_rows_all_files

    pdf_size = (40 * GB) // 100  # 100 PDFs ≈ 40GB (floor)
    rows = []
    for i in range(1000):
        rows.append({
            "drive_file_id": f"doc{i}", "name": f"Doc {i}", "path": f"d/{i}",
            "owner_email": "a@x", "size_bytes": 0, "mime_type": "application/vnd.google-apps.document",
            "is_folder": False, "is_shortcut": False, "modified_time": "",
        })
    for i in range(100):
        rows.append({
            "drive_file_id": f"pdf{i}", "name": f"p{i}.pdf", "path": f"p/{i}",
            "owner_email": "a@x", "size_bytes": pdf_size,
            "mime_type": "application/pdf", "is_folder": False, "is_shortcut": False, "modified_time": "",
        })
    # one more tiny file that pushes over / fills remaining after floor division
    rem = 40 * GB - 100 * pdf_size
    if rem > 0:
        rows.append({
            "drive_file_id": "tail", "name": "tail.bin", "path": "tail.bin",
            "owner_email": "a@x", "size_bytes": rem,
            "mime_type": "application/octet-stream", "is_folder": False, "is_shortcut": False,
            "modified_time": "",
        })
    files = _drive_rows_all_files(rows)
    accounted: set[str] = set()
    taken, used, full = _take_files_as_is_until_cap(
        files, cap_bytes=40 * GB, used_bytes=0, accounted=accounted, already_done=set(),
    )
    _assert(full is True, f"should fill exactly; used={used} full={full}")
    _assert(used == 40 * GB, f"used={used}")
    _assert(sum(1 for r in taken if r["file_id"].startswith("doc")) == 1000, "all natives included")
    _assert(sum(1 for r in taken if r["file_id"].startswith("pdf")) == 100, "all pdfs included")
    print(f"  ok natives=1000 pdfs=100 used={used / GB:.1f}GB", flush=True)


def stress_drive_as_is_multi_round_10gb() -> None:
    """Simulate many walk rounds filling exactly 40GB."""
    print("[stress] drive as-is multi-round 40GB…", flush=True)
    t0 = time.perf_counter()
    used = 0
    accounted: set[str] = set()
    total_taken = 0
    full = False
    for round_num in range(200):
        batch = [
            {"file_id": f"r{round_num}_{i}", "size_bytes": 5 * 1024 * 1024}  # 5MB
            for i in range(50)
        ]
        # sprinkle an oversized file each round
        batch.append({"file_id": f"huge_{round_num}", "size_bytes": 20 * GB})
        taken, used, full = _take_files_as_is_until_cap(
            batch, cap_bytes=40 * GB, used_bytes=used, accounted=accounted, already_done=set(),
        )
        total_taken += len(taken)
        if full:
            break
    _assert(full is True, "should eventually fill 40GB")
    _assert(used <= 40 * GB, f"over cap {used}")
    _assert(used >= 40 * GB - 5 * 1024 * 1024, f"under-filled {used}")
    _assert(total_taken >= 2000, f"expected many 5MB files, got {total_taken}")
    print(f"  ok rounds_stop used={used / GB:.3f}GB files={total_taken} in {time.perf_counter() - t0:.1f}s",
          flush=True)


def stress_dwd_share_and_transfer_mocked() -> None:
    """End-to-end --full-account/--as-is transfer path with mocked Drive APIs (DWD-style)."""
    print("[stress] DWD share + transfer mocked…", flush=True)
    import tools.build_quality_sample as bqs

    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as td:
        ckpt_dir = Path(td)
        copied: list[tuple[str, str, str]] = []
        shared: list[tuple[str, str]] = []
        created_folders: list[str] = []

        class _FakeSvc:
            pass

        def fake_walk(service, *, path_prefix, frontier, folder_budget, modified_before, progress_log):
            # Two rounds then done
            if frontier is None:
                rows = [
                    {"drive_file_id": "gdoc1", "name": "Sheet", "path": "Sheet",
                     "owner_email": "alice@x", "size_bytes": None,
                     "mime_type": "application/vnd.google-apps.spreadsheet",
                     "is_folder": False, "is_shortcut": False, "modified_time": ""},
                    {"drive_file_id": "pdf1", "name": "a.pdf", "path": "a.pdf",
                     "owner_email": "alice@x", "size_bytes": 3 * 1024 * 1024,
                     "mime_type": "application/pdf", "is_folder": False, "is_shortcut": False,
                     "modified_time": ""},
                    {"drive_file_id": "xlsx1", "name": "b.xlsx", "path": "b.xlsx",
                     "owner_email": "alice@x", "size_bytes": 2 * 1024 * 1024,
                     "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     "is_folder": False, "is_shortcut": False, "modified_time": ""},
                ]
                return rows, ["more"]  # non-empty frontier → second round
            rows = [
                {"drive_file_id": "pdf2", "name": "c.pdf", "path": "c.pdf",
                 "owner_email": "alice@x", "size_bytes": 4 * 1024 * 1024,
                 "mime_type": "application/pdf", "is_folder": False, "is_shortcut": False,
                 "modified_time": ""},
                {"drive_file_id": "folder", "name": "dir", "path": "dir",
                 "owner_email": "alice@x", "size_bytes": 0,
                 "mime_type": "application/vnd.google-apps.folder",
                 "is_folder": True, "is_shortcut": False, "modified_time": ""},
            ]
            return rows, []  # done

        def fake_create(service, name, parent_id="root"):
            created_folders.append(name)
            return "destROOT"

        def fake_share(service, folder_id, email, progress_log=print):
            shared.append((folder_id, email))

        def fake_ensure(service, parent_id, name, cache):
            cache[name] = f"child-{name}"
            return cache[name]

        def fake_copy(service, file_id, dest_parent, name):
            copied.append((file_id, dest_parent, name))
            return f"new-{file_id}"

        orig_walk = bqs.walk_my_drive_in_rounds
        orig_create = bqs._create_root_folder
        orig_share = bqs._share_folder_writer
        orig_ensure = bqs._ensure_child_folder
        orig_copy = bqs._drive_copy
        orig_rw = bqs._get_rw_credentials
        try:
            bqs.walk_my_drive_in_rounds = fake_walk
            bqs._create_root_folder = fake_create
            bqs._share_folder_writer = fake_share
            bqs._ensure_child_folder = fake_ensure
            bqs._drive_copy = fake_copy
            bqs._get_rw_credentials = lambda: object()

            admin_svc, user_svc = _FakeSvc(), _FakeSvc()
            # as-is with 8MB cap → should take gdoc(0)+pdf1(3)+xlsx(2)+pdf2(4)=9 > 8,
            # so pdf2 may or may not fit after 5MB → remaining 3MB, pdf2=4MB skipped
            out = bqs.run_full_account_drive_transfer(
                user_svc, "alice@x",
                folder_name="AI Labs Sample Set",
                dest_folder_id="",
                checkpoint_dir=ckpt_dir,
                folders_per_round=10,
                cap_bytes=8 * 1024 * 1024,
                dest_owner_service=admin_svc,
                copy_service=user_svc,
                share_dest_with="alice@x",
                progress_log=lambda m: None,
            )
            _assert(shared == [("destROOT", "alice@x")], f"share={shared}")
            _assert(created_folders and created_folders[0].startswith("AI Labs Sample Set"),
                    f"created={created_folders}")
            ids = {c[0] for c in copied}
            _assert("gdoc1" in ids, "native sheet should copy")
            _assert("pdf1" in ids and "xlsx1" in ids, f"binaries missing: {ids}")
            _assert("pdf2" not in ids, "pdf2 should not fit under 8MB after 5MB used")
            _assert("folder" not in ids, "folders must not copy")
            _assert(len(out) == len(copied), "manifest rows match copies")

            # full-account (no cap) on a fresh checkpoint dir
            ckpt2 = ckpt_dir / "full"
            ckpt2.mkdir()
            copied.clear()
            shared.clear()
            created_folders.clear()
            # reset walk to always return one batch then done — redefine
            def fake_walk_full(service, *, path_prefix, frontier, folder_budget, modified_before, progress_log):
                if frontier is None:
                    return [
                        {"drive_file_id": "a", "name": "a.pdf", "path": "a.pdf",
                         "owner_email": "alice@x", "size_bytes": 100,
                         "mime_type": "application/pdf", "is_folder": False, "is_shortcut": False,
                         "modified_time": ""},
                        {"drive_file_id": "b", "name": "b.docx", "path": "b.docx",
                         "owner_email": "alice@x", "size_bytes": 200,
                         "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                         "is_folder": False, "is_shortcut": False, "modified_time": ""},
                    ], []
                return [], []

            bqs.walk_my_drive_in_rounds = fake_walk_full
            out2 = bqs.run_full_account_drive_transfer(
                user_svc, "alice@x",
                folder_name="AI Labs Sample Set",
                dest_folder_id="",
                checkpoint_dir=ckpt2,
                folders_per_round=10,
                cap_bytes=None,
                dest_owner_service=admin_svc,
                copy_service=user_svc,
                share_dest_with="alice@x",
                progress_log=lambda m: None,
            )
            _assert({c[0] for c in copied} == {"a", "b"}, f"full copy={copied}")
            _assert(len(out2) == 2, f"out2={len(out2)}")
            _assert(shared == [("destROOT", "alice@x")], "should share for full too")
        finally:
            bqs.walk_my_drive_in_rounds = orig_walk
            bqs._create_root_folder = orig_create
            bqs._share_folder_writer = orig_share
            bqs._ensure_child_folder = orig_ensure
            bqs._drive_copy = orig_copy
            bqs._get_rw_credentials = orig_rw

    print(f"  ok in {time.perf_counter() - t0:.1f}s", flush=True)


def stress_cli_workspace_requires_one_user() -> None:
    """Workspace --full-account/--as-is need exactly one --users."""
    print("[stress] CLI workspace requires one --users…", flush=True)
    from tools import build_quality_sample as bqs
    # Fake SA file path that exists as empty file so workspace_mode can engage...
    # Actually workspace needs sa file AND admin email. Without valid SA file it errors earlier.
    with tempfile.TemporaryDirectory() as td:
        sa = Path(td) / "sa.json"
        sa.write_text('{"type":"service_account","client_email":"x@y.iam.gserviceaccount.com"}')
        rc = bqs.main([
            "--service-account", str(sa),
            "--admin-email", "admin@x.com",
            "--full-account",
        ])
        _assert(rc != 0, "full-account without --users should fail")
        rc2 = bqs.main([
            "--service-account", str(sa),
            "--admin-email", "admin@x.com",
            "--users", "a@x.com", "b@x.com",
            "--as-is",
        ])
        _assert(rc2 != 0, "as-is with two --users should fail")
    print("  ok", flush=True)


def stress_share_folder_idempotent() -> None:
    """_share_folder_writer treats already-shared as success."""
    print("[stress] share folder idempotent…", flush=True)
    from googleapiclient.errors import HttpError
    from tools.build_quality_sample import _share_folder_writer

    class Resp:
        def __init__(self, status):
            self.status = status
            self.reason = "Error"

    class FakePerms:
        def __init__(self, status=None, existing=None):
            self.status = status
            self.existing = existing or []
            self.calls = 0

        def list(self, **kwargs):
            self._list_mode = True
            return self

        def create(self, **kwargs):
            self._list_mode = False
            self.calls += 1
            return self

        def execute(self):
            if getattr(self, "_list_mode", False):
                return {"permissions": list(self.existing)}
            if self.status:
                raise HttpError(Resp(self.status), b'{"error":{"message":"already"}}')
            return {"id": "perm1", "role": "writer", "emailAddress": "a@x"}

    class FakeSvc:
        def __init__(self, status=None, existing=None):
            self._perms = FakePerms(status, existing)

        def permissions(self):
            return self._perms

    logs: list[str] = []
    _share_folder_writer(FakeSvc(), "fid", "a@x", progress_log=logs.append)
    _assert(any("shared" in m for m in logs), logs)

    logs.clear()
    _share_folder_writer(
        FakeSvc(existing=[{"emailAddress": "a@x", "role": "writer"}]),
        "fid", "a@x", progress_log=logs.append,
    )
    _assert(any("already has access" in m for m in logs), logs)

    logs.clear()
    _share_folder_writer(FakeSvc(409), "fid", "a@x", progress_log=logs.append)
    _assert(any("allowed" in m for m in logs), logs)
    print("  ok", flush=True)


def stress_drive_as_is_100k_files() -> None:
    """Heavier metadata fill: ~350k candidates → 40GB."""
    print("[stress] drive as-is 350k files → 40GB…", flush=True)
    t0 = time.perf_counter()
    # 350k × 128KB ≈ 44.8GB pool
    batch = [{"file_id": f"f{i}", "size_bytes": 128 * 1024} for i in range(350_000)]
    accounted: set[str] = set()
    taken, used, full = _take_files_as_is_until_cap(
        batch, cap_bytes=40 * GB, used_bytes=0, accounted=accounted, already_done=set(),
    )
    _assert(full is True, "cap full")
    _assert(used == 40 * GB, f"used={used}")
    _assert(len(taken) == (40 * GB) // (128 * 1024), f"count={len(taken)}")
    print(f"  ok files={len(taken)} in {time.perf_counter() - t0:.1f}s", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--quick", action="store_true", help="Skip the heaviest local disk stress cases")
    args = p.parse_args(argv)

    stress_cli_flag_guards()
    stress_cli_workspace_requires_one_user()
    stress_share_folder_idempotent()
    stress_drive_natives_free_on_cap()
    stress_drive_as_is_10gb_selection()
    stress_drive_as_is_multi_round_10gb()
    stress_drive_as_is_100k_files()
    stress_drive_full_account_selection()
    stress_dwd_share_and_transfer_mocked()
    stress_select_helpers_scale()
    if not args.quick:
        stress_local_entire()
        stress_local_as_is_10gb_scale()
        stress_local_multi_sample()
    print("ALL STRESS OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
