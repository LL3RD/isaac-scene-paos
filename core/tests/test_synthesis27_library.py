from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.synthesis27_library import collect, write_library  # noqa: E402


def _write_scene(
    root: Path,
    slug: str,
    models: list[dict],
    ext: list[dict],
    *,
    features: list[dict] | None = None,
) -> None:
    scene_root = root / slug
    scene_root.mkdir(parents=True)
    (scene_root / "scene_info.json").write_text(json.dumps({
        "models": models,
        "features": features or [],
        "camera": {"position": [1, 2, 3]},
    }))
    (scene_root / "scene_data.json").write_text(json.dumps({
        "Result": {
            "SceneDataJson": json.dumps({
                "extDataArr": ext,
                "sceneSetting": {"highQuality": True},
            })
        }
    }))


class Synthesis27LibraryTests(unittest.TestCase):
    def _manifest(self, root: Path, slugs: list[str]) -> Path:
        path = root / "manifest.json"
        path.write_text(json.dumps({
            "scenes": [
                {"id": f"scene-{slug}", "slug": slug, "name": slug}
                for slug in slugs
            ]
        }))
        return path

    def test_repeated_model_instances_keep_distinct_ids_and_source_poses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_scene(
                root,
                "room",
                [
                    {"modelID": "chair", "name": "chair001", "offset": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]},
                    {"modelID": "chair", "name": "chair001", "offset": [2, 0, 0], "rotation": [0, 0, 90], "scale": [1, 1, 1]},
                ],
                [
                    {"modelID": "chair", "idInScene": "a", "modelBusinessType": 1, "data": {}},
                    {"modelID": "chair", "idInScene": "b", "modelBusinessType": 1, "data": {}},
                ],
            )
            library = collect(self._manifest(root, ["room"]), root)
            self.assertEqual(1, len(library["assets"]))
            self.assertEqual(2, library["assets"][0]["usage_count"])
            self.assertEqual(["a", "b"], [row["id_in_scene"] for row in library["instances"]])
            self.assertEqual([2.0, 0.0, 0.0], library["instances"][1]["offset"])

    def test_cross_model_order_can_change_but_duplicate_fifo_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_scene(
                root,
                "room",
                [
                    {"modelID": "a", "name": "a", "offset": [1, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]},
                    {"modelID": "b", "name": "b", "offset": [2, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]},
                    {"modelID": "a", "name": "a", "offset": [3, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]},
                ],
                [
                    {"modelID": "b", "idInScene": "b1", "modelBusinessType": 1, "data": {}},
                    {"modelID": "a", "idInScene": "a1", "modelBusinessType": 1, "data": {}},
                    {"modelID": "a", "idInScene": "a2", "modelBusinessType": 1, "data": {}},
                ],
            )
            library = collect(self._manifest(root, ["room"]), root)
            self.assertEqual(["a1", "b1", "a2"], [row["id_in_scene"] for row in library["instances"]])

    def test_scope_and_external_3dgs_are_recorded_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            feature = {
                "name": "Restaurant",
                "url": "https://example/asset.usdz",
                "shadowUrl": "https://example/asset.ply",
                "offset": [0, 0, 0],
                "rotation": [270, 0, 0],
                "scale": [1, 1, 1],
            }
            _write_scene(
                root,
                "room",
                [
                    {"modelID": "root", "name": "room", "offset": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]},
                    {"modelID": "floor", "name": "floor", "offset": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]},
                    {"modelID": "chair", "name": "chair", "offset": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]},
                    {"modelID": "robot", "name": "robot", "offset": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]},
                ],
                [
                    {"modelID": "root", "idInScene": "r", "modelBusinessType": 2, "data": {}},
                    {"modelID": "floor", "idInScene": "f", "modelBusinessType": 2, "data": {}},
                    {"modelID": "chair", "idInScene": "c", "modelBusinessType": 1, "data": {}},
                    {"modelID": "robot", "idInScene": "bot", "modelBusinessType": 5, "data": {}},
                    {
                        "idInScene": "gs",
                        "modelBusinessType": 3,
                        "isaacUrl": feature["url"],
                        "webUrl": feature["shadowUrl"],
                        "data": {"latestName": feature["name"]},
                    },
                ],
                features=[feature],
            )
            library = collect(self._manifest(root, ["room"]), root)
            self.assertEqual(
                ["scene_root", "scene_layout_component", "independent_object", "robot"],
                [row["asset_scope"] for row in library["instances"]],
            )
            self.assertEqual(1, len(library["external_features"]))
            self.assertEqual("external_feature_3dgs", library["external_features"][0]["asset_scope"])

    def test_remote_usd_never_makes_unvalidated_asset_callable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_scene(
                root,
                "room",
                [{"modelID": "chair", "name": "chair", "offset": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]}],
                [{"modelID": "chair", "idInScene": "c", "modelBusinessType": 1, "data": {}}],
            )
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "assets": [{
                    "model_id": "chair",
                    "has_usd": True,
                    "availability": "remote_usd_available",
                    "is_articulated": True,
                }]
            }))
            library = collect(self._manifest(root, ["room"]), root, acquisition_plan=plan)
            asset = library["assets"][0]
            self.assertTrue(asset["remote_has_usd"])
            self.assertFalse(asset["callable"])
            self.assertIsNone(asset["source_path"])

    def test_semantic_override_wins_and_unknown_model_keeps_generic_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_scene(
                root,
                "room",
                [
                    {"modelID": "reviewed", "name": "floor lamp", "offset": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]},
                    {"modelID": "unknown", "name": "chair", "offset": [1, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]},
                ],
                [
                    {"modelID": "reviewed", "idInScene": "reviewed-1", "modelBusinessType": 1, "data": {}},
                    {"modelID": "unknown", "idInScene": "unknown-1", "modelBusinessType": 1, "data": {}},
                ],
            )
            overrides = root / "semantic-overrides.json"
            overrides.write_text(json.dumps({"overrides": {
                "reviewed": {
                    "canonical_class": "lighting/fixture",
                    "subtype": "floor_lamp",
                    "semantic_name": "reviewed floor lamp",
                }
            }}))
            library = collect(
                self._manifest(root, ["room"]),
                root,
                semantic_overrides=overrides,
            )
            assets = {asset["model_id"]: asset for asset in library["assets"]}
            self.assertEqual("lighting/fixture", assets["reviewed"]["canonical_class"])
            self.assertEqual("floor_lamp", assets["reviewed"]["subtype"])
            self.assertEqual("reviewed floor lamp", assets["reviewed"]["semantic_name"])
            self.assertEqual("furniture/seating/chair", assets["unknown"]["canonical_class"])
            self.assertEqual("chair", assets["unknown"]["subtype"])

    def test_invalid_pose_and_unmatched_occurrence_are_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_scene(
                root,
                "room",
                [{"modelID": "chair", "name": "chair", "offset": [0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]}],
                [
                    {"modelID": "chair", "idInScene": "c", "modelBusinessType": 1, "data": {}},
                    {"modelID": "orphan", "idInScene": "o", "modelBusinessType": 1, "data": {}},
                ],
            )
            library = collect(self._manifest(root, ["room"]), root)
            codes = {row["code"] for row in library["anomalies"]}
            self.assertIn("invalid_source_pose", codes)
            self.assertIn("ext_model_occurrence_unmatched", codes)

    def test_output_is_queryable_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_scene(
                root,
                "room",
                [{"modelID": "chair", "name": "chair", "offset": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]}],
                [{"modelID": "chair", "idInScene": "c", "modelBusinessType": 1, "data": {}}],
            )
            library = collect(self._manifest(root, ["room"]), root)
            out = root / "out"
            summary = write_library(out, library)
            self.assertEqual(1, summary["asset_count"])
            connection = sqlite3.connect(out / "synthesis27_assets.sqlite")
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0])
            self.assertEqual("c", connection.execute("SELECT id_in_scene FROM instances").fetchone()[0])
            connection.close()
            first = (out / "assets.jsonl").read_bytes()
            write_library(out, library)
            self.assertEqual(first, (out / "assets.jsonl").read_bytes())

    def test_rebuild_preserves_download_and_validated_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_scene(
                root,
                "room",
                [{"modelID": "chair", "name": "chair", "offset": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]}],
                [{"modelID": "chair", "idInScene": "c", "modelBusinessType": 1, "data": {}}],
            )
            out = root / "out"
            write_library(out, collect(self._manifest(root, ["room"]), root))
            connection = sqlite3.connect(out / "synthesis27_assets.sqlite")
            connection.execute(
                """UPDATE assets
                   SET download_file_id='record-uuid',
                       download_resolution_status='resolved',
                       is_open_free=1,
                       license_observed_at='2026-08-06T00:00:00+00:00',
                       license_status='cc_by_nc_4_0_platform_declared',
                       availability='local_structural_validated',
                       source_path='/assets/model_chair.usd',
                       meters_per_unit=1.0,
                       bbox_json='{"size_meters":[1,1,1]}',
                       up_axis='Z',
                       insertion_smoke_status='pass',
                       insertion_smoke_report_json='{"scene_path":"/runs/smoke.usda"}',
                       qa_status='pending_license'
                   WHERE model_id='chair'"""
            )
            connection.commit()
            connection.close()

            fresh = collect(self._manifest(root, ["room"]), root)
            write_library(out, fresh)
            connection = sqlite3.connect(out / "synthesis27_assets.sqlite")
            row = connection.execute(
                """SELECT download_file_id, download_resolution_status,
                          is_open_free, license_observed_at, license_status,
                          availability, source_path, up_axis,
                          insertion_smoke_status,
                          insertion_smoke_report_json,
                          qa_status
                   FROM assets WHERE model_id='chair'"""
            ).fetchone()
            connection.close()
            self.assertEqual(
                (
                    "record-uuid",
                    "resolved",
                    1,
                    "2026-08-06T00:00:00+00:00",
                    "cc_by_nc_4_0_platform_declared",
                    "local_structural_validated",
                    "/assets/model_chair.usd",
                    "Z",
                    "pass",
                    '{"scene_path":"/runs/smoke.usda"}',
                    "pending_license",
                ),
                row,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
