"""Self-check for tools/build_quality_sample.py's selection logic. No live API calls.

Run: python tools/test_build_quality_sample.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.build_quality_sample import GB, add_to_thread, greedy_fill, select_top_by_recency


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


if __name__ == "__main__":
    test_greedy_fill_backfills_around_oversized_item()
    test_greedy_fill_empty_and_exact_cap()
    test_add_to_thread_groups_and_sums_by_thread()
    test_select_top_by_recency_picks_newest_and_respects_limit()
    print("OK")
