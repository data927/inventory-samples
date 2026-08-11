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
    greedy_fill,
    select_native_by_account,
    select_top_by_recency,
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
    print("OK")
