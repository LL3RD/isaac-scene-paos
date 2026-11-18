from __future__ import annotations

import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from build_synthesis27_regression_contracts import build_contract  # noqa: E402


class Synthesis27RegressionContractTests(unittest.TestCase):
    def test_generator_input_does_not_leak_oracle_inventory_or_pose(self) -> None:
        catalog = {
            "scenes": [{
                "id": "scene-id",
                "slug": "kitchen",
                "name": "kitchen",
                "category": "Residence",
            }]
        }
        assets = [
            {
                "model_id": "fridge-model-secret",
                "logical_id": "synthesis27:fridge-model-secret",
                "semantic_name": "fridge004",
                "canonical_class": "appliance/kitchen/refrigerator",
                "subtype": "refrigerator",
                "acquisition_priority": "P1_major",
                "is_open_free": True,
                "license_status": "cc_by_nc_4_0_platform_declared",
            },
            {
                "model_id": "microwave-model-secret",
                "logical_id": "synthesis27:microwave-model-secret",
                "semantic_name": "microwave005",
                "canonical_class": "appliance/kitchen/microwave",
                "subtype": "microwave",
                "acquisition_priority": "P1_major",
                "is_open_free": False,
                "license_status": "not_open_free",
            },
        ]
        scenes = [{
            "scene_slug": "kitchen",
            "scene_id": "scene-id",
            "name": "kitchen",
            "category": "Residence",
            "camera": {"position": [99, 98, 97]},
        }]
        instances = [
            {
                "scene_slug": "kitchen",
                "model_id": "fridge-model-secret",
                "asset_scope": "independent_object",
            },
            {
                "scene_slug": "kitchen",
                "model_id": "microwave-model-secret",
                "asset_scope": "independent_object",
            },
        ]
        contract = build_contract(
            catalog=catalog,
            assets=assets,
            scenes=scenes,
            instances=instances,
            snapshot_hash="a" * 64,
        )
        case = contract["cases"][0]
        visible = str(case["generation_inputs"])
        self.assertNotIn("fridge004", visible)
        self.assertNotIn("fridge-model-secret", visible)
        self.assertNotIn("99", visible)
        self.assertIn("fridge-model-secret", str(case["oracle"]))
        self.assertEqual(
            ["microwave-model-secret"],
            [row["model_id"] for row in case["oracle"]["license_excluded_targets"]],
        )
        microwave = next(
            row for row in case["oracle"]["anchor_targets"]
            if row["model_id"] == "microwave-model-secret"
        )
        self.assertEqual("licensed_visual_equivalent", microwave["match_policy"])
        self.assertEqual(1, case["oracle"]["licensed_exact_target_instance_count"])

    def test_domain_and_acceptance_are_mode_specific(self) -> None:
        contract = build_contract(
            catalog={"scenes": [{
                "id": "id",
                "slug": "factory",
                "name": "assembly workshop",
                "category": "Factory",
            }]},
            assets=[],
            scenes=[{
                "scene_slug": "factory",
                "scene_id": "id",
                "name": "assembly workshop",
                "category": "Factory",
                "camera": {},
            }],
            instances=[],
            snapshot_hash="b" * 64,
        )
        case = contract["cases"][0]
        self.assertEqual("industrial_assembly", case["domain"])
        self.assertTrue(
            case["acceptance"]["text_only"]["reference_match_is_diagnostic_only"]
        )
        self.assertEqual(
            0.72,
            case["acceptance"]["reference_guided"]["reference_match_score_min"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
