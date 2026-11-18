from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from sync_synthesis27_preview_catalog import (  # noqa: E402
    _read_front_review,
    _read_preview_rows,
    effective_preview_status,
)


class SynthesisPreviewSyncTests(unittest.TestCase):
    def test_later_preview_manifest_replaces_earlier_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps({
                "assets": [{"asset_id": "synthesis27:model", "status": "fail"}]
            }))
            second.write_text(json.dumps({
                "assets": [{"asset_id": "synthesis27:model", "status": "pass"}]
            }))
            rows = _read_preview_rows([first, second])
            self.assertEqual("pass", rows["model"][1]["status"])

    def test_verified_front_requires_cardinal_axis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "front.json"
            path.write_text(json.dumps({
                "assets": {"model": {"status": "verified", "front_axis": "diagonal"}}
            }))
            with self.assertRaises(ValueError):
                _read_front_review(path)

    def test_ceiling_requires_vertical_views(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            horizontal = {}
            for view in ("neg_y", "pos_x", "pos_y", "neg_x"):
                path = root / f"{view}.png"
                path.write_bytes(b"x" * 5000)
                horizontal[view] = str(path)
            self.assertEqual(
                "fail",
                effective_preview_status(
                    {"status": "pass", "previews": horizontal},
                    canonical_class="architecture/surface/ceiling",
                    subtype="ceiling",
                ),
            )
            vertical = {}
            for view in ("pos_z", "neg_z"):
                path = root / f"{view}.png"
                path.write_bytes(b"x" * 5000)
                vertical[view] = str(path)
            self.assertEqual(
                "pass",
                effective_preview_status(
                    {"status": "pass", "previews": vertical},
                    canonical_class="architecture/surface/ceiling",
                    subtype="ceiling",
                ),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
