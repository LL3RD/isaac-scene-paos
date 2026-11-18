from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from apply_synthesis27_runtime_validation import apply_report  # noqa: E402


class SynthesisRuntimeValidationImportTests(unittest.TestCase):
    def test_import_preserves_local_source_path(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute(
            """CREATE TABLE assets (
                model_id TEXT PRIMARY KEY,
                source_path TEXT,
                availability TEXT,
                meters_per_unit REAL,
                bbox_json TEXT,
                qa_status TEXT,
                callable INTEGER
            )"""
        )
        connection.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?,?,?)",
            (
                "chair-1",
                "/local/package/chair.usd",
                "local_acquired",
                None,
                None,
                "pending_download",
                0,
            ),
        )
        report = {
            "schema_version": "synthesis27-package-validation/v1",
            "results": [{
                "model_id": "chair-1",
                "entrypoint": "/remote/smoke/chair.usd",
                "structural_status": "pass",
                "meters_per_unit": 1.0,
                "up_axis": "Z",
                "bbox": {"size_meters": [0.6, 0.6, 1.0]},
                "joint_count": 5,
                "articulation_root_count": 1,
            }],
        }
        result = apply_report(connection, report)
        row = connection.execute(
            """SELECT source_path, availability, meters_per_unit, up_axis,
                      joint_count, articulation_root_count, callable
               FROM assets WHERE model_id='chair-1'"""
        ).fetchone()
        self.assertEqual(1, result["applied_count"])
        self.assertEqual("/local/package/chair.usd", row[0])
        self.assertEqual("local_structural_validated", row[1])
        self.assertEqual(1.0, row[2])
        self.assertEqual("Z", row[3])
        self.assertEqual(5, row[4])
        self.assertEqual(1, row[5])
        self.assertEqual(0, row[6])


if __name__ == "__main__":
    unittest.main(verbosity=2)
