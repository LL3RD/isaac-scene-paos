from __future__ import annotations

import sys
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError:  # local macOS system Python; Isaac Python provides NumPy
    np = None


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.camera_visibility import summarize_instance_visibility


@unittest.skipIf(np is None, "NumPy is unavailable in the local system Python")
class CameraVisibilityTests(unittest.TestCase):
    def test_mask_reports_pixels_crop_and_missing_anchor(self) -> None:
        mask = np.zeros((100, 120), dtype=np.uint32)
        mask[20:70, 10:50] = 2
        mask[45:100, 90:120] = 3
        result = summarize_instance_visibility(
            mask,
            {0: "BACKGROUND", 2: "/World/Objects/Desk", 3: "/World/Objects/Monitor"},
            [
                {"id": "desk", "prim": "/World/Objects/Desk", "criticality": "anchor"},
                {"id": "monitor", "prim": "/World/Objects/Monitor", "generation_layer": "baseline_required"},
                {"id": "cabinet", "prim": "/World/Objects/Cabinet", "criticality": "anchor"},
            ],
            minimum_recognizable_pixels=100,
        )
        rows = {row["id"]: row for row in result["entities"]}
        self.assertEqual(2000, rows["desk"]["visible_pixels"])
        self.assertIn("right", rows["monitor"]["touches_frame_edges"])
        self.assertIn("bottom", rows["monitor"]["touches_frame_edges"])
        self.assertEqual(0, rows["cabinet"]["visible_pixels"])
        self.assertLess(result["recognizable_anchor_fraction"], 1.0)
        self.assertEqual(1, result["anchor_edge_crop_count"])

    def test_dict_labels_and_string_ids_are_normalized(self) -> None:
        mask = np.full((20, 20), 7, dtype=np.uint32)
        result = summarize_instance_visibility(
            mask,
            {"7": {"scene_instance": "/World/Objects/Bed"}},
            [{"id": "bed", "prim": "/World/Objects/Bed", "criticality": "anchor"}],
            minimum_recognizable_pixels=1,
        )
        self.assertEqual(400, result["entities"][0]["visible_pixels"])
        self.assertEqual(1.0, result["recognizable_anchor_fraction"])


if __name__ == "__main__":
    unittest.main()
