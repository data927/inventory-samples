"""Tests for ``extractors.content_type``."""

from __future__ import annotations

import unittest

from extractors.content_type import infer_content_types


class TestContentType(unittest.TestCase):
    def test_defaults_to_factual(self) -> None:
        self.assertEqual(infer_content_types(), ["Factual"])

    def test_rationale_skews_decision_log(self) -> None:
        self.assertEqual(
            infer_content_types(
                rationale="Incident postmortem and RCA",
                content_summary="",
                snippet="",
            ),
            ["Decision-log"],
        )

    def test_multi_label(self) -> None:
        out = infer_content_types(
            rationale="README onboarding tutorial",
            snippet="metrics dashboard specification",
        )
        self.assertIn("Instructional", out)
        self.assertIn("Factual", out)

    def test_instructional_from_snippet_only(self) -> None:
        self.assertIn(
            "Instructional",
            infer_content_types(
                rationale="",
                snippet="# How to deploy\nstep-by-step guide",
            ),
        )


if __name__ == "__main__":
    unittest.main()
