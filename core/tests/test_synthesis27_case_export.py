from __future__ import annotations

import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from export_synthesis27_generation_case import export_case  # noqa: E402


class Synthesis27CaseExportTests(unittest.TestCase):
    def test_export_contains_input_and_acceptance_but_no_oracle(self) -> None:
        contract = {
            "cases": [{
                "id": "id",
                "slug": "room",
                "domain": "generic_scene",
                "generation_inputs": {
                    "reference_guided": {
                        "text": "make a room",
                        "reference_image": "room/reference.jpg",
                    },
                },
                "acceptance": {"common": {"visible_proxy_count_max": 0}},
                "oracle": {"secret_model_id": "do-not-leak"},
            }]
        }
        case = export_case(
            contract,
            slug="room",
            mode="reference_guided",
            dataset_root=Path("/dataset"),
            output_root=Path("/output"),
        )
        serialized = str(case)
        self.assertNotIn("do-not-leak", serialized)
        self.assertNotIn("'oracle'", serialized)
        self.assertEqual(
            "/dataset/room/reference.jpg",
            case["generation_input"]["reference_image"],
        )
        self.assertEqual(
            "/output/scene_brief.json",
            case["required_outputs"]["scene_brief"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
