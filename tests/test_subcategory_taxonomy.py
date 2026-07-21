"""Sanity checks for sub-category lists per bucket."""

from __future__ import annotations

import unittest

from subcategory_taxonomy import SUBCATEGORIES_BY_BUCKET, labels_for_bucket


class TestSubcategoryTaxonomy(unittest.TestCase):
    def test_buckets_one_through_seven_end_with_other_general(self) -> None:
        for b in range(1, 8):
            labs = labels_for_bucket(b)
            self.assertTrue(labs, f"bucket {b} should have labels")
            self.assertEqual(
                labs[-1],
                "Other / General",
                f"bucket {b} last label must be Other / General",
            )

    def test_expected_label_counts(self) -> None:
        expected = {1: 24, 2: 14, 3: 13, 4: 15, 5: 14, 6: 15, 7: 13}
        for b, n in expected.items():
            self.assertEqual(len(SUBCATEGORIES_BY_BUCKET[b]), n, f"bucket {b} label count")

    def test_invalid_bucket_returns_empty(self) -> None:
        self.assertEqual(labels_for_bucket(0), ())
        self.assertEqual(labels_for_bucket(99), ())
        self.assertEqual(labels_for_bucket(None), ())


if __name__ == "__main__":
    unittest.main()
