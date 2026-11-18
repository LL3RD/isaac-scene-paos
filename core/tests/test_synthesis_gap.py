from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.synthesis_gap import audit_scene  # noqa: E402


class SynthesisGapTests(unittest.TestCase):
    def test_glassware_and_labware_share_one_audit_family(self) -> None:
        spec = {
            "domain": "laboratory",
            "room": {"type": "teaching_lab", "size_m": [8, 6, 3]},
            "entities": [
                {"id": "beaker", "canonical_class": "laboratory/glassware/beaker"},
                {"id": "bottle", "canonical_class": "laboratory/glassware/reagent_bottle"},
            ],
        }
        catalog = {"scenes": [{
            "slug": "laboratory", "domain": "laboratory", "room_types": ["teaching_lab"],
            "aliases": [], "metrics": {"instance_count": 2},
            "semantic_composition": [
                {"canonical_class": "laboratory/labware", "instance_count": 2},
            ],
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
            (root / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            result = audit_scene(root / "spec.json", root / "catalog.json", root / "out.json")
        row = next(
            item for item in result["class_gaps"]
            if item["canonical_class_prefix"] == "laboratory/labware"
        )
        self.assertEqual(2, row["generated_count"])
        self.assertEqual(1.0, row["saturation_coverage"])

    def test_books_notebooks_and_documents_share_small_prop_family(self) -> None:
        spec = {
            "domain": "home_scene",
            "room": {"type": "living_room", "size_m": [6, 5, 3]},
            "entities": [
                {"id": "book", "canonical_class": "household/book"},
                {"id": "notebook", "canonical_class": "office/notebook"},
                {"id": "document", "canonical_class": "office/document"},
            ],
        }
        catalog = {"scenes": [{
            "slug": "living_room", "domain": "home_scene", "room_types": ["living_room"],
            "aliases": [], "metrics": {"instance_count": 3},
            "semantic_composition": [{"canonical_class": "small_prop", "instance_count": 3}],
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
            (root / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            result = audit_scene(root / "spec.json", root / "catalog.json", root / "out.json")
        row = next(item for item in result["class_gaps"] if item["canonical_class_prefix"] == "small_prop")
        self.assertEqual(3, row["generated_count"])
        self.assertEqual(1.0, row["saturation_coverage"])

    def test_room_type_primary_match_beats_broad_domain_aliases(self) -> None:
        spec = {
            "domain": "home_scene",
            "room": {"type": "living_room", "size_m": [6, 5, 3]},
            "entities": [{"id": "sofa", "canonical_class": "furniture/seating/sofa"}],
        }
        catalog = {"scenes": [
            {
                "slug": "dining_room", "title": "Dining Room", "domain": "home_scene",
                "room_types": ["dining", "dining_room", "room"],
                "aliases": ["home", "living room"],
                "metrics": {"instance_count": 8}, "semantic_composition": [],
            },
            {
                "slug": "living_room", "title": "Living Room", "domain": "home_scene",
                "room_types": ["living", "living_room", "room"],
                "aliases": ["home", "living room"],
                "metrics": {"instance_count": 12}, "semantic_composition": [],
            },
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
            (root / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            result = audit_scene(root / "spec.json", root / "catalog.json", root / "out.json", match_count=1)
        self.assertEqual("living_room", result["matched_synthesis_scene_kits"][0]["slug"])

    def test_audit_is_pose_free_and_saturates_extreme_repetition(self) -> None:
        spec = {
            "domain": "warehouse_logistics",
            "room": {"type": "warehouse_packing_area", "size_m": [9, 7, 4]},
            "entities": [
                {"id": "table", "canonical_class": "furniture/surface/table", "translate": [1, 2, 3]},
                {"id": "box", "canonical_class": "logistics/container"},
            ],
            "lights": [],
        }
        catalog = {
            "scenes": [{
                "slug": "logistics_warehousing",
                "domain": "warehouse_logistics",
                "room_types": ["warehouse"],
                "aliases": ["warehouse packing"],
                "metrics": {"instance_count": 1409, "unique_model_count": 28, "prim_count": 10, "mesh_count": 9, "used_layer_count": 8},
                "semantic_composition": [
                    {"canonical_class": "logistics/container", "instance_count": 1194},
                    {"canonical_class": "furniture/storage/rack", "instance_count": 24},
                ],
                "source_world_matrix": [1] * 16,
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
            (root / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            result = audit_scene(root / "spec.json", root / "catalog.json", root / "out.json")
        encoded = json.dumps(result)
        self.assertEqual("none", result["pose_source"])
        self.assertNotIn("source_world_matrix", encoded)
        self.assertGreater(result["scores"]["density_coverage"], 0.0)
        self.assertLess(result["scores"]["density_coverage"], 1.0)
        self.assertTrue(any(row["kind"] == "detail_density" for row in result["repair_plan"]))

    def test_material_and_lighting_are_separate_repair_signals(self) -> None:
        spec = {
            "domain": "home_scene",
            "room": {"type": "living_room", "size_m": [6, 5, 3]},
            "entities": [{"id": "sofa", "canonical_class": "furniture/seating/sofa"}],
            "lights": [{"type": "dome"}, {"type": "rect"}, {"type": "rect"}],
            "lighting_policy": {"mode": "text_guided"},
        }
        catalog = {"scenes": [{
            "slug": "living_room", "domain": "home_scene", "room_types": ["living_room"], "aliases": [],
            "metrics": {"instance_count": 20, "unique_model_count": 10},
            "semantic_composition": [{"canonical_class": "furniture/seating/sofa", "instance_count": 2}],
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
            (root / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            result = audit_scene(root / "spec.json", root / "catalog.json", root / "out.json")
        kinds = {row["kind"] for row in result["repair_plan"]}
        self.assertIn("material_layering", kinds)
        self.assertNotIn("lighting", kinds)
