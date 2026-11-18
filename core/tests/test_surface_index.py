from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.surface_index import (  # noqa: E402
    HorizontalSurfaceIndex,
    _within_z_interest,
    refine_relations_with_surface_index,
)


def surface(owner: str, prim: str, z: float) -> dict:
    return {
        "z_m": z,
        "triangle_xy": [[0, 0], [1, 0], [1, 1]],
        "bounds_xy": [0, 0, 1, 1],
        "area_m2": 0.5,
        "owner_instance_id": owner,
        "owner_model_id": owner,
        "owner_semantic_name": owner,
        "owner_asset_scope": "independent_object",
        "owner_prim": f"/root/{owner}",
        "support_prim": prim,
    }


class SurfaceIndexTests(unittest.TestCase):
    def test_z_interest_filter_keeps_only_queryable_height_bands(self) -> None:
        self.assertTrue(_within_z_interest(0.81, [0.8, 1.4], 0.02))
        self.assertFalse(_within_z_interest(1.0, [0.8, 1.4], 0.02))
        self.assertTrue(_within_z_interest(100.0, [], 0.02))

    def test_query_finds_nearest_real_surface_and_excludes_self(self) -> None:
        index = HorizontalSurfaceIndex(
            [surface("rack", "/root/rack/shelf", 0.8), surface("box", "/root/box/bottom", 0.8)],
            cell_size_m=0.5,
            z_step_m=0.01,
        )
        matches = index.query(
            (0.5, 0.5),
            0.805,
            vertical_tolerance_m=0.02,
            exclude_owner_instance_id="box",
        )
        self.assertEqual("/root/rack/shelf", matches[0]["support_prim"])
        self.assertAlmostEqual(0.005, matches[0]["signed_gap_m"])

    def test_refinement_promotes_exact_mesh_support_target(self) -> None:
        payload = {
            "parameters": {"cell_size_m": 0.5, "z_step_m": 0.01},
            "surfaces": [
                surface("rack", "/root/rack/internal_shelf", 0.8),
                {
                    **surface("rack", "/root/rack/internal_shelf", 0.8),
                    "triangle_xy": [[0, 0], [1, 1], [0, 1]],
                },
            ],
            "contacts": {
                "box": {
                    "bottom_z_m": 0.8,
                    "points_xy": [[0.2, 0.2], [0.8, 0.8]],
                    "geometry_source": "bottom_mesh_vertices",
                }
            },
        }
        result = refine_relations_with_surface_index(
            [{"instance_id": "box", "relation": "unresolved", "regression_status": "pending"}],
            payload,
            minimum_mesh_coverage=0.95,
        )
        row = result["relations"][0]
        self.assertEqual("supported_by_mesh_surface", row["relation"])
        self.assertEqual("/root/rack/internal_shelf", row["support_target"])
        self.assertEqual(1.0, row["mesh_support_coverage"])
        self.assertEqual("pass", row["regression_status"])

    def test_multiple_mesh_prims_on_one_owner_share_coverage(self) -> None:
        left = surface("pallet", "/root/pallet/slat_a", 0.4)
        left["triangle_xy"] = [[0, 0], [0.5, 0], [0.5, 1]]
        left["bounds_xy"] = [0, 0, 0.5, 1]
        right = surface("pallet", "/root/pallet/slat_b", 0.4)
        right["triangle_xy"] = [[0.5, 0], [1, 0], [1, 1]]
        right["bounds_xy"] = [0.5, 0, 1, 1]
        payload = {
            "parameters": {"cell_size_m": 0.5, "z_step_m": 0.01},
            "surfaces": [left, right],
            "contacts": {
                "box": {
                    "bottom_z_m": 0.4,
                    "points_xy": [[0.25, 0.25], [0.75, 0.25]],
                }
            },
        }
        row = refine_relations_with_surface_index(
            [{"instance_id": "box", "relation": "unresolved", "regression_status": "pending"}],
            payload,
            minimum_mesh_coverage=0.95,
        )["relations"][0]
        self.assertEqual(1.0, row["mesh_support_coverage"])
        self.assertEqual("/root/pallet", row["support_target"])
        self.assertEqual(2, len(row["mesh_regression"]["support_mesh_prims"]))

    def test_strict_consensus_floor_contact_survives_missing_floor_mesh(self) -> None:
        payload = {"parameters": {"cell_size_m": 0.5, "z_step_m": 0.01}, "surfaces": [], "contacts": {}}
        row = refine_relations_with_surface_index(
            [{
                "instance_id": "cart",
                "relation": "supported_by_floor",
                "support_target": "floor_plane",
                "floor_z_m": 0.0,
                "signed_gap_m": 0.012,
                "regression_status": "pass",
            }],
            payload,
        )["relations"][0]
        self.assertEqual("grounded_on_floor_plane", row["relation"])
        self.assertEqual("floor", row["support_target"])
        self.assertEqual("pass", row["regression_status"])

    def test_ceiling_mount_is_verified_at_authored_anchor(self) -> None:
        payload = {
            "parameters": {"cell_size_m": 0.5, "z_step_m": 0.01},
            "surfaces": [surface("room", "/root/room/ceiling", 3.0)],
            "contacts": {},
        }
        row = refine_relations_with_surface_index(
            [{
                "instance_id": "lamp",
                "relation": "fixed_mount_candidate",
                "mount_anchor_prim": "/model_lamp",
                "mount_point_world_m": [0.5, 0.5, 3.0],
                "regression_status": "pending",
            }],
            payload,
        )["relations"][0]
        self.assertEqual("fixed_to_ceiling_mesh", row["relation"])
        self.assertEqual("/root/room/ceiling", row["support_target"])
        self.assertEqual("pass", row["regression_status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
