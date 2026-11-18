from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from compose_scene_validation_summary import compose_summary  # noqa: E402
from isaac_scene.validation import load_validation_manifest  # noqa: E402


class SceneValidationSummaryTests(unittest.TestCase):
    def test_realistic_summary_requires_all_reports_provenance_and_no_proxy(self) -> None:
        passed = {"status": "pass", "errors": []}
        summary = compose_summary(
            scene_spec={
                "entities": [{
                    "id": "chair",
                    "role": "chair",
                    "path": "/assets/chair.usd",
                    "logical_id": "catalog:chair",
                    "canonical_class": "furniture/seating/chair",
                }]
            },
            compile_report={"unresolved_count": 0},
            validate_report=passed,
            overlap_report=passed,
            support_report=passed,
            facing_report=passed,
            render_feedback_report=passed,
            quality_policy="realistic",
        )
        self.assertEqual("pass", summary["status"])
        self.assertTrue(summary["gates"]["asset_provenance_pass"])

        failed = compose_summary(
            scene_spec={
                "entities": [{
                    "id": "chair_proxy",
                    "role": "chair",
                    "source": "primitive_proxy",
                }]
            },
            compile_report={"unresolved_count": 0},
            validate_report=passed,
            overlap_report=passed,
            support_report=passed,
            facing_report=passed,
            render_feedback_report=passed,
            quality_policy="reference_match",
        )
        self.assertEqual("fail", failed["status"])
        self.assertFalse(failed["gates"]["proxy_free_pass"])
        self.assertFalse(failed["gates"]["asset_provenance_pass"])

    def test_missing_overlap_evidence_cannot_be_hidden_by_general_validation(self) -> None:
        passed = {"status": "pass", "errors": []}
        summary = compose_summary(
            scene_spec={"entities": []},
            compile_report={"unresolved_count": 0},
            validate_report=passed,
            overlap_report={"status": "pending", "errors": []},
            support_report=passed,
            facing_report=passed,
            render_feedback_report=passed,
            quality_policy="layout_only",
        )
        self.assertEqual("fail", summary["status"])
        self.assertFalse(summary["overlap_validation_pass"])

    def test_runtime_mdl_only_warning_is_accepted_after_dependency_resolution(self) -> None:
        passed = {"status": "pass", "errors": []}
        runtime_mdl = {
            "status": "warn",
            "errors": [],
            "warnings": [{"code": "RUNTIME_MDL_RESOLUTION_REQUIRED"}],
            "unresolved_dependency_count": 0,
        }
        summary = compose_summary(
            scene_spec={"entities": []},
            compile_report={"unresolved_count": 0},
            validate_report=runtime_mdl,
            overlap_report=passed,
            support_report=passed,
            facing_report=passed,
            render_feedback_report=passed,
            quality_policy="layout_only",
        )
        self.assertEqual("pass", summary["status"])
        self.assertTrue(summary["validation_pass"])

    def test_direct_scene_spec_compile_success_has_no_unresolved_observations(self) -> None:
        passed = {"status": "pass", "errors": []}
        summary = compose_summary(
            scene_spec={"entities": []},
            compile_report={"status": "ok", "scene_path": "/tmp/scene.usda"},
            validate_report=passed,
            overlap_report=passed,
            support_report=passed,
            facing_report=passed,
            render_feedback_report=passed,
            quality_policy="layout_only",
        )
        self.assertEqual("pass", summary["status"])
        self.assertEqual(0, summary["unresolved_count"])

    def test_multiple_support_reports_cannot_hide_one_failed_mesh_gate(self) -> None:
        passed = {"status": "pass", "errors": []}
        failed_support = {
            "status": "fail",
            "errors": [{"kind": "support_mesh_miss"}],
        }
        summary = compose_summary(
            scene_spec={"entities": []},
            compile_report={"unresolved_count": 0},
            validate_report=passed,
            overlap_report=passed,
            support_reports=[passed, failed_support],
            facing_report=passed,
            render_feedback_report=passed,
            quality_policy="layout_only",
        )
        self.assertEqual("fail", summary["status"])
        self.assertFalse(summary["support_validation_pass"])
        self.assertEqual(2, summary["report_counts"]["support"])

    def test_validation_manifest_resolves_relative_evidence_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": "isaac-scene-validation-manifest/v1",
                "quality_policy": "realistic",
                "scene_spec": "spec.json",
                "compile_report": "compile.json",
                "validate_scene_reports": ["validate.json"],
                "overlap_reports": ["overlap.json"],
                "support_reports": ["support-a.json", "support-b.json"],
                "facing_reports": ["facing.json"],
                "render_feedback_reports": ["render.json"],
                "output": "summary.json"
            }), encoding="utf-8")
            loaded = load_validation_manifest(manifest)
            self.assertEqual((root / "spec.json").resolve(), loaded["scene_spec"])
            self.assertEqual(
                [(root / "support-a.json").resolve(), (root / "support-b.json").resolve()],
                loaded["support_reports"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
