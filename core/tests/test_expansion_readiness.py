from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.expansion_readiness import audit_catalog


def _asset(name: str, *, count: int, callable_: bool, size: list[float], support: str = "floor") -> dict:
    return {
        "model_id": name,
        "semantic_name": name,
        "canonical_class": "furniture/table",
        "asset_scope": "independent_object",
        "instance_count": count,
        "callable": callable_,
        "bbox_m": {"size": size},
        "support_mode": support if callable_ else None,
        "front_axis_status": "not_applicable" if callable_ else "not_run",
    }


class ExpansionReadinessTests(unittest.TestCase):
    def test_classifies_and_selects_diverse_canaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "kits.json"
            catalog.write_text(json.dumps({
                "schema_version": "isaac-synthesis28-scene-kit-catalog/v1",
                "scenes": [
                    {"slug": "meeting_room", "title": "Meeting", "domain": "generic_scene", "metrics": {},
                     "assets": [_asset("table", count=8, callable_=True, size=[2, 1, .8]), _asset("cup", count=2, callable_=False, size=[.1, .1, .2])]},
                    {"slug": "station", "title": "Station", "domain": "public_transit", "metrics": {},
                     "assets": [_asset("gate", count=2, callable_=True, size=[3, 1, 1.2]), _asset("scanner", count=8, callable_=False, size=[1, 1, 1])]},
                    {"slug": "empty", "title": "Empty", "domain": "generic_scene", "metrics": {},
                     "assets": [_asset("unknown", count=3, callable_=False, size=[2, 1, 1])]},
                ],
            }), encoding="utf-8")
            result = audit_catalog(catalog, root / "audit.json", maximum_canaries=3)
            classes = {row["slug"]: row["readiness_class"] for row in result["scenes"]}
            self.assertEqual("ready_canary", classes["meeting_room"])
            self.assertEqual("conditional_canary", classes["station"])
            self.assertEqual("object_catalog_blocked", classes["empty"])
            self.assertEqual(["meeting_room", "station"], [row["slug"] for row in result["selected_canaries"]])
            self.assertFalse(result["full_28_expansion_ready"])

    def test_blocks_multi_room_even_with_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "kits.json"
            catalog.write_text(json.dumps({
                "schema_version": "isaac-synthesis28-scene-kit-catalog/v1",
                "scenes": [{"slug": "two_bedroom_apartment", "domain": "home_scene", "metrics": {},
                            "assets": [_asset("bed", count=3, callable_=True, size=[2, 1.5, .5])]}],
            }), encoding="utf-8")
            result = audit_catalog(catalog, root / "audit.json")
            self.assertEqual("scale_or_topology_blocked", result["scenes"][0]["readiness_class"])
            self.assertFalse(result["selected_canaries"])

    def test_large_source_extent_does_not_block_compact_prompt_canary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "kits.json"
            catalog.write_text(json.dumps({
                "schema_version": "isaac-synthesis28-scene-kit-catalog/v1",
                "scenes": [{"slug": "subway_station", "domain": "public_transit",
                            "metrics": {"room_bbox_m": [120, 80, 8]},
                            "assets": [_asset("gate", count=8, callable_=True, size=[4, 2, 1.5])]}],
            }), encoding="utf-8")
            result = audit_catalog(catalog, root / "audit.json")
            row = result["scenes"][0]
            self.assertEqual("ready_canary", row["readiness_class"])
            self.assertEqual(["large_source_extent"], row["reference_scale_risks"])

    def test_explicit_domain_priority_selects_industrial_before_higher_scored_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "kits.json"
            catalog.write_text(json.dumps({
                "schema_version": "isaac-synthesis28-scene-kit-catalog/v1",
                "scenes": [
                    {"slug": "living", "domain": "home_scene", "metrics": {},
                     "assets": [_asset("sofa", count=10, callable_=True, size=[2, 1, 1])]},
                    {"slug": "factory", "domain": "industrial_assembly", "metrics": {},
                     "assets": [_asset("machine", count=6, callable_=True, size=[2, 1, 2])]},
                    {"slug": "warehouse", "domain": "warehouse_logistics", "metrics": {},
                     "assets": [_asset("rack", count=5, callable_=True, size=[2, 1, 2])]},
                ],
            }), encoding="utf-8")
            result = audit_catalog(
                catalog, root / "audit.json", maximum_canaries=3,
                preferred_domains=["industrial", "warehouse"],
            )
            self.assertEqual(
                ["factory", "warehouse", "living"],
                [row["slug"] for row in result["selected_canaries"]],
            )
            self.assertEqual(["industrial", "warehouse"], result["policy"]["preferred_domains"])

    def test_mass_repetition_can_become_bounded_compact_canary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "kits.json"
            catalog.write_text(json.dumps({
                "schema_version": "isaac-synthesis28-scene-kit-catalog/v1",
                "scenes": [{
                    "slug": "logistics_warehousing", "domain": "warehouse_logistics", "metrics": {},
                    "assets": [
                        _asset("pallet", count=40, callable_=True, size=[1.2, 1.0, .2]),
                        _asset("carton", count=100, callable_=False, size=[.4, .3, .2]),
                    ],
                }],
            }), encoding="utf-8")
            result = audit_catalog(catalog, root / "audit.json", preferred_domains=["warehouse"])
            row = result["scenes"][0]
            self.assertEqual("compact_canary", row["readiness_class"])
            self.assertEqual("logistics_warehousing", result["selected_canaries"][0]["slug"])
            self.assertEqual(
                "bounded_compact_prompt_variant_without_source_repetition",
                result["selected_canaries"][0]["policy"],
            )


if __name__ == "__main__":
    unittest.main()
