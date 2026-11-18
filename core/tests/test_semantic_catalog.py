from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.semantic_catalog import (  # noqa: E402
    classify_semantics,
    annotate,
    derive_semantic_name,
    infer_retrieval_class,
    initialize,
    rebuild_fts,
    search,
    set_asset_state,
    taxonomy,
    upsert,
)


class SemanticCatalogTests(unittest.TestCase):
    def test_semantic_taxonomy_is_source_independent(self) -> None:
        self.assertEqual("furniture/seating/office_chair", classify_semantics("office_chair004")[0])
        self.assertEqual("medical/lighting", classify_semantics("surgical_shadowless_lamp001")[0])
        self.assertEqual("industrial/machine", classify_semantics("reflow_soldering_machine")[0])
        self.assertEqual("industrial/conveyor", classify_semantics("ConveyorBelt_A42")[0])
        self.assertEqual("logistics/vehicle", classify_semantics("AGV_01")[0])
        self.assertEqual("furniture/seating/office_chair", classify_semantics("SM_ChairOffice_A")[0])
        self.assertNotEqual("furniture/seating/bench", classify_semantics("benchmark_coffee_machine")[0])
        self.assertEqual("decor/textile", classify_semantics("pillow002")[0])
        self.assertEqual("plumbing/fixture", classify_semantics("faucet001")[0])
        self.assertEqual("architecture/partition", classify_semantics("glass_partition_01")[0])
        self.assertEqual("architecture/surface/floor", classify_semantics("ground_02")[0])
        self.assertEqual("furniture/storage/low_cabinet", classify_semantics("modern low cabinet")[0])
        self.assertEqual("furniture/storage/tall_cabinet", classify_semantics("vertical filing cabinet")[0])
        self.assertEqual("appliance/laundry", classify_semantics("washing machine")[0])
        self.assertEqual("appliance/kitchen", classify_semantics("popcorn machine")[0])
        self.assertEqual(
            "appliance/kitchen/refrigerator",
            classify_semantics("full height refrigerator")[0],
        )
        self.assertEqual(
            "appliance/kitchen/freezer",
            classify_semantics("commercial chest freezer")[0],
        )
        self.assertEqual(
            "safety/equipment/guardrail",
            classify_semantics("yellow industrial safety railing")[0],
        )
        self.assertEqual(
            "safety/equipment/fire_extinguisher",
            classify_semantics("SM FireExtinguisher 02")[0],
        )
        self.assertEqual(
            "appliance/kitchen/microwave",
            classify_semantics("countertop microwave")[0],
        )
        self.assertEqual(
            "appliance/kitchen/electric_cooker",
            classify_semantics("rice cooker")[0],
        )
        self.assertEqual(
            "appliance/kitchen/coffee_maker",
            classify_semantics("coffee maker")[0],
        )
        self.assertEqual(
            "appliance/kitchen/oven",
            classify_semantics("countertop oven")[0],
        )
        self.assertEqual(
            "industrial/machine",
            classify_semantics("machine 01", "industrial/factory/props", "object")[0],
        )
        self.assertEqual(
            "human/person",
            classify_semantics(
                "F Medical 01",
                "Isaac/People/Characters/F_Medical_01",
                "asset",
            )[0],
        )
        self.assertEqual(
            "architecture/surface/floor",
            classify_semantics("ground 001", "grscenes/layout/others/ground", "layout")[0],
        )
        self.assertEqual(
            "medical/storage",
            classify_semantics(
                "SM Medical Cabinet 01",
                "Isaac/Environments/Hospital/Props",
                "environment",
            )[0],
        )
        self.assertEqual(
            "logistics/container",
            classify_semantics(
                "SM Pallet A01",
                "Isaac/Environments/Warehouse/assemblies/rack/props",
                "asset",
            )[0],
        )
        self.assertEqual(
            "logistics/container",
            classify_semantics(
                "SM Crate Plywood B01",
                "Isaac/Environments/Warehouse/assemblies/rack/props",
                "asset",
            )[0],
        )
        self.assertEqual(
            "furniture/storage/rack/warehouse",
            classify_semantics("SM Rack Frame 03", "industrial/Simple_Warehouse/Props", "object")[0],
        )
        self.assertEqual(
            "furniture/storage/rack/bike",
            classify_semantics("Bike Rack 01", "outdoor/props", "object")[0],
        )
        self.assertEqual(
            "environment/scene",
            classify_semantics(
                "tree_unmatched",
                "Isaac/Environments/Outdoor/Rivermark/unmatched_assets",
                "environment",
            )[0],
        )
        self.assertEqual("furniture/seating", infer_retrieval_class("chair"))
        self.assertEqual("industrial/conveyor", infer_retrieval_class("conveyor belt"))
        self.assertEqual(
            "furniture/seating/conference_chair",
            infer_retrieval_class("slim conference chair"),
        )
        self.assertEqual(
            "electronics/display/wall_mounted",
            infer_retrieval_class("wall mounted display television"),
        )
        self.assertEqual(
            "electronics/display/mobile_stand",
            infer_retrieval_class("mobile floor standing television"),
        )
        self.assertEqual(
            "lighting/fixture",
            infer_retrieval_class("small table lamp beside a television"),
        )
        self.assertEqual(
            "environment/sample_scene",
            classify_semantics(
                "wander",
                "Isaac/Samples/BehaviorTree/Wander",
                "asset",
            )[0],
        )

    def test_generic_usd_filename_recovers_semantics_from_path(self) -> None:
        path = "/assets/objects/benchmark/barcode_scanner/benchmark_barcode_scanner_000/Aligned.usd"
        self.assertEqual("barcode scanner", derive_semantic_name("Aligned", path))
        self.assertEqual("electronics/retail", classify_semantics(derive_semantic_name("Aligned", path))[0])

    def test_search_can_include_metadata_only_synthesis_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "semantic.sqlite"
            connection = sqlite3.connect(path)
            initialize(connection)
            upsert(connection, {
                "logical_id": "synthesis27:abc",
                "canonical_class": "furniture/seating/conference_chair",
                "subtype": "meeting chair",
                "semantic_name": "meeting chair",
                "aliases_json": "[]",
                "domain_tags_json": "[]",
                "style_tags_json": "[]",
                "material_tags_json": "[]",
                "color_tags_json": "[]",
                "source_library": "synthesis27",
                "source_asset_id": "abc",
                "source_path": None,
                "remote_model_id": "abc",
                "availability": "remote_metadata_only",
                "callable": 0,
                "preview_index_path": None,
                "front_axis": None,
                "meters_per_unit": None,
                "bbox_json": None,
                "license_status": "verify_before_download",
                "usage_count": 4,
                "scene_slugs_json": "[\"meeting_room_02\"]",
                "metadata_json": "{}",
                "search_text": "meeting conference chair metal office",
            })
            rebuild_fts(connection)
            connection.close()
            self.assertEqual([], search(path, "conference chair", callable_only=True))
            hits = search(path, "conference chair", callable_only=False)
            self.assertEqual("synthesis27:abc", hits[0]["logical_id"])
            self.assertFalse(hits[0]["callable"])

            annotated = annotate(
                path,
                logical_id="synthesis27:abc",
                canonical_class="furniture/seating/conference_chair",
                subtype="cantilever chair",
                semantic_name="black cantilever conference chair",
                preview_index_path="/previews/index.json",
                front_axis="-Y",
                meters_per_unit=1.0,
                bbox={"size": [0.5, 0.5, 1.0]},
                style_tags=["modern", "Modern"],
                material_tags=["metal"],
                color_tags=["black"],
                metadata_patch={
                    "orientation_mode": "front_facing",
                    "operator_axis": "-Y",
                },
            )
            self.assertEqual("-Y", annotated["front_axis"])
            self.assertEqual("cantilever chair", annotated["subtype"])
            self.assertEqual("black cantilever conference chair", annotated["semantic_name"])
            self.assertEqual(["modern"], annotated["style_tags"])
            self.assertEqual([0.5, 0.5, 1.0], annotated["bbox"]["size"])
            self.assertEqual("front_facing", annotated["metadata"]["orientation_mode"])
            self.assertEqual("-Y", annotated["metadata"]["operator_axis"])
            tagged_hits = search(path, "modern metal black", callable_only=False)
            self.assertEqual("synthesis27:abc", tagged_hits[0]["logical_id"])
            filtered_hits = search(
                path,
                "conference chair",
                callable_only=False,
                subtype="cantilever",
                style_tags=["modern"],
                material_tags=["metal"],
                color_tags=["black"],
                min_size_m=[0.4, 0.4, 0.8],
                max_size_m=[0.8, 0.8, 1.2],
            )
            self.assertEqual("synthesis27:abc", filtered_hits[0]["logical_id"])
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            rebuilt_row = dict(connection.execute(
                "SELECT * FROM semantic_assets WHERE logical_id='synthesis27:abc'"
            ).fetchone())
            rebuilt_row.update({
                "canonical_class": "furniture/seating/chair",
                "subtype": "chair",
                "semantic_name": "meeting chair",
                "preview_index_path": None,
                "front_axis": None,
                "style_tags_json": "[]",
                "material_tags_json": "[]",
                "color_tags_json": "[]",
                "search_text": "meeting chair",
            })
            upsert(connection, rebuilt_row)
            connection.commit()
            persisted = connection.execute(
                """SELECT canonical_class, subtype, preview_index_path, front_axis
                   FROM semantic_assets WHERE logical_id='synthesis27:abc'"""
            ).fetchone()
            connection.close()
            self.assertEqual("furniture/seating/conference_chair", persisted["canonical_class"])
            self.assertEqual("cantilever chair", persisted["subtype"])
            self.assertEqual("/previews/index.json", persisted["preview_index_path"])
            self.assertEqual("-Y", persisted["front_axis"])
            semantic_view = taxonomy(path)
            self.assertEqual(1, semantic_view["totals"]["asset_count"])
            self.assertEqual("furniture/seating/conference_chair", semantic_view["classes"][0]["canonical_class"])
            self.assertEqual("synthesis27", semantic_view["classes"][0]["provenance"][0]["source_library"])

            quarantined = set_asset_state(
                path,
                logical_id="synthesis27:abc",
                state="quarantine",
                reason="degenerate preview bounds",
                evidence_path="/previews/index.json",
            )
            self.assertFalse(quarantined["callable"])
            self.assertEqual("quarantined", quarantined["availability"])
            self.assertEqual([], search(path, "conference chair", callable_only=True))

            restored = set_asset_state(
                path,
                logical_id="synthesis27:abc",
                state="ready",
                reason="package repaired and preview rerendered",
            )
            self.assertFalse(restored["callable"])
            self.assertEqual("remote_metadata_only", restored["availability"])

    def test_read_only_synthesis_adapter_exposes_only_scoped_ready_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = root / "semantic.sqlite"
            connection = sqlite3.connect(primary)
            initialize(connection)
            rebuild_fts(connection)
            connection.close()
            adapter = root / "objects.sqlite"
            connection = sqlite3.connect(adapter)
            connection.execute(
                """CREATE TABLE objects (
                    model_id TEXT, logical_id TEXT, semantic_name TEXT,
                    aliases_json TEXT, canonical_class TEXT, subtype TEXT,
                    asset_scopes_json TEXT, scene_slugs_json TEXT,
                    usage_count INTEGER, source_path TEXT, wrapper_path TEXT,
                    bbox_m_json TEXT, preview_index_path TEXT, front_axis TEXT,
                    front_axis_status TEXT, review_report_json TEXT,
                    license_status TEXT, license_review_status TEXT,
                    license_policy_id TEXT, allowed_use_scope TEXT,
                    attribution_text TEXT, qa_status TEXT, callable INTEGER
                )"""
            )
            base = (
                "chair-1", "synthesis28:chair-1", "ergonomic office chair",
                json.dumps(["task chair"]), "furniture/seating/office_chair",
                "ergonomic_task_chair", json.dumps(["independent_object"]),
                json.dumps(["office"]), 5, "/source/chair.usd",
                "/entrypoints/chair.usda",
                json.dumps({"size": [0.6, 0.65, 1.1]}),
                "/previews/index.json", "-Y", "verified",
                json.dumps({"support_mode": "floor"}),
                "cc_by_nc_4_0_platform_declared", "verified", "policy-1",
                "internal_noncommercial_research", "EXTWIN chair-1 CC BY-NC 4.0",
                "ready", 1,
            )
            placeholders = ",".join("?" for _ in base)
            connection.execute(
                f"INSERT INTO objects VALUES ({placeholders})", base
            )
            blocked = list(base)
            blocked[0] = "chair-2"
            blocked[1] = "synthesis28:chair-2"
            blocked[19] = None
            blocked[21] = "pending_license"
            blocked[22] = 0
            connection.execute(
                f"INSERT INTO objects VALUES ({placeholders})", blocked
            )
            connection.commit()
            connection.close()

            adapters = [{
                "kind": "synthesis_object_library",
                "path": str(adapter),
                "allowed_use_scope": "internal_noncommercial_research",
            }]
            hits = search(
                primary,
                "office chair",
                canonical_class="furniture/seating/office_chair",
                read_only_adapters=adapters,
            )
            self.assertEqual(["synthesis28:chair-1"], [row["logical_id"] for row in hits])
            self.assertEqual("/entrypoints/chair.usda", hits[0]["path"])
            self.assertEqual(
                "internal_noncommercial_research",
                hits[0]["metadata"]["allowed_use_scope"],
            )
            self.assertIn("chair-1", hits[0]["metadata"]["attribution_text"])
            classes = taxonomy(
                primary,
                prefix="furniture/seating",
                callable_only=True,
                read_only_adapters=adapters,
            )
            self.assertEqual(1, classes["totals"]["callable_count"])
            self.assertEqual(
                "synthesis28_objects",
                classes["classes"][0]["provenance"][0]["source_library"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
