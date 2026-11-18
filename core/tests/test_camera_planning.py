from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.camera_planning import (
    backdrop_score,
    exterior_opening_camera_positions,
    perimeter_camera_positions,
    project_bounds,
    promoted_camera_spec,
    rank_prompt_camera_renders,
    room_usable_bounds,
    score_camera_candidate,
    shell_side_coverage,
    confirm_prompt_camera_selection,
)


class CameraPlanningTests(unittest.TestCase):
    def test_room_bounds_and_perimeter_are_deterministic(self) -> None:
        bounds = room_usable_bounds({"room": {"size_m": [6.0, 4.0, 3.0]}})
        self.assertEqual(((-3.0, -2.0, 0.0), (3.0, 2.0, 3.0)), bounds)
        positions = perimeter_camera_positions(bounds)
        self.assertEqual(16, len(positions))
        self.assertEqual((-2.55, -1.55, 1.55), positions[0])
        self.assertEqual(2.15, positions[-1][2])

    def test_projection_reports_centered_object_visible(self) -> None:
        result = project_bounds(
            ((-0.5, -0.5, 0.0), (0.5, 0.5, 1.0)),
            (0.0, -5.0, 1.5),
            (0.0, 0.0, 0.7),
        )
        self.assertTrue(result["in_front"])
        self.assertGreater(result["frame_fraction"], 0.99)
        self.assertGreater(result["screen_area"], 0.0)

    def test_occluder_reduces_anchor_visibility(self) -> None:
        anchor = {
            "id": "anchor",
            "prim": "/World/Objects/Anchor",
            "bounds": ((-0.5, 0.5, 0.0), (0.5, 1.5, 1.5)),
            "weight": 3.0,
        }
        clear = score_camera_candidate((0.0, -4.0, 1.5), (0.0, 1.0, 0.8), [anchor])
        blocked = score_camera_candidate(
            (0.0, -4.0, 1.5),
            (0.0, 1.0, 0.8),
            [anchor],
            blockers=[{"prim": "/World/Architecture/Partition", "bounds": ((-1.0, -1.0, 0.0), (1.0, -0.8, 3.0))}],
        )
        self.assertGreater(clear["anchor_visibility_score"], blocked["anchor_visibility_score"])
        self.assertEqual(1, blocked["occluded_entity_count"])

    def test_edge_crop_reduces_camera_score(self) -> None:
        anchor = {
            "id": "anchor",
            "prim": "/World/Objects/Anchor",
            "bounds": ((-1.0, -0.4, 0.0), (1.0, 0.4, 1.8)),
            "weight": 3.0,
        }
        safe = score_camera_candidate((0.0, -6.0, 1.8), (0.0, 0.0, 0.9), [anchor])
        cropped = score_camera_candidate((0.0, -2.0, 1.4), (1.2, 0.0, 0.9), [anchor])
        self.assertGreater(safe["anchor_edge_safety_score"], cropped["anchor_edge_safety_score"])
        self.assertGreater(safe["composition_score"], cropped["composition_score"])

    def test_open_shell_backdrop_penalizes_wrong_interior_direction(self) -> None:
        coverage = shell_side_coverage(
            ((-3.0, -2.0, 0.0), (3.0, 2.0, 3.0)),
            [
                {"prim": "/World/Colliders/BackWall", "bounds": ((-3.0, 1.95, 0.0), (3.0, 2.05, 3.0))},
                {"prim": "/World/Colliders/LeftWall", "bounds": ((-3.05, -2.0, 0.0), (-2.95, 2.0, 3.0))},
            ],
        )
        self.assertGreater(coverage["y_max"], 0.95)
        self.assertEqual(0.0, coverage["y_min"])
        self.assertGreater(backdrop_score((2.5, -1.5, 1.5), (0.0, 0.5, 1.0), coverage), 0.8)
        self.assertLess(backdrop_score((-2.5, 1.5, 1.5), (0.5, -0.5, 1.0), coverage), 0.2)
        exterior = exterior_opening_camera_positions(((-3.0, -2.0, 0.0), (3.0, 2.0, 3.0)), coverage)
        self.assertTrue(any(row[0] > 3.0 and row[1] < -2.0 for row in exterior))

    def test_render_ranking_combines_geometry_and_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"candidates": [
                {"candidate_id": "C000", "render_filename": "C000.png", "composition_score": 0.70, "anchor_visibility_score": 0.80},
                {"candidate_id": "C001", "render_filename": "C001.png", "composition_score": 0.90, "anchor_visibility_score": 0.85},
            ]}), encoding="utf-8")
            renders = root / "renders"
            renders.mkdir()
            for name in ("C000.png", "C001.png"):
                (renders / name).write_bytes(b"test")

            def feedback(path: str, **_: object) -> dict:
                status = "pass" if path.endswith("C000.png") else "fail"
                return {"status": status, "metrics": {"mean_luminance": 0.32}, "failures": []}

            with patch("isaac_scene.render_feedback.analyze_image", side_effect=feedback):
                result = rank_prompt_camera_renders(manifest, renders, root / "ranking.json")
            self.assertEqual("C000", result["best"]["candidate_id"])
            self.assertEqual("pass", result["status"])

    def test_promotion_preserves_exposure_and_freezes_geometry(self) -> None:
        source = {
            "camera": {"exposure_ev": -0.7, "render_resolution": [1280, 720]},
            "entities": [{"id": "sofa", "translate": [1, 2, 0]}],
        }
        candidate = {
            "candidate_id": "C003",
            "source": "interior",
            "position": [2, -2, 1.55],
            "target": [0, 0, 1],
            "focal_length_mm": 24,
            "composition_score": 0.88,
            "anchor_visibility_score": 0.91,
            "combined_score": 0.90,
        }
        result = promoted_camera_spec(source, candidate)
        self.assertEqual(-0.7, result["camera"]["exposure_ev"])
        self.assertEqual([1, 2, 0], result["entities"][0]["translate"])
        self.assertTrue(result["camera_selection"]["geometry_frozen"])

    def test_ranking_retains_stronger_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"candidates": [
                {
                    "candidate_id": "challenger", "source": "interior", "render_filename": "challenger.png",
                    "entity_visibility_score": 0.82, "anchor_visibility_score": 0.76, "center_score": 0.9,
                    "fill_score": 0.8, "occlusion_score": 0.9, "dominance_score": 0.9,
                    "shell_backdrop_score": 0.7,
                },
                {
                    "candidate_id": "baseline", "source": "baseline", "render_filename": "baseline.png",
                    "entity_visibility_score": 0.88, "anchor_visibility_score": 0.85, "center_score": 0.9,
                    "fill_score": 0.8, "occlusion_score": 0.9, "dominance_score": 0.9,
                    "shell_backdrop_score": 0.9,
                },
            ]}), encoding="utf-8")
            renders = root / "renders"
            renders.mkdir()
            for name in ("challenger.png", "baseline.png"):
                (renders / name).write_bytes(b"test")
            with patch("isaac_scene.render_feedback.analyze_image", return_value={
                "status": "pass", "metrics": {"mean_luminance": 0.32}, "failures": [],
            }):
                result = rank_prompt_camera_renders(manifest, renders, root / "ranking.json")
            self.assertEqual("baseline", result["best"]["candidate_id"])
            self.assertEqual("retain_baseline", result["acceptance"]["decision"])

    def test_validated_agent_anchor_evidence_can_override_conservative_broad_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"candidates": [
                {
                    "candidate_id": "challenger", "source": "exterior_opening", "render_filename": "challenger.png",
                    "entity_visibility_score": 0.70, "anchor_visibility_score": 0.55, "anchor_edge_safety_score": 1.0,
                    "center_score": 0.9, "fill_score": 0.8, "occlusion_score": 0.5,
                    "dominance_score": 0.9, "shell_backdrop_score": 1.0,
                },
                {
                    "candidate_id": "baseline", "source": "baseline", "render_filename": "baseline.png",
                    "entity_visibility_score": 0.60, "anchor_visibility_score": 0.55, "anchor_edge_safety_score": 0.8,
                    "center_score": 0.7, "fill_score": 0.6, "occlusion_score": 0.5,
                    "dominance_score": 0.8, "shell_backdrop_score": 1.0,
                },
            ]}), encoding="utf-8")
            renders = root / "renders"
            renders.mkdir()
            for name in ("challenger.png", "baseline.png"):
                (renders / name).write_bytes(b"test")
            packet = root / "packet.json"
            packet.write_text(json.dumps({
                "candidates": [{"candidate_id": "challenger"}, {"candidate_id": "baseline"}],
            }), encoding="utf-8")
            review = root / "review.json"
            review.write_text(json.dumps({
                "schema_version": "isaac-scene-visual-agent-review/v1",
                "verdict": "accept",
                "preferred_candidate_id": "challenger",
                "candidate_reviews": [{
                    "candidate_id": "challenger", "visual_score": 0.95, "prompt_coverage_score": 0.9,
                    "composition_score": 0.9, "realism_score": 0.8, "recognizable_anchor_fraction": 0.9,
                }],
            }), encoding="utf-8")
            with patch("isaac_scene.render_feedback.analyze_image", return_value={
                "status": "pass", "metrics": {"mean_luminance": 0.32}, "failures": [],
            }):
                result = rank_prompt_camera_renders(
                    manifest, renders, root / "ranking.json",
                    visual_review_packet_path=packet, visual_agent_review_path=review,
                )
            self.assertEqual("challenger", result["best"]["candidate_id"])
            self.assertTrue(result["acceptance"]["agent_anchor_gate"])
            self.assertEqual("accept_challenger", result["acceptance"]["decision"])

    def test_validated_agent_can_accept_absolute_strong_backdrop_over_closeup_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"candidates": [
                {
                    "candidate_id": "wide", "source": "exterior_opening", "render_filename": "wide.png",
                    "entity_visibility_score": 0.78, "anchor_visibility_score": 0.61,
                    "anchor_edge_safety_score": 0.9, "center_score": 0.9, "fill_score": 0.8,
                    "occlusion_score": 0.65, "dominance_score": 0.9, "shell_backdrop_score": 0.84,
                },
                {
                    "candidate_id": "closeup", "source": "baseline", "render_filename": "closeup.png",
                    "entity_visibility_score": 0.35, "anchor_visibility_score": 0.37,
                    "anchor_edge_safety_score": 0.6, "center_score": 0.7, "fill_score": 0.6,
                    "occlusion_score": 0.76, "dominance_score": 0.6, "shell_backdrop_score": 1.0,
                },
            ]}), encoding="utf-8")
            renders = root / "renders"
            renders.mkdir()
            for name in ("wide.png", "closeup.png"):
                (renders / name).write_bytes(b"test")
            packet = root / "packet.json"
            packet.write_text(json.dumps({
                "candidates": [{"candidate_id": "wide"}, {"candidate_id": "closeup"}],
            }), encoding="utf-8")
            review = root / "review.json"
            review.write_text(json.dumps({
                "schema_version": "isaac-scene-visual-agent-review/v1",
                "verdict": "accept",
                "preferred_candidate_id": "wide",
                "candidate_reviews": [{
                    "candidate_id": "wide", "visual_score": 0.80, "prompt_coverage_score": 0.80,
                    "composition_score": 0.85, "realism_score": 0.64,
                    "recognizable_anchor_fraction": 0.81,
                }],
            }), encoding="utf-8")
            with patch("isaac_scene.render_feedback.analyze_image", return_value={
                "status": "pass", "metrics": {"mean_luminance": 0.32}, "failures": [],
            }):
                result = rank_prompt_camera_renders(
                    manifest, renders, root / "ranking.json",
                    visual_review_packet_path=packet, visual_agent_review_path=review,
                )
            self.assertTrue(result["acceptance"]["agent_anchor_gate"])
            self.assertTrue(result["acceptance"]["backdrop_gate"])
            self.assertEqual("wide", result["best"]["candidate_id"])
            self.assertEqual("accept_challenger", result["acceptance"]["decision"])

    def test_ranking_excludes_baseline_rejected_by_camera_clearance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"candidates": [
                {
                    "candidate_id": "challenger", "source": "interior", "status": "pass",
                    "render_filename": "challenger.png", "entity_visibility_score": 0.86,
                    "anchor_visibility_score": 0.82, "center_score": 0.9, "fill_score": 0.8,
                    "occlusion_score": 0.9, "dominance_score": 0.9, "shell_backdrop_score": 0.9,
                },
                {
                    "candidate_id": "baseline", "source": "baseline", "status": "reject",
                    "reason": "camera_inside_object_clearance", "render_filename": "baseline.png",
                    "composition_score": 0.0, "anchor_visibility_score": 0.0,
                },
            ]}), encoding="utf-8")
            renders = root / "renders"
            renders.mkdir()
            for name in ("challenger.png", "baseline.png"):
                (renders / name).write_bytes(b"test")
            with patch("isaac_scene.render_feedback.analyze_image", return_value={
                "status": "pass", "metrics": {"mean_luminance": 0.32}, "failures": [],
            }):
                result = rank_prompt_camera_renders(manifest, renders, root / "ranking.json")
            self.assertEqual("challenger", result["best"]["candidate_id"])
            self.assertEqual("accept_challenger", result["acceptance"]["decision"])
            self.assertIsNone(result["baseline"])
            self.assertEqual(1, result["rejected_candidate_count"])

    def test_clear_challenger_can_trade_some_backdrop_for_major_occlusion_gain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"candidates": [
                {
                    "candidate_id": "clear", "source": "interior", "render_filename": "clear.png",
                    "entity_visibility_score": 0.86, "anchor_visibility_score": 0.82, "center_score": 0.9,
                    "fill_score": 0.8, "occlusion_score": 0.80, "dominance_score": 0.9,
                    "shell_backdrop_score": 0.55,
                },
                {
                    "candidate_id": "blocked", "source": "baseline", "render_filename": "blocked.png",
                    "entity_visibility_score": 0.36, "anchor_visibility_score": 0.35, "center_score": 0.75,
                    "fill_score": 0.55, "occlusion_score": 0.17, "dominance_score": 0.9,
                    "shell_backdrop_score": 1.0,
                },
            ]}), encoding="utf-8")
            renders = root / "renders"
            renders.mkdir()
            for name in ("clear.png", "blocked.png"):
                (renders / name).write_bytes(b"test")
            with patch("isaac_scene.render_feedback.analyze_image", return_value={
                "status": "pass", "metrics": {"mean_luminance": 0.32}, "failures": [],
            }):
                result = rank_prompt_camera_renders(manifest, renders, root / "ranking.json")
            self.assertEqual("clear", result["best"]["candidate_id"])
            self.assertTrue(result["acceptance"]["backdrop_gate"])

    def test_confirmation_requires_fresh_pass_and_matching_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec, scene = root / "spec.json", root / "scene.usda"
            spec.write_text("{}", encoding="utf-8")
            scene.write_text("#usda 1.0", encoding="utf-8")
            import hashlib
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": "isaac-scene-prompt-camera-candidates/v2",
                "spec_path": str(spec), "scene_path": str(scene),
                "input_fingerprints": {
                    "spec_sha256": hashlib.sha256(spec.read_bytes()).hexdigest(),
                    "scene_sha256": hashlib.sha256(scene.read_bytes()).hexdigest(),
                },
            }), encoding="utf-8")
            ranking = root / "ranking.json"
            ranking.write_text(json.dumps({
                "schema_version": "isaac-scene-prompt-camera-ranking/v2",
                "best": {"candidate_id": "h_good", "source": "exterior_opening"},
                "baseline": {"candidate_id": "baseline", "source": "baseline"},
                "acceptance": {"accepted": True},
            }), encoding="utf-8")
            for name in ("baseline.png", "selected.png"):
                (root / name).write_bytes(b"image")
            feedback = {"status": "pass", "metrics": {"mean_luminance": 0.3}, "failures": []}
            with patch("isaac_scene.render_feedback.analyze_image", return_value=feedback):
                result = confirm_prompt_camera_selection(
                    manifest, ranking, root / "baseline.png", root / "selected.png", root / "confirmation.json",
                )
            self.assertTrue(result["promotion_allowed"])
            self.assertEqual("accept_challenger", result["decision"])

    def test_confirmation_can_replace_a_failed_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec, scene = root / "spec.json", root / "scene.usda"
            spec.write_text("{}", encoding="utf-8")
            scene.write_text("#usda 1.0", encoding="utf-8")
            import hashlib
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": "isaac-scene-prompt-camera-candidates/v2",
                "spec_path": str(spec), "scene_path": str(scene),
                "input_fingerprints": {
                    "spec_sha256": hashlib.sha256(spec.read_bytes()).hexdigest(),
                    "scene_sha256": hashlib.sha256(scene.read_bytes()).hexdigest(),
                },
            }), encoding="utf-8")
            ranking = root / "ranking.json"
            ranking.write_text(json.dumps({
                "schema_version": "isaac-scene-prompt-camera-ranking/v2",
                "best": {"candidate_id": "h_good", "source": "exterior_opening"},
                "baseline": {"candidate_id": "baseline", "source": "baseline"},
                "acceptance": {"accepted": True},
            }), encoding="utf-8")
            for name in ("baseline.png", "selected.png"):
                (root / name).write_bytes(b"image")
            feedback = [
                {"status": "fail", "metrics": {"mean_luminance": 0.1}, "failures": [{"code": "UNDEREXPOSED_RENDER"}]},
                {"status": "pass", "metrics": {"mean_luminance": 0.3}, "failures": []},
            ]
            with patch("isaac_scene.render_feedback.analyze_image", side_effect=feedback):
                result = confirm_prompt_camera_selection(
                    manifest, ranking, root / "baseline.png", root / "selected.png", root / "confirmation.json",
                )
            self.assertTrue(result["promotion_allowed"])
            self.assertFalse(result["mandatory_gates"]["baseline_rgb_feedback_pass"])
            self.assertTrue(result["mandatory_gates"]["baseline_policy_gate"])


if __name__ == "__main__":
    unittest.main()
