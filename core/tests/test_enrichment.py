from __future__ import annotations

import sys
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.enrichment import merge_enrichment_inventory, materialize_enrichment_bindings, plan_scene_enrichment, validate_plan
from isaac_scene.scene_kits import build_catalog, search_catalog


def living_brief() -> dict:
    return {
        "schema_version": "isaac-scene-brief/v1",
        "inputs": {
            "mode": "text",
            "text": "生成一个温馨现代客厅，有沙发、茶几、电视柜、电视、落地灯、书架和植物。",
            "reference_images": [],
        },
        "domain": "home_scene",
        "quality_policy": "realistic",
        "room": {"type": "living_room", "size_m": [6.5, 5.2, 3.2]},
        "objects": [
            {"id": "sofa", "role": "sofa", "semantic_class": "furniture/seating/sofa"},
            {"id": "coffee_table", "role": "coffee_table", "semantic_class": "furniture/surface/coffee_table"},
            {"id": "tv_stand", "role": "tv_stand", "semantic_class": "furniture/storage/tv_stand"},
            {"id": "tv", "role": "television", "semantic_class": "electronics/display/television"},
            {"id": "floor_lamp", "role": "floor_lamp", "semantic_class": "lighting/floor_lamp"},
            {"id": "bookcase", "role": "bookcase", "semantic_class": "furniture/storage/bookcase"},
            {"id": "plant", "role": "plant", "semantic_class": "decor/plant"},
        ],
    }


class SceneEnrichmentTests(unittest.TestCase):
    def _scene_kit_catalog(self, root: Path) -> Path:
        database = root / "objects.sqlite"
        connection = sqlite3.connect(database)
        connection.executescript("""
            CREATE TABLE objects (
                model_id TEXT PRIMARY KEY, logical_id TEXT, canonical_class TEXT,
                subtype TEXT, callable INTEGER, qa_status TEXT, wrapper_path TEXT,
                bbox_m_json TEXT, front_axis TEXT, front_axis_status TEXT,
                review_report_json TEXT, license_status TEXT,
                allowed_use_scope TEXT, attribution_text TEXT
            );
            CREATE TABLE instances (
                scene_slug TEXT, model_id TEXT, semantic_name TEXT, asset_scope TEXT
            );
        """)
        connection.execute(
            "INSERT INTO objects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("arm1", "synthesis28_objects:arm1", "laboratory/equipment", "fume_extractor", 1, "ready", "/assets/arm.usda", json.dumps({"size": [0.4, 0.5, 1.5]}), None, "not_applicable", json.dumps({"support_mode": "surface"}), "CC-BY-NC-4.0", "internal_noncommercial_research", "test attribution"),
        )
        connection.execute("INSERT INTO instances VALUES (?,?,?,?)", ("laboratory", "arm1", "fume extractor arm", "independent_object"))
        connection.commit()
        connection.close()
        output = root / "scene_kits.json"
        output.write_text(json.dumps(build_catalog(database)), encoding="utf-8")
        return output

    def test_scene_kit_catalog_is_pose_free_and_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._scene_kit_catalog(Path(temporary))
            catalog = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("xform_json", path.read_text(encoding="utf-8"))
            matches = search_catalog(catalog, "现代教学实验室", domain="laboratory", room_type="teaching_laboratory")
            self.assertEqual("laboratory", matches[0]["slug"])
            self.assertTrue(all(row["domain"] == "laboratory" for row in matches))
            self.assertEqual("arm1", matches[0]["callable_asset_candidates"][0]["model_id"])

    def test_enrichment_plan_carries_same_scene_candidates(self) -> None:
        brief = {
            "schema_version": "isaac-scene-brief/v1",
            "inputs": {"mode": "text", "text": "现代教学实验室", "reference_images": []},
            "domain": "laboratory", "quality_policy": "realistic",
            "room": {"type": "teaching_laboratory", "size_m": [9, 8, 3.3]},
            "objects": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = self._scene_kit_catalog(Path(temporary))
            plan, observation = plan_scene_enrichment(brief, scene_kit_catalog_path=path)
            self.assertEqual("pass", observation["status"])
            self.assertEqual("laboratory", plan["scene_kit_context"]["matches"][0]["slug"])
            self.assertGreater(plan["metrics"]["scene_kit_match_count"], 0)
    def test_rich_living_plan_is_pose_free_and_layered(self) -> None:
        plan, observation = plan_scene_enrichment(living_brief(), density="rich")
        self.assertEqual("pass", observation["status"])
        self.assertEqual("living_warm_v2", plan["recommended_shell_preset"])
        self.assertEqual("none", plan["input_provenance"]["pose_source"])
        self.assertEqual([], plan["input_provenance"]["reference_images"])
        requests = {
            row["id"]: row
            for layer in plan["layers"]
            for row in layer["requests"]
        }
        self.assertEqual(2, requests["side_table"]["count"])
        self.assertEqual(3, requests["coffee_table_book"]["count"])
        self.assertEqual(6, requests["bookshelf_books"]["count"])
        self.assertEqual("asset_search_required", requests["area_rug"]["binding_status"])
        self.assertEqual("floor_underlay", requests["area_rug"]["placement_policy"])
        self.assertEqual("living_side_tables", plan["functional_groups"][0]["id"])
        self.assertEqual("living_wall_storage_flush", plan["layout_rules"][0]["id"])
        self.assertEqual(["anchors", "functional", "detail"], [row["id"] for row in plan["generation_phases"]])
        self.assertTrue(all(row["advance_requires_content_accept"] for row in plan["generation_phases"]))
        self.assertEqual("functional_coverage_not_object_count", plan["density_control"]["objective"])
        self.assertEqual("forbidden", requests["area_rug"]["selection_policy"]["cross_category_fallback"])
        self.assertTrue(validate_plan(plan)["valid"])

    def test_density_changes_counts_without_changing_required_inventory(self) -> None:
        normal, _ = plan_scene_enrichment(living_brief(), density="normal")
        rich, _ = plan_scene_enrichment(living_brief(), density="rich")
        self.assertEqual(normal["preserve_required_object_ids"], rich["preserve_required_object_ids"])
        self.assertLess(normal["metrics"]["planned_instance_count"], rich["metrics"]["planned_instance_count"])

    def test_enrichment_counts_only_missing_instances_on_the_requested_support(self) -> None:
        brief = living_brief()
        brief["objects"].extend([
            {
                "id": "coffee_book_1", "role": "book", "semantic_class": "household/book",
                "support_target": "coffee_table",
            },
            {
                "id": "coffee_book_2", "role": "book", "semantic_class": "household/book",
                "support_target": "coffee_table",
            },
            {
                "id": "shelf_book_group", "role": "book", "semantic_class": "household/book",
                "support_target": "bookcase", "count": 4,
            },
        ])
        plan, _ = plan_scene_enrichment(brief, density="rich")
        requests = {
            row["id"]: row
            for layer in plan["layers"]
            for row in layer["requests"]
        }
        self.assertEqual(1, requests["coffee_table_book"]["count"])
        self.assertEqual(2, requests["bookshelf_books"]["count"])
        self.assertEqual(2, requests["coffee_table_book"]["existing_count"])
        self.assertEqual(4, requests["bookshelf_books"]["existing_count"])

    def test_grouped_existing_count_can_fully_satisfy_a_request(self) -> None:
        brief = living_brief()
        brief["objects"].append({
            "id": "coffee_book_group", "role": "book", "semantic_class": "household/book",
            "support_target": "coffee_table", "count": 3,
        })
        plan, _ = plan_scene_enrichment(brief, density="rich")
        request_ids = {
            row["id"]
            for layer in plan["layers"]
            for row in layer["requests"]
        }
        self.assertNotIn("coffee_table_book", request_ids)
        satisfied = {row["id"]: row for row in plan["already_satisfied"]}
        self.assertEqual(3, satisfied["coffee_table_book"]["existing_count"])

    def test_privileged_regression_input_is_rejected(self) -> None:
        brief = living_brief()
        brief["objects"][0]["xform_matrix"] = [1.0] * 16
        with self.assertRaisesRegex(ValueError, "privileged fields"):
            plan_scene_enrichment(brief, density="rich")

    def test_unknown_profile_emits_policy_only_warning(self) -> None:
        brief = living_brief()
        brief["room"]["type"] = "observatory"
        plan, observation = plan_scene_enrichment(brief, density="normal")
        self.assertEqual("warn", observation["status"])
        self.assertEqual([], plan["layers"])
        self.assertEqual("ENRICHMENT_PROFILE_NOT_FOUND", observation["warnings"][0]["code"])

    def test_stage33_cross_domain_profiles_route_to_domain_shells(self) -> None:
        cases = [
            ("generic_scene", "meeting_room", "office_glazed_v1"),
            ("industrial_assembly", "auto_workshop", "factory_daylight_v1"),
            ("public_transit", "subway_fare_zone", "transit_concourse_v1"),
        ]
        for domain, room_type, expected_shell in cases:
            brief = {
                "schema_version": "isaac-scene-brief/v1",
                "inputs": {"mode": "text", "text": room_type, "reference_images": []},
                "domain": domain,
                "quality_policy": "realistic",
                "room": {"type": room_type, "size_m": [9.0, 7.0, 4.2]},
                "objects": [],
            }
            plan, observation = plan_scene_enrichment(brief, density="rich")
            self.assertEqual("pass", observation["status"])
            self.assertEqual(expected_shell, plan["recommended_shell_preset"])
            self.assertGreater(plan["metrics"]["planned_instance_count"], 0)

    def test_materialization_preserves_inventory_and_adds_support_dependencies(self) -> None:
        brief = living_brief()
        plan, _ = plan_scene_enrichment(brief, density="sparse")
        decisions = []
        for layer in plan["layers"]:
            for request in layer["requests"]:
                if request["id"] != "coffee_table_book":
                    decisions.append({
                        "request_id": request["id"],
                        "status": "gap",
                        "gap_code": "TEST_GAP",
                        "reason": "not part of this focused test",
                    })
                    continue
                decisions.append({
                    "request_id": request["id"],
                    "status": "bound",
                    "instances": [{
                        "id": "book_1",
                        "semantic_name": "flat hardcover book",
                        "size_m": [0.21, 0.27, 0.016],
                        "asset_binding": {
                            "logical_id": "test:book",
                            "path": "/assets/book.usd",
                            "asset_source": "test",
                            "license_status": "test",
                        },
                    }],
                })
        enriched, report = materialize_enrichment_bindings(brief, plan, {
            "schema_version": "isaac-scene-enrichment-bindings/v1",
            "profile_id": plan["profile_id"],
            "selected_shell_preset": "living_warm_v2",
            "decisions": decisions,
        })
        self.assertEqual("living_warm_v2", enriched["room"]["shell_preset"])
        self.assertTrue(set(plan["preserve_required_object_ids"]).issubset({row["id"] for row in enriched["objects"]}))
        book = next(row for row in enriched["objects"] if row["id"] == "book_1")
        self.assertEqual("coffee_table", book["support_target"])
        self.assertEqual(1, report["metrics"]["added_object_count"])

    def test_materialization_carries_underlay_policy_and_functional_group(self) -> None:
        brief = living_brief()
        plan, _ = plan_scene_enrichment(brief, density="normal")
        decisions = []
        for layer in plan["layers"]:
            for request in layer["requests"]:
                if request["id"] == "area_rug":
                    decisions.append({
                        "request_id": request["id"], "status": "bound",
                        "instances": [{
                            "id": "rug_1", "size_m": [2.0, 3.0, 0.02],
                            "asset_binding": {"logical_id": "test:rug", "path": "/assets/rug.usd"},
                        }],
                    })
                else:
                    decisions.append({
                        "request_id": request["id"], "status": "gap",
                        "gap_code": "TEST_GAP", "reason": "focused test",
                    })
        enriched, _ = materialize_enrichment_bindings(brief, plan, {
            "schema_version": "isaac-scene-enrichment-bindings/v1",
            "profile_id": plan["profile_id"], "decisions": decisions,
        })
        rug = next(row for row in enriched["objects"] if row["id"] == "rug_1")
        self.assertEqual("floor_underlay", rug["placement_policy"])
        self.assertEqual("living_side_tables", enriched["functional_groups"][0]["id"])
        self.assertIn("living_wall_storage_flush", {row["id"] for row in enriched["layout_rules"]})

    def test_materialization_rejects_pose_leakage(self) -> None:
        brief = living_brief()
        plan, _ = plan_scene_enrichment(brief, density="sparse")
        bindings = {
            "schema_version": "isaac-scene-enrichment-bindings/v1",
            "profile_id": plan["profile_id"],
            "source_world_matrix": [1.0] * 16,
            "decisions": [],
        }
        with self.assertRaisesRegex(ValueError, "cannot contain poses"):
            materialize_enrichment_bindings(brief, plan, bindings)

    def test_inventory_merge_preserves_baseline_assets_and_appends_only_declared_ids(self) -> None:
        base = {"entities": [{"id": "desk", "path": "/verified/desk.usd"}], "relations": []}
        compiled = {
            "entities": [
                {"id": "desk", "path": "/placeholder.usd"},
                {"id": "keyboard", "path": "/verified/keyboard.usd", "support_target": "/World/Objects/Desk"},
            ],
            "relations": [{"type": "on", "subject": "keyboard", "target": "desk"}],
            "brief_snapshot": {"schema_version": "isaac-scene-brief/v1"},
            "camera": {"prim": "/World/Cameras/Perspective"},
        }
        merged, report = merge_enrichment_inventory(base, compiled, {"added_object_ids": ["keyboard"]})
        self.assertEqual("/verified/desk.usd", merged["entities"][0]["path"])
        self.assertEqual(["desk", "keyboard"], [row["id"] for row in merged["entities"]])
        self.assertEqual(["baseline_required", "enrichment"], [row["generation_layer"] for row in merged["entities"]])
        self.assertEqual("/World/Cameras/Perspective", merged["camera"]["prim"])
        self.assertEqual(1, report["added_relation_count"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
