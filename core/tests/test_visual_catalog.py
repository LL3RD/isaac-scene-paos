from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.visual_catalog import initialize, search_embeddings, upsert_asset, upsert_view  # noqa: E402
from isaac_scene.preview_manifest import (  # noqa: E402
    asset_is_complete,
    load_or_create,
    upsert_asset as upsert_preview_asset,
    write_atomic,
)
from prepare_asset_preview_batch import balanced_select, merge_unique, recall_class  # noqa: E402
from replace_asset_in_scene_spec import replacement_yaw  # noqa: E402
from search_asset_visual_index import (  # noqa: E402
    bbox_size_meters,
    dimension_similarity,
    preview_candidate_count,
    semantic_candidate_paths,
    semantic_metadata_by_path,
)
from sync_preview_catalog import eligible_rows  # noqa: E402
from evaluate_replacement_regression import replacement_decision  # noqa: E402
from isaac_scene.visual_selection import candidate_coverage  # noqa: E402


class VisualCatalogTests(unittest.TestCase):
    def test_preview_manifest_resume_requires_all_valid_views(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index = root / "preview_index.json"
            views = ["neg_y", "pos_x", "pos_y", "neg_x"]
            previews = {}
            for view in views:
                path = root / f"{view}.png"
                path.write_bytes(b"x" * 16)
                previews[view] = str(path)
            manifest = load_or_create(index, resolution=384, views=views, resume=False)
            row = {"asset_id": "sofa-a", "status": "pass", "previews": previews}
            upsert_preview_asset(manifest, row)
            write_atomic(index, manifest)

            resumed = load_or_create(index, resolution=384, views=views, resume=True)
            self.assertTrue(
                asset_is_complete(
                    resumed["assets"][0],
                    required_views=views,
                    min_output_bytes=8,
                )
            )
            Path(previews["pos_x"]).unlink()
            self.assertFalse(
                asset_is_complete(
                    resumed["assets"][0],
                    required_views=views,
                    min_output_bytes=8,
                )
            )

    def test_preview_manifest_upsert_replaces_interrupted_row(self) -> None:
        manifest = load_or_create(
            Path("/tmp/not-written.json"),
            resolution=384,
            views=["neg_y", "pos_x", "pos_y", "neg_x"],
            resume=False,
        )
        upsert_preview_asset(manifest, {"asset_id": "machine-a", "status": "running"})
        upsert_preview_asset(manifest, {"asset_id": "machine-a", "status": "pass"})
        self.assertEqual(1, len(manifest["assets"]))
        self.assertEqual("pass", manifest["assets"][0]["status"])

    def test_preview_catalog_sync_requires_views_bbox_and_units(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previews = {}
            for view in ("neg_y", "pos_x", "pos_y", "neg_x"):
                image = root / f"{view}.png"
                image.write_bytes(b"x" * 16)
                previews[view] = str(image)
            good = {
                "asset_id": "good",
                "status": "pass",
                "previews": previews,
                "bbox": {"size_meters": [1, 1, 1]},
                "meters_per_unit": 1.0,
            }
            missing_bbox = {**good, "asset_id": "bad", "bbox": None}
            passed, skipped = eligible_rows(
                {"assets": [good, missing_bbox]},
                required_views=["neg_y", "pos_x", "pos_y", "neg_x"],
                min_output_bytes=8,
            )
            self.assertEqual(["good"], [row["asset_id"] for row in passed])
            self.assertEqual(["bad"], [row["asset_id"] for row in skipped])

    def test_merge_unique_preserves_query_priority_then_backfills(self) -> None:
        query_rows = [
            {"logical_id": "a"},
            {"logical_id": "b"},
        ]
        class_rows = [
            {"logical_id": "b"},
            {"logical_id": "c"},
        ]
        self.assertEqual(
            ["a", "b", "c"],
            [row["logical_id"] for row in merge_unique(query_rows, class_rows, limit=3)],
        )

    def test_preview_batch_round_robins_sources(self) -> None:
        rows = [
            {"source_library": "a", "logical_id": "a1"},
            {"source_library": "a", "logical_id": "a2"},
            {"source_library": "b", "logical_id": "b1"},
        ]
        self.assertEqual(
            ["a1", "b1", "a2"],
            [row["logical_id"] for row in balanced_select(rows, 3)],
        )

    def test_preview_batch_recall_class_backfills_generic_names(self) -> None:
        import prepare_asset_preview_batch as preview_batch
        from unittest.mock import patch

        query_rows = [{"logical_id": "specific"}]
        class_rows = [{"logical_id": "generic"}]
        with patch.object(
            preview_batch,
            "search",
            side_effect=[query_rows, class_rows],
        ) as mocked:
            recalled_query, recalled_class = recall_class(
                Path("/tmp/catalog.sqlite"),
                "long dark corporate conference table",
                "furniture/surface/meeting_table",
                20,
            )
        self.assertEqual(query_rows, recalled_query)
        self.assertEqual(class_rows, recalled_class)
        self.assertEqual(
            [
                "long dark corporate conference table",
                "meeting table",
            ],
            [call.args[1] for call in mocked.call_args_list],
        )
        self.assertTrue(
            all(
                call.kwargs["canonical_class"] == "furniture/surface/meeting_table"
                for call in mocked.call_args_list
            )
        )

    def test_asset_replacement_preserves_world_facing(self) -> None:
        self.assertEqual(0.0, replacement_yaw(90.0, "-Y", "+X"))
        self.assertEqual(180.0, replacement_yaw(-90.0, "-Y", "+X"))
        self.assertEqual(-90.0, replacement_yaw(0.0, "-Y", "+X"))

    def test_visual_search_respects_candidate_paths_and_best_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "visual.sqlite"
            connection = sqlite3.connect(path)
            initialize(connection)
            for asset_id, source_path in (("a", "/assets/a.usd"), ("b", "/assets/b.usd")):
                upsert_asset(
                    connection,
                    asset_id=asset_id,
                    source_path=source_path,
                    semantic_name="chair",
                    category="furniture/seating/chair",
                    preview_index_path="/previews/index.json",
                    bbox=None,
                )
            upsert_view(connection, asset_id="a", view_name="front", preview_path="/a.png", model_id="clip", embedding=[1, 0])
            upsert_view(connection, asset_id="a", view_name="side", preview_path="/a_side.png", model_id="clip", embedding=[0, 1])
            upsert_view(connection, asset_id="b", view_name="front", preview_path="/b.png", model_id="clip", embedding=[0.8, 0.2])
            connection.commit()
            connection.close()

            ranking = search_embeddings(
                path,
                model_id="clip",
                query_embeddings={"text": [1, 0]},
                weights={"text": 1},
                candidate_paths={"/assets/a.usd"},
            )
            self.assertEqual(["a"], [row["asset_id"] for row in ranking])
            self.assertEqual("front", ranking[0]["best_view"])
            self.assertAlmostEqual(1.0, ranking[0]["score"])
            self.assertEqual("best_available", ranking[0]["view_policy"])
            front_restricted = search_embeddings(
                path,
                model_id="clip",
                query_embeddings={"text": [0, 1]},
                weights={"text": 1},
                candidate_paths={"/assets/a.usd"},
                preferred_view_by_path={"/assets/a.usd": "front"},
            )
            self.assertEqual("front", front_restricted[0]["best_view"])
            self.assertEqual("verified_front", front_restricted[0]["view_policy"])
            self.assertAlmostEqual(0.0, front_restricted[0]["score"])
            self.assertEqual(
                1,
                preview_candidate_count(
                    path,
                    model_id="clip",
                    candidate_paths={"/assets/a.usd"},
                ),
            )

    def test_visual_size_filter_normalizes_bbox_units(self) -> None:
        self.assertEqual(
            [1.0, 2.0, 3.0],
            bbox_size_meters('{"size": [100, 200, 300]}', 0.01),
        )
        self.assertEqual(
            [1.0, 2.0, 3.0],
            bbox_size_meters('{"size_meters": [1, 2, 3]}', 0.01),
        )
        self.assertEqual(
            [1.0, 2.0, 3.0],
            bbox_size_meters("[1, 2, 3]", 0.01),
        )
        self.assertIsNone(bbox_size_meters(None, 1.0))

    def test_dimension_similarity_is_xy_rotation_invariant(self) -> None:
        self.assertAlmostEqual(1.0, dimension_similarity([4.0, 2.0, 1.0], [2.0, 4.0, 1.0]))
        self.assertGreater(
            dimension_similarity([4.0, 2.0, 1.0], [4.0, 2.0, 1.0]),
            dimension_similarity([8.0, 4.0, 2.0], [4.0, 2.0, 1.0]),
        )

    def test_visual_result_metadata_uses_authoritative_semantic_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "semantic.sqlite"
            connection = sqlite3.connect(path)
            connection.execute(
                """CREATE TABLE semantic_assets(
                       source_path TEXT, callable INTEGER, canonical_class TEXT,
                       subtype TEXT, semantic_name TEXT, front_axis TEXT,
                       metadata_json TEXT
                   )"""
            )
            connection.execute(
                "INSERT INTO semantic_assets VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    "/assets/fridge.usd",
                    1,
                    "appliance/kitchen/refrigerator",
                    "double door",
                    "refrigerator",
                    "+Y",
                    '{"equivalence_group":"fridge-pair"}',
                ),
            )
            connection.commit()
            connection.close()
            rows = semantic_metadata_by_path(
                SimpleNamespace(
                    semantic_catalog=path,
                    include_unavailable=False,
                    canonical_class="appliance/kitchen/refrigerator",
                )
            )
            self.assertEqual(
                "appliance/kitchen/refrigerator",
                rows["/assets/fridge.usd"]["canonical_class"],
            )
            self.assertEqual("+Y", rows["/assets/fridge.usd"]["front_axis"])
            self.assertEqual("fridge-pair", rows["/assets/fridge.usd"]["equivalence_group"])

    def test_visual_candidate_pool_unions_preferred_and_fallback_classes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "semantic.sqlite"
            connection = sqlite3.connect(path)
            connection.execute(
                """CREATE TABLE semantic_assets(
                       source_path TEXT, callable INTEGER, canonical_class TEXT,
                       bbox_json TEXT, meters_per_unit REAL
                   )"""
            )
            connection.executemany(
                "INSERT INTO semantic_assets VALUES(?, ?, ?, ?, ?)",
                [
                    (
                        "/assets/conference.usd",
                        1,
                        "furniture/surface/meeting_table",
                        '{"size_meters":[3.0,1.2,0.75]}',
                        1.0,
                    ),
                    (
                        "/assets/generic.usd",
                        1,
                        "furniture/surface/table",
                        '{"size_meters":[2.5,1.0,0.75]}',
                        1.0,
                    ),
                    (
                        "/assets/chair.usd",
                        1,
                        "furniture/seating/chair",
                        '{"size_meters":[0.6,0.6,0.9]}',
                        1.0,
                    ),
                ],
            )
            connection.commit()
            connection.close()
            paths = semantic_candidate_paths(
                SimpleNamespace(
                    semantic_catalog=path,
                    include_unavailable=False,
                    canonical_class="furniture/surface/meeting_table",
                    fallback_class=["furniture/surface/table"],
                    min_size_m=None,
                    max_size_m=None,
                )
            )
            self.assertEqual(
                {"/assets/conference.usd", "/assets/generic.usd"},
                paths,
            )

    def test_replacement_regression_rolls_back_scene_level_score_drop(self) -> None:
        gates = {
            "geometry_pass": True,
            "facing_pass": True,
            "lighting_pass": True,
            "anchor_definition_pass": True,
            "anchor_real_asset_pass": True,
            "proxy_free_pass": True,
            "reference_match_pass": False,
        }
        baseline = {"result": {"gates": gates, "reference_match": {"score": 0.434246}}}
        challenger = {"result": {"gates": gates, "reference_match": {"score": 0.423781}}}
        result = replacement_decision(baseline, challenger, tolerance=0.005)
        self.assertEqual("rollback", result["decision"])
        self.assertAlmostEqual(-0.010465, result["score_delta"])
        self.assertIn("whole_scene_reference_score_regressed", result["reasons"])

    def test_small_semantic_inventory_can_pass_only_when_exhausted(self) -> None:
        self.assertTrue(
            candidate_coverage(
                preview_count=13,
                inventory_count=13,
                minimum_candidates=20,
                minimum_exhausted_candidates=10,
            )["pass"]
        )
        self.assertFalse(
            candidate_coverage(
                preview_count=12,
                inventory_count=13,
                minimum_candidates=20,
                minimum_exhausted_candidates=10,
            )["pass"]
        )
        self.assertFalse(
            candidate_coverage(
                preview_count=8,
                inventory_count=8,
                minimum_candidates=20,
                minimum_exhausted_candidates=10,
            )["pass"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
