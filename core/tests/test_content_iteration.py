from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.content_iteration import (
    CONTENT_SCORE_FIELDS,
    create_content_iteration_packet,
    create_scene_revision_request,
    validate_content_iteration_review,
)


class ContentIterationTests(unittest.TestCase):
    def _inputs(self, root: Path) -> tuple[Path, Path, Path]:
        brief = root / "brief.json"
        spec = root / "spec.json"
        renders = root / "renders.json"
        brief.write_text(json.dumps({
            "schema_version": "isaac-scene-brief/v1",
            "inputs": {"mode": "text", "text": "a coherent workshop", "reference_images": []},
            "domain": "industrial_assembly",
            "room": {"type": "workshop", "size_m": [8, 6, 4]},
            "objects": [{"id": "robot", "role": "robot", "semantic_class": "robot/industrial"}],
        }), encoding="utf-8")
        spec.write_text(json.dumps({
            "schema_version": "isaac-scene-spec/v2",
            "scene_id": "workshop",
            "domain": "industrial_assembly",
            "entities": [{
                "id": "robot", "role": "robot", "canonical_class": "robot/industrial",
                "criticality": "anchor", "translate": [1, 2, 0], "rotate": [0, 0, 90],
            }],
            "relations": [{"type": "faces", "subject": "robot", "target": "conveyor", "required": True}],
        }), encoding="utf-8")
        renders.write_text(json.dumps({
            "schema_version": "isaac-scene-content-render-manifest/v1",
            "renders": [
                {"view_id": "perspective", "render_path": "/tmp/perspective.png", "camera_path": "/World/CameraA"},
                {"view_id": "top_down", "render_path": "/tmp/top.png", "camera_path": "/World/CameraB", "view_kind": "top_down"},
            ],
        }), encoding="utf-8")
        return brief, spec, renders

    def _review(self, packet: dict, *, verdict: str, score: float, issues: list[dict] | None = None) -> dict:
        return {
            "schema_version": "isaac-scene-content-agent-review/v1",
            "scene_id": packet["scene_id"],
            "iteration": packet["iteration"],
            "phase": packet["phase"],
            "verdict": verdict,
            "scores": {key: score for key in CONTENT_SCORE_FIELDS},
            "issues": issues or [],
        }

    def test_packet_is_pose_free_and_uses_generic_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief, spec, renders = self._inputs(root)
            packet = create_content_iteration_packet(brief, spec, renders, root / "packet.json")
            self.assertNotIn("translate", packet["required_entities"][0])
            self.assertNotIn("rotate", packet["required_entities"][0])
            invariants = packet["review_contract"]["generic_hard_invariants"]
            self.assertIn("verified_asset_identity", invariants)
            self.assertIn("content_review_acceptance_before_final_render", invariants)
            self.assertIn("object count alone is not richness", packet["review_contract"]["density_policy"])

    def test_revise_creates_complete_spec_request_not_pose_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief, spec, renders = self._inputs(root)
            packet = create_content_iteration_packet(brief, spec, renders, root / "packet.json", phase="functional")
            review_path = root / "review.json"
            review_path.write_text(json.dumps(self._review(packet, verdict="revise", score=0.6, issues=[{
                "id": "broken_chain",
                "category": "functional_relation",
                "severity": "high",
                "affected_entity_ids": ["robot"],
                "evidence_view_ids": ["perspective", "top_down"],
                "diagnosis": "the declared workcell relation is visually incoherent",
                "repair_action": {
                    "type": "rebuild_functional_group",
                    "instruction": "rebuild the robot and conveyor group from its declared relation",
                },
            }])), encoding="utf-8")
            validation = validate_content_iteration_review(root / "packet.json", review_path, root / "validation.json")
            self.assertEqual("pass", validation["status"])
            self.assertEqual("pending", validation["content_gate"])
            request = create_scene_revision_request(root / "packet.json", root / "validation.json", root / "request.json")
            self.assertEqual("regenerate_complete_spec", request["action"])
            self.assertEqual(
                "complete_scene_brief_and_complete_scene_spec_not_delta",
                request["regeneration_contract"]["output_mode"],
            )
            self.assertIn("translate", request["regeneration_contract"]["forbidden_fields_in_agent_output"])

    def test_accept_advances_phase_and_only_detail_accept_allows_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief, spec, renders = self._inputs(root)
            for phase, expected_action, final_allowed in (
                ("anchors", "advance_phase", False),
                ("detail", "freeze_content", True),
            ):
                packet_path = root / f"packet_{phase}.json"
                review_path = root / f"review_{phase}.json"
                validation_path = root / f"validation_{phase}.json"
                packet = create_content_iteration_packet(brief, spec, renders, packet_path, phase=phase)
                review_path.write_text(json.dumps(self._review(packet, verdict="accept", score=0.9)), encoding="utf-8")
                validation = validate_content_iteration_review(packet_path, review_path, validation_path)
                self.assertEqual("pass", validation["content_gate"])
                request = create_scene_revision_request(packet_path, validation_path, root / f"request_{phase}.json")
                self.assertEqual(expected_action, request["action"])
                self.assertEqual(final_allowed, request["final_render_allowed"])

    def test_review_cannot_smuggle_object_transform(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief, spec, renders = self._inputs(root)
            packet = create_content_iteration_packet(brief, spec, renders, root / "packet.json")
            review = self._review(packet, verdict="revise", score=0.7, issues=[{
                "id": "move_robot",
                "category": "composition",
                "severity": "medium",
                "evidence_view_ids": ["perspective"],
                "diagnosis": "robot is visually isolated",
                "repair_action": {
                    "type": "preserve_and_recompose",
                    "instruction": "recompose the functional group",
                    "translate": [1, 2, 3],
                },
            }])
            (root / "review.json").write_text(json.dumps(review), encoding="utf-8")
            result = validate_content_iteration_review(root / "packet.json", root / "review.json")
            self.assertEqual("fail", result["status"])
            self.assertIn("CONTENT_REVIEW_EXECUTABLE_PATCH_FORBIDDEN", {row["code"] for row in result["errors"]})


if __name__ == "__main__":
    unittest.main()
