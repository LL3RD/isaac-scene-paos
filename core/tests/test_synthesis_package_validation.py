from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from validate_synthesis27_packages import select_entrypoint  # noqa: E402


class SynthesisPackageValidationTests(unittest.TestCase):
    def test_top_level_model_stage_wins_over_support_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "resource" / "v_0").mkdir(parents=True)
            (root / "resource" / "material.usd").touch()
            (root / "resource" / "v_0" / "link_0.usd").touch()
            expected = root / "model_sofa.usd"
            expected.touch()
            self.assertEqual(expected, select_entrypoint(root))

    def test_nested_stage_is_used_only_when_no_top_level_stage_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            expected = root / "nested" / "model_asset.usda"
            expected.touch()
            self.assertEqual(expected, select_entrypoint(root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
