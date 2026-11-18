from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from evaluate_synthesis27_asset_matching import evaluate  # noqa: E402


class SynthesisMatchingTests(unittest.TestCase):
    def test_matching_separates_exact_name_from_visual_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            synthesis = root / "synthesis.sqlite"
            source = sqlite3.connect(synthesis)
            source.execute(
                """CREATE TABLE assets(
                       model_id TEXT, logical_id TEXT, semantic_name TEXT,
                       canonical_class TEXT, subtype TEXT, business_type INTEGER,
                       asset_scopes_json TEXT, usage_count INTEGER, scene_count INTEGER,
                       scene_slugs_json TEXT, callable INTEGER, availability TEXT,
                       acquisition_priority TEXT
                   )"""
            )
            source.execute(
                "INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "m1", "synthesis27:m1", "office chair",
                    "furniture/seating/office_chair", "office chair", 1,
                    '["independent_object"]', 4, 1, '["office"]', 0,
                    "remote_usd_available", "P1_major",
                ),
            )
            source.commit()
            source.close()

            catalog = root / "catalog.sqlite"
            target = sqlite3.connect(catalog)
            target.execute(
                """CREATE TABLE semantic_assets(
                       logical_id TEXT, semantic_name TEXT, canonical_class TEXT,
                       subtype TEXT, source_library TEXT, source_path TEXT,
                       callable INTEGER, availability TEXT, preview_index_path TEXT,
                       bbox_json TEXT, front_axis TEXT, meters_per_unit REAL
                   )"""
            )
            target.execute(
                "INSERT INTO semantic_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "pcnl:chair", "office chair",
                    "furniture/seating/office_chair", "task chair", "grscenes",
                    "/assets/chair.usd", 1, "ready", None, None, None, 1.0,
                ),
            )
            target.commit()
            target.close()

            report = evaluate(synthesis, catalog)
            self.assertEqual(1, report["summary"]["exact_name_callable_count"])
            self.assertEqual(0, report["summary"]["exact_name_previewed_count"])
            self.assertEqual(
                "exact_name_semantic_only",
                report["assets"][0]["substitution_status"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
