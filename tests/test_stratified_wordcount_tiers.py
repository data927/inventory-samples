"""Bounds for word-count tiers used by ``tools/build_stratified_wordcount_workbook.py``."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def _load_build_script():
    root = Path(__file__).resolve().parents[1]
    path = root / "tools" / "build_stratified_wordcount_workbook.py"
    spec = importlib.util.spec_from_file_location("strat_wc", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestWordCountTiers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_build_script()

    def test_partition_coverage(self) -> None:
        tf = self.mod.tier_for_word_count
        self.assertEqual(tf(0), "lt100")
        self.assertEqual(tf(99), "lt100")
        self.assertEqual(tf(100), "100_499")
        self.assertEqual(tf(499), "100_499")
        self.assertEqual(tf(500), "500_1499")
        self.assertEqual(tf(1499), "500_1499")
        self.assertEqual(tf(1500), "1500_2499")
        self.assertEqual(tf(2499), "1500_2499")
        self.assertEqual(tf(2500), "ge2500")

    def test_quotas_sum_to_50(self) -> None:
        self.assertEqual(sum(self.mod.QUOTAS.values()), 50)


if __name__ == "__main__":
    unittest.main()
