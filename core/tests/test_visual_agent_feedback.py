from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.visual_agent_feedback import (
    create_quality_review_packet,
    create_visual_review_packet,
    validate_quality_review,
    validate_visual_agent_review,
)


class VisualAgentFeedbackTests(unittest.TestCase):
    def test_quality_axes_are_separate_and_fixed_camera_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "comparison.json"
            manifest.write_text(json.dumps({
                "schema_version": "isaac-scene-render-comparison/v1",
                "scene_id": "factory",
                "prompt": "factory prompt",
                "scene_path": "/tmp/factory.usda",
                "candidates": [
                    {"candidate_id": "fast", "scene_sha256": "abc", "camera_path": "/World/Camera",
                     "render_path": "/tmp/fast.png", "render_profile": "review", "renderer": "RayTracedLighting"},
                    {"candidate_id": "pt", "scene_sha256": "abc", "camera_path": "/World/Camera",
                     "render_path": "/tmp/pt.png", "render_profile": "final", "renderer": "PathTracing"},
                ],
            }), encoding="utf-8")
            packet = create_quality_review_packet(manifest, "rendering_quality", root / "packet.json")
            self.assertNotIn("asset_richness", packet["review_contract"]["required_scores"])
            review = root / "review.json"
            scores = {key: 0.8 for key in packet["review_contract"]["required_scores"]}
            review.write_text(json.dumps({
                "schema_version": "isaac-scene-quality-agent-review/v1",
                "review_axis": "rendering_quality",
                "verdict": "revise",
                "candidate_reviews": [
                    {"candidate_id": candidate_id, "scores": scores, "notes": "independent render review"}
                    for candidate_id in ("fast", "pt")
                ],
                "issues": [{"primary_cause": "rendering", "suggested_constrained_patch": "increase convergence"}],
            }), encoding="utf-8")
            self.assertEqual("pass", validate_quality_review(root / "packet.json", review)["status"])

            content_packet = create_quality_review_packet(manifest, "scene_content_quality", root / "content.json")
            self.assertIn("asset_richness", content_packet["review_contract"]["required_scores"])
            self.assertNotIn("noise_and_aliasing", content_packet["review_contract"]["required_scores"])

    def test_packet_and_review_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = root / "spec.json"
            manifest = root / "manifest.json"
            ranking = root / "ranking.json"
            spec.write_text(json.dumps({"scene_id": "office", "entities": [{
                "id": "desk", "prim": "/World/Objects/Desk", "criticality": "anchor",
            }]}), encoding="utf-8")
            manifest.write_text(json.dumps({"candidates": [
                {"candidate_id": "a", "render_filename": "a.png"},
                {"candidate_id": "baseline", "render_filename": "baseline.png", "source": "baseline"},
            ]}), encoding="utf-8")
            ranking.write_text(json.dumps({"top_candidates": [
                {"candidate_id": "a", "combined_score": 0.8},
                {"candidate_id": "baseline", "combined_score": 0.7},
            ]}), encoding="utf-8")
            packet = create_visual_review_packet(spec, manifest, ranking, root, root / "packet.json")
            self.assertEqual(2, len(packet["candidates"]))
            review = root / "review.json"
            review.write_text(json.dumps({
                "schema_version": "isaac-scene-visual-agent-review/v1",
                "verdict": "accept",
                "preferred_candidate_id": "a",
                "candidate_reviews": [
                    {"candidate_id": candidate_id, "visual_score": score, "prompt_coverage_score": score,
                     "composition_score": score, "realism_score": score, "recognizable_anchor_fraction": score}
                    for candidate_id, score in (("a", 0.9), ("baseline", 0.7))
                ],
                "issues": [{"category": "camera", "severity": "low", "suggested_constrained_patch": "none"}],
            }), encoding="utf-8")
            result = validate_visual_agent_review(root / "packet.json", review)
            self.assertEqual("pass", result["status"])

    def test_review_rejects_executable_patch_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "packet.json").write_text(json.dumps({"candidates": [{"candidate_id": "a"}]}), encoding="utf-8")
            (root / "review.json").write_text(json.dumps({
                "schema_version": "isaac-scene-visual-agent-review/v1", "verdict": "accept",
                "preferred_candidate_id": "a",
                "candidate_reviews": [{"candidate_id": "a", **{key: 0.8 for key in (
                    "visual_score", "prompt_coverage_score", "composition_score", "realism_score", "recognizable_anchor_fraction")}}],
                "issues": [{"category": "camera", "severity": "high", "suggested_constrained_patch": {"translate": [1, 2, 3]}}],
            }), encoding="utf-8")
            result = validate_visual_agent_review(root / "packet.json", root / "review.json")
            self.assertEqual("fail", result["status"])


if __name__ == "__main__":
    unittest.main()
