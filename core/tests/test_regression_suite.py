from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.regression_suite import audit_suite


class PromptRegressionSuiteTests(unittest.TestCase):
    def _case(self, root: Path, case_id: str, *, realism: float, gaps: int = 0) -> dict:
        case_root = root / case_id
        case_root.mkdir()
        (case_root / "spec.json").write_text(json.dumps({
            "brief_snapshot": {"inputs": {"text": f"build {case_id}"}},
            "entities": [{"id": "anchor", "source_library": "verified"}],
            "asset_gaps": [{"id": f"gap-{index}"} for index in range(gaps)],
        }), encoding="utf-8")
        (case_root / "render.png").write_bytes(b"pixels")
        (case_root / "review.json").write_text(json.dumps({
            "preferred_candidate_id": "best",
            "candidate_reviews": [{
                "candidate_id": "best", "prompt_coverage_score": 0.95,
                "realism_score": realism, "composition_score": 0.9,
                "recognizable_anchor_fraction": 0.95,
            }],
            "issues": [],
        }), encoding="utf-8")
        (case_root / "summary.json").write_text(json.dumps({
            "status": "pass", "placement": {"quarantined": 0, "errors": 0},
            "overlaps": "pass", "camera": {"fresh_render_confirmation": "pass"},
        }), encoding="utf-8")
        return {
            "id": case_id, "spec": f"{case_id}/spec.json", "final_render": f"{case_id}/render.png",
            "visual_review": f"{case_id}/review.json", "reports": {"summary": f"{case_id}/summary.json"},
        }

    def test_suite_blocks_expansion_and_ranks_low_realism_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = [self._case(root, "strong", realism=0.72), self._case(root, "weak", realism=0.42)]
            manifest = root / "suite.json"
            manifest.write_text(json.dumps({
                "schema_version": "isaac-scene-prompt-regression-suite/v1",
                "suite_id": "test", "thresholds": {"minimum_case_count": 2}, "cases": cases,
            }), encoding="utf-8")
            result = audit_suite(manifest, root / "audit.json")
            self.assertFalse(result["expansion_ready"])
            self.assertEqual("weak", result["repair_queue"][0]["case_id"])
            self.assertIn("realism_below_threshold", result["cases"][1]["expansion_blockers"])

    def test_suite_allows_ready_holdout_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = [self._case(root, "a", realism=0.7), self._case(root, "b", realism=0.68)]
            manifest = root / "suite.json"
            manifest.write_text(json.dumps({
                "schema_version": "isaac-scene-prompt-regression-suite/v1",
                "thresholds": {"minimum_case_count": 2}, "cases": cases,
            }), encoding="utf-8")
            result = audit_suite(manifest, root / "audit.json")
            self.assertTrue(result["expansion_ready"])
            self.assertEqual("expand_with_holdout_cases", result["recommendation"])


if __name__ == "__main__":
    unittest.main()
