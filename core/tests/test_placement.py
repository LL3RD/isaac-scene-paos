from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.placement import (  # noqa: E402
    apply_placement_solution,
    architectural_obstacle_root_for_spec,
    automatic_obstacles,
    build_support_dependency_graph,
    clear_pending_entity,
    derive_constraint_layout_spec,
    derive_semantic_layout_spec,
    inspect_functional_group_layout,
    is_architectural_prim_path,
    is_volumetric_aggregate_row,
    resolve_functional_group_preference,
    refresh_procedural_room_shell,
    select_architectural_obstacle_rows,
    solve_fixed_mount_translation,
)


class PlacementPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = {
            "schema_version": "isaac-scene-spec/v2",
            "support_colliders": [
                {"id": "floor", "prim": "/World/Room/Floor", "type": "box"},
            ],
            "entities": [
                {"id": "chair", "role": "furniture", "path": "/a/chair.usd", "support_target": "floor"},
                {"id": "table", "role": "furniture", "path": "/a/table.usd", "support_target": "floor"},
                {
                    "id": "monitor_a", "role": "prop", "path": "/a/monitor.usd",
                    "support_target": "/World/Objects/table/Top", "translate": [1, 2, 3],
                },
                {
                    "id": "monitor_b", "role": "prop", "path": "/a/monitor.usd",
                    "support_target": "/World/Objects/table/Top",
                },
            ],
        }

    def test_support_graph_orders_ground_then_support_then_props(self) -> None:
        graph = build_support_dependency_graph(self.spec)
        self.assertEqual("pass", graph["status"])
        self.assertLess(graph["order"].index("table"), graph["order"].index("chair"))
        self.assertLess(graph["order"].index("table"), graph["order"].index("monitor_a"))
        tiers = {row["entity_id"]: row["tier"] for row in graph["nodes"]}
        self.assertEqual("ground_object", tiers["chair"])
        self.assertEqual("support_furniture", tiers["table"])
        self.assertEqual("supported_prop", tiers["monitor_a"])

    def test_already_placed_peer_becomes_same_support_obstacle(self) -> None:
        graph = build_support_dependency_graph(self.spec)
        self.assertEqual(
            ["/World/Objects/monitor_a"],
            automatic_obstacles(graph, "monitor_b", ["chair", "table", "monitor_a"]),
        )

    def test_compound_support_obstacles_are_isolated_by_surface_level(self) -> None:
        spec = deepcopy(self.spec)
        spec["entities"][2]["layout_constraints"] = {"support_surface_z_m": 0.6}
        spec["entities"][3]["layout_constraints"] = {"support_surface_z_m": 1.2}
        graph = build_support_dependency_graph(spec)
        self.assertEqual([], automatic_obstacles(graph, "monitor_b", ["monitor_a"]))
        spec["entities"][3]["layout_constraints"] = {"support_surface_z_m": 0.6}
        graph = build_support_dependency_graph(spec)
        self.assertEqual(["/World/Objects/monitor_a"], automatic_obstacles(graph, "monitor_b", ["monitor_a"]))

    def test_architectural_obstacles_are_filtered_by_support_height_and_span(self) -> None:
        rows = [
            {"prim": "/Room/Floor", "min": [-4, -4, -0.1], "max": [4, 4, 0], "size": [8, 8, .1]},
            {"prim": "/Room/DecorativeFloor", "min": [-4, -4, 0], "max": [4, 4, .1], "size": [8, 8, .1]},
            {"prim": "/Room/RaisedFloor", "min": [-4, -4, .1], "max": [4, 4, .2], "size": [8, 8, .1]},
            {"prim": "/Room/Wall", "min": [-4, -4, 0], "max": [-3.9, 4, 3], "size": [.1, 8, 3]},
            {"prim": "/Room/Wall/Aggregate", "min": [-4, -4, 0], "max": [4, 4, 3], "size": [8, 8, 3]},
            {"prim": "/Room/Ceiling", "min": [-4, -4, 2.9], "max": [4, 4, 3], "size": [8, 8, .1]},
            {"prim": "/Room/Aggregate", "min": [-8, -8, 0], "max": [8, 8, 3], "size": [16, 16, 3]},
        ]
        chair = select_architectural_obstacle_rows(
            rows,
            object_z_min=0.0,
            object_z_max=1.2,
            support_prim_path="/Room/Floor",
        )
        self.assertEqual(["/Room/Wall"], chair["selected_prims"])
        self.assertEqual(1, chair["skipped"]["support"])
        self.assertEqual(2, chair["skipped"]["support_contact_layer"])
        self.assertEqual(1, chair["skipped"]["volumetric_aggregate"])
        self.assertEqual(1, chair["skipped"]["broad"])
        self.assertEqual(1, chair["skipped"]["outside_height_interval"])

        bookcase = select_architectural_obstacle_rows(
            rows,
            object_z_min=0.0,
            object_z_max=3.0,
            support_prim_path="/Room/Floor",
        )
        self.assertEqual(["/Room/Wall", "/Room/Ceiling"], bookcase["selected_prims"])

    def test_architectural_path_filter_keeps_shell_and_builtin_semantics(self) -> None:
        self.assertTrue(is_architectural_prim_path("/Room/E_wall_2/E_Component/P_mesh"))
        self.assertTrue(is_architectural_prim_path("/World/Architecture/RightBaseboard"))
        self.assertTrue(is_architectural_prim_path("/World/Architecture/LeftSkirting"))
        self.assertTrue(is_architectural_prim_path("/World/Architecture/LeftGlazing"))
        self.assertTrue(is_architectural_prim_path("/World/Architecture/LeftMullion03"))
        self.assertTrue(is_architectural_prim_path("/World/Architecture/BackTransom02"))
        self.assertTrue(is_architectural_prim_path("/Room/E_cabinet3/P_mesh"))
        self.assertFalse(is_architectural_prim_path("/Room/E_medical_cart/P_mesh"))
        self.assertTrue(is_architectural_prim_path("/Room/custom_guard/P_mesh", ["guard"]))
        self.assertTrue(is_volumetric_aggregate_row({"size": [6.9, 5.8, 2.9]}))
        self.assertFalse(is_volumetric_aggregate_row({"size": [.2, 7.0, 3.0]}))

    def test_procedural_room_uses_composed_architecture_as_obstacle_root(self) -> None:
        self.assertEqual(
            "/World/Architecture",
            architectural_obstacle_root_for_spec({"room": {"size_m": [6, 5, 3]}}),
        )
        self.assertEqual(
            "/World/Objects/Room",
            architectural_obstacle_root_for_spec({
                "entities": [{"id": "room", "asset_scope": "scene_root", "prim": "/World/Objects/Room"}],
            }),
        )

    def test_functional_members_reserve_space_before_ground_decor(self) -> None:
        spec = {
            "entities": [
                {"id": "desk", "support_target": "floor", "layout_role": "support_furniture", "asset_bounds_m": {"size": [2, 1, 1]}},
                {
                    "id": "chair", "support_target": "floor", "layout_role": "ground_object",
                    "asset_bounds_m": {"size": [.7, .7, 1]},
                    "layout_constraints": {"functional_group": {"id": "workstation", "anchor_id": "desk"}},
                },
                {"id": "plant", "support_target": "floor", "layout_role": "ground_object", "asset_bounds_m": {"size": [1, 1, 1]}},
            ],
        }
        graph = build_support_dependency_graph(spec)
        self.assertLess(graph["order"].index("desk"), graph["order"].index("chair"))
        self.assertLess(graph["order"].index("chair"), graph["order"].index("plant"))
        chair = next(row for row in graph["nodes"] if row["entity_id"] == "chair")
        self.assertEqual("functional_group_member", chair["tier"])
        self.assertEqual(["desk"], chair["depends_on"])

    def test_functional_members_follow_declared_member_rule_order(self) -> None:
        spec = {
            "entities": [
                {
                    "id": "robot", "support_target": "floor",
                    "layout_role": "ground_object",
                    "asset_bounds_m": {"size": [1, 1, 2]},
                },
                {
                    "id": "large_console", "support_target": "floor",
                    "layout_role": "ground_object",
                    "asset_bounds_m": {"size": [4, 2, 2]},
                    "layout_constraints": {"functional_group": {
                        "id": "guarded_cell", "anchor_id": "robot",
                        "member_rule_index": 1,
                    }},
                },
                {
                    "id": "small_fence", "support_target": "floor",
                    "layout_role": "ground_object",
                    "asset_bounds_m": {"size": [1, .05, 2]},
                    "layout_constraints": {"functional_group": {
                        "id": "guarded_cell", "anchor_id": "robot",
                        "member_rule_index": 0,
                    }},
                },
            ],
        }
        graph = build_support_dependency_graph(spec)
        self.assertLess(
            graph["order"].index("small_fence"),
            graph["order"].index("large_console"),
        )

    def test_fixed_mount_is_a_dependency_but_not_a_surface_prop(self) -> None:
        spec = {
            "entities": [
                {"id": "room", "path": "/room.usd", "prim": "/World/Objects/room"},
                {
                    "id": "lamp",
                    "path": "/lamp.usd",
                    "mount_target": "/World/Objects/room/Ceiling",
                    "mount_anchor_prim": "/World/Objects/lamp/Anchor",
                    "mount_mode": "fixed",
                },
            ]
        }
        graph = build_support_dependency_graph(spec)
        self.assertEqual("pass", graph["status"])
        self.assertLess(graph["order"].index("room"), graph["order"].index("lamp"))
        lamp = next(row for row in graph["nodes"] if row["entity_id"] == "lamp")
        self.assertEqual("mounted_fixture", lamp["tier"])
        self.assertEqual("fixed_mount", lamp["support"]["relation_kind"])

    def test_solution_creates_new_spec_and_records_evidence(self) -> None:
        updated = apply_placement_solution(
            self.spec,
            "monitor_a",
            {
                "suggested_local_translation_delta": [0.25, -0.5, -0.1],
                "suggested_footprint_center_xy": [2.0, 2.5],
                "recommended_yaw_delta_deg": 90.0,
                "mesh_support_coverage": 1.0,
                "support_margin_coverage": 1.0,
            },
            support_prim="/World/Objects/table/Top",
            obstacle_prims=["/World/Objects/monitor_b"],
        )
        original = self.spec["entities"][2]
        solved = updated["entities"][2]
        self.assertEqual([1, 2, 3], original["translate"])
        self.assertEqual([1.25, 1.5, 2.9], solved["translate"])
        self.assertEqual([0.0, 0.0, 90.0], solved["rotate"])
        self.assertEqual(1.0, solved["placement_solver"]["mesh_support_coverage"])
        self.assertEqual([2.0, 2.5], solved["placement_solver"]["solved_footprint_center_xy"])

    def test_functional_group_inspection_uses_solved_visible_footprint_center(self) -> None:
        spec = {
            "entities": [
                {"id": "anchor", "translate": [1.0, 2.0, 0.0], "rotate": [0.0, 0.0, 0.0]},
                {
                    "id": "offset_pivot_member",
                    "translate": [3.5, 2.0, 0.0],
                    "rotate": [0.0, 0.0, 0.0],
                    "placement_solver": {"solved_footprint_center_xy": [3.0, 2.0]},
                    "layout_constraints": {"functional_group": {
                        "id": "fixture", "anchor_id": "anchor",
                        "offset_local_m": [2.0, 0.0], "yaw_offset_deg": 0.0,
                    }},
                },
            ]
        }
        result = inspect_functional_group_layout(spec, position_tolerance_m=0.01)
        self.assertEqual("pass", result["status"])
        self.assertEqual("placement_solver_visible_footprint_center", result["members"][0]["actual_xy_source"])

    def test_successful_placement_pending_id_filter_handles_strings_and_records(self) -> None:
        spec = {"layout_pending_entities": ["chair", {"id": "table", "reason": "retry"}, "lamp"]}
        updated = clear_pending_entity(spec, "table")
        self.assertEqual(["chair", "lamp"], updated["layout_pending_entities"])
        self.assertEqual(3, len(spec["layout_pending_entities"]))

    def test_cycle_is_explicit_failure(self) -> None:
        spec = {
            "entities": [
                {"id": "a", "role": "prop", "path": "/a.usd", "support_target": "b"},
                {"id": "b", "role": "prop", "path": "/b.usd", "support_target": "a"},
            ]
        }
        graph = build_support_dependency_graph(spec)
        self.assertEqual("fail", graph["status"])
        self.assertEqual(["a", "b"], graph["cycles"])

    def test_exact_spec_derives_coarse_constraint_input_without_pose_leak(self) -> None:
        bounds = {"min": [-3.0, -2.0, 0.0], "max": [3.0, 2.0, 3.0], "size": [6.0, 4.0, 3.0]}
        exact = {
            "schema_version": "isaac-scene-spec/v2",
            "domain": "demo",
            "room": {"type": "office", "quality_target": "synthesis28_reconstruction"},
            "entities": [
                {
                    "id": "room", "asset_scope": "scene_root", "prim": "/World/Objects/room",
                    "path": "/room.usd",
                    "xform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [10, 20, 0, 1]],
                    "source_world_matrix": [[1, 0, 0, 0]] * 4,
                    "regression_alignment": {"wrapper_local_bounds_m": bounds},
                    "regression_instance_opinions": {"source_layer": "/source.usd"},
                },
                {
                    "id": "table", "prim": "/World/Objects/table", "path": "/table.usd",
                    "support_target": "floor",
                    "xform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [12, 19, 0, 1]],
                    "regression_alignment": {"wrapper_local_bounds_m": {"min": [-1, -.5, 0], "max": [1, .5, 1], "size": [2, 1, 1]}},
                    "regression_instance_opinions": {"source_layer": "/source.usd"},
                },
                {
                    "id": "cup", "prim": "/World/Objects/cup", "path": "/cup.usd",
                    "support_target": "/World/Objects/table/Source/Top",
                    "xform_matrix": [[0, 1, 0, 0], [-1, 0, 0, 0], [0, 0, 1, 0], [12.1, 19.1, 1, 1]],
                    "regression_alignment": {"wrapper_local_bounds_m": {"min": [-.05, -.05, 0], "max": [.05, .05, .1], "size": [.1, .1, .1]}},
                },
                {
                    "id": "poster", "prim": "/World/Objects/poster", "path": "/poster.usd",
                    "support_target": "/World/Objects/room/Source",
                    "xform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [8, 20, 1, 1]],
                    "regression_alignment": {"wrapper_local_bounds_m": bounds},
                },
            ],
            "relations": [
                {"type": "grounded_on", "subject": "table", "target": "floor", "resolved_transform": [12, 19, 0]},
                {"type": "supported_by", "subject": "cup", "target": "/World/Objects/table/Source/Top", "resolved_transform": [12.1, 19.1, 1]},
            ],
            "reconstruction_provenance": {"scene_slug": "demo"},
            "reconstruction_report": {"status": "pass"},
        }
        derived, report = derive_constraint_layout_spec(exact, zone_columns=3, zone_rows=3)
        self.assertEqual("pass", report["status"])
        self.assertEqual(3, report["active_entity_count"])
        self.assertEqual(1, report["pending_entity_count"])
        self.assertEqual([], report["forbidden_fields_present"])
        self.assertNotIn("reconstruction_provenance", derived)
        self.assertEqual("poster", derived["layout_pending_entities"][0]["id"])
        self.assertEqual("architectural_mount_relation_required", derived["layout_pending_entities"][0]["reason"])
        self.assertEqual("/World/Colliders/ConstraintFloor", derived["support_colliders"][0]["prim"])
        by_id = {item["id"]: item for item in derived["entities"]}
        self.assertEqual("support_furniture", by_id["table"]["layout_role"])
        self.assertEqual("supported_prop", by_id["cup"]["layout_role"])
        self.assertEqual(90.0, by_id["cup"]["layout_constraints"]["yaw_bucket_deg"])
        self.assertNotIn("resolved_transform", derived["relations"][0])

    def test_declared_layout_roles_prioritize_large_support_before_ground_items(self) -> None:
        spec = {
            "entities": [
                {"id": "room", "prim": "/World/Objects/room", "path": "/room.usd"},
                {
                    "id": "plant", "prim": "/World/Objects/plant", "path": "/plant.usd",
                    "support_target": "/World/Objects/room/Source/Floor", "layout_role": "ground_object",
                },
                {
                    "id": "bookcase", "prim": "/World/Objects/bookcase", "path": "/bookcase.usd",
                    "support_target": "/World/Objects/room/Source/Floor", "layout_role": "support_furniture",
                },
                {
                    "id": "book", "prim": "/World/Objects/book", "path": "/book.usd",
                    "support_target": "/World/Objects/bookcase/Source/Shelf", "layout_role": "supported_prop",
                },
            ]
        }
        graph = build_support_dependency_graph(spec)
        self.assertEqual("pass", graph["status"])
        self.assertLess(graph["order"].index("bookcase"), graph["order"].index("plant"))
        self.assertLess(graph["order"].index("bookcase"), graph["order"].index("book"))

    def test_floor_underlay_precedes_furniture_but_is_not_an_obstacle(self) -> None:
        spec = {
            "entities": [
                {"id": "rug", "prim": "/World/Objects/rug", "path": "/rug.usd", "support_target": "floor", "layout_role": "floor_underlay", "placement_policy": "floor_underlay"},
                {"id": "sofa", "prim": "/World/Objects/sofa", "path": "/sofa.usd", "support_target": "floor", "layout_role": "ground_object"},
            ],
            "support_colliders": [{"id": "room_floor", "prim": "/World/Floor"}],
        }
        graph = build_support_dependency_graph(spec)
        self.assertEqual(["rug", "sofa"], graph["order"])
        self.assertEqual([], automatic_obstacles(graph, "sofa", ["rug"]))

    def test_semantic_layout_uses_functional_zones_and_ignores_prior_pose(self) -> None:
        inventory = {
            "schema_version": "isaac-scene-spec/v2",
            "room": {
                "type": "office",
                "usable_bounds_m": {"min": [-4, -3, 0], "max": [4, 3, 3], "size": [8, 6, 3]},
            },
            "entities": [
                {
                    "id": "room", "asset_scope": "scene_root", "path": "/room.usd",
                    "asset_bounds_m": {"min": [-4, -3, 0], "max": [4, 3, 3], "size": [8, 6, 3]},
                    "translate": [99, 98, 97], "rotate": [1, 2, 3],
                },
                {
                    "id": "desk", "semantic_name": "workbench", "path": "/desk.usd",
                    "support_target": "floor", "asset_bounds_m": {"size": [2, 1, 1]},
                    "translate": [50, 50, 50], "rotate": [0, 0, 173],
                    "layout_constraints": {"zone": {"id": "oracle"}, "preferred_xy": [50, 50]},
                },
                {
                    "id": "monitor", "semantic_name": "computer", "path": "/monitor.usd",
                    "support_target": "/World/Objects/desk/Top", "asset_bounds_m": {"size": [.5, .2, .4]},
                },
                {
                    "id": "plant", "semantic_name": "potted plant", "path": "/plant.usd",
                    "support_target": "floor", "asset_bounds_m": {"size": [.4, .4, 1.2]},
                },
                {
                    "id": "door", "semantic_name": "door", "path": "/door.usd",
                    "support_target": "/World/Objects/room/Source", "asset_bounds_m": {"size": [1, .1, 2]},
                },
            ],
            "relations": [{"type": "on", "subject": "monitor", "target": "/World/Objects/desk/Top", "resolved_transform": [4, 5, 6]}],
        }
        brief = {
            "schema_version": "isaac-scene-brief/v1",
            "inputs": {"mode": "text", "text": "office with work rows and plants at the perimeter"},
            "domain": "generic_scene", "quality_policy": "realistic",
            "room": {"type": "office", "size_m": [8, 6, 3]}, "objects": [],
        }
        profile = {
            "default_zone": "interior",
            "default_zones": [
                {"id": "work", "bounds_norm": [.25, .25, .75, .75], "distribution": "center"},
                {"id": "perimeter", "bounds_norm": [.1, .1, .9, .9], "distribution": "perimeter"},
                {"id": "interior", "bounds_norm": [.15, .15, .85, .85], "distribution": "grid"},
            ],
            "rules": [
                {
                    "id": "desk_rule", "match": {"semantic_terms": ["workbench"]},
                    "zone": "work", "distribution": "center", "preferred_yaw_deg": 0,
                    "minimum_mesh_coverage": 0.62,
                },
                {"id": "plant_rule", "match": {"semantic_terms": ["plant"]}, "zone": "perimeter", "distribution": "perimeter"},
            ],
        }
        derived, report = derive_semantic_layout_spec(inventory, brief, layout_profile=profile)
        self.assertEqual("pass", report["status"])
        self.assertEqual("none", report["pose_source"])
        by_id = {row["id"]: row for row in derived["entities"]}
        self.assertEqual("/World/Background", by_id["room"]["prim"])
        self.assertEqual("/World/Objects/desk", by_id["desk"]["prim"])
        self.assertEqual("/World/Objects/monitor", by_id["monitor"]["prim"])
        self.assertEqual([0.0, 0.0], by_id["desk"]["layout_constraints"]["preferred_xy"])
        self.assertEqual("work", by_id["desk"]["layout_constraints"]["zone"]["id"])
        self.assertEqual(0.62, by_id["desk"]["layout_constraints"]["minimum_mesh_coverage"])
        self.assertEqual("work", by_id["monitor"]["layout_constraints"]["zone"]["id"])
        self.assertEqual("inherit_support", by_id["monitor"]["layout_constraints"]["distribution"])
        self.assertEqual("perimeter", by_id["plant"]["layout_constraints"]["zone"]["id"])
        self.assertNotIn("door", by_id)
        self.assertEqual("architectural_mount_relation_required", derived["layout_pending_entities"][0]["reason"])
        self.assertNotIn("resolved_transform", derived["relations"][0])

        changed = deepcopy(inventory)
        changed["entities"][1]["translate"] = [-80, 20, 9]
        changed["entities"][1]["rotate"] = [0, 0, -91]
        changed["entities"][1]["layout_constraints"] = {"preferred_xy": [-80, 20]}
        derived_changed, _ = derive_semantic_layout_spec(changed, brief, layout_profile=profile)
        self.assertEqual(derived, derived_changed)

    def test_semantic_layout_rejects_exact_regression_fields(self) -> None:
        inventory = {
            "room": {"usable_bounds_m": {"min": [-1, -1, 0], "max": [1, 1, 2]}},
            "entities": [{"id": "room", "asset_scope": "scene_root", "xform_matrix": [[1, 0, 0, 0]] * 4}],
        }
        brief = {"room": {"size_m": [2, 2, 2]}}
        profile = {"default_zone": "all", "default_zones": [{"id": "all", "bounds_norm": [0, 0, 1, 1]}]}
        with self.assertRaisesRegex(ValueError, "privileged regression fields"):
            derive_semantic_layout_spec(inventory, brief, layout_profile=profile)

    def test_semantic_layout_materializes_brief_lighting(self) -> None:
        inventory = {
            "schema_version": "isaac-scene-spec/v2",
            "room": {"usable_bounds_m": {"min": [-2, -2, 0], "max": [2, 2, 3]}},
            "entities": [
                {"id": "table", "semantic_name": "table", "path": "/table.usd", "support_target": "floor"},
            ],
        }
        brief = {
            "schema_version": "isaac-scene-brief/v1",
            "inputs": {"mode": "text", "text": "bright room"},
            "domain": "generic_scene",
            "room": {"type": "room", "size_m": [4, 4, 3]},
            "objects": [],
            "lighting": {
                "mode": "text_guided",
                "environment": {"enabled": True, "intensity": 800, "color": [0.8, 0.9, 1.0]},
                "fixtures": [{"id": "key", "type": "rect", "translate": [0, 0, 2.5], "intensity": 1600}],
            },
        }
        profile = {"default_zone": "all", "default_zones": [{"id": "all", "bounds_norm": [0, 0, 1, 1]}]}
        derived, _ = derive_semantic_layout_spec(inventory, brief, layout_profile=profile)
        self.assertEqual("text_guided", derived["lighting_policy"]["mode"])
        self.assertEqual(2, len(derived["lights"]))
        self.assertEqual("dome", derived["lights"][0]["type"])
        self.assertEqual("/World/Lights/Key", derived["lights"][1]["prim"])

    def test_semantic_layout_keeps_rotated_asset_footprint_inside_room(self) -> None:
        inventory = {
            "schema_version": "isaac-scene-spec/v2",
            "room": {"usable_bounds_m": {"min": [-4, -3, 0], "max": [4, 3, 3]}},
            "entities": [{
                "id": "cabinet", "semantic_name": "base cabinet", "path": "/cabinet.usd",
                "support_target": "floor", "asset_bounds_m": {"size": [0.7, 0.8, 0.9]},
            }],
        }
        brief = {
            "schema_version": "isaac-scene-brief/v1",
            "inputs": {"mode": "text", "text": "cabinet along the room edge"},
            "domain": "home_scene", "room": {"type": "kitchen", "size_m": [8, 6, 3]},
            "objects": [],
        }
        profile = {
            "default_zone": "edge",
            "default_zones": [{"id": "edge", "bounds_norm": [0.0, 0.0, 0.1, 0.1], "distribution": "center"}],
            "rules": [{
                "id": "edge_cabinet", "match": {"semantic_terms": ["cabinet"]},
                "zone": "edge", "distribution": "center", "preferred_yaw_deg": 90,
            }],
        }
        derived, _ = derive_semantic_layout_spec(inventory, brief, layout_profile=profile, room_margin_m=0.25)
        cabinet = next(row for row in derived["entities"] if row["id"] == "cabinet")
        # At 90 degrees the 0.7 x 0.8 footprint becomes 0.8 x 0.7.
        self.assertGreaterEqual(cabinet["translate"][0], -4 + 0.25 + 0.4)
        self.assertGreaterEqual(cabinet["translate"][1], -3 + 0.25 + 0.35)

    def test_runtime_functional_group_preference_reclamps_after_anchor_solve(self) -> None:
        anchor = {
            "id": "anchor", "translate": [0.0, -2.28, 0.0], "rotate": [0.0, 0.0, 0.0],
            "placement_solver": {"solved_footprint_center_xy": [0.0, -2.28]},
        }
        member = {
            "id": "member", "translate": [0.0, 0.0, 0.0], "rotate": [0.0, 0.0, 0.0],
            "asset_bounds_m": {"size": [0.668, 0.764, 0.786]},
            "layout_constraints": {"functional_group": {
                "anchor_id": "anchor", "offset_local_m": [0.0, -0.764],
                "yaw_offset_deg": 0.0, "rotate_with_anchor": True,
            }},
        }
        preference = resolve_functional_group_preference(
            member, {"anchor": anchor, "member": member},
            room_bounds={"min": [-4, -3, 0], "max": [4, 3, 3]}, room_margin_m=0.25,
        )
        self.assertIsNotNone(preference)
        self.assertAlmostEqual(-3 + 0.25 + 0.382, preference["preferred_xy"][1])

    def test_toward_anchor_preference_uses_verified_front_axis(self) -> None:
        anchor = {"id": "table", "translate": [0.0, 0.0, 0.0], "rotate": [0.0, 0.0, 0.0]}
        sofa = {
            "id": "sofa", "translate": [2.0, 0.0, 0.0], "rotate": [0.0, 0.0, 0.0],
            "front_axis": "+X",
            "layout_constraints": {"functional_group": {
                "anchor_id": "table", "offset_local_m": [2.0, 0.0],
                "yaw_offset_deg": 0.0, "rotate_with_anchor": True,
                "facing_policy": "toward_anchor",
            }},
        }
        preference = resolve_functional_group_preference(sofa, {"table": anchor, "sofa": sofa})
        self.assertIsNotNone(preference)
        self.assertAlmostEqual(-180.0, preference["desired_yaw_deg"])
        sofa["rotate"][2] = -180.0
        report = inspect_functional_group_layout({"entities": [anchor, sofa]})
        self.assertEqual("pass", report["status"])

    def test_wall_flush_priority_overrides_furniture_facing_target(self) -> None:
        inventory = {
            "schema_version": "isaac-scene-spec/v2",
            "room": {"usable_bounds_m": {"min": [-3, -2, 0], "max": [3, 2, 3]}},
            "entities": [
                {"id": "room", "asset_scope": "scene_root", "path": "/room.usd"},
                {
                    "id": "cabinet", "semantic_name": "white bookcase", "path": "/cabinet.usd",
                    "support_target": "floor", "front_axis": "+Y", "facing_target": "sofa",
                    "asset_bounds_m": {"size": [1.2, .32, 1.9]},
                },
                {"id": "sofa", "semantic_name": "sofa", "path": "/sofa.usd", "support_target": "floor", "asset_bounds_m": {"size": [2, 1, 1]}},
            ],
        }
        brief = {
            "schema_version": "isaac-scene-brief/v1", "inputs": {"mode": "text", "text": "living room"},
            "domain": "home_scene", "room": {"type": "living_room", "size_m": [6, 4, 3]}, "objects": [],
            "zones": [{"id": "storage", "bounds_norm": [.2, .7, .8, .9]}],
            "layout_rules": [
                {"id": "face_sofa", "priority": 0, "match": {"semantic_terms": ["bookcase"]}, "zone": "storage"},
                {
                    "id": "flush_storage", "priority": 100, "match": {"semantic_terms": ["bookcase"]},
                    "zone": "storage", "orientation_priority": "wall_flush", "wall_affinity": "back",
                    "wall_clearance_m": .12,
                },
            ],
        }
        derived, _ = derive_semantic_layout_spec(inventory, brief)
        cabinet = next(row for row in derived["entities"] if row["id"] == "cabinet")
        self.assertEqual(-180.0, cabinet["rotate"][2])
        self.assertAlmostEqual(1.72, cabinet["translate"][1])
        self.assertNotIn("facing_target", cabinet)
        self.assertEqual("sofa", cabinet["layout_constraints"]["suppressed_facing_target"])
        self.assertEqual([0.0], cabinet["layout_constraints"]["yaw_deltas_deg"])

    def test_functional_group_pairs_members_and_tracks_solved_anchor(self) -> None:
        inventory = {
            "schema_version": "isaac-scene-spec/v2",
            "room": {"usable_bounds_m": {"min": [-4, -3, 0], "max": [4, 3, 3]}},
            "entities": [
                {"id": "room", "asset_scope": "scene_root", "path": "/room.usd"},
                {
                    "id": "desk", "semantic_name": "workbench", "path": "/desk.usd",
                    "support_target": "floor", "asset_bounds_m": {"size": [2, 1, 1]},
                },
                {
                    "id": "chair_a", "semantic_name": "office chair", "path": "/chair.usd",
                    "support_target": "floor", "front_axis": "-Y", "asset_bounds_m": {"size": [.8, .8, 1]},
                },
                {
                    "id": "chair_b", "semantic_name": "task chair", "path": "/chair.usd",
                    "support_target": "floor", "asset_bounds_m": {"size": [.8, .8, 1]},
                },
            ],
        }
        brief = {
            "schema_version": "isaac-scene-brief/v1", "inputs": {"mode": "text", "text": "office"},
            "domain": "generic_scene", "quality_policy": "realistic",
            "room": {"type": "office", "size_m": [8, 6, 3]}, "objects": [],
        }
        profile = {
            "default_zone": "all",
            "default_zones": [{"id": "all", "bounds_norm": [.2, .2, .8, .8], "distribution": "center"}],
            "rules": [{"id": "desk", "match": {"semantic_terms": ["workbench"]}, "zone": "all"}],
            "functional_groups": [{
                "id": "workstation",
                "anchor_match": {"semantic_terms": ["workbench"]},
                "members": [{
                    "match": {"semantic_terms": ["chair"]}, "per_anchor": 2,
                    "offsets_m": [[0, -1.2], [0, 1.2]], "yaw_offsets_deg": [180, 0],
                    "facing_policy": "toward_anchor",
                }],
            }],
        }
        derived, report = derive_semantic_layout_spec(inventory, brief, layout_profile=profile)
        self.assertEqual(2, report["functional_group_member_count"])
        self.assertEqual(1, report["functional_group_orientation_pending_count"])
        by_id = {row["id"]: row for row in derived["entities"]}
        self.assertEqual([0.0, -1.2], by_id["chair_a"]["layout_constraints"]["preferred_xy"])
        self.assertEqual("desk", by_id["chair_a"]["facing_target"])
        self.assertNotIn("facing_target", by_id["chair_b"])
        self.assertEqual("pending_front_axis", by_id["chair_b"]["layout_constraints"]["functional_group"]["orientation_status"])

        by_id["desk"]["translate"] = [1.0, 2.0, 0.0]
        by_id["desk"]["rotate"] = [0.0, 0.0, 90.0]
        preference = resolve_functional_group_preference(by_id["chair_a"], by_id)
        self.assertIsNotNone(preference)
        self.assertAlmostEqual(2.2, preference["preferred_xy"][0])
        self.assertAlmostEqual(2.0, preference["preferred_xy"][1])
        self.assertEqual([90.0], preference["yaw_deltas_deg"])
        by_id["chair_a"]["translate"] = [2.2, 2.0, 0.0]
        by_id["chair_a"]["rotate"] = [0.0, 0.0, 270.0]
        by_id["chair_b"]["translate"] = [-0.2, 2.0, 0.0]
        by_id["chair_b"]["rotate"] = [0.0, 0.0, 90.0]
        inspection = inspect_functional_group_layout(derived, position_tolerance_m=0.01)
        self.assertEqual("pass", inspection["status"])
        self.assertEqual(2, inspection["passed_member_count"])

    def test_atomic_functional_group_connection_ports_define_member_pose(self) -> None:
        inventory = {
            "schema_version": "isaac-scene-spec/v2",
            "room": {"usable_bounds_m": {"min": [-5, -4, 0], "max": [5, 4, 3]}},
            "entities": [
                {"id": "line", "semantic_name": "assembly line", "path": "/line.usd", "support_target": "floor", "asset_bounds_m": {"size": [4, 1, 1]}},
                {"id": "conveyor", "semantic_name": "short conveyor", "path": "/conveyor.usd", "support_target": "floor", "asset_bounds_m": {"size": [2, 1, .6]}},
            ],
        }
        brief = {
            "schema_version": "isaac-scene-brief/v1", "inputs": {"mode": "text", "text": "connected line"},
            "domain": "industrial_assembly", "room": {"type": "factory", "size_m": [10, 8, 3]}, "objects": [],
            "zones": [{"id": "all", "bounds_norm": [.1, .1, .9, .9], "distribution": "center"}],
            "default_zone": "all", "layout_rules": [],
            "functional_groups": [{
                "id": "flow", "atomic": True, "anchor_match": {"entity_ids": ["line"]},
                "members": [{
                    "match": {"entity_ids": ["conveyor"]}, "per_anchor": 1, "offsets_m": [[99, 99]],
                    "yaw_offsets_deg": [0], "position_tolerance_m": .12,
                    "connection": {
                        "anchor_port_local_m": [2.0, 0.0, .5],
                        "member_port_local_m": [-1.0, 0.0, .5],
                        "position_tolerance_m": .05,
                    },
                }],
            }],
        }
        derived, _ = derive_semantic_layout_spec(inventory, brief)
        by_id = {row["id"]: row for row in derived["entities"]}
        group = by_id["conveyor"]["layout_constraints"]["functional_group"]
        self.assertTrue(group["atomic"])
        self.assertEqual([3.0, 0.0], group["offset_local_m"])
        self.assertEqual(.12, resolve_functional_group_preference(by_id["conveyor"], by_id)["position_tolerance_m"])
        by_id["conveyor"]["translate"] = [by_id["line"]["translate"][0] + 3.0, 0.0, 0.0]
        result = inspect_functional_group_layout(derived, position_tolerance_m=.01)
        self.assertEqual("pass", result["status"])
        self.assertAlmostEqual(0.0, result["members"][0]["connection_error_m"])

    def test_semantic_connection_rejects_coincident_ports_with_wrong_normals(self) -> None:
        inventory = {
            "schema_version": "isaac-scene-spec/v2",
            "room": {"usable_bounds_m": {"min": [-5, -4, 0], "max": [5, 4, 3]}},
            "entities": [
                {"id": "aoi", "semantic_name": "AOI", "path": "/aoi.usd", "support_target": "floor", "asset_bounds_m": {"size": [1, 1, 1]}},
                {"id": "line", "semantic_name": "assembly line", "path": "/line.usd", "support_target": "floor", "asset_bounds_m": {"size": [2, 1, 1]}},
            ],
        }
        brief = {
            "schema_version": "isaac-scene-brief/v1", "inputs": {"mode": "text", "text": "connected AOI"},
            "domain": "industrial_assembly", "room": {"type": "factory", "size_m": [10, 8, 3]}, "objects": [],
            "zones": [{"id": "all", "bounds_norm": [.1, .1, .9, .9], "distribution": "center"}],
            "default_zone": "all", "layout_rules": [],
            "functional_groups": [{
                "id": "process", "atomic": True, "anchor_match": {"entity_ids": ["aoi"]},
                "members": [{
                    "match": {"entity_ids": ["line"]}, "per_anchor": 1,
                    "offsets_m": [[99, 99]], "yaw_offsets_deg": [0],
                    "connection": {
                        "anchor_port_local_m": [.5, 0, .5],
                        "member_port_local_m": [-1, 0, .5],
                        "require_semantic_evidence": True,
                        "require_opposed_normals": True,
                        "anchor_port": {
                            "id": "aoi_output", "role": "output", "normal_axis": "+X",
                            "evidence_status": "verified", "evidence_source": "source audit",
                        },
                        "member_port": {
                            "id": "line_input", "role": "input", "normal_axis": "+X",
                            "evidence_status": "verified", "evidence_source": "asset audit",
                        },
                    },
                }],
            }],
        }
        derived, _ = derive_semantic_layout_spec(inventory, brief)
        inspection = inspect_functional_group_layout(derived, position_tolerance_m=.01)
        self.assertEqual("pass", inspection["members"][0]["connection_position_status"])
        self.assertEqual("verified", inspection["members"][0]["semantic_port_status"])
        self.assertEqual("fail", inspection["members"][0]["port_normal_status"])
        self.assertEqual("fail", inspection["status"])

        line = next(row for row in derived["entities"] if row["id"] == "line")
        line["layout_constraints"]["functional_group"]["connection"]["member_port"]["normal_axis"] = "-X"
        inspection = inspect_functional_group_layout(derived, position_tolerance_m=.01)
        self.assertEqual("pass", inspection["members"][0]["port_normal_status"])
        self.assertEqual("pass", inspection["status"])

    def test_connection_ports_compensate_measured_placement_references(self) -> None:
        inventory = {
            "schema_version": "isaac-scene-spec/v2",
            "room": {"usable_bounds_m": {"min": [-5, -4, 0], "max": [5, 4, 3]}},
            "entities": [
                {
                    "id": "anchor", "semantic_name": "anchor conveyor", "path": "/anchor.usd",
                    "support_target": "floor", "asset_bounds_m": {"size": [2, 1, 1]},
                    "asset_local_geometry": {"bottom_contact_center_m": [0.1, 0.2, 0.0]},
                },
                {
                    "id": "member", "semantic_name": "offset machine", "path": "/member.usd",
                    "support_target": "floor", "asset_bounds_m": {"size": [2, 1, 1]},
                    "asset_local_geometry": {"bottom_contact_center_m": [-0.3, 0.4, 0.0]},
                },
            ],
        }
        brief = {
            "schema_version": "isaac-scene-brief/v1", "inputs": {"mode": "text", "text": "connected"},
            "domain": "industrial_assembly", "room": {"type": "factory", "size_m": [10, 8, 3]}, "objects": [],
            "zones": [{"id": "all", "bounds_norm": [.1, .1, .9, .9], "distribution": "center"}],
            "default_zone": "all", "layout_rules": [],
            "functional_groups": [{
                "id": "ports", "atomic": True, "anchor_match": {"entity_ids": ["anchor"]},
                "members": [{
                    "match": {"entity_ids": ["member"]}, "per_anchor": 1,
                    "offsets_m": [[99, 99]], "yaw_offsets_deg": [0],
                    "connection": {
                        "anchor_port_local_m": [1.0, 0.0, .5],
                        "member_port_local_m": [-1.0, 0.0, .5],
                    },
                }],
            }],
        }
        derived, _ = derive_semantic_layout_spec(inventory, brief)
        by_id = {row["id"]: row for row in derived["entities"]}
        group = by_id["member"]["layout_constraints"]["functional_group"]
        self.assertAlmostEqual(1.6, group["offset_local_m"][0])
        self.assertAlmostEqual(0.2, group["offset_local_m"][1])
        by_id["anchor"]["translate"] = [-0.1, -0.2, 0.0]
        by_id["member"]["translate"] = [1.9, -0.2, 0.0]
        by_id["anchor"]["placement_solver"] = {"solved_footprint_center_xy": [0.0, 0.0]}
        by_id["member"]["placement_solver"] = {"solved_footprint_center_xy": [1.6, 0.2]}
        result = inspect_functional_group_layout(derived, position_tolerance_m=.01)
        self.assertEqual("pass", result["status"])
        self.assertAlmostEqual(0.0, result["members"][0]["connection_error_m"])

    def test_asset_origin_offsets_compile_to_placement_reference_offsets(self) -> None:
        inventory = {
            "schema_version": "isaac-scene-spec/v2",
            "room": {"usable_bounds_m": {"min": [-5, -4, 0], "max": [5, 4, 3]}},
            "entities": [
                {
                    "id": "conveyor", "semantic_name": "conveyor", "path": "/conveyor.usd",
                    "support_target": "floor", "asset_bounds_m": {"size": [2, 1, 1]},
                    "asset_local_geometry": {"bottom_contact_center_m": [-0.01885, 0.000349, 0.0]},
                },
                {
                    "id": "robot", "semantic_name": "robot", "path": "/robot.usd",
                    "support_target": "floor", "asset_bounds_m": {"size": [.6, 1.6, 1.7]},
                    "asset_local_geometry": {"bottom_contact_center_m": [-0.027765, -0.37825, 0.0]},
                },
            ],
        }
        brief = {
            "schema_version": "isaac-scene-brief/v1", "inputs": {"mode": "text", "text": "robot service"},
            "domain": "industrial_assembly", "room": {"type": "factory", "size_m": [10, 8, 3]}, "objects": [],
            "zones": [{"id": "all", "bounds_norm": [.1, .1, .9, .9], "distribution": "center"}],
            "default_zone": "all", "layout_rules": [],
            "functional_groups": [{
                "id": "service", "atomic": True, "anchor_match": {"entity_ids": ["conveyor"]},
                "members": [{
                    "match": {"entity_ids": ["robot"]}, "per_anchor": 1,
                    "offsets_m": [[0.0, 1.31]], "offset_reference": "asset_origin",
                    "yaw_offsets_deg": [0], "work_envelope": {"radius_m": 1.45, "target_offset_local_m": [0, 0, 0]},
                }],
            }],
        }
        derived, _ = derive_semantic_layout_spec(inventory, brief)
        robot = next(row for row in derived["entities"] if row["id"] == "robot")
        group = robot["layout_constraints"]["functional_group"]
        self.assertAlmostEqual(-0.008915, group["offset_local_m"][0], places=6)
        self.assertAlmostEqual(0.931401, group["offset_local_m"][1], places=6)
        self.assertEqual("asset_origin", group["offset_reference"])

    def test_functional_group_members_do_not_displace_anchor_zone_slot(self) -> None:
        inventory = {
            "schema_version": "isaac-scene-spec/v2",
            "room": {"usable_bounds_m": {"min": [-5, -4, 0], "max": [5, 4, 3]}},
            "entities": [
                {"id": "center", "semantic_name": "center conveyor", "path": "/center.usd", "support_target": "floor", "asset_bounds_m": {"size": [2, 1, .6]}},
                {"id": "left", "semantic_name": "left conveyor", "path": "/left.usd", "support_target": "floor", "asset_bounds_m": {"size": [2, 1, .6]}},
                {"id": "right", "semantic_name": "right conveyor", "path": "/right.usd", "support_target": "floor", "asset_bounds_m": {"size": [2, 1, .6]}},
            ],
        }
        brief = {
            "schema_version": "isaac-scene-brief/v1", "inputs": {"mode": "text", "text": "connected conveyor line"},
            "domain": "industrial_assembly", "room": {"type": "factory", "size_m": [10, 8, 3]}, "objects": [],
            "zones": [{"id": "line", "bounds_norm": [.1, .4, .9, .6], "distribution": "grid"}],
            "default_zone": "line",
            "layout_rules": [{"id": "line_rule", "match": {"semantic_terms": ["conveyor"]}, "zone": "line", "distribution": "grid"}],
            "functional_groups": [{
                "id": "flow", "atomic": True, "anchor_match": {"entity_ids": ["center"]},
                "members": [{
                    "match": {"entity_ids": ["left", "right"]}, "per_anchor": 2,
                    "offsets_m": [[-2, 0], [2, 0]], "yaw_offsets_deg": [0, 0],
                }],
            }],
        }
        derived, _ = derive_semantic_layout_spec(inventory, brief)
        by_id = {row["id"]: row for row in derived["entities"]}
        self.assertEqual([0.0, 0.0], by_id["center"]["layout_constraints"]["preferred_xy"])
        self.assertEqual([-2.0, 0.0], by_id["left"]["layout_constraints"]["preferred_xy"])
        self.assertEqual([2.0, 0.0], by_id["right"]["layout_constraints"]["preferred_xy"])

    def test_supported_prop_can_use_anchor_slot_and_support_dependency(self) -> None:
        inventory = {
            "schema_version": "isaac-scene-spec/v2",
            "room": {"usable_bounds_m": {"min": [-4, -3, 0], "max": [4, 3, 3]}},
            "entities": [
                {
                    "id": "bench", "semantic_name": "manual workbench", "path": "/bench.usd",
                    "support_target": "floor", "asset_bounds_m": {"size": [2, 1, 1]},
                },
                {
                    "id": "computer", "semantic_name": "workbench computer", "path": "/computer.usd",
                    "support_target": "bench", "asset_bounds_m": {"size": [.5, .4, .5]},
                },
            ],
        }
        brief = {
            "schema_version": "isaac-scene-brief/v1", "inputs": {"mode": "text", "text": "equipped bench"},
            "domain": "industrial_assembly", "room": {"type": "factory", "size_m": [8, 6, 3]}, "objects": [],
            "zones": [{"id": "all", "bounds_norm": [.1, .1, .9, .9], "distribution": "center"}],
            "default_zone": "all", "layout_rules": [],
            "functional_groups": [{
                "id": "bench_detail", "anchor_match": {"entity_ids": ["bench"]},
                "members": [{
                    "match": {"entity_ids": ["computer"]}, "per_anchor": 1,
                    "offsets_m": [[0.25, 0.0]], "yaw_offsets_deg": [0],
                }],
            }],
        }
        derived, report = derive_semantic_layout_spec(inventory, brief)
        computer = next(row for row in derived["entities"] if row["id"] == "computer")
        self.assertEqual("supported_prop", computer["layout_role"])
        self.assertEqual("bench", computer["layout_constraints"]["functional_group"]["anchor_id"])
        self.assertEqual(0, report["functional_group_incomplete_count"])

    def test_refresh_procedural_shell_resizes_stale_floor(self) -> None:
        inventory = {
            "schema_version": "isaac-scene-spec/v2",
            "room": {"type": "factory", "size_m": [9, 7, 4.2], "shell_preset": "factory_daylight_v1"},
            "support_colliders": [{"id": "floor", "prim": "/World/Colliders/Floor", "size": [9, 7, .1]}],
            "entities": [],
        }
        brief = {"room": {"type": "factory", "size_m": [16, 11, 5.2], "shell_preset": "factory_daylight_v1"}}
        refreshed, report = refresh_procedural_room_shell(inventory, brief)
        floor = next(row for row in refreshed["support_colliders"] if row["id"] == "floor")
        self.assertEqual("pass", report["status"])
        self.assertEqual([16.0, 11.0], floor["size"][:2])
        self.assertEqual([16.0, 11.0, 5.2], refreshed["room"]["usable_bounds_m"]["size"])

    def test_ceiling_mount_aligns_anchor_to_target_bottom(self) -> None:
        solved = solve_fixed_mount_translation(
            anchor_world=[1.0, 2.0, 0.5],
            target_bounds={"min": [-2.0, -1.0, 3.0], "max": [2.0, 1.0, 3.1]},
            preferred_xy=[0.5, 0.25],
            mount_surface="ceiling",
        )
        self.assertEqual("pass", solved["status"])
        self.assertEqual([0.5, 0.25, 3.0], solved["target_point_world"])
        self.assertEqual([-0.5, -1.75, 2.5], solved["suggested_world_translation_delta"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
