from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.scene_brief import (  # noqa: E402
    compile_to_scene_spec,
    initialize_brief,
    nearest_target_footprint_point,
    solve_facing_yaw,
    validate,
)


class SceneBriefTests(unittest.TestCase):
    def test_instance_support_target_of_counted_family_is_known(self) -> None:
        brief = initialize_brief(
            text="two benches and a monitor", reference_images=[], domain="industrial_assembly",
            room_type="factory", room_size_m=[8, 6, 3], quality_policy="realistic",
        )
        brief["objects"] = [
            {
                "id": "workbench", "role": "workbench",
                "semantic_class": "furniture/surface/workbench", "count": 2,
                "size_m": [2.0, .8, 1.0],
            },
            {
                "id": "monitor", "role": "control display",
                "semantic_class": "electronics/display/monitor",
                "support_target": "workbench_01", "size_m": [.2, .5, .5],
            },
        ]
        result = validate(brief)
        self.assertNotIn(
            "BRIEF_RELATION_TARGET_UNKNOWN",
            {row["code"] for row in result["warnings"]},
        )

    def test_functional_zone_bounds_and_rule_targets_are_validated(self) -> None:
        brief = initialize_brief(
            text="office work rows",
            reference_images=[],
            domain="generic_scene",
            room_type="office",
            room_size_m=[8, 6, 3],
            quality_policy="realistic",
        )
        brief["zones"] = [{"id": "work", "bounds_norm": [0.7, 0.2, 0.3, 0.8]}]
        brief["layout_rules"] = [{"id": "desks", "zone": "missing"}]
        result = validate(brief)
        self.assertFalse(result["valid"])
        self.assertEqual(
            {"BRIEF_ZONE_BOUNDS", "BRIEF_LAYOUT_RULE_ZONE_UNKNOWN"},
            {row["code"] for row in result["errors"]},
        )

    def test_functional_group_requires_enough_member_offsets(self) -> None:
        brief = initialize_brief(
            text="office", reference_images=[], domain="generic_scene", room_type="office",
            room_size_m=[8, 6, 3], quality_policy="realistic",
        )
        brief["functional_groups"] = [{
            "id": "workstation", "anchor_match": {"semantic_terms": ["desk"]},
            "members": [{"match": {"semantic_terms": ["chair"]}, "per_anchor": 2, "offsets_m": [[0, -1]]}],
        }]
        result = validate(brief)
        self.assertFalse(result["valid"])
        self.assertIn("BRIEF_FUNCTIONAL_GROUP_OFFSETS", {row["code"] for row in result["errors"]})

    def test_functional_group_offset_reference_is_validated(self) -> None:
        brief = initialize_brief(
            text="factory", reference_images=[], domain="industrial_assembly", room_type="factory",
            room_size_m=[8, 6, 3], quality_policy="realistic",
        )
        brief["functional_groups"] = [{
            "id": "service", "anchor_match": {"semantic_terms": ["conveyor"]},
            "members": [{
                "match": {"semantic_terms": ["robot"]}, "per_anchor": 1,
                "offsets_m": [[0, 1]], "offset_reference": "world_origin",
            }],
        }]
        result = validate(brief)
        self.assertFalse(result["valid"])
        self.assertIn("BRIEF_FUNCTIONAL_GROUP_OFFSET_REFERENCE", {row["code"] for row in result["errors"]})

    def test_required_semantic_connection_ports_must_be_verified(self) -> None:
        brief = initialize_brief(
            text="factory process line", reference_images=[], domain="industrial_assembly",
            room_type="factory", room_size_m=[12, 8, 4], quality_policy="realistic",
        )
        brief["functional_groups"] = [{
            "id": "process_flow", "anchor_match": {"entity_ids": ["aoi"]},
            "members": [{
                "match": {"entity_ids": ["line"]}, "per_anchor": 1,
                "offsets_m": [[1, 0]],
                "connection": {
                    "anchor_port_local_m": [0.5, 0, 0.8],
                    "member_port_local_m": [-1.0, 0, 0.8],
                    "require_semantic_evidence": True,
                    "anchor_port": {
                        "id": "aoi_output", "role": "output", "normal_axis": "+X",
                        "evidence_status": "verified", "evidence_source": "source multiview audit",
                    },
                    "member_port": {
                        "id": "line_input", "role": "input", "normal_axis": "-X",
                        "evidence_status": "pending",
                    },
                },
            }],
        }]
        result = validate(brief)
        self.assertFalse(result["valid"])
        self.assertIn(
            "BRIEF_FUNCTIONAL_GROUP_PORT_EVIDENCE_REQUIRED",
            {row["code"] for row in result["errors"]},
        )

    def test_reference_brief_compiles_to_executable_scene_spec(self) -> None:
        brief = json.loads((SKILL_ROOT / "tests" / "fixtures" / "meeting_room_scene_brief.json").read_text())
        brief["camera"]["exposure_ev"] = -1.25
        validation = validate(brief)
        self.assertEqual("pass", validation["status"])
        spec, observation = compile_to_scene_spec(brief, scene_output="/runs/meeting.usda")
        self.assertEqual("pass", observation["status"])
        self.assertEqual(4, len(spec["entities"]))
        self.assertEqual(0, observation["unresolved_count"])
        self.assertEqual([1.6, -7.02, 2.8], spec["camera"]["translate"])
        self.assertEqual([0.1, 0.0, 1.15], spec["camera"]["look_at"])
        self.assertEqual(-1.25, spec["camera"]["exposure_ev"])
        self.assertEqual(4, len(spec["support_colliders"]))
        self.assertEqual("floor", spec["entities"][0]["support_target"])
        self.assertEqual([2.7, 5.0, 0.75], spec["entities"][0]["asset_bounds_m"]["size"])
        self.assertEqual("/World/Objects/ConferenceTable", spec["entities"][1]["facing_target"])
        self.assertEqual(
            "front_axis_to_nearest_target_footprint",
            spec["entities"][1]["orientation_solver"]["method"],
        )

    def test_facing_solver_maps_verified_local_front_to_world_target(self) -> None:
        self.assertEqual(90.0, solve_facing_yaw([0, 0, 0], [1, 0, 0], "-Y"))
        self.assertEqual(-90.0, solve_facing_yaw([0, 0, 0], [-1, 0, 0], "-Y"))
        self.assertEqual(0.0, solve_facing_yaw([0, 0, 0], [0, 1, 0], "+Y"))
        self.assertIsNone(solve_facing_yaw([0, 0, 0], [0, 0, 1], "+X"))

    def test_facing_solver_projects_chair_to_rotated_table_footprint(self) -> None:
        target_point = nearest_target_footprint_point(
            [-1.05, -0.9, 0.44],
            [0.0, 0.4, 0.376],
            [3.0, 1.2, 0.752],
            [0.0, 0.0, 90.0],
        )
        self.assertIsNotNone(target_point)
        self.assertAlmostEqual(-0.6, target_point[0], places=6)
        self.assertAlmostEqual(-0.9, target_point[1], places=6)
        self.assertEqual(
            0.0,
            solve_facing_yaw(
                [-1.05, -0.9, 0.44],
                target_point,
                "+X",
            ),
        )

    def test_initializer_keeps_image_observation_unresolved(self) -> None:
        brief = initialize_brief(
            text=None,
            reference_images=["missing_reference.jpg"],
            domain="generic_scene",
            room_type="office",
            room_size_m=[6, 7, 3],
            quality_policy="reference_match",
        )
        validation = validate(brief)
        self.assertTrue(validation["valid"])
        self.assertEqual("warn", validation["status"])
        self.assertEqual("image", brief["inputs"]["mode"])

    def test_reference_brief_rejects_object_disguised_as_architecture(self) -> None:
        brief = {
            "schema_version": "isaac-scene-brief/v1",
            "inputs": {
                "mode": "hybrid",
                "text": "Match the meeting room.",
                "reference_images": [{"path": "reference.jpg"}],
            },
            "domain": "generic_scene",
            "quality_policy": "reference_match",
            "room": {
                "type": "meeting_room",
                "size_m": [7, 8, 3],
                "size_confidence": "low",
                "architectural_elements": [{
                    "id": "rear_presentation_panel",
                    "type": "box",
                    "translate": [0, 3.9, 1.7],
                    "size": [1.5, 0.06, 0.8],
                    "evidence": "Use a black panel rather than an unverified TV proxy.",
                }],
            },
            "camera": {
                "position_m": [0, -5, 1.5],
                "look_at_m": [0, 1, 1.2],
                "confidence": "medium",
            },
            "objects": [],
        }
        validation = validate(brief)
        self.assertFalse(validation["valid"])
        self.assertIn(
            "BRIEF_ARCHITECTURAL_OBJECT_PROXY",
            {item["code"] for item in validation["errors"]},
        )

    def test_reference_anchor_requires_provenance_and_visual_gate(self) -> None:
        brief = {
            "schema_version": "isaac-scene-brief/v1",
            "inputs": {
                "mode": "hybrid",
                "text": "Match the meeting room.",
                "reference_images": [{"path": "reference.jpg"}],
            },
            "domain": "generic_scene",
            "quality_policy": "reference_match",
            "room": {
                "type": "meeting_room",
                "size_m": [7, 8, 3],
                "size_confidence": "low",
            },
            "camera": {
                "position_m": [0, -5, 1.5],
                "look_at_m": [0, 1, 1.2],
                "confidence": "medium",
            },
            "objects": [{
                "id": "conference_chairs",
                "role": "chair",
                "semantic_class": "furniture/seating/conference_chair",
                "criticality": "anchor",
                "confidence": "high",
                "size_m": [0.6, 0.6, 0.9],
                "asset_binding": {
                    "path": "/assets/chair.usd",
                    "front_axis": "+X",
                },
                "instances": [{
                    "id": "chair_01",
                    "translate_m": [1, 0, 0],
                    "rotate_deg": [0, 0, 180],
                }],
            }],
        }
        validation = validate(brief)
        self.assertFalse(validation["valid"])
        codes = {item["code"] for item in validation["errors"]}
        self.assertIn("BRIEF_ANCHOR_PROVENANCE_REQUIRED", codes)
        self.assertIn("BRIEF_ANCHOR_VISUAL_GATE_REQUIRED", codes)

    def test_independent_review_override_requires_complete_audit(self) -> None:
        brief = json.loads(
            (SKILL_ROOT / "tests" / "fixtures" / "meeting_room_scene_brief.json").read_text()
        )
        evidence = brief["objects"][0]["asset_binding"]["reference_match_evidence"]
        evidence["selection_basis"] = "independent_review_override"
        evidence["manual_review"] = {"reviewers": ["one-reviewer"]}
        invalid = validate(brief)
        self.assertFalse(invalid["valid"])
        self.assertIn(
            "BRIEF_MANUAL_REVIEW_OVERRIDE_INVALID",
            {item["code"] for item in invalid["errors"]},
        )

        evidence["manual_review"] = {
            "reviewers": ["retrieval-agent", "main-agent"],
            "selected_preview_path": "/previews/table/pos_y.png",
            "comparison_preview_paths": [
                "/previews/table/pos_y.png",
                "/previews/workstation/pos_y.png",
            ],
            "reference_features": [
                "single long freestanding rectangular top",
                "conference-table height and central pedestal",
            ],
            "rejected_candidates": [
                {
                    "logical_id": "fixture:workstation",
                    "reason": "attached partition and cabinets do not match",
                }
            ],
            "automatic_gate_failures": ["top-two margin below 0.015"],
            "identity_gate": "pass",
            "geometry_gate": "pass",
            "orientation_gate": "pass",
            "rationale": "Both reviewers independently selected the table.",
        }
        valid = validate(brief)
        self.assertTrue(valid["valid"])
        self.assertNotIn(
            "BRIEF_MANUAL_REVIEW_OVERRIDE_INVALID",
            {item["code"] for item in valid["errors"]},
        )

    def test_compile_marks_asset_layout_and_orientation_gaps(self) -> None:
        brief = {
            "schema_version": "isaac-scene-brief/v1",
            "inputs": {"mode": "text", "text": "Add a sofa."},
            "domain": "home_scene",
            "quality_policy": "realistic",
            "room": {"type": "living_room", "size_m": [5, 6, 3]},
            "objects": [{
                "id": "sofa",
                "role": "sofa",
                "semantic_class": "furniture/seating/sofa",
                "criticality": "anchor",
                "size_m": [2, 0.9, 0.8]
            }]
        }
        spec, observation = compile_to_scene_spec(brief)
        self.assertEqual("warn", observation["status"])
        reasons = {item["reason"] for item in observation["unresolved"]}
        self.assertEqual(
            {"asset_resolution_required", "layout_solver_required", "orientation_verification_required"},
            reasons,
        )
        self.assertIn("query", spec["entities"][0])

    def test_compile_preserves_asset_identity_semantics_and_license(self) -> None:
        brief = {
            "schema_version": "isaac-scene-brief/v1",
            "inputs": {"mode": "text", "text": "Add a refrigerator."},
            "domain": "home_scene",
            "quality_policy": "realistic",
            "room": {"type": "kitchen", "size_m": [5, 6, 3]},
            "objects": [{
                "id": "fridge",
                "role": "appliance",
                "semantic_class": "appliance/kitchen/refrigerator",
                "asset_binding": {
                    "logical_id": "synthesis27:fridge-id",
                    "model_id": "fridge-id",
                    "canonical_class": "appliance/kitchen/refrigerator",
                    "asset_source": "synthesis27",
                    "license_status": "cc_by_nc_4_0_verified",
                    "license_evidence": {"record_id": "record-uuid"},
                    "path": "/assets/fridge-id/model.usd",
                    "front_axis": "-Y",
                },
                "instances": [{
                    "id": "fridge",
                    "translate_m": [1, 2, 0],
                    "rotate_deg": [0, 0, 0],
                }],
            }],
        }
        spec, observation = compile_to_scene_spec(brief)
        self.assertEqual("pass", observation["status"])
        entity = spec["entities"][0]
        self.assertEqual("synthesis27:fridge-id", entity["logical_id"])
        self.assertEqual("fridge-id", entity["model_id"])
        self.assertEqual(
            "appliance/kitchen/refrigerator", entity["canonical_class"]
        )
        self.assertEqual("synthesis27", entity["asset_source"])
        self.assertEqual("cc_by_nc_4_0_verified", entity["license_status"])
        self.assertEqual("record-uuid", entity["license_evidence"]["record_id"])

    def test_compile_accepts_verified_axial_table_without_fake_front(self) -> None:
        brief = {
            "schema_version": "isaac-scene-brief/v1",
            "inputs": {"mode": "text", "text": "Add a symmetric meeting table."},
            "domain": "generic_scene",
            "quality_policy": "realistic",
            "room": {"type": "meeting_room", "size_m": [6, 8, 3]},
            "objects": [{
                "id": "meeting_table",
                "role": "table",
                "semantic_class": "furniture/surface/meeting_table",
                "asset_binding": {
                    "logical_id": "fixture:axial-table",
                    "path": "/assets/axial_table.usd",
                    "orientation_mode": "axial",
                    "primary_axis": "X",
                    "orientation_confidence": "high",
                    "orientation_source": "four-view preview",
                },
                "instances": [{
                    "id": "meeting_table",
                    "translate_m": [0, 0, 0],
                    "rotate_deg": [0, 0, 90],
                }],
            }],
        }
        spec, observation = compile_to_scene_spec(brief)
        self.assertEqual("pass", observation["status"])
        self.assertEqual("axial", spec["entities"][0]["orientation_mode"])
        self.assertEqual("X", spec["entities"][0]["primary_axis"])
        self.assertNotIn("front_axis", spec["entities"][0])

    def test_asymmetric_room_shell_and_architecture_are_compiled(self) -> None:
        brief = {
            "schema_version": "isaac-scene-brief/v1",
            "inputs": {"mode": "text", "text": "Empty glass meeting room."},
            "domain": "generic_scene",
            "quality_policy": "realistic",
            "room": {
                "type": "meeting_room",
                "size_m": [12, 17, 3.2],
                "center_m": [0, -4, 0],
                "open_sides": ["front", "left", "right"],
                "ceiling": {"color_rgb": [0.7, 0.7, 0.7]},
                "architectural_elements": [{
                    "id": "left_glass",
                    "type": "glass_panel",
                    "translate": [-5.5, -2, 1.4],
                    "size": [0.03, 12, 2.8],
                    "display_color": [0.2, 0.3, 0.4],
                    "display_opacity": 0.2,
                }],
            },
            "objects": [],
        }
        spec, _observation = compile_to_scene_spec(brief)
        by_id = {item["id"]: item for item in spec["support_colliders"]}
        self.assertEqual([0.0, -4.0, -0.06], by_id["floor"]["translate"])
        self.assertEqual([0.0, 4.5, 1.6], by_id["back_wall"]["translate"])
        self.assertEqual(0.2, by_id["left_glass"]["display_opacity"])
        self.assertIn("ceiling", by_id)

    def test_reference_guided_lighting_overrides_default_rig(self) -> None:
        brief = {
            "schema_version": "isaac-scene-brief/v1",
            "inputs": {"mode": "text", "text": "Bright corporate meeting room."},
            "domain": "generic_scene",
            "quality_policy": "realistic",
            "room": {"type": "meeting_room", "size_m": [7, 8, 3]},
            "lighting": {
                "mode": "reference_guided",
                "environment": {
                    "enabled": True,
                    "intensity": 220,
                    "color": [0.88, 0.93, 1.0],
                    "texture": "/lighting/studio.hdr",
                },
                "fixtures": [
                    {
                        "id": "ceiling_left",
                        "type": "rect",
                        "translate": [-1.5, 0, 2.85],
                        "look_at": [-1.5, 0, 0],
                        "width": 1.2,
                        "height": 5.5,
                        "intensity": 1350,
                        "color": [1.0, 0.95, 0.88],
                        "temperature": 5200,
                        "exposure": -0.5,
                    },
                    {
                        "id": "ceiling_right",
                        "type": "rect",
                        "translate": [1.5, 0, 2.85],
                        "width": 1.2,
                        "height": 5.5,
                        "intensity": 1350,
                        "color": [1.0, 0.95, 0.88],
                    },
                ],
            },
            "objects": [],
        }
        spec, observation = compile_to_scene_spec(brief)
        self.assertEqual("pass", observation["status"])
        self.assertEqual(3, len(spec["lights"]))
        self.assertEqual("/lighting/studio.hdr", spec["lights"][0]["texture"])
        self.assertEqual("/World/Lights/CeilingLeft", spec["lights"][1]["prim"])
        self.assertEqual([-1.5, 0, 0], spec["lights"][1]["look_at"])
        self.assertEqual(5200, spec["lights"][1]["temperature"])
        self.assertEqual(-0.5, spec["lights"][1]["exposure"])
        self.assertEqual("reference_guided", spec["lighting_policy"]["mode"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
