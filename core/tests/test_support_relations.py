from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.support_relations import infer_scene_support_relations  # noqa: E402


def instance(instance_id: str, minimum: list[float], maximum: list[float]) -> dict:
    return {
        "instance_id": instance_id,
        "model_id": instance_id,
        "semantic_name": instance_id,
        "asset_scope": "independent_object",
        "source_prim_path": f"/root/{instance_id}",
        "bounds": {"min": minimum, "max": maximum},
        "origin_world_m": [
            (minimum[0] + maximum[0]) * 0.5,
            (minimum[1] + maximum[1]) * 0.5,
            minimum[2],
        ],
    }


class SupportRelationTests(unittest.TestCase):
    def test_extracts_floor_table_and_desktop_chain(self) -> None:
        rows = [
            instance("cabinet", [2.0, 0.0, 0.0], [3.0, 1.0, 1.2]),
            instance("table", [0.0, 0.0, 0.0], [1.5, 1.0, 0.8]),
            instance("monitor", [0.4, 0.2, 0.8], [1.0, 0.7, 1.3]),
        ]
        result = infer_scene_support_relations(rows, floor_tolerance_m=0.02)
        by_id = {row["instance_id"]: row for row in result["relations"]}
        self.assertEqual("supported_by_floor", by_id["table"]["relation"])
        self.assertEqual("supported_by_floor", by_id["cabinet"]["relation"])
        self.assertEqual("supported_by_instance", by_id["monitor"]["relation"])
        self.assertEqual("/root/table", by_id["monitor"]["support_target"])
        self.assertEqual("pass", by_id["monitor"]["regression_status"])

    def test_every_instance_receives_an_explicit_state(self) -> None:
        rows = [
            instance("floor_item", [0, 0, 0], [1, 1, 1]),
            instance("wall_picture", [5, 5, 2], [5.1, 5.1, 2.5]),
            {
                "instance_id": "missing",
                "model_id": "missing",
                "semantic_name": "missing",
                "asset_scope": "unclassified",
                "source_prim_path": "/root/missing",
                "bounds": None,
            },
        ]
        result = infer_scene_support_relations(rows, floor_tolerance_m=0.02)
        self.assertEqual(3, len(result["relations"]))
        by_id = {row["instance_id"]: row for row in result["relations"]}
        self.assertEqual("unresolved", by_id["wall_picture"]["relation"])
        self.assertEqual("fail", by_id["missing"]["regression_status"])

    def test_ceiling_fixture_requires_mesh_verification(self) -> None:
        floor = instance("floor_asset", [-2, -2, 0], [2, 2, 0.1])
        lamp = instance("lamp", [-0.5, -0.5, 1.0], [0.5, 0.5, 3.0])
        lamp["semantic_name"] = "surgical shadowless lamp"
        lamp["origin_world_m"] = [0.0, 0.0, 3.0]
        result = infer_scene_support_relations([floor, lamp], floor_tolerance_m=0.02)
        row = {item["instance_id"]: item for item in result["relations"]}["lamp"]
        self.assertEqual("fixed_mount_candidate", row["relation"])
        self.assertEqual("pending", row["regression_status"])
        self.assertEqual([0.0, 0.0, 3.0], row["mount_point_world_m"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
