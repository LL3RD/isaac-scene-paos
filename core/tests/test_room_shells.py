from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.room_shells import apply_preset, list_presets
from isaac_scene.scene_brief import compile_to_scene_spec, validate


def minimal_brief(preset: str, size: list[float]) -> dict:
    return {
        "schema_version": "isaac-scene-brief/v1",
        "inputs": {"mode": "text", "text": "Empty realistic architectural shell."},
        "domain": "generic_scene",
        "quality_policy": "realistic",
        "room": {"type": "room", "size_m": size, "shell_preset": preset},
        "objects": [],
    }


class RoomShellPresetTests(unittest.TestCase):
    def test_library_is_small_and_discoverable(self) -> None:
        rows = list_presets()
        self.assertEqual(
            ["factory_daylight_v1", "living_warm_v2", "office_glazed_v1", "transit_concourse_v1"],
            [row["id"] for row in rows],
        )
        self.assertTrue(all(row["fixture_count"] >= 1 for row in rows))

    def test_living_v2_has_real_window_opening_and_finish_layers(self) -> None:
        brief = apply_preset(minimal_brief("living_warm_v2", [6.5, 5.2, 3.2]), "living_warm_v2")
        room = brief["room"]
        ids = {row["id"] for row in room["architectural_elements"]}
        roles = {row["semantic_role"] for row in room["architectural_elements"]}
        self.assertIn("side_window_glass", ids)
        self.assertIn("entry_door_panel", ids)
        self.assertIn("back_baseboard", ids)
        self.assertIn("baseboard", roles)
        self.assertIn("left", room["open_sides"])
        self.assertGreaterEqual(len(room["architectural_elements"]), 18)
        self.assertEqual(5, len(brief["lighting"]["fixtures"]) + 1)

    def test_office_preset_expands_repeated_architecture(self) -> None:
        brief = apply_preset(minimal_brief("office_glazed_v1", [8.0, 10.0, 3.3]), "office_glazed_v1")
        room = brief["room"]
        ids = {row["id"] for row in room["architectural_elements"]}
        self.assertIn("left_mullion_15", ids)
        self.assertIn("back_blind_17", ids)
        self.assertEqual(["front", "right"], room["open_sides"])
        self.assertEqual(3, len(brief["lighting"]["fixtures"]) + 1)
        self.assertTrue(room["shell_preset_resolved"]["object_free"])

    def test_factory_grid_and_metric_normalization(self) -> None:
        brief = apply_preset(minimal_brief("factory_daylight_v1", [14.0, 12.0, 5.0]), "factory_daylight_v1")
        columns = [
            row for row in brief["room"]["architectural_elements"]
            if row["id"].startswith("high_bay_column_")
        ]
        self.assertEqual(2, len(columns))
        for actual, expected in zip(columns[0]["size"], [0.392, 0.36, 5.0]):
            self.assertAlmostEqual(expected, actual)
        self.assertAlmostEqual(-6.16, columns[0]["translate"][0])

    def test_declared_preset_compiles_to_scene_spec(self) -> None:
        brief = minimal_brief("transit_concourse_v1", [14.0, 24.0, 4.2])
        self.assertTrue(validate(brief)["valid"])
        spec, observation = compile_to_scene_spec(brief)
        self.assertEqual("pass", observation["status"])
        self.assertEqual("transit_concourse_v1", spec["room"]["shell_preset"])
        self.assertGreater(len(spec["support_colliders"]), 12)
        self.assertEqual("text_guided", spec["lighting_policy"]["mode"])

    def test_transit_columns_stay_in_perimeter_service_band(self) -> None:
        brief = apply_preset(minimal_brief("transit_concourse_v1", [9.0, 7.0, 4.2]), "transit_concourse_v1")
        right = [row for row in brief["room"]["architectural_elements"] if row["id"].startswith("column_right_")]
        self.assertEqual(3, len(right))
        self.assertTrue(all(row["translate"][0] >= 4.0 for row in right))
        self.assertTrue(all(row["size"][0] <= 0.31 for row in right))

    def test_unknown_preset_is_rejected(self) -> None:
        report = validate(minimal_brief("missing_preset", [4.0, 4.0, 3.0]))
        self.assertFalse(report["valid"])
        self.assertEqual("BRIEF_ROOM_SHELL_PRESET_UNKNOWN", report["errors"][0]["code"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
