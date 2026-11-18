from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.synthesis_reconstruction import build_reconstruction_spec, export_reconstruction_specs  # noqa: E402
from isaac_scene.synthesis_reconstruction import (  # noqa: E402
    _comparison_camera_from_bounds,
    _matrix_inverse,
    _matrix_multiply,
    _support_cycle_nodes,
    rank_reference_camera_renders,
)


class SynthesisReconstructionTests(unittest.TestCase):
    def test_exports_exact_matrices_and_remaps_support_prim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "objects.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript("""
                CREATE TABLE objects (
                    model_id TEXT, logical_id TEXT, wrapper_path TEXT,
                    wrapper_status TEXT, callable INTEGER, qa_status TEXT,
                    license_status TEXT
                );
                CREATE TABLE instances (
                    instance_id TEXT, scene_slug TEXT, model_id TEXT,
                    source_prim_path TEXT, semantic_name TEXT, asset_scope TEXT,
                    xform_json TEXT
                );
            """)
            wrapper = root / "asset.usda"
            wrapper.write_text("#usda 1.0\n", encoding="utf-8")
            connection.executemany(
                "INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                [
                    ("desk", "synthesis28_objects:desk", str(wrapper), "structural_pass", 1, "ready", "approved"),
                    ("monitor", "synthesis28_objects:monitor", str(wrapper), "structural_pass", 0, "pending", "approved"),
                ],
            )
            matrix_a = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [1, 2, 0, 1]]
            matrix_b = [[0, 1, 0, 0], [-1, 0, 0, 0], [0, 0, 1, 0], [1, 2, 0.8, 1]]
            connection.executemany(
                "INSERT INTO instances VALUES (?,?,?,?,?,?,?)",
                [
                    ("scene:demo:desk", "demo", "desk", "/root/model_desk", "desk", "independent_object", json.dumps({"world_matrix": matrix_a, "xform_ops": []})),
                    ("scene:demo:monitor", "demo", "monitor", "/root/model_monitor", "monitor", "independent_object", json.dumps({"world_matrix": matrix_b, "xform_ops": []})),
                ],
            )
            connection.commit()
            connection.close()
            relations = root / "relations.json"
            relations.write_text(json.dumps({
                "scenes": [{
                    "scene_slug": "demo",
                    "relations": [
                        {
                            "instance_id": "scene:demo:desk",
                            "regression_status": "pass",
                            "relation": "grounded_on_floor_plane",
                            "support_target": "floor",
                            "floor_z_m": 0.0,
                            "signed_gap_m": 0.0,
                            "confidence": "high",
                        },
                        {
                            "instance_id": "scene:demo:monitor",
                            "regression_status": "pass",
                            "relation": "supported_by_mesh_surface",
                            "support_instance_id": "scene:demo:desk",
                            "support_target": "/root/model_desk/v_0/top",
                            "confidence": "high",
                        },
                    ],
                }],
            }), encoding="utf-8")

            result = export_reconstruction_specs(database, relations, root / "out")
            self.assertEqual("pass", result["status"])
            self.assertEqual(2, result["exported_entity_count"])
            self.assertEqual(2, result["verified_support_edge_count"])
            spec = json.loads((root / "out" / "demo.scene_spec.json").read_text(encoding="utf-8"))
            by_id = {row["id"]: row for row in spec["entities"]}
            self.assertEqual(matrix_b, by_id["scene:demo:monitor"]["xform_matrix"])
            self.assertFalse(by_id["scene:demo:desk"]["regression_only"])
            self.assertTrue(by_id["scene:demo:monitor"]["regression_only"])
            self.assertEqual("floor", by_id["scene:demo:desk"]["support_target"])
            self.assertEqual("ground", by_id["scene:demo:desk"]["support_mode"])
            self.assertEqual(
                by_id["scene:demo:desk"]["prim"] + "/Source/v_0/top",
                by_id["scene:demo:monitor"]["support_target"],
            )

    def test_wrapper_normalization_compensation_restores_source_pose(self) -> None:
        normalization = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [3, -2, 0.5, 1]]
        source_world = [[0, 1, 0, 0], [-1, 0, 0, 0], [0, 0, 1, 0], [8, 4, 1, 1]]
        entity_world = _matrix_multiply(_matrix_inverse(normalization), source_world)
        restored = _matrix_multiply(normalization, entity_world)
        for row in range(4):
            for column in range(4):
                self.assertAlmostEqual(source_world[row][column], restored[row][column])

    def test_exact_spec_uses_inverse_wrapper_frame_and_instance_opinion_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "asset.usda"
            wrapper.write_text(
                "#usda 1.0\n"
                "def Xform \"Object\" {\n"
                "  def Xform \"Source\" {\n"
                "    matrix4d xformOp:transform = (\n"
                "      (1, 0, 0, 0), (0, 1, 0, 0),\n"
                "      (0, 0, 1, 0), (3, -2, 0.5, 1)\n"
                "    )\n"
                "    uniform token[] xformOpOrder = [\"xformOp:transform\"]\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            source_world = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [8, 4, 1, 1]]
            spec, report = build_reconstruction_spec(
                "demo",
                [{
                    "instance_id": "scene:demo:item",
                    "model_id": "item",
                    "source_prim_path": "/root/model_item",
                    "semantic_name": "item",
                    "asset_scope": "independent_object",
                    "xform_json": json.dumps({"world_matrix": source_world}),
                }],
                {"item": {
                    "model_id": "item",
                    "logical_id": "synthesis28_objects:item",
                    "wrapper_path": str(wrapper),
                    "wrapper_status": "structural_pass",
                    "callable": 0,
                    "qa_status": "pending",
                    "license_status": "approved",
                    "bbox_m_json": json.dumps({"min": [0, 0, 0], "max": [1, 1, 1]}),
                }},
                {},
                source_scene_path="/scenes/demo/scene.usd",
                source_local_bounds={"scene:demo:item": {"min": [0, 0, 0], "max": [2, 1, 1]}},
                instance_attachments={"scene:demo:item": {
                    "source_attachment_path": "/v_0",
                    "source_attachment_local_matrix": [
                        [1, 0, 0, 0], [0, 1, 0, 0],
                        [0, 0, 1, 0], [0.25, 0.5, 0, 1],
                    ],
                }},
            )
            entity = spec["entities"][0]
            normalization = entity["wrapper_normalization_matrix"]
            restored = _matrix_multiply(normalization, entity["xform_matrix"])
            self.assertEqual(source_world, restored)
            self.assertEqual("wrapper_normalization_inverse", entity["regression_alignment"]["mode"])
            self.assertEqual(
                "visual_instance_overrides_v1",
                entity["regression_instance_opinions"]["policy"],
            )
            self.assertEqual("/v_0", entity["regression_instance_opinions"]["source_attachment_path"])
            self.assertEqual(1, report["instance_opinion_entity_count"])

    def test_mutual_support_is_demoted_as_a_cycle(self) -> None:
        relations = {
            "a": {"regression_status": "pass", "support_instance_id": "b"},
            "b": {"regression_status": "pass", "support_instance_id": "a"},
            "c": {"regression_status": "pass", "support_instance_id": "a"},
        }
        self.assertEqual({"a", "b"}, _support_cycle_nodes(relations))

    def test_bounds_camera_is_deterministic_and_targets_scene_interior(self) -> None:
        first = _comparison_camera_from_bounds([-5.0, -2.0, 0.0], [5.0, 6.0, 4.0])
        second = _comparison_camera_from_bounds([-5.0, -2.0, 0.0], [5.0, 6.0, 4.0])
        self.assertEqual(first, second)
        self.assertEqual("bounds_fallback", first["mode"])
        self.assertEqual([0.0, 2.0, 1.68], first["target"])
        self.assertGreater(first["position"][2], first["target"][2])

    def test_reference_camera_ranking_is_score_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "candidates": [
                    {"candidate_id": "C000", "render_filename": "C000.png"},
                    {"candidate_id": "C001", "render_filename": "C001.png"},
                ],
            }), encoding="utf-8")
            render_dir = root / "renders"
            render_dir.mkdir()
            (render_dir / "C000.png").write_bytes(b"first")
            (render_dir / "C001.png").write_bytes(b"second")

            def comparison(_reference: str | Path, rendered: str | Path) -> dict:
                score = 0.81 if Path(rendered).name == "C001.png" else 0.42
                return {"status": "pass", "score": score, "metrics": {"mock": score}}

            with patch("isaac_scene.quality.compare_images", side_effect=comparison):
                result = rank_reference_camera_renders(
                    manifest,
                    root / "reference.png",
                    render_dir,
                    root / "ranking.json",
                )
            self.assertEqual("pass", result["status"])
            self.assertEqual("C001", result["best"]["candidate_id"])
            self.assertEqual(2, result["rendered_candidate_count"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
