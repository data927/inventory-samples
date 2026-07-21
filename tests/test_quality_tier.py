"""Tests for ``extractors.quality_tier``."""

from __future__ import annotations

import unittest

from extractors.quality_tier import infer_quality_tier


class TestQualityTier(unittest.TestCase):
    def test_pii_does_not_downgrade_tier(self) -> None:
        """PII is ignored; content may be redacted before training."""
        self.assertEqual(
            infer_quality_tier(
                token_count=50_000,
                word_count=40_000,
                pii_flag="Yes",
                modality="text",
                content_type="Instructional",
                extension="md",
                confidence="high",
            ),
            "High",
        )

    def test_tiny_extract_low(self) -> None:
        self.assertEqual(
            infer_quality_tier(
                token_count=10,
                word_count=5,
                pii_flag="No",
                modality="text",
                content_type="Factual",
                extension="txt",
                confidence="high",
            ),
            "Low",
        )

    def test_high_tokens_and_strong_content(self) -> None:
        self.assertEqual(
            infer_quality_tier(
                token_count=12_000,
                word_count=9_000,
                pii_flag="No",
                modality="text",
                content_type="Decision-log; Factual",
                extension="pdf",
                confidence="high",
            ),
            "High",
        )

    def test_low_confidence_downgrades_high(self) -> None:
        self.assertEqual(
            infer_quality_tier(
                token_count=12_000,
                word_count=9_000,
                pii_flag="No",
                modality="text",
                content_type="Narrative",
                extension="pdf",
                confidence="low",
            ),
            "Medium",
        )

    def test_medium_band(self) -> None:
        self.assertEqual(
            infer_quality_tier(
                token_count=2_000,
                word_count=1_800,
                pii_flag="No",
                modality="text",
                content_type="Factual",
                extension="pdf",
                confidence="medium",
            ),
            "Medium",
        )


if __name__ == "__main__":
    unittest.main()
