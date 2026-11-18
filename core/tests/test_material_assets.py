from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import scene_tools  # noqa: E402
from isaac_scene.material_quality import classify_material_signals, summarize_material_rows  # noqa: E402


class MaterialAssetTests(unittest.TestCase):
    def test_material_path_resolves_from_library_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            material = root / "materials" / "floor.usd"
            material.parent.mkdir()
            material.touch()
            config = {"libraries": [{"name": "test", "root": str(root)}]}
            resolved = scene_tools._material_asset_path(config, {
                "library": "test", "relative_path": "materials/floor.usd",
            })
        self.assertEqual(str(material.resolve()), resolved)

    def test_material_path_rejects_missing_asset(self) -> None:
        with self.assertRaises(SystemExit):
            scene_tools._material_asset_path(
                {"libraries": [{"name": "test", "root": "/does/not/exist"}]},
                {"library": "test", "relative_path": "floor.usd"},
            )

    def test_declared_texture_without_uv_or_projection_is_not_effective(self) -> None:
        result = classify_material_signals({
            "bound": True,
            "texture_assets": ["floor_albedo.png", "floor_normal.png"],
            "color_texture": True,
            "micro_texture": True,
            "has_st": False,
            "project_uvw": False,
        })
        self.assertEqual("texture_declared_unmapped", result["tier"])
        self.assertFalse(result["texture_mapping_effective"])

    def test_projection_promotes_texture_and_micro_surface(self) -> None:
        result = classify_material_signals({
            "bound": True,
            "texture_assets": ["floor_albedo.png", "floor_orm.png", "floor_normal.png"],
            "color_texture": True,
            "micro_texture": True,
            "has_st": False,
            "project_uvw": True,
        })
        self.assertEqual("textured_microdetail", result["tier"])
        self.assertTrue(result["micro_surface_effective"])

    def test_weighted_summary_prioritizes_large_constant_surface(self) -> None:
        result = summarize_material_rows([
            {
                "prim_path": "/World/Floor",
                "material_path": "/World/Looks/Flat",
                "role": "shell",
                "weight": 0.8,
                "tier": "constant_pbr",
                "quality_score": 0.36,
                "texture_mapping_effective": False,
                "micro_surface_effective": False,
            },
            {
                "prim_path": "/World/Forklift",
                "material_path": "/World/Looks/Forklift",
                "role": "object",
                "weight": 0.2,
                "tier": "textured_microdetail",
                "quality_score": 1.0,
                "texture_mapping_effective": True,
                "micro_surface_effective": True,
            },
        ])
        self.assertEqual("fail", result["status"])
        self.assertEqual("/World/Floor", result["priority_gaps"][0]["prim_path"])
        self.assertAlmostEqual(0.2, result["coverage_fractions"]["effective_texture"])


if __name__ == "__main__":
    unittest.main()
