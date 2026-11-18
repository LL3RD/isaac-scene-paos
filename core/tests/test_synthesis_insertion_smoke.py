from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from sync_synthesis27_insertion_smoke import sync  # noqa: E402


class SynthesisInsertionSmokeTests(unittest.TestCase):
    def _library(self, path: Path, *, license_status: str) -> None:
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE assets (
                   model_id TEXT PRIMARY KEY,
                   availability TEXT,
                   preview_status TEXT,
                   front_axis_status TEXT,
                   license_status TEXT,
                   qa_status TEXT,
                   callable INTEGER
               )"""
        )
        connection.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?,?,?)",
            (
                "asset-a",
                "local_structural_validated",
                "pass",
                "verified",
                license_status,
                "pending",
                0,
            ),
        )
        connection.commit()
        connection.close()

    def _evidence(self, path: Path, checks: dict[str, str]) -> None:
        path.write_text(json.dumps({
            "schema_version": "synthesis27-insertion-smoke-evidence/v1",
            "scene_path": "/runs/smoke.usda",
            "render_path": "/runs/smoke.png",
            "assets": [{
                "model_id": "asset-a",
                "required_checks": [
                    "scene_validation",
                    "unit_audit",
                    "bounds",
                    "support",
                    "world_orientation",
                    "render_feedback",
                ],
                "checks": checks,
            }],
        }))

    def test_passing_smoke_remains_non_callable_until_license_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = root / "library.sqlite"
            evidence = root / "evidence.json"
            self._library(library, license_status="verify_before_authenticated_download")
            self._evidence(evidence, {
                "scene_validation": "pass",
                "unit_audit": "pass",
                "bounds": "pass",
                "support": "pass",
                "world_orientation": "pass",
                "render_feedback": "pass",
            })
            report = sync(library, evidence, strict=True)
            self.assertEqual("pass", report["results"][0]["status"])
            self.assertEqual("pending_license", report["results"][0]["qa_status"])
            self.assertFalse(report["results"][0]["callable"])

    def test_verified_license_and_all_gates_promote_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = root / "library.sqlite"
            evidence = root / "evidence.json"
            self._library(library, license_status="synthesis_terms_verified")
            self._evidence(evidence, {
                "scene_validation": "pass",
                "unit_audit": "pass",
                "bounds": "pass",
                "support": "pass",
                "world_orientation": "pass",
                "render_feedback": "pass",
            })
            report = sync(library, evidence, strict=True)
            self.assertEqual("ready", report["results"][0]["qa_status"])
            self.assertTrue(report["results"][0]["callable"])

    def test_missing_required_check_keeps_smoke_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = root / "library.sqlite"
            evidence = root / "evidence.json"
            self._library(library, license_status="synthesis_terms_verified")
            self._evidence(evidence, {
                "scene_validation": "pass",
                "unit_audit": "pass",
                "bounds": "pass",
                "support": "pass",
                "world_orientation": "pass",
            })
            report = sync(library, evidence, strict=True)
            self.assertEqual("pending", report["results"][0]["status"])
            self.assertIn("render_feedback", report["results"][0]["missing_checks"])
            self.assertFalse(report["results"][0]["callable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
