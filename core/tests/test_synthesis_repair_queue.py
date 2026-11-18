from __future__ import annotations

import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from build_synthesis27_repair_queue import (  # noqa: E402
    _acquisition_index,
    _generated_gaps,
    _declared_asset_gaps,
    _match_source_candidates,
    _source_models,
)


class SynthesisRepairQueueTests(unittest.TestCase):
    def test_explicit_asset_gap_survives_proxy_free_generation(self) -> None:
        gaps = _declared_asset_gaps({
            "asset_gaps": [{
                "canonical_class": "transport/station_equipment/fare_gate",
                "criticality": "anchor",
                "reason": "No callable verified fare gate.",
            }]
        })
        self.assertEqual(1, len(gaps))
        self.assertEqual("missing", gaps[0]["asset_kind"])
        self.assertTrue(gaps[0]["is_anchor"])
        self.assertEqual("scene_spec.asset_gaps", gaps[0]["source"])

    def test_proxy_anchor_maps_to_same_class_synthesis_model(self) -> None:
        spec = {
            "entities": [
                {
                    "id": "main_refrigerator",
                    "role": "refrigerator",
                    "source": "primitive_proxy",
                    "criticality": "anchor",
                },
                {
                    "id": "floor",
                    "role": "room_shell",
                    "source": "authored_shell",
                },
            ]
        }
        info = {
            "models": [
                {
                    "name": "fridge004",
                    "modelID": "fridge-model",
                    "server": "https://example.invalid",
                    "offset": [0, 0, 0],
                    "rotation": [0, 0, 0],
                    "scale": [1, 1, 1],
                },
                {
                    "name": "office_chair001",
                    "modelID": "chair-model",
                    "server": "https://example.invalid",
                },
            ]
        }
        gaps = _generated_gaps(spec)
        self.assertEqual(1, len(gaps))
        self.assertTrue(gaps[0]["is_anchor"])
        candidates = _match_source_candidates(gaps[0], _source_models(info))
        self.assertEqual("fridge-model", candidates[0]["model_id"])
        self.assertFalse(candidates[0]["callable"])

    def test_source_inventory_deduplicates_repeated_instances(self) -> None:
        info = {
            "models": [
                {"name": "chair001", "modelID": "same", "offset": [0, 0, 0]},
                {"name": "chair001_1", "modelID": "same", "offset": [1, 0, 0]},
            ]
        }
        models = _source_models(info)
        self.assertEqual(1, len(models))
        self.assertEqual(2, models[0]["usage_count"])
        self.assertEqual("synthesis_metadata_only", models[0]["availability"])

    def test_acquisition_plan_marks_remote_usd_without_making_asset_callable(self) -> None:
        index = _acquisition_index({
            "asset_count": 1,
            "availability_counts": {"remote_usd_available": 1},
            "scene_collections": {
                "kitchen": {
                    "assets": [
                        {"model_id": "fridge-model", "priority": "P0_anchor"},
                    ]
                }
            },
        })
        gap = {
            "semantic_name": "refrigerator",
            "role": "refrigerator",
            "entity_id": "main_fridge",
            "canonical_class": "appliance/kitchen",
        }
        source_models = [{
            "model_id": "fridge-model",
            "semantic_name": "fridge",
            "aliases": ["fridge004"],
            "canonical_class": "appliance/kitchen",
            "usage_count": 1,
            "server": "https://example.invalid",
        }]
        candidate = _match_source_candidates(gap, source_models, index)[0]
        self.assertTrue(candidate["remote_usd_available"])
        self.assertEqual("P0_anchor", candidate["acquisition_priority"])
        self.assertFalse(candidate["callable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
