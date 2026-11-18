from __future__ import annotations

import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from evaluate_synthesis27_regression import contract_coverage  # noqa: E402


class Synthesis27RegressionEvaluationTests(unittest.TestCase):
    def test_coverage_requires_explicit_source_and_semantic_provenance(self) -> None:
        case = {
            "oracle": {
                "exact_asset_targets": [
                    {
                        "model_id": "table-id",
                        "logical_id": "synthesis27:table-id",
                    },
                    {
                        "model_id": "chair-id",
                        "logical_id": "synthesis27:chair-id",
                    },
                ],
                "anchor_targets": [
                    {
                        "model_id": "table-id",
                        "canonical_class": "furniture/surface/table",
                        "count": 1,
                        "match_policy": "exact_source_asset",
                    },
                    {
                        "model_id": "chair-id",
                        "canonical_class": "furniture/seating/chair",
                        "count": 2,
                        "match_policy": "exact_source_asset",
                    },
                ],
            }
        }
        spec = {
            "entities": [
                {
                    "logical_id": "synthesis27:table-id",
                    "canonical_class": "furniture/surface/table",
                },
                {
                    "asset": {
                        "model_id": "chair-id",
                        "canonical_class": "furniture/seating/chair",
                    }
                },
                {"id": "unproven-chair", "role": "chair", "path": "/assets/chair.usd"},
            ]
        }
        coverage = contract_coverage(case, spec)
        self.assertEqual(2 / 3, coverage["licensed_exact_anchor_coverage"])
        self.assertEqual(2 / 3, coverage["semantic_anchor_coverage"])
        self.assertEqual(
            1, coverage["entity_missing_model_and_class_provenance_count"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
