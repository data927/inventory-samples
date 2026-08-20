"""Self-check for tools/build_quality_sample.py's selection logic. No live API calls.

Run: python tools/test_build_quality_sample.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.build_quality_sample import (
    GB,
    add_to_thread,
    allocate_binary_by_account,
    allocate_equally_by_account,
    greedy_fill,
    select_native_by_account,
    select_threads_first_n,
    select_top_by_recency,
    _account_has_enough,
    _drive_rows_all_files,
    _group_by_account,
    _take_files_as_is_until_cap,
)


def test_greedy_fill_backfills_around_oversized_item() -> None:
    candidates = [
        {"id": "huge", "size_bytes": 8 * GB},   # doesn't fit alone under a 5GB cap
        {"id": "big", "size_bytes": 3 * GB},
        {"id": "small", "size_bytes": 1 * GB},
        {"id": "tiny", "size_bytes": 500 * 1024 * 1024},
    ]
    selected, total = greedy_fill(candidates, cap_bytes=5 * GB)
    ids = {c["id"] for c in selected}
    assert "huge" not in ids, "oversized item must be skipped, not truncate the whole budget"
    assert ids == {"big", "small", "tiny"}, ids
    assert total <= 5 * GB
    assert total == 3 * GB + 1 * GB + 500 * 1024 * 1024


def test_greedy_fill_empty_and_exact_cap() -> None:
    assert greedy_fill([], cap_bytes=5 * GB) == ([], 0)
    selected, total = greedy_fill([{"id": "x", "size_bytes": 5 * GB}], cap_bytes=5 * GB)
    assert total == 5 * GB and len(selected) == 1


def test_add_to_thread_groups_and_sums_by_thread() -> None:
    by_thread: dict = {}
    add_to_thread(by_thread, "a@co.com", {"thread_id": "t1", "subject": "Hi", "size_bytes": 100, "message_id": "m1"})
    add_to_thread(by_thread, "a@co.com", {"thread_id": "t1", "subject": "Hi", "size_bytes": 250, "message_id": "m2"})
    add_to_thread(by_thread, "a@co.com", {"thread_id": "t2", "subject": "Other", "size_bytes": 50, "message_id": "m3"})
    add_to_thread(by_thread, "b@co.com", {"thread_id": "t1", "subject": "Hi", "size_bytes": 10, "message_id": "m4"})

    assert len(by_thread) == 3, "distinct (email, thread_id) pairs must not merge across users"
    t1 = by_thread[("a@co.com", "t1")]
    assert t1["size_bytes"] == 350
    assert t1["message_ids"] == ["m1", "m2"]
    assert by_thread[("a@co.com", "t2")]["size_bytes"] == 50
    assert by_thread[("b@co.com", "t1")]["size_bytes"] == 10


def test_select_top_by_recency_picks_newest_and_respects_limit() -> None:
    rows = [
        {"id": "old", "modified_time": "2023-01-01T00:00:00.000Z"},
        {"id": "newest", "modified_time": "2026-01-01T00:00:00.000Z"},
        {"id": "mid", "modified_time": "2024-06-01T00:00:00.000Z"},
    ]
    picked = select_top_by_recency(rows, limit=2)
    assert [r["id"] for r in picked] == ["newest", "mid"]
    assert select_top_by_recency(rows, limit=0) == []
    assert len(select_top_by_recency(rows, limit=100)) == 3


def _native_row(file_id: str, owner_email: str, modified_time: str) -> dict:
    return {"file_id": file_id, "owner_email": owner_email, "modified_time": modified_time}


def test_select_native_by_account_tops_up_when_floor_is_under_cap() -> None:
    rows = [_native_row(f"a{i}", "a@co.com", f"2026-01-{10 - i:02d}") for i in range(1, 6)]
    rows += [_native_row(f"b{i}", "b@co.com", f"2026-01-{5 - i:02d}") for i in range(1, 6)]

    picked = select_native_by_account(rows, per_account_limit=2, overall_cap=6)
    ids = {r["file_id"] for r in picked}
    # floor: newest 2 from each account; topup: next 2 most-recent overall (both from account a)
    assert ids == {"a1", "a2", "b1", "b2", "a3", "a4"}, ids
    assert "b3" not in ids, "topup must prefer globally-newer files over a weaker account's 3rd pick"


def test_select_native_by_account_floor_guarantee_is_never_trimmed() -> None:
    rows = [_native_row(f"a{i}", "a@co.com", f"2026-01-0{i}") for i in range(1, 4)]
    rows += [_native_row(f"b{i}", "b@co.com", f"2026-01-0{i}") for i in range(1, 4)]

    picked = select_native_by_account(rows, per_account_limit=3, overall_cap=2)
    assert len(picked) == 6, "per-account floor (3+3=6) must not be trimmed back to the smaller overall_cap"


def _binary_row(file_id: str, owner_email: str, size_bytes: int) -> dict:
    return {"file_id": file_id, "owner_email": owner_email, "size_bytes": size_bytes}


def test_allocate_binary_by_account_prioritizes_bigger_account_when_granularity_allows() -> None:
    MB = 1024 * 1024
    # account a: 200MB across 20 similar-sized files; account b: 20MB across 20 files.
    # Enough files per account for the round-robin to express priority cleanly.
    rows = [_binary_row(f"a{i}", "a@co.com", 10 * MB) for i in range(20)]
    rows += [_binary_row(f"b{i}", "b@co.com", 1 * MB) for i in range(20)]
    selected, total = allocate_binary_by_account(rows, cap_bytes=150 * MB)
    a_total = sum(r["size_bytes"] for r in selected if r["owner_email"] == "a@co.com")
    b_total = sum(r["size_bytes"] for r in selected if r["owner_email"] == "b@co.com")
    assert total == 150 * MB, "cap should be fully used"
    assert a_total > 0 and b_total > 0, "both accounts must be represented"
    assert a_total > b_total, "the account with more data should get the bigger absolute slice"


def test_allocate_binary_by_account_empty() -> None:
    assert allocate_binary_by_account([], cap_bytes=1000) == ([], 0)


def test_allocate_binary_by_account_guarantees_every_account_is_included() -> None:
    MB = 1024 * 1024
    # account a: 60MB across 3 chunky files; account b: 15MB across 2 files; cap = 30MB.
    # A pure proportional split (a's ~24MB share, b's ~6MB share) fits none of a's files
    # and only b's smallest — a naive allocation would shut b's second file out and waste
    # cap. The per-account guarantee must still get both accounts represented.
    rows = [
        _binary_row("a1", "a@co.com", 30 * MB),
        _binary_row("a2", "a@co.com", 20 * MB),
        _binary_row("a3", "a@co.com", 10 * MB),
        _binary_row("b1", "b@co.com", 10 * MB),
        _binary_row("b2", "b@co.com", 5 * MB),
    ]
    selected, total = allocate_binary_by_account(rows, cap_bytes=30 * MB)
    a_total = sum(r["size_bytes"] for r in selected if r["owner_email"] == "a@co.com")
    b_total = sum(r["size_bytes"] for r in selected if r["owner_email"] == "b@co.com")
    assert a_total > 0 and b_total > 0, "neither account should ever be shut out entirely"
    assert total <= 30 * MB


def test_allocate_binary_by_account_never_exceeds_cap_single_account() -> None:
    MB = 1024 * 1024
    rows = [_binary_row(f"c{i}", "c@co.com", s * MB) for i, s in enumerate([50, 30, 20, 10, 5])]
    selected, total = allocate_binary_by_account(rows, cap_bytes=65 * MB)
    assert total == 65 * MB, "should pack the cap as tightly as plain greedy_fill would"
    assert sum(r["size_bytes"] for r in selected) == total
    assert total <= 65 * MB


def _empty_buckets() -> dict:
    return {"binary": [], "gsheets": [], "gdocs": [], "gslides": []}


def test_account_has_enough_requires_both_native_and_binary_thresholds() -> None:
    thresholds = dict(gsheets_per_account=30, gdocs_per_account=40, gslides_per_account=20, drive_cap_bytes=10 * GB)

    empty = _empty_buckets()
    assert not _account_has_enough(empty, **thresholds), "nothing found yet -> not enough"

    native_only = _empty_buckets()
    native_only["gsheets"] = [{"file_id": f"s{i}"} for i in range(90)]   # 30 * 3 multiplier
    native_only["gdocs"] = [{"file_id": f"d{i}"} for i in range(120)]    # 40 * 3
    native_only["gslides"] = [{"file_id": f"p{i}"} for i in range(60)]   # 20 * 3
    assert not _account_has_enough(native_only, **thresholds), "native thresholds met but binary bytes are still 0"

    binary_only = _empty_buckets()
    binary_only["binary"] = [{"file_id": "big", "size_bytes": 10 * GB}]
    assert not _account_has_enough(binary_only, **thresholds), "binary threshold met but native counts are still 0"

    both = _empty_buckets()
    both["gsheets"] = native_only["gsheets"]
    both["gdocs"] = native_only["gdocs"]
    both["gslides"] = native_only["gslides"]
    both["binary"] = binary_only["binary"]
    assert _account_has_enough(both, **thresholds), "both thresholds met -> enough"

    one_short = _empty_buckets()
    one_short["gsheets"] = native_only["gsheets"][:-1]  # 89, just under the 90 threshold
    one_short["gdocs"] = native_only["gdocs"]
    one_short["gslides"] = native_only["gslides"]
    one_short["binary"] = binary_only["binary"]
    assert not _account_has_enough(one_short, **thresholds), "one native type just short -> still not enough"


def test_group_by_account_supports_custom_key() -> None:
    drive_rows = [{"owner_email": "a@co.com", "x": 1}, {"owner_email": "b@co.com", "x": 2}]
    assert set(_group_by_account(drive_rows)) == {"a@co.com", "b@co.com"}

    gmail_rows = [{"user_email": "c@co.com", "x": 1}, {"user_email": "c@co.com", "x": 2}]
    grouped = _group_by_account(gmail_rows, key="user_email")
    assert set(grouped) == {"c@co.com"}
    assert len(grouped["c@co.com"]) == 2


def _thread(email: str, tid: str, size_bytes: int) -> dict:
    return {"user_email": email, "thread_id": tid, "subject": "x", "size_bytes": size_bytes,
            "message_ids": [f"{tid}-m1"]}


def test_allocate_equally_by_account_splits_evenly_ignoring_data_volume() -> None:
    rows = [
        _thread("a@co.com", "t1", 3 * GB), _thread("a@co.com", "t2", 5 * GB), _thread("a@co.com", "t3", 10 * GB),
        _thread("b@co.com", "t4", 1 * GB),
        _thread("c@co.com", "t5", 1 * GB), _thread("c@co.com", "t6", 1 * GB),
    ]
    cap = 6 * GB  # 3 accounts -> 2GB nominal equal share each
    selected, total = allocate_equally_by_account(rows, cap)
    by_acct: dict[str, int] = {}
    for r in selected:
        by_acct[r["user_email"]] = by_acct.get(r["user_email"], 0) + r["size_bytes"]

    assert total == cap, "leftover reclaim should fully use the cap"
    assert set(by_acct) == {"a@co.com", "b@co.com", "c@co.com"}, "every account represented"
    # a has 18GB of data (9x more than b) but its equal share is the same nominal 2GB as
    # everyone else — it only ends up with more because reclaim happened to fit its file.
    assert by_acct["b@co.com"] == 1 * GB
    assert by_acct["c@co.com"] == 2 * GB


def test_allocate_equally_by_account_empty() -> None:
    assert allocate_equally_by_account([], cap_bytes=1000) == ([], 0)


def test_allocate_equally_by_account_cannot_exceed_cap() -> None:
    rows = [_thread("small@co.com", "t1", 1 * GB), _thread("chunky@co.com", "t2", 9 * GB)]
    selected, total = allocate_equally_by_account(rows, cap_bytes=2 * GB)
    assert total == 1 * GB, "the 9GB thread can never fit a 2GB cap, reclaim or not"
    assert total <= 2 * GB


def test_drive_rows_all_files_keeps_zero_size_natives() -> None:
    rows = [
        {"drive_file_id": "1", "name": "doc", "path": "a/doc", "owner_email": "a@x",
         "size_bytes": 0, "mime_type": "application/vnd.google-apps.document",
         "is_folder": False, "is_shortcut": False, "modified_time": ""},
        {"drive_file_id": "2", "name": "pdf", "path": "a/pdf", "owner_email": "a@x",
         "size_bytes": 100, "mime_type": "application/pdf",
         "is_folder": False, "is_shortcut": False, "modified_time": ""},
        {"drive_file_id": "3", "name": "folder", "path": "a", "owner_email": "a@x",
         "size_bytes": 0, "mime_type": "application/vnd.google-apps.folder",
         "is_folder": True, "is_shortcut": False, "modified_time": ""},
        {"drive_file_id": "4", "name": "link", "path": "a/link", "owner_email": "",
         "size_bytes": 0, "mime_type": "application/vnd.google-apps.shortcut",
         "is_folder": False, "is_shortcut": True, "modified_time": ""},
    ]
    got = _drive_rows_all_files(rows, default_owner="fallback@x")
    assert {r["file_id"] for r in got} == {"1", "2"}
    assert next(r for r in got if r["file_id"] == "2")["size_bytes"] == 100


def test_take_files_as_is_until_cap_walk_order() -> None:
    batch = [
        {"file_id": "a", "size_bytes": 4},
        {"file_id": "huge", "size_bytes": 100},
        {"file_id": "b", "size_bytes": 3},
        {"file_id": "c", "size_bytes": 2},
    ]
    accounted: set[str] = set()
    taken, used, full = _take_files_as_is_until_cap(
        batch, cap_bytes=9, used_bytes=0, accounted=accounted, already_done=set(),
    )
    assert [r["file_id"] for r in taken] == ["a", "b", "c"]
    assert "huge" not in {r["file_id"] for r in taken}
    assert used == 9 and full is True


def test_take_files_as_is_counts_already_done_toward_cap() -> None:
    batch = [
        {"file_id": "done1", "size_bytes": 6},
        {"file_id": "new", "size_bytes": 5},
        {"file_id": "small", "size_bytes": 3},
    ]
    accounted: set[str] = set()
    taken, used, full = _take_files_as_is_until_cap(
        batch, cap_bytes=10, used_bytes=0, accounted=accounted, already_done={"done1"},
    )
    assert [r["file_id"] for r in taken] == ["small"]
    assert used == 9 and full is False


def test_select_threads_first_n_discovery_order_and_cap() -> None:
    threads = [{"thread_id": f"t{i}", "size_bytes": 10} for i in range(25)]
    got = select_threads_first_n(threads, max_threads=20, cap_bytes=10_000)
    assert [t["thread_id"] for t in got] == [f"t{i}" for i in range(20)]

    # Cap that forces skipping an oversized early thread, then fill later ones
    small = [
        {"thread_id": "big", "size_bytes": 900},
        {"thread_id": "a", "size_bytes": 100},
        {"thread_id": "b", "size_bytes": 100},
    ]
    got2 = select_threads_first_n(small, max_threads=20, cap_bytes=250)
    assert [t["thread_id"] for t in got2] == ["a", "b"]


if __name__ == "__main__":
    test_greedy_fill_backfills_around_oversized_item()
    test_greedy_fill_empty_and_exact_cap()
    test_add_to_thread_groups_and_sums_by_thread()
    test_select_top_by_recency_picks_newest_and_respects_limit()
    test_select_native_by_account_tops_up_when_floor_is_under_cap()
    test_select_native_by_account_floor_guarantee_is_never_trimmed()
    test_allocate_binary_by_account_prioritizes_bigger_account_when_granularity_allows()
    test_allocate_binary_by_account_empty()
    test_allocate_binary_by_account_guarantees_every_account_is_included()
    test_allocate_binary_by_account_never_exceeds_cap_single_account()
    test_account_has_enough_requires_both_native_and_binary_thresholds()
    test_group_by_account_supports_custom_key()
    test_allocate_equally_by_account_splits_evenly_ignoring_data_volume()
    test_allocate_equally_by_account_empty()
    test_allocate_equally_by_account_cannot_exceed_cap()
    test_drive_rows_all_files_keeps_zero_size_natives()
    test_take_files_as_is_until_cap_walk_order()
    test_take_files_as_is_counts_already_done_toward_cap()
    test_select_threads_first_n_discovery_order_and_cap()
    print("OK")
