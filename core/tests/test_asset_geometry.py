from __future__ import annotations

import math
from pathlib import Path
import sys
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.asset_entrypoint import inspect_asset_entrypoint
from isaac_scene.asset_geometry import summarize_local_geometry_points


class AssetGeometryTests(unittest.TestCase):
    def test_summary_preserves_pivot_offset_and_face_ranges(self) -> None:
        points = [
            [-2.0, -1.0, 0.0],
            [2.0, -1.0, 0.0],
            [-2.0, 3.0, 2.0],
            [2.0, 3.0, 2.0],
        ]
        result = summarize_local_geometry_points(points)
        self.assertEqual([-2.0, -1.0, 0.0], result["visible_bounds_m"]["min"])
        self.assertEqual([2.0, 3.0, 2.0], result["visible_bounds_m"]["max"])
        self.assertEqual([0.0, 1.0, 1.0], result["pivot_to_visible_center_m"])
        self.assertEqual(-2.0, result["candidate_attachment_faces"]["x_min"]["plane_m"])
        self.assertEqual([-1.0, 3.0], result["candidate_attachment_faces"]["x_min"]["span_m"]["y"])
        self.assertAlmostEqual(math.sqrt(13.0), result["maximum_horizontal_radius_from_pivot_m"])

    def test_summary_converts_authored_units_to_meters(self) -> None:
        result = summarize_local_geometry_points(
            [[-50, -25, 0], [50, 25, 100]],
            meters_per_unit=0.01,
        )
        self.assertEqual([1.0, 0.5, 1.0], result["visible_bounds_m"]["size"])
        self.assertEqual([0.0, 0.0, 0.5], result["visible_bounds_m"]["center"])

    def test_summary_rejects_empty_or_nonfinite_points(self) -> None:
        with self.assertRaises(ValueError):
            summarize_local_geometry_points([])
        with self.assertRaises(ValueError):
            summarize_local_geometry_points([[float("nan"), 0.0, 0.0]])

    def test_raw_y_up_layer_requires_sibling_alignment_wrapper(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "Aligned.usd"
            raw.write_bytes(b"raw")
            (root / "object_parameters.json").write_text(
                '{"upAxis": ["y"], "model_path": "objects/example/Aligned.usd"}',
                encoding="utf-8",
            )
            (root / "Aligned.usda").write_text(
                '#usda 1.0\ndef Xform "World" { quatf xformOp:orient = (1, 0, 0, 0) '
                'def Xform "entity" (prepend payload = @./Aligned.usd@) '
                '{ quatf xformOp:orient = (0.70711, 0.70711, 0, 0) } }',
                encoding="utf-8",
            )
            result = inspect_asset_entrypoint(raw)
            self.assertEqual("fail", result["status"])
            self.assertEqual("RAW_ASSET_REQUIRES_ALIGNMENT_WRAPPER", result["code"])
            self.assertEqual(str((root / "Aligned.usda").resolve()), result["recommended_path"])

    def test_selected_wrapper_passes(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            wrapper = Path(directory) / "Aligned.usda"
            wrapper.write_text('#usda 1.0\ndef Xform "World" {}', encoding="utf-8")
            result = inspect_asset_entrypoint(wrapper)
            self.assertEqual("pass", result["status"])


if __name__ == "__main__":
    unittest.main()
