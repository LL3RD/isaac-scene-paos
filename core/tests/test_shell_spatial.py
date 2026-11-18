from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.shell_spatial import (  # noqa: E402
    _empty_bounds,
    _extend,
    _finish_bounds,
    _room_regions,
)


class ShellSpatialTests(unittest.TestCase):
    def test_world_point_bounds_are_reported_in_meters(self) -> None:
        bounds = _empty_bounds()
        _extend(bounds, (-2.0, 1.0, 0.1))
        _extend(bounds, (3.0, 5.0, 2.6))
        result = _finish_bounds(bounds)
        self.assertIsNotNone(result)
        self.assertEqual([-2.0, 1.0, 0.1], result["min_m"])
        self.assertEqual([3.0, 5.0, 2.6], result["max_m"])
        self.assertEqual([5.0, 4.0, 2.5], result["size_m"])

    def test_coincident_floor_layers_collapse_into_one_region(self) -> None:
        rows = [
            {"prim": "/Floor/A", "min_m": [0, 0, 0], "max_m": [4, 5, 0], "size_m": [4, 5, 0]},
            {"prim": "/Floor/B", "min_m": [0.001, 0.001, 0], "max_m": [4.001, 5.001, 0], "size_m": [4, 5, 0]},
            {"prim": "/Floor/C", "min_m": [6, 0, 0], "max_m": [8, 2, 0], "size_m": [2, 2, 0]},
        ]
        regions = _room_regions(rows, duplicate_iou_threshold=0.98, clearance_m=0.1)
        self.assertEqual(2, len(regions))
        self.assertEqual(2, regions[0]["coincident_layer_count"])
        self.assertAlmostEqual(3.8, regions[0]["clear_size_xy_m"][0])
        self.assertAlmostEqual(4.8, regions[0]["clear_size_xy_m"][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
