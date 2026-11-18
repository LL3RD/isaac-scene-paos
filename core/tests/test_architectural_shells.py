from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.architectural_shells import (  # noqa: E402
    apply_shell,
    list_shells,
    resolve_for_compiled_spec,
    select_shell,
)


class ArchitecturalShellTests(unittest.TestCase):
    def test_catalog_distinguishes_asset_and_procedural_shells(self) -> None:
        rows = list_shells()
        by_id = {row["id"]: row for row in rows}
        self.assertTrue(by_id["grscenes_home_architecture_mvu_v1"]["asset_backed"])
        self.assertFalse(by_id["office_glazed_v1"]["asset_backed"])

    def test_selects_verified_door_window_light_shell(self) -> None:
        result = select_shell(
            "home",
            required_features=["doors", "windows", "lights"],
            max_occupancy="sparse",
        )
        self.assertEqual("pass", result["status"])
        self.assertEqual("grscenes_home_architecture_mvu_v1", result["selected"]["id"])
        self.assertEqual("architecture_only", result["selected"]["occupancy"])

    def test_missing_verified_office_openings_stays_pending(self) -> None:
        result = select_shell(
            "office",
            required_features=["doors", "windows", "lights"],
        )
        self.assertEqual("pending", result["status"])
        self.assertIsNone(result["selected"])

    def test_subway_fare_zone_discovers_transit_procedural_shell(self) -> None:
        result = select_shell(
            "subway_fare_zone",
            required_features=["lights"],
            max_occupancy="empty",
        )
        self.assertEqual("pass", result["status"])
        self.assertEqual("transit_concourse_v1", result["selected"]["id"])
        self.assertFalse(str(result["selected"]["kind"]).startswith("asset_backed"))

    def test_apply_asset_shell_hides_furniture_and_preserves_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            spec = {"schema_version": "isaac-scene-spec/v2", "entities": []}
            config = {"libraries": [{"name": "grscenes_scenes", "root": temp}]}
            result = apply_shell(spec, "grscenes_home_architecture_mvu_v1", config)
            self.assertEqual([], spec["entities"])
            self.assertTrue(result["entities"][0]["path"].startswith(temp))
            self.assertEqual([0.01, 0.01, 0.01], result["entities"][0]["scale"])
            self.assertEqual("preserve", result["entities"][0]["unit_mode"])
            hidden = {row["prim"] for row in result["prim_overrides"] if row.get("visible") is False}
            self.assertIn("/World/Background/Meshes/Furnitures", hidden)
            self.assertEqual("architecture_only", result["room"]["architectural_shell"]["occupancy"])

    def test_shell_change_invalidates_existing_layout_and_resolved_relations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            spec = {
                "schema_version": "isaac-scene-spec/v2",
                "entities": [
                    {
                        "id": "tv_stand", "role": "tv_stand", "prim": "/World/Objects/TVStand",
                        "translate": [1.9, 0.0, 0.4], "rotate": [0.0, 0.0, 0.0],
                        "support_target": "floor", "placement_solver": {"mesh_support_coverage": 1.0},
                    },
                    {
                        "id": "tv", "role": "television", "prim": "/World/Objects/TV",
                        "translate": [1.9, 0.0, 0.9], "rotate": [0.0, 0.0, -90.0],
                        "support_target": "tv_stand", "placement_solver": {"mesh_support_coverage": 1.0},
                    },
                ],
                "relations": [{
                    "type": "on", "subject": "tv", "target": "tv_stand",
                    "resolved_transform": [1.9, 0.0, 0.9],
                    "resolution_source": "old_shell_solver",
                }],
            }
            config = {"libraries": [{"name": "grscenes_scenes", "root": temp}]}
            result = apply_shell(spec, "grscenes_home_architecture_mvu_v1", config)
            by_id = {row["id"]: row for row in result["entities"]}
            for entity_id in ("tv_stand", "tv"):
                self.assertNotIn("translate", by_id[entity_id])
                self.assertNotIn("rotate", by_id[entity_id])
                self.assertNotIn("placement_solver", by_id[entity_id])
                self.assertTrue(by_id[entity_id]["layout_constraints"]["requires_relayout_after_shell_change"])
            pending = {row["id"] for row in result["layout_pending_entities"]}
            self.assertEqual({"tv_stand", "tv"}, pending)
            self.assertNotIn("resolved_transform", result["relations"][0])
            self.assertEqual(
                ["tv_stand", "tv"],
                result["room"]["architectural_shell"]["invalidated_layout_entity_ids"],
            )

    def test_preserving_layout_after_shell_change_is_explicit_diagnostic_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            spec = {"entities": [{"id": "chair", "translate": [1.0, 2.0, 0.0]}]}
            config = {"libraries": [{"name": "grscenes_scenes", "root": temp}]}
            result = apply_shell(
                spec, "grscenes_home_architecture_mvu_v1", config,
                preserve_existing_layout=True,
            )
            self.assertEqual([1.0, 2.0, 0.0], result["entities"][1]["translate"])
            self.assertTrue(result["room"]["architectural_shell"]["preserved_existing_layout"])
            self.assertEqual([], result["room"]["architectural_shell"]["invalidated_layout_entity_ids"])

    def test_auto_compile_resolution_replaces_procedural_room_and_lights(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            spec = {
                "schema_version": "isaac-scene-spec/v2",
                "room": {"type": "home"},
                "entities": [{"id": "chair", "role": "chair"}],
                "support_colliders": [{"id": "room_floor"}],
                "lights": [{"prim": "/World/Lights/Key"}],
                "lighting_policy": {"mode": "reference"},
            }
            brief = {"quality_policy": "realistic", "room": {"type": "home"}}
            config = {"libraries": [{"name": "grscenes_scenes", "root": temp}]}
            result, observation = resolve_for_compiled_spec(spec, brief, config)
            self.assertEqual("pass", observation["status"])
            self.assertTrue(observation["procedural_shell_replaced"])
            self.assertEqual("room_shell", result["entities"][0]["role"])
            self.assertEqual([], result["support_colliders"])
            self.assertEqual([], result["lights"])
            self.assertEqual("asset_authored", result["lighting_policy"]["mode"])
            self.assertEqual("reference", result["lighting_policy"]["requested_policy"]["mode"])

    def test_auto_compile_resolution_keeps_procedural_fallback_when_unavailable(self) -> None:
        spec = {"room": {"type": "office"}, "support_colliders": [{"id": "room_floor"}]}
        brief = {"quality_policy": "realistic", "room": {"type": "office"}}
        result, observation = resolve_for_compiled_spec(spec, brief, {"libraries": []})
        self.assertEqual("pending", observation["status"])
        self.assertEqual(spec, result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
