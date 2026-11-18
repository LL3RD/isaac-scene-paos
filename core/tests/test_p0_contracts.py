from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import scene_tools  # noqa: E402
from isaac_scene.contracts import envelope  # noqa: E402
from isaac_scene.domains import discover_domains, expand_query, route_domain  # noqa: E402
from isaac_scene.handoff import export_handoff, verify_handoff  # noqa: E402
from isaac_scene.orientation import facing_error_deg, required_yaw_deg  # noqa: E402
from isaac_scene.render_feedback import assess_metrics  # noqa: E402
from isaac_scene.scene_spec import normalize as normalize_scene_spec  # noqa: E402
from isaac_scene.support import compact_support_triangles, discover_horizontal_support_surfaces, mesh_bounds_from_world_meshes, select_support_surface_z, solve_mesh_support_placement  # noqa: E402
from isaac_scene.units import (  # noqa: E402
    UnitScaleMismatch,
    resolve_reference_scale,
    select_source_unit_contract,
)


class P0ContractTests(unittest.TestCase):
    def test_support_bounds_use_render_mesh_points_not_helper_bounds(self) -> None:
        meshes = [
            (
                [(1.0, 2.0, 0.8), (1.4, 2.0, 0.8), (1.4, 2.3, 1.2), (1.0, 2.3, 1.2)],
                [(0, 1, 2), (0, 2, 3)],
            )
        ]
        bounds = mesh_bounds_from_world_meshes(meshes)
        self.assertIsNotNone(bounds)
        self.assertEqual([1.0, 2.0, 0.8], bounds["min"])
        self.assertEqual([1.4, 2.3, 1.2], bounds["max"])
        self.assertAlmostEqual(0.4, bounds["size"][2])

    def test_domain_discovery_and_chinese_routing(self) -> None:
        names = {item["name"] for item in discover_domains(SKILL_ROOT)}
        self.assertTrue({
            "generic_scene",
            "home_scene",
            "industrial_assembly",
            "warehouse_logistics",
            "laboratory",
            "medical_clinical",
            "retail_store",
            "public_transit",
            "education_scene",
        } <= names)
        routed = route_domain(SKILL_ROOT, "生成一个仓库物流场景，加入托盘和搬运机器人")
        self.assertEqual("warehouse_logistics", routed["domain"])
        self.assertEqual("medical_clinical", route_domain(SKILL_ROOT, "生成一个现代手术室和麻醉机")["domain"])
        self.assertEqual("retail_store", route_domain(SKILL_ROOT, "生成便利店收银台和中岛货架")["domain"])
        self.assertEqual("public_transit", route_domain(SKILL_ROOT, "生成地铁站台屏蔽门")["domain"])
        self.assertEqual("education_scene", route_domain(SKILL_ROOT, "生成幼儿园教室")["domain"])
        expanded = expand_query(SKILL_ROOT, "轮式人形机器人")
        self.assertIn("g2 omnipicker", expanded["expanded"].lower())

    def test_result_envelope_contract(self) -> None:
        result = envelope("doctor", {"status": "pass"})
        required = {"schema_version", "ok", "status", "command", "run_id", "result", "warnings", "errors"}
        self.assertTrue(required <= set(result))
        self.assertEqual("external_harness", json.loads((SKILL_ROOT / "config" / "capabilities.json").read_text())["agent_loop_owner"])
        capabilities = json.loads((SKILL_ROOT / "config" / "capabilities.json").read_text())["capabilities"]
        capability_names = {item["name"] for item in capabilities}
        self.assertIn("inspect-asset-units", capability_names)
        self.assertIn("inspect-scene-overlaps", capability_names)
        self.assertIn("inspect-support-contact", capability_names)
        self.assertIn("solve-support-placement", capability_names)
        self.assertIn("derive-constraint-layout-spec", capability_names)
        self.assertIn("solve-scene-placement-batch", capability_names)
        self.assertIn("select-architectural-shell", capability_names)
        self.assertIn("inspect-architectural-shell-spatial", capability_names)
        self.assertIn("extract-synthesis-support-relations", capability_names)
        self.assertIn("refine-synthesis-support-relations", capability_names)
        self.assertIn("inspect-facing", capability_names)
        self.assertIn("inspect-render-feedback", capability_names)
        self.assertIn("inspect-reference-match", capability_names)
        self.assertIn("evaluate-scene-quality", capability_names)
        self.assertIn("compose-scene-summary", capability_names)

    def test_atomic_support_solver_respects_preferred_radius(self) -> None:
        result = solve_mesh_support_placement(
            object_contact_points=[(-.5, -.5), (-.5, .5), (.5, -.5), (.5, .5)],
            object_xy_points=[(-.5, -.5), (-.5, .5), (.5, -.5), (.5, .5)],
            support_triangles=[((-4, -4), (4, -4), (4, 4)), ((-4, -4), (4, 4), (-4, 4))],
            current_anchor_xy=(0, 0), preferred_xy=(3.8, 3.8), yaw_deltas_deg=[0],
            max_preferred_distance_m=.1,
        )
        self.assertEqual("fail", result["status"])

    def test_directional_contract_solves_and_checks_yaw(self) -> None:
        self.assertAlmostEqual(-90.0, required_yaw_deg([0, 0], [1, 0], "+Y"))
        self.assertAlmostEqual(90.0, required_yaw_deg([0, 0], [-1, 0], "+Y"))
        self.assertAlmostEqual(0.0, facing_error_deg([1, 0], [2, 0]))
        self.assertAlmostEqual(180.0, facing_error_deg([-1, 0], [2, 0]))

    def test_vertical_look_at_uses_non_collinear_up_axis(self) -> None:
        self.assertEqual(
            (0.0, 1.0, 0.0),
            scene_tools._look_at_up_axis([0, 0, 2.8], [0, 0, 0]),
        )
        self.assertEqual(
            (0.0, 0.0, 1.0),
            scene_tools._look_at_up_axis([0, -5, 2.8], [0, 0, 1.0]),
        )
        with self.assertRaises(ValueError):
            scene_tools._look_at_up_axis([1, 2, 3], [1, 2, 3])

    def test_render_feedback_detects_underexposure_without_touching_geometry(self) -> None:
        dark = assess_metrics({
            "mean_luminance": 0.14,
            "median_luminance": 0.06,
            "p05_luminance": 0.01,
            "p95_luminance": 0.55,
            "shadow_fraction": 0.57,
            "highlight_clip_fraction": 0.0,
            "channel_means": [0.15, 0.14, 0.14],
        })
        self.assertEqual("fail", dark["status"])
        self.assertIn("UNDEREXPOSED_RENDER", {row["code"] for row in dark["failures"]})
        self.assertGreater(dark["recommended_adjustments"]["exposure_delta_ev"], 1.0)
        self.assertTrue(dark["recommended_adjustments"]["preserve_geometry"])
        good = assess_metrics({
            "mean_luminance": 0.32,
            "median_luminance": 0.28,
            "p05_luminance": 0.10,
            "p95_luminance": 0.70,
            "shadow_fraction": 0.12,
            "highlight_clip_fraction": 0.005,
            "channel_means": [0.33, 0.32, 0.31],
        })
        self.assertEqual("pass", good["status"])

    def test_render_feedback_rejects_dark_subject_region_behind_bright_wall(self) -> None:
        misleading_global_average = assess_metrics({
            "mean_luminance": 0.21,
            "median_luminance": 0.23,
            "p05_luminance": 0.004,
            "p95_luminance": 0.39,
            "shadow_fraction": 0.29,
            "highlight_clip_fraction": 0.0,
            "channel_means": [0.21, 0.21, 0.20],
            "subject_region_mean_luminance": 0.15,
            "subject_region_shadow_fraction": 0.46,
        })
        self.assertEqual("fail", misleading_global_average["status"])
        self.assertIn(
            "SUBJECT_REGION_UNDEREXPOSED",
            {row["code"] for row in misleading_global_average["failures"]},
        )
        self.assertTrue(
            misleading_global_average["recommended_adjustments"]["subject_fill_required"]
        )

    def test_aabb_overlap_distinguishes_contact_from_penetration(self) -> None:
        table_leg = {"min": [0.9, 0.2, 0.0], "max": [1.1, 0.4, 0.9]}
        floor = {"min": [-10.0, -10.0, -0.3], "max": [10.0, 10.0, 0.0]}
        machine = {"min": [0.3, -0.1, 0.002], "max": [1.8, 3.9, 0.405]}
        self.assertIsNone(scene_tools._aabb_intersection(table_leg, floor, tolerance=0.005))
        overlap = scene_tools._aabb_intersection(table_leg, machine, tolerance=0.005)
        self.assertIsNotNone(overlap)
        self.assertGreater(overlap["size"][2], 0.4)

    def test_support_contact_reports_floating_and_penetrating(self) -> None:
        support = {"prim": "/World/Table", "min": [-1.0, -1.0, 0.0], "max": [1.0, 1.0, 1.0]}
        touching = {"prim": "/World/Robot", "min": [-0.2, -0.2, 1.002], "max": [0.2, 0.2, 2.0]}
        floating = {"prim": "/World/Robot", "min": [-0.2, -0.2, 1.02], "max": [0.2, 0.2, 2.0]}
        penetrating = {"prim": "/World/Robot", "min": [-0.2, -0.2, 0.98], "max": [0.2, 0.2, 2.0]}
        missed = {"prim": "/World/Robot", "min": [2.0, 2.0, 1.0], "max": [2.2, 2.2, 2.0]}
        self.assertEqual("pass", scene_tools._support_contact_from_rows(touching, support)["status"])
        self.assertEqual("floating_above_support", scene_tools._support_contact_from_rows(floating, support)["status"])
        self.assertEqual("penetrating_support", scene_tools._support_contact_from_rows(penetrating, support)["status"])
        self.assertEqual("support_xy_miss", scene_tools._support_contact_from_rows(missed, support)["status"])

    def test_mesh_support_coverage_preserves_a_support_hole(self) -> None:
        support_triangles = [
            ((0.0, 0.0), (0.4, 0.0), (0.4, 1.0)),
            ((0.0, 0.0), (0.4, 1.0), (0.0, 1.0)),
            ((0.6, 0.0), (1.0, 0.0), (1.0, 1.0)),
            ((0.6, 0.0), (1.0, 1.0), (0.6, 1.0)),
        ]
        grounded = scene_tools._mesh_support_coverage_from_points(
            [(0.1, 0.2), (0.3, 0.8), (0.8, 0.5)],
            support_triangles,
        )
        over_hole = scene_tools._mesh_support_coverage_from_points(
            [(0.1, 0.2), (0.5, 0.5)],
            support_triangles,
        )
        self.assertEqual(1.0, grounded["mesh_support_coverage"])
        self.assertEqual(0.5, over_hole["mesh_support_coverage"])
        self.assertEqual([[0.5, 0.5]], over_hole["unsupported_contact_point_samples"])

    def test_compound_mesh_discovers_distinct_real_shelf_levels(self) -> None:
        points = [
            (0, 0, 0.60), (1, 0, 0.60), (1, 1, 0.60), (0, 1, 0.60),
            (0, 0, 1.20), (1, 0, 1.20), (1, 1, 1.20), (0, 1, 1.20),
            (0, 0, 1.21), (1, 0, 1.21), (1, 1, 1.21), (0, 1, 1.21),
        ]
        triangles = [
            (0, 1, 2), (0, 2, 3),
            (4, 5, 6), (4, 6, 7),
            (8, 9, 10), (8, 10, 11),
        ]
        surfaces = discover_horizontal_support_surfaces([(points, triangles)])
        self.assertEqual([0.6, 1.21], [round(row["surface_z_m"], 2) for row in surfaces])
        self.assertEqual([1.0, 1.0], [row["area_m2"] for row in surfaces])

    def test_dense_support_triangles_are_compacted_deterministically(self) -> None:
        triangle = ((0.001, 0.001), (0.499, 0.001), (0.001, 0.499))
        compacted, report = compact_support_triangles([triangle] * 20001, maximum=10)
        self.assertEqual(1, len(compacted))
        self.assertEqual(20001, report["source_triangle_count"])
        self.assertGreater(report["quantization_m"], 0.0)

    def test_support_height_clusters_do_not_chain_up_vertical_braces(self) -> None:
        points, triangles = [], []
        # Tiny horizontal bevel facets form a continuous ladder of heights.
        # They must not merge the two wide shelves into a single tall cluster.
        for z in [index * 0.02 for index in range(71)]:
            start = len(points)
            points.extend([(0, 0, z), (.001, 0, z), (0, .001, z)])
            triangles.append((start, start + 1, start + 2))
        for z in (.60, 1.20):
            start = len(points)
            points.extend([(0, 0, z), (1, 0, z), (1, 1, z), (0, 1, z)])
            triangles.extend([(start, start+1, start+2), (start, start+2, start+3)])
        surfaces = discover_horizontal_support_surfaces([(points, triangles)])
        self.assertEqual([.6, 1.2], [round(row['surface_z_m'], 2) for row in surfaces])

    def test_dense_contact_index_matches_exhaustive_points_and_edges(self) -> None:
        from isaac_scene.support import point_in_triangle_xy, mesh_support_coverage_from_points
        triangles = [((0, 0), (.4, 0), (.4, 1)), ((0, 0), (.4, 1), (0, 1)),
                     ((.6, 0), (1, 0), (1, 1)), ((.6, 0), (1, 1), (.6, 1))] * 40
        points = [(x/100, y/100) for x in range(-1, 102, 3) for y in range(-1, 102, 7)]
        points += [(1+1e-8, .5), (-1e-8, .5), (.4+1e-8, .5), (.5, .5)]
        expected = [p for p in points if any(point_in_triangle_xy(p,t) for t in triangles)]
        actual = mesh_support_coverage_from_points(points, triangles)
        self.assertEqual(len(expected), actual['supported_contact_point_count'])
        self.assertEqual(len(points), actual['contact_point_count'])
        self.assertEqual(len(expected)/len(points), actual['mesh_support_coverage'])

    def test_analytic_cube_support_uses_real_transformed_faces(self) -> None:
        try:
            from pxr import Usd, UsdGeom
        except ImportError:
            self.skipTest('USD runtime required')
        from isaac_scene.support import world_mesh_points_and_triangles, top_support_triangles, mesh_support_coverage_from_points
        stage=Usd.Stage.CreateInMemory()
        cube=UsdGeom.Cube.Define(stage,'/Floor')
        cube.CreateSizeAttr(2)
        cube.AddTranslateOp().Set((0,0,-.05))
        cube.AddScaleOp().Set((4,3.5,.05))
        meshes=world_mesh_points_and_triangles(cube.GetPrim(),Usd,UsdGeom,render_visible_only=True)
        triangles=top_support_triangles(meshes,support_surface_z=0,tolerance=.001)
        self.assertEqual(2,len(triangles))
        coverage=mesh_support_coverage_from_points([(3.9,3.4),(4.1,0)],triangles)
        self.assertEqual(.5,coverage['mesh_support_coverage'])

    def test_compound_support_prefers_real_horizontal_surface_over_handle_bbox(self) -> None:
        surfaces = [{"surface_z_m": 0.41}, {"surface_z_m": 0.72}]
        selected, source = select_support_surface_z(surfaces, bbox_max_z=1.4)
        self.assertEqual(0.72, selected)
        self.assertEqual("discovered_highest_horizontal_surface", source)
        selected, source = select_support_surface_z(surfaces, bbox_max_z=1.4, requested_z=0.41)
        self.assertEqual((0.41, "entity_constraint"), (selected, source))

    def test_mesh_support_solver_moves_a_footprint_off_a_real_hole(self) -> None:
        support_triangles = [
            ((0.0, 0.0), (0.4, 0.0), (0.4, 1.0)),
            ((0.0, 0.0), (0.4, 1.0), (0.0, 1.0)),
            ((0.6, 0.0), (1.0, 0.0), (1.0, 1.0)),
            ((0.6, 0.0), (1.0, 1.0), (0.6, 1.0)),
        ]
        footprint = [(0.45, 0.45), (0.55, 0.45), (0.45, 0.55), (0.55, 0.55)]
        solved = solve_mesh_support_placement(
            object_contact_points=footprint,
            object_xy_points=footprint,
            support_triangles=support_triangles,
            current_anchor_xy=(0.5, 0.5),
            support_margin=0.01,
            grid_step=0.05,
            minimum_coverage=1.0,
        )
        self.assertEqual("pass", solved["status"])
        self.assertEqual(1.0, solved["mesh_support_coverage"])
        self.assertNotEqual([0.0, 0.0], solved["suggested_world_translation_delta_xy"])

    def test_mesh_support_solver_respects_obstacle_clearance(self) -> None:
        support_triangles = [
            ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),
            ((0.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        ]
        footprint = [(0.45, 0.45), (0.55, 0.45), (0.45, 0.55), (0.55, 0.55)]
        solved = solve_mesh_support_placement(
            object_contact_points=footprint,
            object_xy_points=footprint,
            support_triangles=support_triangles,
            current_anchor_xy=(0.5, 0.5),
            obstacle_bounds=[{"min": [-1.0, -1.0], "max": [2.0, 2.0]}],
            support_margin=0.0,
            grid_step=0.1,
            max_candidates=200,
        )
        self.assertEqual("fail", solved["status"])
        self.assertEqual("no_valid_support_placement", solved["reason"])
        self.assertGreater(solved["rejections"]["obstacle_overlap"], 0)

    def test_mesh_support_solver_faces_from_the_suggested_anchor(self) -> None:
        support_triangles = [
            ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),
            ((0.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        ]
        footprint = [(0.35, 0.35), (0.45, 0.35), (0.35, 0.45), (0.45, 0.45)]
        target = (0.5, -1.0)
        solved = solve_mesh_support_placement(
            object_contact_points=footprint,
            object_xy_points=footprint,
            support_triangles=support_triangles,
            current_anchor_xy=(0.3, 0.4),
            current_front_vector=(0.0, -1.0),
            facing_target_xy=target,
            support_margin=0.0,
            grid_step=0.05,
            max_candidates=400,
        )
        yaw = solved["recommended_yaw_delta_deg"]
        radians = math.radians(yaw)
        rotated_front = (math.sin(radians), -math.cos(radians))
        bounds = solved["candidate_object_bounds_xy"]
        subject_center = [(bounds[0] + bounds[2]) / 2.0, (bounds[1] + bounds[3]) / 2.0]
        self.assertLess(
            facing_error_deg(
                rotated_front,
                [target[0] - subject_center[0], target[1] - subject_center[1]],
            ),
            1e-4,
        )

    def test_reference_units_auto_normalize_and_reject_extreme_conflict(self) -> None:
        audit = resolve_reference_scale(
            source_meters_per_unit=0.01,
            target_meters_per_unit=1.0,
            authored_source_units=True,
        )
        self.assertEqual([0.01, 0.01, 0.01], audit["effective_scale"])
        self.assertEqual("auto_normalized", audit["resolution"])
        artistic = resolve_reference_scale(
            source_meters_per_unit=0.01,
            target_meters_per_unit=1.0,
            authored_source_units=True,
            explicit_scale=[1.5, 1.5, 1.5],
            unit_mode="multiply",
        )
        self.assertEqual([0.015, 0.015, 0.015], artistic["effective_scale"])
        with self.assertRaises(UnitScaleMismatch):
            resolve_reference_scale(
                source_meters_per_unit=0.01,
                target_meters_per_unit=1.0,
                authored_source_units=True,
                explicit_scale=[1.0, 1.0, 1.0],
            )

    def test_verified_asset_unit_audit_fills_unannotated_entry_layer(self) -> None:
        contract = select_source_unit_contract(
            layer_meters_per_unit=0.01,
            layer_units_authored=False,
            declared_meters_per_unit=0.01,
            declared_units_verified=True,
        )
        self.assertEqual(0.01, contract["meters_per_unit"])
        self.assertTrue(contract["units_verified"])
        self.assertEqual("verified_asset_audit", contract["source"])
        unverified = select_source_unit_contract(
            layer_meters_per_unit=0.01,
            layer_units_authored=False,
            declared_meters_per_unit=0.01,
            declared_units_verified=False,
        )
        self.assertFalse(unverified["units_verified"])

    def test_default_config_is_unconfigured_and_offline(self) -> None:
        config = scene_tools.load_config(SKILL_ROOT / "config" / "config.json")
        self.assertIsNone(config["_profile"])
        self.assertFalse(config["deployment_configured"])
        self.assertEqual({}, config["runtime"])
        self.assertEqual([], config["libraries"])

    def test_geniesim_robot_and_chinese_asset_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "assets"
            (root / "robot" / "G2_omnipicker").mkdir(parents=True)
            (root / "robot" / "G2_omnipicker" / "robot.usd").write_text("#usda 1.0\n")
            (root / "objects" / "benchmark" / "sofa" / "benchmark_sofa_000").mkdir(parents=True)
            (root / "objects" / "benchmark" / "sofa" / "benchmark_sofa_000" / "Aligned.usd").write_text("#usda 1.0\n")
            config = {
                "runtime": {"state_root": str(Path(temp) / "state")},
                "search_policy": {"default_libraries": ["geniesim"], "slow_libraries": [], "staged_default_search": False},
                "libraries": [{"name": "geniesim", "scanner": "geniesim", "root": str(root)}],
            }
            robot_hits = scene_tools.search_assets(config, "轮式人形机器人", types=["robot"], topk=5)
            self.assertTrue(robot_hits)
            self.assertEqual("geniesim_g2_omnipicker", robot_hits[0]["asset_id"])
            sofa_hits = scene_tools.search_assets(config, "沙发", types=["object"], topk=5)
            self.assertTrue(sofa_hits)
            self.assertEqual("benchmark_sofa_000", sofa_hits[0]["asset_id"])

    def test_fabrica_stale_absolute_path_rebases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "fabrica"
            package = root / "fabrica_official_car_fixture_pickup_sdf512_v1"
            (package / "scene").mkdir(parents=True)
            (package / "scene" / "scene.usda").write_text("#usda 1.0\n")
            (package / "PACKAGE_MANIFEST.json").write_text(json.dumps({"assembly": "car", "package_name": package.name, "entrypoints": {"scene_usda": "scene/scene.usda"}}))
            (package / "scene" / "scene_spec.json").write_text(json.dumps({"assembly": "car", "physics_alignment": {"collision": {"sdf_margin_m": 0.001}}, "scene": {"parts": {}}}))
            (root / "ASSET_INDEX.json").write_text(json.dumps({"packages": [{"assembly": "car", "package": package.name, "path": "/old/server/task_bundles/" + package.name, "scene_usda": "scene/scene.usda"}]}))
            records = scene_tools._scan_fabrica_task_bundles({"name": "fabrica_task_bundles", "root": str(root)})
            recipes = [row for row in records if row["asset_type"] == "assembly_set"]
            self.assertEqual(1, len(recipes))
            self.assertEqual(str(package / "scene" / "scene.usda"), recipes[0]["path"])

    def test_handoff_can_be_resumed_without_history(self) -> None:
        try:
            from pxr import Usd
        except ImportError:
            self.skipTest("USD handoff requires the USD runtime")
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            scene = temp_path / "scene.usda"
            scene.write_text("#usda 1.0\n")
            output = temp_path / "handoff"
            state = {
                "goal": "Build a warehouse scene",
                "domain": "warehouse_logistics",
                "phase": "validation",
                "scene_path": str(scene),
                "completed_steps": ["asset_selection", "composition"],
                "open_issues": [],
                "next_recommended_actions": ["run physics validation"],
                "unit_audit": {"target_meters_per_unit": 1.0, "references": [{"id": "warehouse", "effective_scale": [1, 1, 1]}]},
            }
            config = {"_profile": "test", "_config_path": "fixture", "runtime": {"python": {"executable": sys.executable}}}
            exported = export_handoff(state, output, config=config, skill_root=SKILL_ROOT)
            verified = verify_handoff(exported["manifest_path"])
            self.assertEqual("pass", verified["status"])
            self.assertEqual("validation", verified["manifest"]["phase"])
            self.assertEqual(1.0, verified["manifest"]["unit_audit"]["target_meters_per_unit"])

    def test_scene_spec_v2_maps_without_hidden_layout_reasoning(self) -> None:
        spec, observation = normalize_scene_spec({
            "schema_version": "isaac-scene-spec/v2",
            "entities": [
                {"id": "warehouse", "role": "background", "path": "/assets/warehouse.usd", "unit_mode": "preserve"},
                {"id": "robot", "role": "mobile_robot", "asset_id": "geniesim_g2_omnipicker", "translate": [0, 0, 0.05]},
                {
                    "id": "pallet",
                    "role": "pallet",
                    "asset_id": "pallet_01",
                    "front_axis": "+Y",
                    "source_meters_per_unit": 0.01,
                    "source_units_verified": True,
                    "facing_target": "/World/Objects/Robot",
                    "regression_instance_opinions": {
                        "source_layer": "/source/scene.usd",
                        "source_prim_path": "/root/model_pallet",
                        "source_attachment_path": "/v_0",
                        "target_prim_suffix": "/Source",
                        "policy": "visual_instance_overrides_v1"
                    }
                },
            ],
            "relations": [{"type": "near", "subject": "robot", "target": "pallet"}],
        })
        self.assertEqual("/assets/warehouse.usd", spec["background"]["path"])
        self.assertEqual("preserve", spec["background"]["unit_mode"])
        self.assertEqual(2, len(spec["references"]))
        self.assertEqual("+Y", spec["references"][1]["front_axis"])
        self.assertEqual(0.01, spec["references"][1]["source_meters_per_unit"])
        self.assertTrue(spec["references"][1]["source_units_verified"])
        self.assertEqual(
            "visual_instance_overrides_v1",
            spec["references"][1]["regression_instance_opinions"]["policy"],
        )
        self.assertEqual("layout_solver_required", observation["unresolved_relations"][0]["reason"])

    def test_regression_visual_opinions_copy_typed_child_edits(self) -> None:
        try:
            from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
        except ImportError:
            self.skipTest("pxr is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_path = root / "source.usda"
            source = Usd.Stage.CreateNew(str(source_path))
            UsdGeom.Xform.Define(source, "/root/model")
            moved = UsdGeom.Xform.Define(source, "/root/model/Part")
            moved.AddTranslateOp().Set(Gf.Vec3d(0.1, 0.2, 0.3))
            hidden = UsdGeom.Xform.Define(source, "/root/model/Disabled").GetPrim()
            hidden.SetActive(False)
            shader = UsdShade.Shader.Define(source, "/root/model/materials/mat/PBRShader")
            shader.CreateIdAttr("UsdPreviewSurface")
            shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.27)
            source.GetRootLayer().Save()

            output_path = root / "rebuilt.usda"
            rebuilt = Usd.Stage.CreateNew(str(output_path))
            UsdGeom.Xform.Define(rebuilt, "/World/Object/Source/Part")
            UsdGeom.Xform.Define(rebuilt, "/World/Object/Source/Disabled")
            UsdShade.Shader.Define(rebuilt, "/World/Object/Source/materials/mat/PBRShader")
            report = scene_tools._apply_regression_visual_opinions(
                rebuilt,
                "/World/Object",
                {
                    "source_layer": str(source_path),
                    "source_prim_path": "/root/model",
                    "source_attachment_path": "",
                    "target_prim_suffix": "/Source",
                    "policy": "visual_instance_overrides_v1",
                },
            )
            self.assertEqual(1, report["deactivated_prim_count"])
            self.assertGreaterEqual(report["copied_property_count"], 3)
            copied_xform = UsdGeom.Xformable(rebuilt.GetPrimAtPath("/World/Object/Source/Part"))
            self.assertEqual((0.1, 0.2, 0.3), tuple(copied_xform.GetOrderedXformOps()[0].Get()))
            self.assertFalse(rebuilt.GetPrimAtPath("/World/Object/Source/Disabled").IsActive())
            copied_shader = UsdShade.Shader(
                rebuilt.GetPrimAtPath("/World/Object/Source/materials/mat/PBRShader")
            )
            self.assertAlmostEqual(0.27, copied_shader.GetInput("roughness").Get())


if __name__ == "__main__":
    unittest.main(verbosity=2)
