from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.appearance import apply_appearance  # noqa: E402


class AppearancePassTests(unittest.TestCase):
    def _spec(self, domain: str, room_type: str) -> dict:
        return {
            "domain": domain,
            "room": {"type": room_type, "size_m": [8, 6, 3]},
            "entities": [{"id": "desk", "translate": [1, 2, 0], "rotate": [0, 0, 90], "support_target": "floor"}],
            "support_colliders": [
                {"id": "floor", "translate": [0, 0, -0.05], "size": [8, 6, 0.1], "display_color": [0.5, 0.5, 0.5]},
                {"id": "back_wall", "translate": [0, 3, 1.5], "size": [8, 0.1, 3], "display_color": [0.8, 0.8, 0.8]},
            ],
        }

    def test_appearance_freezes_entity_and_shell_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._spec("laboratory", "teaching_laboratory")
            (root / "in.json").write_text(json.dumps(source), encoding="utf-8")
            report = apply_appearance(root / "in.json", root / "out.json")
            output = json.loads((root / "out.json").read_text())
        self.assertTrue(report["entity_transforms_frozen"])
        self.assertTrue(report["shell_geometry_frozen"])
        self.assertEqual(source["entities"], output["entities"])
        for before, after in zip(source["support_colliders"], output["support_colliders"]):
            self.assertEqual(before["translate"], after["translate"])
            self.assertEqual(before["size"], after["size"])
        self.assertEqual(4, len(output["appearance_overlays"]))
        self.assertEqual(4, len(output["lights"]))

    def test_industrial_and_transit_profiles_bind_real_material_assets(self) -> None:
        cases = [
            ("industrial_assembly", "auto_workshop", "service_floor"),
            ("public_transit", "subway_fare_zone", "terrazzo_floor"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, (domain, room_type, overlay_id) in enumerate(cases):
                source = root / f"in_{index}.json"
                output = root / f"out_{index}.json"
                source.write_text(json.dumps(self._spec(domain, room_type)), encoding="utf-8")
                report = apply_appearance(source, output)
                result = json.loads(output.read_text())
                overlays = {row["id"]: row for row in result["appearance_overlays"]}
                self.assertEqual("isaac_official", overlays[overlay_id]["material_asset"]["library"])
                self.assertTrue(overlays[overlay_id]["material_asset"].get("project_uvw", True))
                self.assertEqual([overlay_id], result["room"]["materials"]["material_asset_ids"])
                self.assertEqual(1, report["material_asset_count"])
                if domain == "industrial_assembly":
                    shell = {row["id"]: row for row in result["support_colliders"]}
                    self.assertTrue(shell["floor"]["material_asset"]["project_uvw"])
                    self.assertEqual([0.55, 0.55], shell["floor"]["material_asset"]["texture_scale"])
                    self.assertTrue(shell["back_wall"]["material_asset"]["project_uvw"])
                    self.assertEqual(2, report["surface_material_asset_count"])
                    lights = {row.get("id", "dome"): row for row in result["lights"]}
                    self.assertEqual(520.0, result["lights"][0]["intensity"])
                    self.assertEqual(900.0, lights["workshop_key_right"]["intensity"])

    def test_domain_exposure_is_applied_without_moving_camera(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._spec("generic_scene", "meeting_room")
            source["camera"] = {"translate": [4, -5, 2], "look_at": [0, 0, 1], "exposure_ev": -0.35}
            (root / "in.json").write_text(json.dumps(source), encoding="utf-8")
            apply_appearance(root / "in.json", root / "out.json")
            output = json.loads((root / "out.json").read_text())
        self.assertEqual([4, -5, 2], output["camera"]["translate"])
        self.assertEqual([0, 0, 1], output["camera"]["look_at"])
        self.assertEqual(0.35, output["camera"]["exposure_ev"])

    def test_auto_profile_routes_all_core_domains(self) -> None:
        cases = [
            ("generic_scene", "office", "office"),
            ("home_scene", "living_room", "living"),
            ("medical_clinical", "consulting_room", "clinical"),
            ("warehouse_logistics", "warehouse_packing_area", "warehouse"),
            ("industrial_assembly", "auto_workshop", "workshop"),
            ("public_transit", "subway_fare_zone", "transit"),
            ("generic_scene", "meeting_room", "office"),
            ("laboratory", "teaching_laboratory", "laboratory"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, (domain, room, expected) in enumerate(cases):
                source = root / f"in_{index}.json"
                output = root / f"out_{index}.json"
                source.write_text(json.dumps(self._spec(domain, room)), encoding="utf-8")
                self.assertEqual(expected, apply_appearance(source, output)["profile"])


if __name__ == "__main__":
    unittest.main()
